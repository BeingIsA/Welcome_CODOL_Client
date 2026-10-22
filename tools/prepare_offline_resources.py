# SPDX-License-Identifier: AGPL-3.0-or-later
"""只读新版 IFS 源包，将启动资源发布到 client/resources/v1649370，中间材料保留在 client/workspaces。

资源 AES-CTR/zlib 算法迁移自旧研究工程 Client/src/codol/resources/codec.py
（AGPL-3.0-or-later）。旧工程仅提供已校验的提取工具及解码算法材料；所有
输出资源都必须来自本项目 Original Client 的新版包。密钥仅保留于内存。
本工具不会执行游戏、加载保护组件或覆盖任何不同内容的现有文件。
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / 'client/src'))
from codol_rebuild.resources.fastfile import decode_fastfile
# 解码与 IWI 校验复用资源层统一实现，避免准备阶段与消费阶段校验规则不一致。
from codol_rebuild.resources.offline_resources import decode_loose as shared_decode_loose, validate_iwi

PACKAGES = {
    'ff_init_common_marketplace_V95.ifs': '867258212cfc7c715bb1ec106f363d159c8688b7deb7f336e3bf0f1b56589d22',
    'lf_init_common_gfx_V63.ifs': 'b544f992fe962247845f57094d4c8a52ab616c4b003f8c2dcb73b87c89a842b4',
    'ff_init_common_gfx_V109.ifs': 'c3a561e41ca05b9c0ba7f8a3f8fd9235784e827cbb323e349e633f182dbf512d',
    'ff_init_common_mp_V111.ifs': 'd6a0b3613c3c7a7c9e0b6eebb383ed03dfd546d7229b6119b38b559233004aed',
}
EXTRACTOR_SHA = '82aa733ab9fa97e1ad551d7d6b1e44772a65793d2e50cdb7adcce6e0749e338e'
IFS_SHA = '5b0324e0b2ba3545a6c02626287d4fbc6a3116594744f4647333982d85508421'
CODEC_SHA = 'f2c3bbee55444fd1be7f9a0f188fd1c3cda6b57532bde80d1d0f56778d8cc176'
EXE_SHA = '36997674b18f2896de5e9d755686f6922af10708f355175714058d0a0172b537'


def sha(data: bytes) -> str:
    """计算公开输入或输出的指纹；调用方不得将密钥明文交给报告。"""
    return hashlib.sha256(data).hexdigest()


def checked(path: Path, expected: str) -> bytes:
    """版本绑定输入，遇到未知版本立即失败。"""
    data = path.read_bytes()
    if sha(data) != expected:
        raise ValueError(f'输入指纹不符: {path}')
    return data


def publish(path: Path, data: bytes) -> None:
    """只允许固定资源目录或工作区输出，排他创建；同字节重跑可复用，绝不覆盖不同内容。"""
    if not any(path.resolve().is_relative_to((PROJECT / base).resolve()) for base in ('client/workspaces', 'client/resources/v1649370')):
        raise ValueError('输出必须位于 client/resources/v1649370 或 client/workspaces')
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError(f'已有输出不同，拒绝覆盖: {path}')
    else:
        with path.open('xb') as stream:
            stream.write(data)


def safe_name(name: str) -> str:
    """拒绝盘符、绝对路径、父目录及非资源根，防止归档路径逃逸。"""
    name = name.replace('\\', '/')
    p = PurePosixPath(name)
    if p.is_absolute() or '..' in p.parts or ':' in name or p.parts[0].lower() not in ('main', 'client_shipretail'):
        raise ValueError(f'无效包内资源路径: {name}')
    return name


def decode_loose(raw: bytes, filename: str, key: bytes) -> bytes:
    """复用资源层统一解码；IWI 额外按完整 32 字节 IWi09 头校验，与消费端规则一致。"""
    data = shared_decode_loose(raw, filename, key)
    if filename.lower().endswith('.iwi'):
        validate_iwi(data)
    return data


def prepare(args) -> dict:
    """复制且核对新版包，再提取副本；保留 FF 加密形态供原引擎加载。"""
    original = PROJECT / 'Original Client'
    extractor = args.legacy_client / 'assets/extract_local_ifs.exe'
    codec = args.legacy_client / 'src/codol/resources/codec.py'
    ifs = original / 'IIPS/IFS2.dll'
    exe = original / 'codoMP_client_shipRetail.exe'
    checked(extractor, EXTRACTOR_SHA); checked(ifs, IFS_SHA); checked(exe, EXE_SHA)
    # 只读取经过绑定的算法材料，不导入旧应用或执行其初始化代码。
    codec_text = checked(codec, CODEC_SHA).decode('utf-8')
    match = re.search(r"KEY = bytes.fromhex\('([0-9a-f]+)'\)", codec_text)
    if match is None:
        raise ValueError('无法识别已绑定算法材料')
    key = bytes.fromhex(match[1])
    raw_root = PROJECT / 'client/workspaces/offline-resource-raw'
    input_root = PROJECT / 'client/workspaces/offline-resource-inputs'
    report = {'schema': 1, 'scope': '新版基础启动资源，非完整运行依赖闭包',
              'source_exe_sha256': EXE_SHA, 'ifs2_sha256': IFS_SHA,
              'extractor_sha256': EXTRACTOR_SHA, 'codec_source_sha256': CODEC_SHA,
              'source_resource_version': 461, 'old_version_resources_used': False,
              'output_root': str(args.output.resolve()), 'archives': [], 'resources': [],
              'absent_pre_gfx_patches': ['patch_code_pre_gfx_mp.ff', 'patch_localized_code_pre_gfx_mp.ff'],
              'limitations': ['patch_pre 文件未在新版 IFS 明文清单中发现；未生成空补丁。',
                             'common_mp 等可能引用其他 loose 包；本工具不声称完成全部依赖闭包。']}
    for archive_name, digest in PACKAGES.items():
        source = original / 'IIPS/IIPSDownload' / archive_name
        archive_data = checked(source, digest)
        alias = input_root / archive_name
        publish(alias, archive_data)
        folder = raw_root / source.stem
        # 每轮均从绑定包副本重新提取，不能把未验证的缓存冒充源包内容。
        # 临时目录位于固定工作区，退出时只清理已解析且验证边界的该目录。
        input_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='ifs-verify-', dir=input_root) as tmp:
            temporary = Path(tmp).resolve()
            if not temporary.is_relative_to(input_root.resolve()):
                raise ValueError('临时目录越界')
            task = subprocess.run([str(extractor), str(ifs), str(alias), str(temporary)], capture_output=True, timeout=60)
            if task.returncode not in (0, 10):
                raise ValueError(f'资源提取失败 {archive_name}: {task.returncode}')
            for fresh in temporary.rglob('*'):
                if fresh.is_file():
                    publish(folder / fresh.relative_to(temporary), fresh.read_bytes())
        listings = list(folder.glob('*.lst'))
        if len(listings) != 1:
            raise ValueError('归档清单数量异常')
        listing = listings[0]
        entries = [line for line in listing.read_text(encoding='utf-8-sig').splitlines() if line and not line.lower().endswith('.lst')]
        package = {'path': str(source), 'sha256': digest, 'size': len(archive_data),
                   'listing_sha256': sha(listing.read_bytes()), 'listed_resources': len(entries), 'verified_resources': 0}
        report['archives'].append(package)
        for name in entries:
            name = safe_name(name)
            raw_path = folder / name
            if not raw_path.resolve().is_relative_to(folder.resolve()):
                raise ValueError('提取结果路径逃逸')
            raw = raw_path.read_bytes()
            item = {'game_path': name, 'archive': archive_name, 'encoded_sha256': sha(raw), 'encoded_size': len(raw)}
            if name.lower().endswith('.ff'):
                if not raw.startswith(b'IWff0100') or int.from_bytes(raw[8:12], 'little') != 461:
                    raise ValueError('FF 格式或版本不符')
                verification = decode_fastfile(raw_path, exe)
                item['fastfile_verification'] = verification
                item['transformation'] = '归档原字节保留；仅在内存中验证 FF RSA/SHA/zlib/Zone'
                data = raw
            else:
                data = decode_loose(raw, PurePosixPath(name).name, key)
                item['transformation'] = 'AES-CTR 解码 + 严格 zlib EOF/尾部/尺寸校验'
            item.update(size=len(data), sha256=sha(data))
            publish(args.output / name, data)
            report['resources'].append(item)
            package['verified_resources'] += 1
        if checked(source, digest) != archive_data:
            raise ValueError('源包处理期间变化')
        print(f'{archive_name}: {package["verified_resources"]} 个资源已校验并发布', flush=True)
    report['resource_count'] = len(report['resources'])
    report['total_output_bytes'] = sum(item['size'] for item in report['resources'])
    publish(args.manifest, (json.dumps(report, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
    return report


def main() -> int:
    """命令行入口；--help 说明依赖，复跑要求现有资源逐字节一致。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--legacy-client', type=Path, default=Path(r'D:\Games\CODOL\Client'), help='仅读取提取器与 codec.py 算法材料的旧研究工具目录')
    parser.add_argument('--output', type=Path, default=PROJECT / 'client/resources/v1649370/offline-resources', help='位于本项目固定资源目录或 workspaces，保持原游戏相对路径')
    parser.add_argument('--manifest', type=Path, default=PROJECT / 'client/resources/v1649370/offline-resources-manifest-v2.json', help='排他创建完整来源清单；旧清单作为历史记录保留')
    args = parser.parse_args()
    if not any(args.output.resolve().is_relative_to((PROJECT / base).resolve()) for base in ('client/workspaces', 'client/resources/v1649370')):
        parser.error('输出目录越界')
    report = prepare(args)
    print(json.dumps({'resource_count': report['resource_count'], 'total_output_bytes': report['total_output_bytes']}, ensure_ascii=False))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
