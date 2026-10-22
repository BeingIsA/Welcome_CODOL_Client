# SPDX-License-Identifier: AGPL-3.0-or-later
"""校验指定新版 IFS 并建立只含编码资源的缓存及逐文件摘要，不发布游戏资源。

输入必须为本项目 Original Client 内的明确包名和事先记录的完整 SHA256。
只调用绑定指纹的资源提取器与 IFS2；不会启动游戏、加载保护库或修改源包。
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from prepare_offline_resources import PROJECT, EXTRACTOR_SHA, IFS_SHA, checked, publish


def cache_archive(archive_name: str, expected_sha256: str, legacy_client: Path) -> dict:
    """先核对包和工具，提取到临时目录，发布缓存后最后写完成收据。"""
    if Path(archive_name).name!=archive_name or not archive_name.endswith('.ifs'):
        raise ValueError('必须指定单个新版IFS包文件名')
    source=PROJECT/'Original Client/IIPS/IIPSDownload'/archive_name
    data=checked(source,expected_sha256)
    folder=PROJECT/'client/workspaces/offline-resource-raw'/source.stem
    receipt_path=folder/'extraction-inventory.json'
    if receipt_path.exists():
        # 重用已有完成收据时逐文件验证，不能仅凭 .lst 或目录存在宣称可用。
        receipt=json.loads(receipt_path.read_text(encoding='utf-8'))
        if receipt['archive']['sha256']!=expected_sha256:raise ValueError('已有缓存属于其他源包')
        for item in receipt['entries'].values():
            path=folder/item['path']
            if not path.resolve().is_relative_to(folder.resolve()):raise ValueError('缓存路径越界')
            checked(path,item['encoded_sha256'])
        return receipt
    input_root=PROJECT/'client/workspaces/offline-resource-inputs'
    alias=input_root/archive_name;publish(alias,data)
    extractor=legacy_client/'assets/extract_local_ifs.exe';ifs=PROJECT/'Original Client/IIPS/IFS2.dll'
    checked(extractor,EXTRACTOR_SHA);checked(ifs,IFS_SHA)
    with tempfile.TemporaryDirectory(prefix='ifs-cache-',dir=input_root)as tmp:
        temporary=Path(tmp).resolve()
        if not temporary.is_relative_to(input_root.resolve()):raise ValueError('临时目录越界')
        task=subprocess.run([str(extractor),str(ifs),str(alias),str(temporary)],capture_output=True,timeout=120)
        if task.returncode not in (0,10):raise ValueError(f'资源提取失败:{task.returncode}')
        listings=list(temporary.glob('*.lst'))
        # 小补丁可能只有内部(listfile)，提取器不一定导出普通.lst；不能据此认定空包。
        # fallback只接受已绑定提取器输出的ok路径，并将该TSV作为文件名来源固定摘要。
        listing_kind='archive_public_lst'
        if len(listings)==1:
            listing=listings[0]
            canonical={line.replace('\\','/').lower():line.replace('\\','/')for line in listing.read_text(encoding='utf-8-sig').splitlines()}
        elif not listings:
            traces=list(temporary.glob('*.extract.tsv'))
            if len(traces)!=1:raise ValueError('资源提取路径收据数量异常')
            listing=traces[0];canonical={};missing=[];listing_kind='extractor_actual_path_tsv_from_internal_listfile'
            for line in listing.read_text(encoding='utf-8-sig').splitlines():
                status,rel=line.split('\t',1);rel=rel.replace('\\','/')
                if not rel or rel.startswith('/') or ':' in rel or '..' in rel.split('/'):
                    raise ValueError('提取器路径越界')
                if status=='ok':
                    if rel.lower()in canonical:raise ValueError('提取器报告重复文件名')
                    canonical[rel.lower()]=rel
                elif status=='missing':missing.append(rel)
                else:raise ValueError('未知提取器状态')
            if not canonical:raise ValueError('内部清单没有已提取资源，不能登记空包')
            for name in missing:
                if not any(path.startswith(name.lower().rstrip('/')+'/')for path in canonical):
                    raise ValueError('内部清单存在缺失资源，而非目录节点')
        else:raise ValueError('资源公共清单数量异常')
        entries={}
        for item in sorted(temporary.rglob('*')):
            if not item.is_file():continue
            rel=item.relative_to(temporary).as_posix();raw=item.read_bytes()
            if rel.split('/')[0].lower()in ('main','hires','client_shipretail'):
                listed=canonical.get(rel.lower())
                if listed is None:raise ValueError('实际资源缺少公共清单来源')
                entries[rel.lower()]={'path':rel,'decode_filename':listed.rsplit('/',1)[-1],'encoded_size':len(raw),'encoded_sha256':hashlib.sha256(raw).hexdigest()}
            publish(folder/rel,raw)
        if listing_kind=='extractor_actual_path_tsv_from_internal_listfile' and set(entries)!=set(canonical):
            raise ValueError('TSV成功路径与实际提取文件集合不一致')
        checked(source,expected_sha256)
        receipt={'schema':1,'archive':{'path':str(source),'name':source.name,'size':len(data),'sha256':expected_sha256},'archive_copy':str(alias),'cache_root':str(folder),'extractor_sha256':EXTRACTOR_SHA,'ifs2_sha256':IFS_SHA,'listing':{'name':listing.name,'sha256':hashlib.sha256(listing.read_bytes()).hexdigest(),'kind':listing_kind},'actual_resource_count':len(entries),'entries':entries,'scope':'仅提取已绑定新版源包到编码缓存，未全量发布。公共.lst可能覆盖同组分包；仅entries表示本包实际承载资源。','fresh_extraction_this_receipt':True}
        publish(receipt_path,(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n').encode('utf-8'))
    return receipt


def main() -> int:
    """输出完成收据及其完整指纹，供宿主允许名单绑定。"""
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive',required=True)
    parser.add_argument('--archive-sha256',required=True)
    parser.add_argument('--legacy-client',type=Path,default=Path(r'D:\Games\CODOL\Client'))
    parser.add_argument('--check-resource',help='额外确认一个相对资源真实存在；不触发解码发布')
    args=parser.parse_args();receipt=cache_archive(args.archive,args.archive_sha256,args.legacy_client)
    path=Path(receipt['cache_root'])/'extraction-inventory.json'
    result={'receipt':str(path),'receipt_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'actual_resource_count':receipt['actual_resource_count'],'listing':receipt['listing']}
    if args.check_resource:
        key=args.check_resource.replace('\\','/').lower()
        result['resource']=receipt['entries'].get(key)
        if result['resource']is None:raise ValueError('此分包未实际承载指定资源')
    print(json.dumps(result,ensure_ascii=True));return 0

if __name__=='__main__':
    raise SystemExit(main())
