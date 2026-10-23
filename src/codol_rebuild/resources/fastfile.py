# SPDX-License-Identifier: AGPL-3.0-or-later
"""CODOL Fastfile 的离线解码与完整性验证；不执行客户端或导入游戏 DLL。

AES 小端 CTR、签名块遍历及 Zone 目录算法迁移自：
D:/Games/CODOL/GameServer/native/src/codol/assets/codol_fastfile.c、
codol_client_crypto.c、codol_assets.c（AGPL-3.0-or-later）。
本模块新增 461 版本材料定位、发行方 RSA 签名强制验证、阶段化诊断和 CLI。
私钥及 AES 材料仅存在于进程内存，不打印、不导出、不嵌入代码。
"""
from __future__ import annotations

import argparse
from collections import Counter as Counts
import hashlib
import hmac
import json
from pathlib import Path
import struct
import sys
import zlib

from Crypto.Cipher import AES, PKCS1_OAEP, PKCS1_v1_5
from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15
from Crypto.Util import Counter

FF_HEADER = 0x115
BLOCK_SIZE = 0x2000
MAX_FILE_SIZE = 512 * 1024 * 1024
MATERIAL_SHA256 = '3a2f7891ac98f1fdf6a9a221c5586725157df270d6831e0ff82c383cd262cbb5'
SIGNING_PUBLIC_SHA256 = '9ac28dd61aa9c92ac8931fc85d4932fa9759781ea51347f0320d80c23205c48c'
MATERIAL_OFFSETS = {334: 0x83C248, 365: 0x8AE880, 461: 0x6984B0}


class FastfileError(ValueError):
    """带失败阶段的格式错误；details 只允许公开偏移、长度及摘要。"""

    def __init__(self, stage: str, message: str, **details):
        """保存可序列化诊断，不包含解密材料或底层密码异常内容。"""
        super().__init__(message)
        self.stage = stage
        self.details = details

    def as_dict(self):
        """返回 CLI 和证据文件统一使用的错误表示。"""
        return {'ok': False, 'stage': self.stage, 'message': str(self), 'details': self.details}


def _sha(data: bytes) -> str:
    """计算用于来源追溯和完整性记录的 SHA-256 十六进制摘要。"""
    return hashlib.sha256(data).hexdigest()


def _read_bounded(path: Path, limit: int = MAX_FILE_SIZE) -> bytes:
    """限制读取大小，避免意外把不相关大文件载入内存。"""
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise FastfileError('input_size', '输入超过允许大小', limit=limit)
    return data


def inspect_fastfile(path: str | Path) -> dict:
    """只读检查外层头、版本、块边界和输入指纹，不接触任何密钥。"""
    path = Path(path).resolve()
    return _inspect(_read_bounded(path), path)


