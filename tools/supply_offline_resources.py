# SPDX-License-Identifier: AGPL-3.0-or-later
"""从指定新版 IFS 精确补充 loose 资源，扩展完整来源清单。

依赖 prepare_offline_resources.py 的严格资源解码器（来源见该模块）。
每次重新提取已校验源包的副本，仅发布明确指定的资源；不运行游戏或保护库。
示例：--archive lf_init_common_marketplace_4_V46.ifs --archive-sha256 SHA
      --resource main/images/asym_bot_04_env_ft.iwi
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
from prepare_offline_resources import PROJECT, CODEC_SHA, EXTRACTOR_SHA, IFS_SHA, checked, decode_loose, publish, safe_name, sha


def main() -> int:
    """要求显式包摘要和资源名；对相同目标只允许逐字节一致的内容。"""
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive',required=True,help='本项目 Original Client/IIPS/IIPSDownload 下的包文件名')
    parser.add_argument('--archive-sha256',required=True,help='指定新版源包的完整 SHA256')
    parser.add_argument('--resource',action='append',required=True,help='包内完整 main/... 路径；可重复')
    parser.add_argument('--legacy-client',type=Path,default=Path(r'D:\Games\CODOL\Client'))
    parser.add_argument('--base-manifest',type=Path,default=PROJECT/'client/resources/v1649370/offline-resources-manifest-v2.json')
    parser.add_argument('--manifest',type=Path,default=PROJECT/'client/resources/v1649370/offline-resources-manifest-v3.json')
    args=parser.parse_args()
    workspace=(PROJECT/'client/workspaces').resolve()
    if Path(args.archive).name!=args.archive or not args.archive.endswith('.ifs'):
        parser.error('必须指定单个 IFS 文件名')
    for p in (args.base_manifest,args.manifest):
        if not p.resolve().is_relative_to((PROJECT/'client/resources/v1649370').resolve()):parser.error('清单必须位于本项目固定资源目录')
    requested=[safe_name(name) for name in args.resource]
    if any(not name.lower().startswith('main/') for name in requested):parser.error('此工具仅发布 main loose 资源')
    original=PROJECT/'Original Client'
    source=original/'IIPS/IIPSDownload'/args.archive
    archive=checked(source,args.archive_sha256)
    extractor=args.legacy_client/'assets/extract_local_ifs.exe'
    ifs=original/'IIPS/IFS2.dll'
    checked(extractor,EXTRACTOR_SHA);checked(ifs,IFS_SHA)
    codec=checked(args.legacy_client/'src/codol/resources/codec.py',CODEC_SHA).decode('utf-8')
    match=re.search(r"KEY = bytes.fromhex\('([0-9a-f]+)'\)",codec)
    if not match:raise ValueError('无法识别已绑定资源算法材料')
    key=bytes.fromhex(match[1])
    input_root=workspace/'offline-resource-inputs';alias=input_root/args.archive
    publish(alias,archive)
    base_data=args.base_manifest.read_bytes();report=json.loads(base_data)
    output=(PROJECT/'client/resources/v1649370/offline-resources').resolve()
    if not output.is_relative_to((PROJECT/'client/resources/v1649370').resolve()):raise ValueError('输出目录越界')
    additions=[]
    with tempfile.TemporaryDirectory(prefix='ifs-select-',dir=input_root) as tmp:
        folder=Path(tmp).resolve()
        if not folder.is_relative_to(input_root.resolve()):raise ValueError('临时目录越界')
        task=subprocess.run([str(extractor),str(ifs),str(alias),str(folder)],capture_output=True,timeout=120)
        if task.returncode not in (0,10):raise ValueError(f'提取失败: {task.returncode}')
        listings=list(folder.glob('*.lst'))
        if len(listings)!=1:raise ValueError('包清单数量异常')
        listing=listings[0];lines=listing.read_text(encoding='utf-8-sig').splitlines()
        canonical={name.replace('\\','/').lower():name.replace('\\','/') for name in lines}
        for name in requested:
            packed_name=canonical.get(name.lower())
            if packed_name is None:raise ValueError(f'公共清单没有指定资源: {name}')
            packed_name=safe_name(packed_name)
            src=folder/packed_name
            if not src.is_file():raise ValueError(f'公共清单命中，但此分包未实际承载资源: {packed_name}')
            raw=src.read_bytes();data=decode_loose(raw,PurePosixPath(packed_name).name,key)
            publish(output/name,data)
            entry={'game_path':name,'archive':args.archive,'source_archive_path':str(source),'archive_sha256':args.archive_sha256,'source_archive_resource':packed_name,'encoded_size':len(raw),'encoded_sha256':sha(raw),'size':len(data),'sha256':sha(data),'transformation':'AES-CTR 解码 + 严格 zlib EOF/尾部/尺寸校验'}
            if name.lower().endswith('.iwi'):
                entry['iwi_header_hex']=data[:32].hex();entry['iwi_version_verified']=9
            additions.append(entry)
        archive_entry={'path':str(source),'sha256':args.archive_sha256,'size':len(archive),'listing_sha256':sha(listing.read_bytes()),'listed_resources':len(lines)-1,'verified_resources':len(additions),'subset_only':True}
    checked(source,args.archive_sha256)
    by_name={r['game_path'].lower():r for r in report['resources']}
    for entry in additions:
        prior=by_name.get(entry['game_path'].lower())
        if prior and prior['sha256']!=entry['sha256']:raise ValueError('历史清单同名资源指纹冲突')
        by_name[entry['game_path'].lower()]=entry
    report['resources']=list(by_name.values())
    prior_archive=next((a for a in report['archives']if a['path']==str(source)),None)
    if prior_archive is None:report['archives'].append(archive_entry)
    elif prior_archive['sha256']!=archive_entry['sha256']:raise ValueError('历史包指纹冲突')
    else:prior_archive['verified_resources']=sum(r['archive']==args.archive for r in report['resources'])
    report['previous_manifest']={'path':str(args.base_manifest),'sha256':sha(base_data)}
    report['resource_count']=len(report['resources']);report['total_output_bytes']=sum(r['size']for r in report['resources'])
    report['incremental_resources']=[r['game_path']for r in additions]
    publish(args.manifest,(json.dumps(report,ensure_ascii=False,indent=2)+'\n').encode('utf-8'))
    print(json.dumps({'manifest':str(args.manifest),'resource_count':report['resource_count'],'added':additions},ensure_ascii=True))
    return 0

if __name__=='__main__':
    raise SystemExit(main())
