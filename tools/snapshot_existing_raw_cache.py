# SPDX-License-Identifier: AGPL-3.0-or-later
"""将先前已验证的原始编码缓存补登记为完成收据；逐项对照旧提取清单，不重新提取。"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
from prepare_offline_resources import PROJECT, EXTRACTOR_SHA, IFS_SHA, checked, publish


def snapshot(archive_name: str, manifest_path: Path) -> dict:
    """绑定源包、公共列表与先前逐项摘要后，排他写入缓存收据。"""
    if Path(archive_name).name != archive_name or not archive_name.endswith('.ifs'):
        raise ValueError('必须为单个IFS文件名')
    raw_manifest = manifest_path.read_bytes()
    manifest = json.loads(raw_manifest)
    record = next(a for a in manifest['archives'] if Path(a['path']).name == archive_name)
    source = PROJECT / 'Original Client/IIPS/IIPSDownload' / archive_name
    checked(source, record['sha256'])
    folder = PROJECT / 'client/workspaces/offline-resource-raw' / source.stem
    listing, = folder.glob('*.lst')
    checked(listing, record['listing_sha256'])
    canonical = {s.replace('\\','/').lower():s.replace('\\','/') for s in listing.read_text(encoding='utf-8-sig').splitlines()}
    bound = {a['game_path'].lower():a for a in manifest['resources'] if a['archive'] == archive_name}
    entries = {}
    for path in sorted(folder.rglob('*')):
        if not path.is_file():
            continue
        rel = path.relative_to(folder).as_posix()
        if rel.split('/')[0].lower() not in ('main','hires','client_shipretail'):
            continue
        item = bound[rel.lower()]
        raw = checked(path,item['encoded_sha256'])
        if len(raw) != item['encoded_size']:
            raise ValueError('编码资源尺寸不符')
        entries[rel.lower()] = {'path':rel,'decode_filename':canonical[rel.lower()].rsplit('/',1)[-1], 'encoded_size':len(raw),'encoded_sha256':item['encoded_sha256']}
    if set(entries) != set(bound):
        raise ValueError('已有编码资源与先前清单不一致')
    receipt = {'schema':1,'archive':{'path':str(source),'name':source.name,'size':source.stat().st_size,'sha256':record['sha256']},'archive_copy':str(PROJECT/'client/workspaces/offline-resource-inputs'/source.name),'cache_root':str(folder),'extractor_sha256':EXTRACTOR_SHA,'ifs2_sha256':IFS_SHA,'listing':{'name':listing.name,'sha256':record['listing_sha256']},'actual_resource_count':len(entries),'entries':entries,'scope':'为先前完成且逐项对照原始提取清单的编码缓存补建摘要；本次没有重新提取。','fresh_extraction_this_receipt':False,'prior_extraction_manifest':{'path':str(manifest_path),'sha256':hashlib.sha256(raw_manifest).hexdigest()}}
    output = folder/'extraction-inventory.json'
    publish(output,(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n').encode('utf-8'))
    return {'receipt':str(output),'receipt_sha256':hashlib.sha256(output.read_bytes()).hexdigest(),'actual_resource_count':len(entries),'archive':receipt['archive']}


def main() -> int:
    """命令行显式指定此前清单与包，拒绝无摘要缓存。"""
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--archive',required=True)
    p.add_argument('--manifest',type=Path,default=PROJECT/'client/resources/v1649370/offline-resources-manifest-v2.json')
    a=p.parse_args()
    print(json.dumps(snapshot(a.archive,a.manifest),ensure_ascii=True))
    return 0

if __name__=='__main__':
    raise SystemExit(main())