def _inspect(raw: bytes, path: Path | None = None) -> dict:
    """验证外层结构；对未知版本明确拒绝，防止把猜测当兼容性。"""
    if len(raw) < FF_HEADER:
        raise FastfileError('outer_header', 'Fastfile 头被截断', size=len(raw))
    magic = raw[:8]
    if magic not in (b'IWff0100', b'IWffu100'):
        raise FastfileError('outer_header', '未知 Fastfile 魔数')
    version = struct.unpack_from('<I', raw, 8)[0]
    if version not in MATERIAL_OFFSETS:
        raise FastfileError('version', '尚未支持该 Fastfile 版本', version=version)
    signed = magic == b'IWff0100'
    body_size = len(raw) - FF_HEADER
    if not body_size or (signed and (body_size % BLOCK_SIZE or body_size < 3 * BLOCK_SIZE)):
        raise FastfileError('outer_header', '签名块边界或文件长度不合法', body_size=body_size)
    return {'path': str(path) if path else None, 'size': len(raw), 'sha256': _sha(raw),
            'magic': magic.decode('ascii'), 'version': version, 'signed': signed,
            'body_size': body_size, 'body_blocks': body_size // BLOCK_SIZE if signed else None}


def _load_keys(exe: bytes, version: int, require_signing: bool):
    """只导入已知指纹的客户端材料，返回对象及不含材料的来源信息。"""
    offset = MATERIAL_OFFSETS[version]
    der = exe[offset:offset + 1191]
    if _sha(der) != MATERIAL_SHA256:
        raise FastfileError('rsa_material', '客户端版本或 RSA 材料指纹不匹配', offset=offset)
    try:
        private_key = RSA.import_key(der)
    except (ValueError, IndexError, TypeError):
        raise FastfileError('rsa_material', '已定位的 RSA 材料无法解析') from None
    signing_key = None
    signing_offset = None
    if require_signing:
        # 公钥位于新版 EXE 的 0x698958。其余版本仅扫描同一已知指纹，绝不自动信任新公钥。
        offset_candidates = [0x698958]
        start = 0
        while True:
            start = exe.find(b'\x30\x82\x01\x0a', start)
            if start < 0:
                break
            offset_candidates.append(start)
            start += 1
        for candidate in offset_candidates:
            public_der = exe[candidate:candidate + 270]
            if _sha(public_der) == SIGNING_PUBLIC_SHA256:
                signing_key = RSA.import_key(public_der)
                signing_offset = candidate
                break
        if signing_key is None:
            raise FastfileError('signing_key', '找不到已验证指纹的发行方签名公钥')
    return private_key, signing_key, {
        'material_offset': offset, 'material_size': 1191, 'material_sha256': MATERIAL_SHA256,
        'signing_public_offset': signing_offset, 'signing_public_size': 270 if signing_key else None,
        'signing_public_sha256': SIGNING_PUBLIC_SHA256 if signing_key else None}


def _decrypt_material(encrypted: bytes, private_key, version: int) -> bytes:
    """334 使用 OAEP-SHA256；365/461 使用 PKCS#1 v1.5，并严格要求 40 字节。"""
    try:
        if version == 334:
            material = PKCS1_OAEP.new(private_key, hashAlgo=SHA256).decrypt(encrypted)
        else:
            material = PKCS1_v1_5.new(private_key).decrypt(encrypted, b'', expected_pt_len=40)
    except (ValueError, TypeError):
        material = b''
    if len(material) != 40:
        raise FastfileError('rsa_padding', 'RSA 填充或解密材料长度校验失败')
    return material


def _ctr(data: bytes, key: bytes, iv: bytes) -> bytes:
    """按原客户端的 128 位小端计数器运行 AES-192 CTR，每块独立重置 IV。"""
    if len(key) != 24 or len(iv) < 16:
        raise FastfileError('aes_material', 'AES 材料长度错误')
    counter = Counter.new(128, initial_value=int.from_bytes(iv[:16], 'little'), little_endian=True)
    return AES.new(key, AES.MODE_CTR, counter=counter).decrypt(data)


def _signed_payload(body: bytes, material: bytes, signing_key) -> tuple[bytes, dict]:
    """强制验证头部 RSA 签名及所有 SHA-256 哈希链，再输出压缩流。"""
    key = material[:24]
    header = _ctr(body[:BLOCK_SIZE], key, material[24:])
    if header[:8] != b'IWffs100':
        raise FastfileError('signed_header', '解密后的签名头魔数不匹配')
    digest = SHA256.new(header[0x12C:0x1FD0])
    if not hmac.compare_digest(digest.digest(), header[0xC:0x2C]):
        raise FastfileError('header_hash', '签名头覆盖区 SHA-256 校验失败')
    try:
        pkcs1_15.new(signing_key).verify(digest, header[0x2C:0x12C])
    except (ValueError, TypeError):
        raise FastfileError('publisher_signature', '发行方 RSA PKCS#1 v1.5 SHA256 签名校验失败') from None
    chunks = []
    section = 0
    table_index = 0
    tables = 0
    data_blocks = 0
    table = b''
    for block_index in range(1, len(body) // BLOCK_SIZE):
        if not table_index:
            if section >= (0x1FD0 - 0x150) // 32:
                raise FastfileError('hash_table', '哈希表数量超出签名头容量', section=section)
            entry = header[0x150 + section * 32:0x170 + section * 32]
        else:
            entry = table[(table_index - 1) * 32:table_index * 32]
        block = _ctr(body[block_index * BLOCK_SIZE:(block_index + 1) * BLOCK_SIZE], key, entry)
        if not hmac.compare_digest(hashlib.sha256(block).digest(), entry):
            raise FastfileError('table_hash' if not table_index else 'data_hash',
                                '签名块 SHA-256 校验失败', block_index=block_index, section=section)
        if not table_index:
            table = block
            tables += 1
            table_index = 1
        else:
            chunks.append(block)
            data_blocks += 1
            table_index += 1
            if table_index == 257:
                section += 1
                table_index = 0
    if table_index == 1:
        raise FastfileError('hash_table', '文件末尾存在没有数据块的哈希表')
    return b''.join(chunks), {
        'header_magic_verified': True, 'header_sha256_verified': True,
        'publisher_signature_verified': True, 'publisher_signature_algorithm': 'RSA-2048/PKCS1-v1_5/SHA256',
        'verified_hash_tables': tables, 'verified_data_blocks': data_blocks,
        'verified_blocks_including_header': 1 + tables + data_blocks}


def _inflate(compressed: bytes, signed: bool, max_zone_size: int) -> tuple[bytes, dict]:
    """有界解压并检查 zlib 结束标记和 Adler32；已签名末块允许任意填充。"""
    decoder = zlib.decompressobj()
    try:
        zone = decoder.decompress(compressed, max_zone_size + 1)
    except zlib.error:
        raise FastfileError('zlib', 'zlib 流或 Adler32 校验失败') from None
    if len(zone) > max_zone_size:
        raise FastfileError('zone_size', '解压后的 Zone 超过限制', limit=max_zone_size)
    if not decoder.eof or decoder.unconsumed_tail:
        raise FastfileError('zlib', 'zlib 流未完整结束')
    padding = len(decoder.unused_data)
    if (signed and padding >= BLOCK_SIZE) or (not signed and padding):
        raise FastfileError('zlib_padding', 'zlib 结束后的填充长度不合法', padding=padding)
    return zone, {'zlib_eof_verified': True, 'adler32_verified': True, 'trailing_padding_bytes': padding}


def validate_zone(zone: bytes) -> dict:
    """验证 Zone 长度、字符串边界和资产目录的连续覆盖，不声称资产内部布局已兼容。"""
    size = len(zone)
    if size < 0x38 or struct.unpack_from('<I', zone)[0] != size - 0x28:
        raise FastfileError('zone_header', 'Zone 声明长度与实际长度不匹配', size=size)
    string_count = struct.unpack_from('<I', zone, 0x28)[0]
    asset_count = struct.unpack_from('<I', zone, 0x30)[0]
    if string_count > (size - 0x38) // 4:
        raise FastfileError('zone_strings', '脚本字符串指针表越界', count=string_count)
    pos = 0x38 + string_count * 4
    for index in range(string_count):
        pointer = struct.unpack_from('<I', zone, 0x38 + index * 4)[0]
        if pointer == 0xFFFFFFFF:
            end = zone.find(b'\0', pos)
            if end < 0:
                raise FastfileError('zone_strings', '脚本字符串缺少结束符', index=index)
            pos = end + 1
    if asset_count > (size - pos) // 16:
        raise FastfileError('zone_directory', '资产目录越界', count=asset_count, offset=pos)
    expected = pos + asset_count * 16
    entries = []
    for index in range(asset_count):
        kind, offset, pointer, extent = struct.unpack_from('<4I', zone, pos + index * 16)
        if offset + 0x28 != expected or extent > size - expected:
            raise FastfileError('zone_extent', '资产范围不连续或越界', index=index,
                                expected_offset=expected, actual_offset=offset + 0x28, size=extent)
        entries.append({'index': index, 'type': kind, 'offset': expected, 'size': extent, 'pointer': pointer})
        expected += extent
    if expected != size:
        raise FastfileError('zone_extent', '目录未覆盖整个 Zone', covered=expected, actual=size)
    return {'size': size, 'sha256': _sha(zone), 'declared_length_verified': True,
            'string_count': string_count, 'asset_count': asset_count, 'directory_offset': pos,
            'directory_coverage_verified': True, 'block_sizes': list(struct.unpack_from('<8I', zone, 8)),
            'asset_type_counts': dict(sorted(Counts(str(item['type']) for item in entries).items(), key=lambda item: int(item[0]))),
            'asset_entries': entries, 'asset_internal_layout_compatible': None}


def _write_new_or_identical(path: Path, data: bytes) -> None:
    """只排他创建新产物；现有同内容文件可复跑，不改写任何已有文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open('xb') as stream:
            stream.write(data)
    except FileExistsError:
        if path.stat().st_size != len(data) or _sha(_read_bounded(path)) != _sha(data):
            raise FastfileError('output', '输出已存在且内容不同，拒绝覆盖', path=str(path)) from None


def _check_output_path(output: Path, inputs: tuple[Path, ...], client_exe: Path) -> None:
    """禁止写入原客户端目录，或经路径别名指向任何输入文件。"""
    if output.is_relative_to(client_exe.parent) or output in inputs:
        raise FastfileError('output', '输出不得覆盖输入或写入原客户端目录', path=str(output))
    if output.exists() and any(output.samefile(item) for item in inputs if item.exists()):
        raise FastfileError('output', '输出别名指向输入文件', path=str(output))


def decode_fastfile(path: str | Path, client_exe: str | Path,
                    output: str | Path | None = None, *, max_zone_size: int = MAX_FILE_SIZE) -> dict:
    """离线解码一个 FF；全部验证通过才写 Zone，已存在相同输出可安全复跑。"""
    if not 0 < max_zone_size <= MAX_FILE_SIZE:
        raise FastfileError('zone_size', '解压上限必须在 1 到 512 MiB 之间')
    path, client_exe = Path(path).resolve(), Path(client_exe).resolve()
    if output is not None:
        _check_output_path(Path(output).resolve(), (path, client_exe), client_exe)
    raw = _read_bounded(path)
    outer = _inspect(raw, path)
    exe = _read_bounded(client_exe)
    private_key, signing_key, key_source = _load_keys(exe, outer['version'], outer['signed'])
    material = _decrypt_material(raw[0x15:FF_HEADER], private_key, outer['version'])
    if outer['signed']:
        compressed, checks = _signed_payload(raw[FF_HEADER:], material, signing_key)
    else:
        compressed = _ctr(raw[FF_HEADER:], material[:24], material[24:])
        checks = {'publisher_signature_verified': False, 'reason': 'unsigned_fastfile'}
    zone, zlib_checks = _inflate(compressed, outer['signed'], max_zone_size)
    directory = validate_zone(zone)
    checks.update(zlib_checks)
    report = {'ok': True, 'scope': 'archive_integrity_and_zone_directory_only',
              'input': outer, 'client': {'path': str(client_exe), 'size': len(exe), 'sha256': _sha(exe)},
              'key_source': key_source, 'rsa_padding': 'OAEP-SHA256' if outer['version'] == 334 else 'PKCS1-v1_5',
              'aes': 'AES-192-CTR/little-endian-counter', 'checks': checks, 'zone': directory, 'output': None}
    if output is not None:
        output = Path(output).resolve()
        _write_new_or_identical(output, zone)
        report['output'] = str(output)
    return report


def main(argv=None) -> int:
    """命令行入口：inspect 只检查外层，decode 执行全部校验并可选写出 Zone。"""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    inspect_parser = commands.add_parser('inspect', help='只检查 Fastfile 外层')
    inspect_parser.add_argument('fastfile', type=Path)
    decode_parser = commands.add_parser('decode', help='验证并解码 Fastfile')
    decode_parser.add_argument('fastfile', type=Path)
    decode_parser.add_argument('--client-exe', type=Path, required=True)
    decode_parser.add_argument('--output', type=Path)
    decode_parser.add_argument('--report', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == 'inspect':
            result = {'ok': True, 'input': inspect_fastfile(args.fastfile)}
        else:
            if args.report:
                inputs = (args.fastfile.resolve(), args.client_exe.resolve())
                if args.output:
                    inputs += (args.output.resolve(),)
                _check_output_path(args.report.resolve(), inputs, args.client_exe.resolve())
            result = decode_fastfile(args.fastfile, args.client_exe, args.output)
            if args.report:
                encoded = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
                _write_new_or_identical(args.report.resolve(), encoded.encode('utf-8'))
    except FastfileError as error:
        result = error.as_dict()
    except OSError as error:
        result = {'ok': False, 'stage': 'io', 'message': str(error)}
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    print(encoded)
    return 0 if result.get('ok') else 1


if __name__ == '__main__':
    sys.exit(main())
