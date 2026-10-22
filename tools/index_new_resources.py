# SPDX-License-Identifier: AGPL-3.0-or-later
"""为新版 IFS 明文清单建立资源候选索引；不解包、不运行游戏。

每包仅读取前 1 MiB。共享 .lst 会令分包出现假命中，因此查到名称仅代表候选，
必须另以真实提取结果、源包完整 SHA 和严格资源校验确认承载关系。本工具不会
把 hires 自动伪装成 main，也不会将清单命中写成成功供给。
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

PROJECT=Path(__file__).resolve().parents[2]
PREFIX_BYTES=1024*1024
ENTRY=re.compile(rb'(?m)^((?:main|hires|client_shipretail)[/\\][\x20-\x7e]+?)\r?$',re.I)
EXE_SHA='36997674b18f2896de5e9d755686f6922af10708f355175714058d0a0172b537'


def safe_path(value: str) -> str:
    """规范相对游戏路径，拒绝设备路径和目录逃逸。"""
    value=value.replace('\\','/')
    p=PurePosixPath(value)
    if p.is_absolute() or '..' in p.parts or any(c in value for c in ':<>"|?*'):
        raise ValueError('无效相对资源路径')
    if not p.parts or p.parts[0].lower() not in ('main','hires','client_shipretail'):
        raise ValueError('未知资源根目录')
    return value


def build_index() -> dict:
    """扫描当前新版源目录，只给出前缀摘要，绝不冒充完整包摘要。"""
    original=PROJECT/'Original Client'
    exe=original/'codoMP_client_shipRetail.exe'
    if hashlib.sha256(exe.read_bytes()).hexdigest()!=EXE_SHA:
        raise ValueError('原始客户端版本指纹不符')
    result={'schema':1,'source_root':str(original),'source_exe_sha256':EXE_SHA,
            'scope':'IFS 前1MiB ASCII资源清单候选；需实际解包验证承载关系',
            'prefix_limit_bytes':PREFIX_BYTES,'archives':{},'resources':{}}
    for archive in sorted((original/'IIPS/IIPSDownload').glob('*.ifs')):
        with archive.open('rb') as stream:prefix=stream.read(PREFIX_BYTES)
        names=[]
        for match in ENTRY.finditer(prefix):
            try:name=safe_path(match[1].decode('ascii'))
            except ValueError:continue
            if name not in names:names.append(name)
        result['archives'][archive.name]={'path':str(archive),'size':archive.stat().st_size,
          'prefix_bytes':len(prefix),'prefix_sha256':hashlib.sha256(prefix).hexdigest(),
          'candidate_resource_count':len(names),'full_archive_sha256_verified':False}
        for name in names:
            result['resources'].setdefault(name.lower(),[]).append({'archive':archive.name,'resource':name})
    result['archive_count']=len(result['archives'])
    result['unique_resource_count']=len(result['resources'])
    return result


def lookup(index: dict, resource: str, include_hires: bool=False) -> list[dict]:
    """精确查名称，并报告缓存实际存在性；hires 必须显式请求且明确标记。"""
    if resource.replace('\\','/').lower().startswith(('images/','models/','sound/')):
        resource='main/'+resource
    resource=safe_path(resource).lower()
    candidates=list(index['resources'].get(resource,[]))
    if include_hires and resource.startswith('main/images/'):
        candidates+=index['resources'].get('hires/'+resource.split('/',1)[1],[])
    answer=[]
    for candidate in candidates:
        archive=index['archives'][candidate['archive']]
        raw=PROJECT/'client/workspaces/offline-resource-raw'/Path(candidate['archive']).stem/candidate['resource']
        answer.append({**candidate,'archive_path':archive['path'],'archive_size':archive['size'],
          'archive_prefix_sha256':archive['prefix_sha256'],'full_archive_sha256_verified':False,
          'actual_cached_file':str(raw) if raw.is_file() else None,
          'requires_hires_alias':candidate['resource'].lower()!=resource,
          'warning':'清单命中不是本分包实际承载或完整性证明'})
    answer.sort(key=lambda x:(x['requires_hires_alias'],x['actual_cached_file'] is None,bool(re.search(r'\.\d+\.ifs$',x['archive'])),x['archive']))
    return answer


def main() -> int:
    """build 创建排他索引；lookup 返回候选信息且不触发提取。"""
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    build=sub.add_parser('build',help='只读取新版包前缀并建立索引')
    build.add_argument('--output',type=Path,default=PROJECT/'client/workspaces/new-resource-index.json')
    query=sub.add_parser('lookup',help='精确查询路径，例如 images/asym_bot_06_env_ft.iwi')
    query.add_argument('resource')
    query.add_argument('--index',type=Path,default=PROJECT/'client/workspaces/new-resource-index.json')
    query.add_argument('--include-hires',action='store_true',help='同时列出hires候选，不隐式改变游戏路径')
    args=parser.parse_args()
    if args.command=='lookup':
        result=lookup(json.loads(args.index.read_text(encoding='utf-8')),args.resource,args.include_hires)
        print(json.dumps(result,ensure_ascii=True,indent=2));return 0 if result else 2
    if not args.output.resolve().is_relative_to((PROJECT/'client/workspaces').resolve()):
        parser.error('索引必须写入本项目workspaces')
    index=build_index();encoded=(json.dumps(index,ensure_ascii=False,separators=(',',':'))+'\n').encode('utf-8')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    if args.output.exists():
        if args.output.read_bytes()!=encoded:raise ValueError('拒绝覆盖不同索引，请另指定输出路径')
    else:
        with args.output.open('xb')as stream:stream.write(encoded)
    print(json.dumps({'index':str(args.output),'size':len(encoded),'sha256':hashlib.sha256(encoded).hexdigest(),'archives':index['archive_count'],'unique_resources':index['unique_resource_count']}))
    return 0

if __name__=='__main__':
    raise SystemExit(main())
