# SPDX-License-Identifier: AGPL-3.0-or-later
"""完整原生资源库：固定版本来源、原生覆盖顺序、批量发布与清单读取。

复用既有散装解码和 Fastfile 校验；不改变游戏加载器，不在运行时补件。
game 内只有原生文件，构建计划、冲突来源和完成清单保存在其同级目录。
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import zlib

from codol_rebuild.resources.fastfile import decode_fastfile
from codol_rebuild.resources.library_manifest import EXE_SHA, validate_library_manifest
from codol_rebuild.resources.loose_library import inspect_format, PRIORITY_SHA
from codol_rebuild.resources.offline_resources import (
    canonical_iwi, decode_loose, file_digest, MAX_RESOURCE, RESOURCE_KEY, validate_iwi,
)
from codol_rebuild.resources.runtime_cache import save_receipt, tree_stamp

LIBRARY_KIND = 'native_game_resources'
MOUNT_DIRECTORIES = ('main', 'hires', 'client_shipretail', 'locales')
RUNTIME_FILES = (
    'gcloudvoice.dll', 'bink2w32.dll', 'dbghelp.dll', 'libcef.dll', 'mss32.dll',
    'd3dx9_43.dll', 'd3dcompiler_43.dll', 'libegl.dll', 'libglesv2.dll', 'icudtl.dat',
    'natives_blob.bin', 'snapshot_blob.bin', 'cef.pak', 'cef_100_percent.pak',
    'cef_200_percent.pak', 'cef_extensions.pak', 'localization.txt', 'logo.bmp', 'splash.bmp',
)


def canonical_native_resource(name: str) -> str | None:
    """限定当前版本实际存在的资源类型，复用 Windows 路径检查。"""
    canonical_iwi(name)
    normalized = name.replace('\\', '/').lower()
    suffix = PurePosixPath(normalized).suffix
    if normalized in RUNTIME_FILES or normalized == 'main/filesyscheck.cfg':
        return normalized
    for prefix, extensions in (
        ('main/images/', ('.iwi',)), ('hires/images/', ('.iwi',)),
        ('main/models/', ('',)), ('main/sound/', ('.mp3', '.wav')),
        ('main/video/', ('.bk2',)), ('client_shipretail/chinese/', ('.ff',)),
        ('locales/', ('.pak',)),
    ):
        if normalized.startswith(prefix) and suffix in extensions:
            return normalized
    return None


def manifest_path(game_root: Path) -> Path:
    """完成标记位于资源根之外，不进入原生搜索目录。"""
    return Path(game_root).parent / 'native-library-manifest.json'


def read_manifest(game_root: Path, *, verify_resources: bool = True) -> tuple[bytes, dict]:
    """读取完成清单；普通启动仅检查装配依赖，显式复核检查全部路径。"""
    manifest_bytes = manifest_path(game_root).read_bytes()
    manifest = json.loads(manifest_bytes)
    validate_library_manifest(manifest, LIBRARY_KIND)
    names = [entry['game_path'] for entry in manifest['resources']]
    checked_names = names if verify_resources else [name for name in names if '/' not in name]
    if len(set(checked_names)) != len(checked_names) or any(canonical_native_resource(name) != name for name in checked_names):
        raise ValueError('原生资源清单存在非法或重复路径')
    if any(not any(name.startswith(directory + '/') for name in names) for directory in MOUNT_DIRECTORIES):
        raise ValueError('原生资源清单缺少必要顶层目录')
    if not set(RUNTIME_FILES).issubset(names):
        raise ValueError('原生资源清单缺少运行依赖')
    return manifest_bytes, manifest


def collect_candidates(raw_root: Path, mount_order: list[dict]) -> tuple[dict, list[dict]]:
    """按真实提取收据收集所有候选；目录清单项不算缺失文件。"""
    candidates = defaultdict(list)
    receipts = []
    family_listings = {}
    if [entry['index'] for entry in mount_order] != list(range(len(mount_order))):
        raise ValueError('原生挂载顺序索引不连续')
    if len({entry['archive'] for entry in mount_order}) != len(mount_order):
        raise ValueError('原生挂载顺序存在重复包')
    for mount in mount_order:
        archive = mount['archive']
        if Path(archive).name != archive:
            raise ValueError('包名不是单一文件名')
        package = raw_root / Path(archive).stem
        package_stamp = tree_stamp(package)
        inventory_path = package / 'extraction-inventory.json'
        expected_archive_hash = None
        original_names = {}
        listing_proof = None
        if inventory_path.is_file():
            inventory = json.loads(inventory_path.read_bytes())
            if inventory['archive']['name'] != archive:
                raise ValueError('提取收据包名不符: ' + archive)
            expected_archive_hash = inventory['archive']['sha256']
            entries = list(inventory['entries'].values())
        else:
            inventory_path = package / 'receipt.json'
            inventory = json.loads(inventory_path.read_bytes())
            if inventory['archive'] != archive or inventory['other']:
                raise ValueError('提取收据异常: ' + archive)
            entries = inventory['files']
            # 批量提取器把输出路径转成小写，AES nonce 却使用原包文件名的大小写。
            # lst 只恢复解码参数；文件存在性和尺寸仍由真实收据与磁盘逐项核对。
            listing_paths = list(package.glob('*.lst'))
            if len(listing_paths) > 1:
                raise ValueError('原包文件名列表不唯一: ' + archive)
            family_name = re.sub(r'_V\d+(?:\.\d+)?$', '', package.name)
            if listing_paths:
                listing_path = listing_paths[0]
                for original_name in listing_path.read_text(encoding='utf-8-sig').splitlines():
                    original_name = original_name.replace('\\', '/')
                    if '/' not in original_name:
                        continue
                    if canonical_native_resource(original_name) is None:
                        continue
                    normalized_name = original_name.lower()
                    if normalized_name in original_names and original_names[normalized_name] != original_name:
                        raise ValueError('原包存在大小写冲突: ' + archive + '/' + original_name)
                    original_names[normalized_name] = original_name
                listing_proof = {'path': str(listing_path.resolve()), 'sha256': file_digest(listing_path)}
                family_listings[family_name] = (original_names, listing_proof)
            else:
                # 分包可能只含资源，名称列表由此前同系列基础包或补丁提供。
                original_names, listing_proof = family_listings.get(family_name, ({}, None))
            extracted_names = [entry['path'].replace('\\', '/').lower() for entry in entries]
            for missing_name in inventory['missing']:
                prefix = missing_name.replace('\\', '/').lower().rstrip('/') + '/'
                if not any(name.startswith(prefix) for name in extracted_names):
                    raise ValueError('未证明为目录的缺失项: ' + archive + '/' + missing_name)
        registered = set()
        for entry in entries:
            relative = entry['path'].replace('\\', '/')
            if '/' not in relative:  # 包内 lst 和提取日志不是运行资源。
                continue
            name = canonical_native_resource(relative)
            if name is None or not name.startswith(('main/', 'hires/', 'client_shipretail/')):
                raise ValueError('未知包内资源: ' + relative)
            if name in registered:
                raise ValueError('包内重复逻辑路径: ' + relative)
            registered.add(name)
            if not inventory_path.name.startswith('extraction-') and not name.endswith('.ff') and name not in original_names:
                raise ValueError('原包列表缺少解码文件名: ' + archive + '/' + name)
            source = package / relative
            encoded_size = entry.get('encoded_size', entry.get('size'))
            if not source.resolve().is_relative_to(package.resolve()) or source.stat().st_size != encoded_size:
                raise ValueError('提取资源路径或尺寸不符: ' + str(source))
            candidates[name].append({
                'source': str(source.resolve()), 'archive': archive, 'mount_index': mount['index'],
                'encoded_size': encoded_size, 'expected_encoded_sha256': entry.get('encoded_sha256'),
                'decode_filename': entry.get('decode_filename', PurePosixPath(original_names.get(name, relative)).name),
            })
        actual = {name.lower() for name in package_stamp if '/' in name}
        if actual != registered:
            raise ValueError('提取目录与收据不一致: ' + archive)
        receipts.append({'archive': archive, 'path': str(inventory_path.resolve()),
                         'sha256': file_digest(inventory_path), 'resource_count': len(registered),
                         'original_name_listing': listing_proof,
                         'expected_archive_sha256': expected_archive_hash})
    return dict(candidates), receipts


def decoded_size(candidate: dict, name: str) -> int:
    """构建前读取声明长度，核算去重后的磁盘需求；解码时仍验证实际长度。"""
    if name.endswith('.ff') or candidate.get('copy_only'):
        return candidate['encoded_size']
    with Path(candidate['source']).open('rb') as source_stream:
        source_stream.seek(-4, 2)
        length = struct.unpack('<I', source_stream.read(4))[0]
    if not 1 <= length <= MAX_RESOURCE:
        raise ValueError('资源声明尺寸无效: ' + name)
    return length


def create_plan(project_root: Path) -> dict:
    """核对固定版本、974 包来源和现有依赖，形成可复查的完整构建计划。"""
    project_root = Path(project_root).resolve()
    version_root = project_root / 'client/resources/v1649370'
    original_root = project_root / 'Original Client'
    client_image = original_root / 'codoMP_client_shipRetail.exe'
    if file_digest(client_image) != EXE_SHA:
        raise ValueError('原版客户端版本指纹不符')
    priority_path = version_root / 'iips-patch-priority-final.json'
    if file_digest(priority_path) != PRIORITY_SHA:
        raise ValueError('原生挂载顺序证据指纹不符')
    mount_order = json.loads(priority_path.read_bytes())['complete_configured_mount_order']
    if len(mount_order) != 974:
        raise ValueError('固定版本必须具有完整 974 包挂载顺序')
    candidates, receipts = collect_candidates(project_root / 'client/workspaces/offline-resource-raw', mount_order)
    for source_root, prefix in ((original_root / 'main/video', 'main/video/'),
                                (version_root / 'runtime-deps', '')):
        for relative, stamp in tree_stamp(source_root).items():
            name = canonical_native_resource(prefix + relative)
            if name is None:
                raise ValueError('未知独立运行资源: ' + relative)
            if name in candidates:
                raise ValueError('独立文件与包内资源重名: ' + name)
            candidates[name] = [{'source': str((source_root / relative).resolve()),
                                 'encoded_size': stamp[1], 'copy_only': True}]
    resources = []
    for name, sources in sorted(candidates.items()):
        resources.append({'game_path': name, 'size': decoded_size(sources[-1], name), 'candidates': sources})
    return {'schema': 1, 'source_exe_sha256': EXE_SHA, 'client_image': str(client_image),
            'priority_sha256': PRIORITY_SHA, 'source_receipts': receipts,
            'mount_order': mount_order, 'resources': resources,
            'resource_count': len(resources), 'expected_output_bytes': sum(entry['size'] for entry in resources),
            'duplicate_path_count': sum(len(entry['candidates']) > 1 for entry in resources),
            'selection': 'ordinary same-path replacement: last native FileItem wins; no delta or deletion inference'}


def read_candidate(candidate: dict) -> tuple[bytes, dict]:
    """所有候选核对尺寸和来源摘要；被完整替换的旧版本不进入解码器。"""
    source = Path(candidate['source'])
    encoded = source.read_bytes()
    encoded_hash = hashlib.sha256(encoded).hexdigest()
    if len(encoded) != candidate['encoded_size']:
        raise ValueError('候选尺寸变化')
    expected_hash = candidate.get('expected_encoded_sha256')
    if expected_hash is not None and encoded_hash != expected_hash:
        raise ValueError('候选与提取收据摘要不符')
    provenance = {**candidate, 'encoded_sha256': encoded_hash}
    return encoded, provenance


def load_candidate(candidate: dict, name: str, client_image: Path) -> tuple[bytes, dict]:
    """验证最终候选外层和内层结构；FF 校验后仍发布原始 FF 字节。"""
    encoded, provenance = read_candidate(candidate)
    if candidate.get('copy_only'):
        decoded = encoded
        provenance['validation'] = 'source copy with SHA-256'
    elif name.endswith('.ff'):
        report = decode_fastfile(candidate['source'], client_image)
        if report['input']['version'] != 461 or report['input']['sha256'] != provenance['encoded_sha256']:
            raise ValueError('Fastfile 版本或验证期间内容变化')
        provenance['validation'] = {'checks': report['checks'], 'zone_sha256': report['zone']['sha256']}
        decoded = encoded
    else:
        try:
            decoded = decode_loose(encoded, candidate['decode_filename'], RESOURCE_KEY)
        except zlib.error as error:
            raise ValueError('散装资源 zlib 校验失败: ' + str(error)) from error
        if name.endswith('.iwi'):
            validate_iwi(decoded)
            provenance['validation'] = 'AES/zlib complete; IWi09 header'
        else:
            kind = 'model' if name.startswith('main/models/') else 'config' if name.endswith('.cfg') else PurePosixPath(name).suffix[1:]
            provenance['validation'] = inspect_format(decoded, kind)
    provenance['decoded_sha256'] = hashlib.sha256(decoded).hexdigest()
    provenance['decoded_size'] = len(decoded)
    return decoded, provenance


def build_library(project_root: Path, *, publish: bool, progress=print) -> dict:
    """先完整核算再排他发布；任何失败都不生成可供启动的完成清单。"""
    project_root = Path(project_root).resolve()
    version_root = project_root / 'client/resources/v1649370'
    game_root = version_root / 'game'
    if game_root.is_symlink() or (game_root.exists() and game_root.resolve() != game_root):
        raise ValueError('目标资源根不可重定向')
    plan = create_plan(project_root)
    existing = tree_stamp(game_root) if game_root.exists() else {}
    wanted_names = {entry['game_path'] for entry in plan['resources']}
    if set(existing) - wanted_names:
        raise ValueError('目标目录已有未登记文件')
    needed_bytes = sum(entry['size'] for entry in plan['resources'] if entry['game_path'] not in existing)
    plan['additional_output_bytes'] = needed_bytes
    plan['available_bytes'] = shutil.disk_usage(version_root).free
    save_receipt(version_root / 'native-library-plan.json', plan)
    progress({'phase': 'plan', 'resources': plan['resource_count'], 'bytes': plan['expected_output_bytes'],
              'additional_bytes': needed_bytes, 'available_bytes': plan['available_bytes']})
    if not publish:
        return plan
    if needed_bytes > plan['available_bytes']:
        raise ValueError('空间不足以发布完整资源库，保留原始材料和旧库')
    archive_root = project_root / 'Original Client/IIPS/IIPSDownload'
    archive_proofs = []
    for mount, receipt in zip(plan['mount_order'], plan['source_receipts']):
        archive_path = archive_root / mount['archive']
        archive_hash = file_digest(archive_path)
        if archive_path.stat().st_size != mount['archive_size']:
            raise ValueError('原包尺寸与固定挂载配置不符: ' + mount['archive'])
        if receipt['expected_archive_sha256'] and receipt['expected_archive_sha256'] != archive_hash:
            raise ValueError('原包摘要与提取收据不符: ' + mount['archive'])
        archive_proofs.append({'archive': mount['archive'], 'sha256': archive_hash})
    progress({'phase': 'archives-verified', 'count': len(archive_proofs)})
    resources, issues, overrides = [], [], []
    published = already_present = 0
    for entry_index, entry in enumerate(plan['resources']):
        name = entry['game_path']
        try:
            candidate_proofs = []
            for candidate in entry['candidates'][:-1]:
                superseded_bytes, provenance = read_candidate(candidate)
                provenance['validation'] = 'source size and SHA-256 only; superseded by full replacement'
                if name.endswith('.ff'):
                    provenance['fastfile_header_version'] = struct.unpack_from('<I', superseded_bytes, 8)[0]
                candidate_proofs.append(provenance)
            decoded, provenance = load_candidate(entry['candidates'][-1], name, Path(plan['client_image']))
            candidate_proofs.append(provenance)
            if len(decoded) != entry['size']:
                raise ValueError('输出尺寸与计划不符')
            target = game_root / name
            output_hash = hashlib.sha256(decoded).hexdigest()
            if target.exists():
                if target.stat().st_size != len(decoded) or file_digest(target) != output_hash:
                    raise ValueError('已存在不同输出，拒绝覆盖')
                already_present += 1
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open('xb') as output_stream:
                    output_stream.write(decoded)
                if file_digest(target) != output_hash:
                    raise ValueError('发布后摘要复核失败')
                published += 1
            resources.append({'game_path': name, 'size': len(decoded), 'sha256': output_hash,
                              'source': candidate_proofs[-1]})
            if len(candidate_proofs) > 1:
                overrides.append({'game_path': name, 'candidates': candidate_proofs,
                                  'selected_mount_index': candidate_proofs[-1]['mount_index']})
        except (OSError, ValueError, KeyError) as error:
            issues.append({'game_path': name, 'error': str(error)})
        if (entry_index + 1) % 1000 == 0:
            progress({'phase': 'resources', 'processed': entry_index + 1, 'errors': len(issues)})
    completed = not issues and len(resources) == plan['resource_count']
    summary = {'completed': completed, 'status': 'complete' if completed else 'failed',
               'total': plan['resource_count'], 'processed': plan['resource_count'],
               'published': published, 'already_present': already_present, 'conflict': 0, 'error': len(issues),
               'expected_output_bytes': plan['expected_output_bytes'],
               'validated_output_bytes': sum(entry['size'] for entry in resources)}
    report = {'schema': 1, 'library_kind': LIBRARY_KIND, 'source_exe_sha256': EXE_SHA,
              'source_resource_version': 461, 'old_version_resources_used': False,
              'summary': summary, 'issues': issues, 'resources': resources,
              'archive_proofs': archive_proofs, 'source_receipts': plan['source_receipts'],
              'priority_sha256': PRIORITY_SHA, 'overrides': overrides}
    save_receipt(version_root / 'native-library-build.json', report)
    if completed:
        if set(tree_stamp(game_root)) != wanted_names:
            raise ValueError('发布目录与完整计划不一致')
        validate_library_manifest(report, LIBRARY_KIND)
        save_receipt(manifest_path(game_root), report)
    progress({'phase': 'finished', **summary})
    return report
