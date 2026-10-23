# SPDX-License-Identifier: AGPL-3.0-or-later
"""固定33包的模型/音频/指定配置数据资源库；不发布可执行程序。"""
from __future__ import annotations
import json
from pathlib import Path, PurePosixPath
import struct
from codol_rebuild.resources import offline_resources
from codol_rebuild.resources.offline_resources import (OfflineResourceSupplier, RAW_INVENTORIES, ARCHIVES,
                                canonical_iwi, decode_loose, digest)
from codol_rebuild.resources.image_library import build_library

AUDIT_PATH = 'client/resources/v1649370/startup-non-iwi-resources.json'
AUDIT_SHA = '7b34af13e5cb7f1e17325af1f91c2180efa88ee52a1f4654e3a3605e5514ca8c'
PRIORITY_PATH = 'client/resources/v1649370/iips-patch-priority-final.json'
PRIORITY_SHA = '18426be6fca7f966033df8f3a871359f227be2e4e7ace5b3004a207c1d3f19d9'


def canonical_loose_resource(name: str) -> str | None:
    """拒绝特殊路径；仅无后缀模型、mp3/wav和唯一fileSysCheck配置。"""
    canonical_iwi(name)  # 复用控制字符、穿越、ADS、保留设备名等Windows路径检查。
    name = name.replace('\\','/')
    while name.startswith('./'): name = name[2:]
    name = name.lower(); parts = name.split('/')
    if name == 'main/filesyscheck.cfg': return name
    if len(parts) >= 3 and parts[:2] == ['main','models'] and not PurePosixPath(name).suffix:
        return name
    if len(parts) >= 3 and parts[:2] == ['main','sound'] and PurePosixPath(name).suffix in ('.mp3','.wav'):
        return name
    return None


def resource_type(name: str) -> str:
    if canonical_loose_resource(name) != name:
        raise ValueError('非IWI资源路径不在已审类型范围内')
    if name == 'main/filesyscheck.cfg': return 'config'
    if name.startswith('main/models/'): return 'model'
    return PurePosixPath(name).suffix[1:]


def inspect_format(data: bytes, kind: str) -> dict:
    """仅声明实际可验证的内层边界；不冒充完整模型语义或音频帧解码。"""
    if kind == 'model':
        if len(data) < 32 or struct.unpack_from('<I',data)[0] != 1:
            raise ValueError('模型版本或32字节头边界不符')
        return {'kind':'codol_loose_model','version':1,'scope':'版本字段和32字节头边界；完整模型语义交由原生加载器'}
    if kind == 'config':
        if '\x00' in data.decode('utf-8'): raise ValueError('配置含NUL')
        return {'kind':'utf8_text'}
    if kind == 'wav':
        if len(data)<12 or data[:4]!=b'RIFF' or data[8:12]!=b'WAVE': raise ValueError('WAVE签名不符')
        end=struct.unpack_from('<I',data,4)[0]+8
        if end!=len(data): raise ValueError('WAVE声明尺寸不符')
        offset=12; names=[]
        while offset<end:
            if offset+8>end: raise ValueError('WAVE块头截断')
            size=struct.unpack_from('<I',data,offset+4)[0];names.append(data[offset:offset+4])
            offset+=8+size+(size&1)
            if offset>end: raise ValueError('WAVE块越界')
        if offset!=end or b'fmt ' not in names or b'data' not in names: raise ValueError('WAVE必要块缺失')
        return {'kind':'RIFF_WAVE','scope':'容器及块边界；未解码全部采样'}
    if kind != 'mp3': raise ValueError('未知数据类别')
    offset=0
    if data.startswith(b'ID3'):
        if len(data)<10 or data[3] not in (2,3,4) or any(v&128 for v in data[6:10]): raise ValueError('ID3头无效')
        size=0
        for v in data[6:10]:size=(size<<7)|v
        offset=10+size
        if offset>len(data): raise ValueError('ID3标签越界')
    if offset+4>len(data): raise ValueError('MP3首帧缺失')
    h=int.from_bytes(data[offset:offset+4],'big')
    if not ((h>>21)==0x7ff and ((h>>19)&3)!=1 and ((h>>17)&3)!=0 and ((h>>12)&15) not in (0,15) and ((h>>10)&3)!=3):
        raise ValueError('MP3首帧头无效')
    return {'kind':'MPEG_audio','scope':'ID3边界和首帧头；未解码全部音频帧'}


