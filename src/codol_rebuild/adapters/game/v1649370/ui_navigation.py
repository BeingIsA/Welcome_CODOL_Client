"""固定版本页面导航与商城空分类保护；只生成经指纹校验的内存资源改动，不操作进程。"""
from __future__ import annotations

from codol_rebuild.adapters.game.v1649370 import movie_identity

LOAD_SITE = movie_identity.LOAD_SITE
LOAD_BYTES = movie_identity.LOAD_BYTES
RESOURCE_NAME = movie_identity.RESOURCE_NAME
RESOURCE_SIZE = movie_identity.RESOURCE_SIZE
COMPRESSED_SIZE = movie_identity.COMPRESSED_SIZE
RESOURCE_SHA256 = movie_identity.RESOURCE_SHA256
METHOD_OFFSET = 5447049
ORIGINAL_METHOD = bytes.fromhex(
    'f104f0d002d030ef01ce0100d002ef01cf0101d202ef01d00102d302ef01d10103d502'
    'ef01d20104d602f0d2025d195d1a661a668b01461901808c01d6f0d3025d195d1a661a'
    '668d01461901808e01d7f0d5025d195d1a661a668f01461901736304f0d6026204d266'
    '9001b0762a7612090000296204d2669101ae76766305f0d7026205762a76120f000029'
    '5d195d1a661a6692014619019676766305f0d802d0660d62054693010129f0d902d066'
    '0dd3d2d14694010329f0da02d06614d14695010129f0db0247')


# 退出页奖励组件可能未激活；关闭时不得对空计时器调用 reset。
QUIT_TIMER_OFFSET = 6883748
QUIT_TIMER_BEFORE = bytes.fromhex(
    'f103f03dd030f03fd045110029f040d06616461c0029f041d066165d1766176618d06614461d0229f042d0206816f043'
    '47'
)
QUIT_TIMER_AFTER = bytes.fromhex(
    'd030d045110029d06616121b0000d06616461c0029d066165d1766176618d06614461d0229d020681647020202020202'
    '02'
)

# 推荐分类缺失时 onSetTab 仍传入空值；归一为空 Vector<Object>，
# 使原生列表收尾及后续栏目显示、页面切换能够继续执行。
MARKET_ITEMS_OFFSET = 1052641
MARKET_ITEMS_BEFORE = bytes.fromhex(
    'f103f08f01d030208029d62400d7ef0152008f01f09101d166062400af762a76120c000029'
    'd12400661f5d176617b37612520000ef0153019301ef0154029401f093015d2a662a5d026603'
    '5d176617530124004201d1462b028029d6f09401240073d71017000009f09601d1d3661f5d17'
    '661787d2612cf09401d3c073d7d3d1660615e1fffff09a01d0d145280129f09b0147'
)
# 前 23 字节：建立作用域；非空跳到原逻辑；空值构造空向量。
# 原 local2/local3 的初值无读取者，两者在商品分支中均先赋值再使用。
MARKET_ITEMS_AFTER = (
    bytes.fromhex('d030d1111000005d0266035d146614530142008018d502')
    + MARKET_ITEMS_BEFORE[23:]
)


# 普通房间没有 playListID：先走原有默认倍率分支，避免提前读取空配置。
# 只调整 showRoomInfo 的判断顺序；有编号时保留原查询和倍率读取。
ROOM_DETAIL_OFFSET = 7318633
ROOM_DETAIL_BEFORE = bytes.fromhex(
    'f0a2045d6866684661005d69666966fb01620566fc015601466f0280b9016307'
    'f0a404620766fd01756308f0a504620766fe01756309f0a704620566fc015dff0166ff0114120000'
)
ROOM_DETAIL_AFTER = bytes.fromhex(
    '620566fc015dff0166ff01133900005d6866684661005d69666966fb01620566fc015601466f0280b9016307'
    'f0a404620766fd01756308f0a504620766fe01756309101400000202'
)


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
    return [(METHOD_OFFSET, ORIGINAL_METHOD, guarded_method()),
            (QUIT_TIMER_OFFSET, QUIT_TIMER_BEFORE, QUIT_TIMER_AFTER),
            (MARKET_ITEMS_OFFSET, MARKET_ITEMS_BEFORE, MARKET_ITEMS_AFTER),
            (ROOM_DETAIL_OFFSET, ROOM_DETAIL_BEFORE, ROOM_DETAIL_AFTER)]


def patch_movie(original: bytes) -> bytes:
    """应用本模块的页面修正；整体指纹与原始方法字节由编排层校验。"""
    return movie_identity.apply_rules(original, replacements())


def build_resource_patch(read, header_address: int) -> dict:
    """兼容既有调用：只包含本模块页面修正的资源写计划。"""
    return movie_identity.build_resource_patch(read, header_address, replacements())


def find_resource_patch(read, regions) -> dict:
    """兼容既有调用：只包含本模块页面修正的资源扫描。"""
    return movie_identity.find_resource_patch(read, regions, replacements())
