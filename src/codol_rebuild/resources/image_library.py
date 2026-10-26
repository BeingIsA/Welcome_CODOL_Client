# SPDX-License-Identifier: AGPL-3.0-or-later
"""通过只读来源接口批量建立固定资源库；冲突不选边，失败不隐瞒。"""
from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import struct
import time
import uuid
import zlib
from codol_rebuild.resources.offline_resources import (ResourceSource, OfflineResourceSupplier, ARCHIVES, RAW_INVENTORIES,
                                EXE_SHA, MAX_RESOURCE, canonical_iwi, digest)


def _json(path: Path, data: dict) -> None:
    """进度与清单原子替换；资源文件本身只允许排他创建或同字节复用。"""
    parent = path.parent.resolve()
    tmp = parent/('.'+path.name+'.'+uuid.uuid4().hex+'.tmp')
    if tmp.resolve().parent != parent:
        raise ValueError('清单临时路径越界')
    try:
        with tmp.open('x', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _known_conflicts(project: Path, source: ResourceSource) -> tuple[dict, list]:
    # 同组分片已证明存在不同解码内容；即使该分片尚未加入供应器，也不能忽略证据。
    conflicts = {}; evidence = []
    path = project/'client/resources/v1649370/startup-resource-content-conflicts-v2.json'
    if not path.is_file():
        path = project/'client/resources/v1649370/common-mp-fragment-conflicts.json'
    if path.is_file():
        raw = path.read_bytes(); report = json.loads(raw)
        evidence.append({'path':str(path),'sha256':digest(raw)})
        for name, item in report['resources'].items():
            logical = source.canonical_resource(name)
            if logical and item.get('decoded_content_identical') is False:
                conflicts[logical] = {'reason':'同版本主包与分片的严格解码内容不同，原生优先级尚未确认',
                                      'candidates':item['candidates']}
    return conflicts, evidence


def plan_library(project: Path, source: ResourceSource) -> dict:
    """统计尾部声明大小；声明尚不是解码结果，发布时必须重新完整校验。"""
    project = Path(project).resolve()
    if source.project != project:
        raise ValueError('来源索引与素材库项目不一致')
    directory = getattr(source,'library_directory','offline-image-library')
    kind = getattr(source,'library_kind',None)
    if (directory,kind) not in {('offline-image-library',None),('offline-loose-library','loose_startup_resources')}:
        raise ValueError('只支持固定图片库或固定非图片启动数据资源库')
    root = (project/'client/resources/v1649370'/directory).resolve()
    expected = project/'client/resources/v1649370'/directory
    if root != expected or not root.is_relative_to((project/'client/resources/v1649370').resolve()):
        raise ValueError('素材库目录存在越界链接')
    conflicts, evidence = _known_conflicts(project,source)
    names = source.indexed_names()
    items = []; declared_total = 0; missing_total = 0
    for name in names:
        if source.canonical_resource(name) != name:
            raise ValueError('来源索引含类型范围外的路径')
        item = {'game_path':name}
        try:
            if name in conflicts and source.resolution_for(name) is None:
                items.append({**item,'status':'conflict',**conflicts[name]}); continue
            sizes = []
            cached_entry = source.cached_entry(name)
            if cached_entry is not None:
                sizes.append(cached_entry[1]['size'])
            for _, folder, entry, _ in source.receipt_entries(name):
                path = (folder/entry['path']).resolve()
                if not path.is_relative_to(folder):
                    raise ValueError('来源缓存路径越界')
                stat = path.stat()
                if stat.st_size != entry['encoded_size'] or not 5 <= stat.st_size <= MAX_RESOURCE:
                    raise ValueError('编码资源尺寸与收据不符')
                with path.open('rb') as stream:
                    stream.seek(-4, 2); sizes.append(struct.unpack('<I',stream.read(4))[0])
            if not sizes or any(not isinstance(n,int) or not (1 if kind else 32) <= n <= MAX_RESOURCE for n in sizes):
                raise ValueError('资源声明输出大小无效')
            rule = source.resolution_for(name)
            size = (next(c['decoded_size'] for c in rule['candidates'] if c['archive']==rule['selected_archive']) if rule else max(sizes))
            if size not in sizes:
                raise ValueError('已证明赢家尺寸不在实际候选声明中')
            target = (root/name).resolve()
            if not target.is_relative_to(root/'main'):
                raise ValueError('目标图片路径越界')
            declared_total += size
            if not target.is_file(): missing_total += size
            items.append({**item,'status':'ready','expected_size':size,'candidate_declared_sizes':sizes})
        except (OSError, ValueError, KeyError, TypeError, struct.error) as exc:
            items.append({**item,'status':'error','reason':str(exc)})
    constraints = source.library_constraints()
    if constraints is not None and (len(names)!=constraints['resource_count'] or declared_total!=constraints['expected_output_bytes']):
        raise ValueError('完整类型库条目或声明尺寸与已审闭包不符')
    probe = root if root.exists() else project
    usage = shutil.disk_usage(probe)
    reserve = max(64*1024*1024, missing_total//20)
    return {'schema':1,'scope':('当前33包已固定真实模型、音频和fileSysCheck数据；非全客户端或已进入菜单声明' if kind else '仅当前已固定真实 main/images IWI，非全客户端或全部启动依赖完成声明'),
            'library_kind':kind,
            'output_root':str(root),'source_exe_sha256':EXE_SHA,'source_resource_version':461,
            'old_version_resources_used':False,'resource_count':len(names),
            'expected_output_bytes':declared_total,'estimated_missing_output_bytes':missing_total,
            'size_basis':('已校验收据的尾部声明；构建时校验编码摘要、严格AES/zlib和指定数据类型边界' if kind else '已校验收据对应实际文件的尾部声明值；构建时验证编码摘要及严格 AES/zlib/IWi09'),
            'disk_free_bytes':usage.free,'disk_required_with_reserve':missing_total+reserve,
            'space_sufficient':usage.free >= missing_total+reserve,
            'archives':dict(ARCHIVES),'inventories':dict(RAW_INVENTORIES),
            'decoded_manifest':source.manifest,'conflict_evidence':evidence,'items':items}


def build_library(project: Path, *, source: ResourceSource | None = None,
                  plan_only: bool = False, progress=None) -> dict:
    """固定目录发布全部无歧义候选，可重跑；每个失败和冲突都出现在最终清单中。"""
    project = Path(project).resolve()
    source = source if source is not None else OfflineResourceSupplier(project)
    plan = plan_library(project, source)
    root = Path(plan['output_root']); root.mkdir(parents=True,exist_ok=True)
    _json(root/'plan.json',plan)
    if progress: progress({'phase':'plan',**{k:plan[k]for k in ['resource_count','expected_output_bytes','estimated_missing_output_bytes','disk_free_bytes','space_sufficient']}})
    if plan_only: return plan
    if not plan['space_sufficient']:
        raise ValueError('磁盘空间不足；仅保存计划，未发布资源')
    summary = {'completed':False,'total':len(plan['items']),'processed':0,'published':0,
               'already_present':0,'conflict':0,'error':0,'replaced_known_obsolete':0,'validated_output_bytes':0,
               'decoded_candidate_bytes':0,'expected_output_bytes':plan['expected_output_bytes']}
    manifest = {'schema':1,'scope':plan['scope'],'output_root':str(root),
                'source_exe_sha256':EXE_SHA,'source_resource_version':461,'old_version_resources_used':False,
                'archives':plan['archives'],'inventories':plan['inventories'],
                'conflict_evidence':plan['conflict_evidence'],'summary':summary,'resources':[],'issues':[],'resolutions':[],'replacements':[]}
    if plan['library_kind'] is not None:
        manifest['library_kind'] = plan['library_kind']
    started = time.monotonic(); last_progress = 0.0
    _json(root/'manifest.json',manifest)
    try:
        for item in plan['items']:
            name = item['game_path']
            try:
                if item['status'] != 'ready':
                    manifest['issues'].append(item); summary[item['status']] += 1
                    continue
                candidates = source.load_candidates(name)
                if not candidates:
                    raise ValueError('没有可验证的实际候选')
                data, provenance = candidates[0]
                summary['decoded_candidate_bytes'] += sum(len(raw)for raw,_ in candidates)
                if source.resolution_for(name) is None and any(other != data for other,_ in candidates[1:]):
                    manifest['issues'].append({'game_path':name,'status':'conflict',
                        'reason':'同名候选经严格解码后内容不同，未发布',
                        'candidates':[{'size':len(raw),'sha256':digest(raw),'provenance':p}for raw,p in candidates]})
                    summary['conflict'] += 1; continue
                data, provenance = source.select_candidate(name,candidates)
                if 'resolution' in provenance:
                    manifest['resolutions'].append({'game_path':name,**provenance['resolution']})
                if len(data) != item['expected_size']:
                    raise ValueError('实际解码大小与计划声明不符')
                target = (root/name).resolve()
                if not target.is_relative_to(root/'main'):
                    raise ValueError('目标路径越界')
                previous = target.read_bytes() if target.exists() else None
                replacement = None
                if previous is not None and previous != data:
                    rule = provenance.get('resolution')
                    old_sha = digest(previous)
                    if rule is None or not any(c['decoded_sha256']==old_sha and c['decoded_size']==len(previous) for c in rule['candidates']):
                        raise ValueError('已有库文件内容不同且不属于已证明旧候选，拒绝覆盖')
                    # 只有已证明被原生后续包替代的精确旧候选可以更新；先永久保留旧字节。
                    backup = (root/'superseded'/(name+'.'+old_sha)).resolve()
                    if not backup.is_relative_to(root/'superseded'):
                        raise ValueError('旧候选保留路径越界')
                    backup.parent.mkdir(parents=True,exist_ok=True)
                    if backup.exists():
                        if backup.read_bytes()!=previous: raise ValueError('旧候选保留文件内容不符')
                    else:
                        os.link(target,backup)
                    replacement = {'game_path':name,'old_sha256':old_sha,'new_sha256':digest(data),'old_copy':str(backup),'reason':'已证明原生覆盖规则选择后挂载资源'}
                if previous == data:
                    status = 'already_present'
                else:
                    target.parent.mkdir(parents=True,exist_ok=True)
                    temporary = target.with_name('.'+target.name+'.'+uuid.uuid4().hex+'.building')
                    try:
                        with temporary.open('xb') as stream:
                            stream.write(data); stream.flush()
                        if replacement is None:
                            os.link(temporary, target)
                        else:
                            if target.read_bytes()!=previous: raise ValueError('更新前旧候选发生变化')
                            os.replace(temporary,target)
                    finally:
                        temporary.unlink(missing_ok=True)
                    status = 'published'
                if replacement is not None:
                    manifest['replacements'].append(replacement)
                    summary['replaced_known_obsolete'] += 1
                summary[status] += 1; summary['validated_output_bytes'] += len(data)
                manifest['resources'].append({'game_path':name,'size':len(data),'sha256':digest(data),
                    'archive':provenance['archive'],'status':status,'provenance':provenance,
                    'all_verified_candidates':[p for _,p in candidates]})
                if hasattr(source,'resource_type'):
                    manifest['resources'][-1]['resource_type'] = source.resource_type(name)
            except (OSError, ValueError, TypeError, KeyError, zlib.error) as exc:
                manifest['issues'].append({'game_path':name,'status':'error','reason':str(exc)})
                summary['error'] += 1
            finally:
                summary['processed'] += 1
                now = time.monotonic()
                if now-last_progress >= 2 or summary['processed']==summary['total']:
                    summary['elapsed_seconds'] = round(now-started,3)
                    _json(root/'progress.json',summary)
                    if progress: progress({'phase':'build',**summary})
                    last_progress = now
        summary['completed'] = True
    finally:
        summary['elapsed_seconds'] = round(time.monotonic()-started,3)
        summary['status'] = ('interrupted' if not summary['completed'] else 'errors' if summary['error']
                             else 'completed_with_conflicts' if summary['conflict'] else 'complete')
        _json(root/'manifest.json',manifest); _json(root/'summary.json',summary)
        _json(root/'progress.json',summary)
    return manifest