class LooseResourceSource(OfflineResourceSupplier):
    """与图库共享固定源包、收据、解码、覆盖选择；输出只允许指定非IWI数据类型。"""
    library_directory = 'offline-loose-library'
    library_kind = 'loose_startup_resources'
    canonical_resource = staticmethod(canonical_loose_resource)
    resource_type = staticmethod(resource_type)

    def __init__(self, project_root: Path):
        super().__init__(project_root)
        proof=self.project/AUDIT_PATH;raw=proof.read_bytes()
        if digest(raw)!=AUDIT_SHA: raise ValueError('非IWI完整审计证据摘要不符')
        if digest((self.project/PRIORITY_PATH).read_bytes())!=PRIORITY_SHA: raise ValueError('原生覆盖规则证据摘要不符')
        audit=json.loads(raw)
        if audit['issues']: raise ValueError('非IWI审计尚有未解决问题')
        self.expected={r['game_path']:r for r in audit['resources']}
        if len(self.expected)!=audit['unique_resource_count']: raise ValueError('非IWI审计有重复或缺失条目')
        self.expected_count=audit['unique_resource_count'];self.expected_bytes=audit['total_selected_decoded_bytes']
        self._rules={}
        for item in audit['overrides']:
            self._rules[item['resource']]={'selected_archive':item['selected_archive'],'file_item_index':item['file_item_index'],
                'evidence':{'path':AUDIT_PATH,'sha256':AUDIT_SHA},
                'candidates':[{k:c[k] for k in ('archive','encoded_sha256','decoded_sha256','decoded_size')}for c in item['candidates']]}
        self._decoded={};self._receipts={};self.manifest=None
        for archive,receipt_hash in RAW_INVENTORIES.items():
            folder=(self.workspace/'offline-resource-raw'/Path(archive).stem).resolve()
            if not folder.is_relative_to(self.workspace): raise ValueError('非IWI缓存目录越界')
            data=(folder/'extraction-inventory.json').read_bytes()
            if digest(data)!=receipt_hash: raise ValueError('非IWI源收据摘要不符')
            receipt=json.loads(data)
            if receipt['archive']['name']!=archive or receipt['archive']['sha256']!=ARCHIVES[archive]: raise ValueError('非IWI收据原包不符')
            for name,entry in receipt['entries'].items():
                logical=canonical_loose_resource(name)
                if logical is None:
                    if canonical_iwi(name) is not None: continue
                    raise ValueError('源收据含未审的非IWI资源类别：'+name)
                if canonical_loose_resource(entry['path'])!=logical or PurePosixPath(entry['path']).name.lower()!=entry['decode_filename'].lower() or PurePosixPath(entry['decode_filename']).name!=entry['decode_filename']:
                    raise ValueError('非IWI源条目路径或解码名不符')
                self._receipts.setdefault(logical,[]).append((archive,folder,entry,receipt_hash))
        if set(self._receipts)!=set(self.expected): raise ValueError('非IWI实际收据集合与完整审计不符')

    def resolution_for(self,name: str) -> dict | None:
        return self._rules.get(name)

    def _decode_resource(self,raw: bytes,filename: str) -> bytes:
        return decode_loose(raw,filename,offline_resources.RESOURCE_KEY)

    def _load_encoded(self,candidate: tuple) -> tuple[bytes,dict]:
        data,provenance=super()._load_encoded(candidate)
        kind=resource_type(canonical_loose_resource(candidate[2]['path']))
        provenance['format']=inspect_format(data,kind)
        provenance['verification']='固定新版源包/实际收据/编码摘要 + 严格AES/zlib尺寸/EOF/尾部 + 指定内层类型边界'
        return data,provenance

    def select_candidate(self,name: str,candidates: list[tuple[bytes,dict]]) -> tuple[bytes,dict]:
        data,provenance=super().select_candidate(name,candidates)
        expected=self.expected[name]
        if (provenance['archive'],provenance['encoded_sha256'],digest(data),len(data))!=(expected['archive'],expected['encoded_sha256'],expected['decoded_sha256'],expected['decoded_size']):
            raise ValueError('非IWI实际赢家与已审来源及内容指纹不符')
        provenance['whole_group_audit']={'path':AUDIT_PATH,'sha256':AUDIT_SHA}
        return data,provenance


def prepare_loose_library(project: Path, *, plan_only: bool = False, progress=None) -> dict:
    return build_library(project,source=LooseResourceSource(project),plan_only=plan_only,progress=progress)
