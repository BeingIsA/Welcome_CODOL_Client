"""固定版本主电影资源的组合补丁编排：校验原版指纹、应用等长方法替换并重新压缩。

同一份 codo.gfx 承载多处不同职责的 ABC 方法修正（大厅空赛季保护、离线字符串净化短路），
它们位于不同 DoABC 段且互不重叠。本模块只负责资源身份、替换规则顺序、重叠与压缩边界；
具体方法字节仍由各适配模块提供，不修改磁盘资源、共享资源包或在线接口。
"""
from __future__ import annotations

import hashlib
import struct
import zlib

RESOURCE_NAME = b'scaleform/default/codo.gfx\0'
RESOURCE_SIZE = 7664688
COMPRESSED_SIZE = 3023971
RESOURCE_SHA256 = '314c4abd6936b02955e2f31f781b20ac119ea9878f83698d1a1d6f106558c31c'
LOAD_SITE = 0x5EB9F7
LOAD_BYTES = bytes.fromhex('e8a4812300')


def replacements() -> list:
    """汇总各适配模块的替换规则；延迟导入避免常量模块之间的循环依赖。"""
    from codol_rebuild.client.v1649370 import player_name, sanitize, ui_navigation

    return (list(ui_navigation.replacements()) + list(sanitize.replacements())
            + list(player_name.replacements()))


def apply_rules(original: bytes, rules) -> bytes:
    """按原版指纹逐条校验等长替换；越界、重叠或原始字节不符均拒绝。"""
    if (len(original) != RESOURCE_SIZE
            or hashlib.sha256(original).hexdigest() != RESOURCE_SHA256):
        raise ValueError('主电影资源指纹不符')
    patched = bytearray(original)
    spans = []
    for offset, before, after in rules:
        if type(offset) is not int or offset < 0 or offset + len(before) > RESOURCE_SIZE:
            raise ValueError('主电影替换区间越界')
        if len(before) != len(after):
            raise ValueError('主电影替换必须保持方法体等长')
        if original[offset:offset + len(before)] != before:
            raise ValueError(f'主电影替换原始字节不符：{offset:#x}')
        for start, end in spans:
            if offset < end and start < offset + len(before):
                raise ValueError('主电影替换区间重叠')
        spans.append((offset, offset + len(before)))
        patched[offset:offset + len(after)] = after
    return bytes(patched)


def patch_movie(original: bytes) -> bytes:
    """应用全部已登记替换，供离线会话内注入使用。"""
    return apply_rules(original, replacements())


def build_resource_patch(read, header_address: int, rules=None) -> dict:
    """校验重定位 RawFile 头及完整解压内容，返回数据和长度两项显式写计划。"""
    header = read(header_address, 16)
    name, compressed_size, size, data = struct.unpack('<4I', header)
    if (compressed_size, size) != (COMPRESSED_SIZE, RESOURCE_SIZE):
        raise ValueError('主电影 RawFile 长度不符')
    if read(name, len(RESOURCE_NAME)) != RESOURCE_NAME:
        raise ValueError('主电影 RawFile 名称不符')
    compressed = read(data, compressed_size)
    decoder = zlib.decompressobj()
    original = decoder.decompress(compressed, RESOURCE_SIZE + 1)
    if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise ValueError('主电影压缩流不完整或包含尾部数据')
    patched = apply_rules(original, replacements() if rules is None else rules)
    replacement = zlib.compress(patched, 9)
    if len(replacement) > compressed_size:
        raise ValueError('主电影压缩资源超出原缓冲区')
    return {'header_address': header_address, 'header_before': header,
            'data_address': data, 'data_before': compressed, 'data_after': replacement,
            'length_address': header_address + 4,
            'length_after': struct.pack('<I', len(replacement)),
            'patched_sha256': hashlib.sha256(patched).hexdigest()}


def find_resource_patch(read, regions, rules=None) -> dict:
    """扫描精确长度对后校验名称与完整资源指纹；歧义或缺失时拒绝修改。"""
    marker = struct.pack('<II', COMPRESSED_SIZE, RESOURCE_SIZE)
    found = {}
    for base, size in regions:
        for offset in range(0, size, 1024 * 1024):
            try:
                chunk = read(base + offset, min(size - offset, 1024 * 1024 + 16))
            except OSError:
                continue
            start = 0
            while (index := chunk.find(marker, start)) >= 0:
                start = index + 1
                address = base + offset + index - 4
                if address in found:
                    continue
                try:
                    plan = build_resource_patch(read, address, rules)
                except (OSError, ValueError, zlib.error):
                    continue
                found[address] = plan
    if len(found) != 1:
        raise ValueError(f'主电影资源匹配数必须为 1，实际为 {len(found)}')
    return next(iter(found.values()))
