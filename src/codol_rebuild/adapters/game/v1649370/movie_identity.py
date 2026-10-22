"""固定版本主电影资源的身份常量与通用替换执行器。

本模块只承载资源指纹（文件名、长度、哈希、加载点）和与具体规则无关的
校验、应用、打包、扫描能力，不导入任何规则提供者模块；规则提供者与
主电影聚合编排层（ui_movie）均单向依赖本模块，避免规则收集与执行
能力之间形成循环依赖。不修改磁盘资源、共享资源包或在线接口。
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


def build_resource_patch(read, header_address: int, rules) -> dict:
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
    patched = apply_rules(original, rules)
    replacement = zlib.compress(patched, 9)
    if len(replacement) > compressed_size:
        raise ValueError('主电影压缩资源超出原缓冲区')
    return {'header_address': header_address, 'header_before': header,
            'data_address': data, 'data_before': compressed, 'data_after': replacement,
            'length_address': header_address + 4,
            'length_after': struct.pack('<I', len(replacement)),
            'patched_sha256': hashlib.sha256(patched).hexdigest()}


def find_resource_patch(read, regions, rules) -> dict:
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
