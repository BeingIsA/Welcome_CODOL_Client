"""固定版本大厅空赛季保护；只生成经指纹校验的内存资源改动，不操作进程。"""
from __future__ import annotations

from codol_rebuild.client.v1649370 import ui_movie

LOAD_SITE = ui_movie.LOAD_SITE
LOAD_BYTES = ui_movie.LOAD_BYTES
RESOURCE_NAME = ui_movie.RESOURCE_NAME
RESOURCE_SIZE = ui_movie.RESOURCE_SIZE
COMPRESSED_SIZE = ui_movie.COMPRESSED_SIZE
RESOURCE_SHA256 = ui_movie.RESOURCE_SHA256
METHOD_OFFSET = 5447049
ORIGINAL_METHOD = bytes.fromhex(
    'f104f0d002d030ef01ce0100d002ef01cf0101d202ef01d00102d302ef01d10103d502'
    'ef01d20104d602f0d2025d195d1a661a668b01461901808c01d6f0d3025d195d1a661a'
    '668d01461901808e01d7f0d5025d195d1a661a668f01461901736304f0d6026204d266'
    '9001b0762a7612090000296204d2669101ae76766305f0d7026205762a76120f000029'
    '5d195d1a661a6692014619019676766305f0d802d0660d62054693010129f0d902d066'
    '0dd3d2d14694010329f0da02d06614d14695010129f0db0247')


def guarded_method() -> bytes:
    """空赛季短路后的方法体；去掉调试元数据腾出空间，长度与栈上限不变。"""
    # pushfalse; getlocal2; iffalse -> 原 convert_b; pop; 原日期表达式。
    guarded = (b'\x02' + ORIGINAL_METHOD[5:98]
               + b'\x27\xd2\x12\x18\x00\x00\x29' + ORIGINAL_METHOD[101:])
    if len(guarded) != len(ORIGINAL_METHOD):
        raise ValueError('大厅方法长度改变')
    return guarded


def replacements() -> list:
    """返回本模块登记的资源方法替换，由主电影编排层统一校验与应用。"""
    return [(METHOD_OFFSET, ORIGINAL_METHOD, guarded_method())]


def patch_movie(original: bytes) -> bytes:
    """仅应用大厅修正；整体指纹与原始方法字节由编排层校验。"""
    return ui_movie.apply_rules(original, replacements())


def build_resource_patch(read, header_address: int) -> dict:
    """兼容既有调用：只包含大厅修正的资源写计划。"""
    return ui_movie.build_resource_patch(read, header_address, replacements())


def find_resource_patch(read, regions) -> dict:
    """兼容既有调用：只包含大厅修正的资源扫描。"""
    return ui_movie.find_resource_patch(read, regions, replacements())
