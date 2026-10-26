"""本地充值按钮的 GFx 桥；余额规则由应用层计算，适配层负责原生发布与补偿。

主电影将按钮改为单参数 OpenWebPage(1) 本地请求。原版网页调用固定为六参数，
适配器仅截获单参数形式，其他网页入口继续调用原处理器。仅在 local-profile 安装。
"""
from __future__ import annotations

import struct
from codol_rebuild.adapters.game.v1649370.accounts.profile import currency_snapshot, publish_currency

HANDLER_SLOT = 0xDC8218
HANDLER = 0x5CC2F0


def validate_contract(read):
    for address, expected in (
        (HANDLER_SLOT, struct.pack("<I", HANDLER)),
        (HANDLER, bytes.fromhex("558bec83e4f883ec3c")),
        (0x6440E0, bytes.fromhex("c7001482dc00")),
        (0x644122, bytes.fromhex("686c0cdc00")),
        (0xDC0C6C, b"OpenWebPage\0"),
    ):
        if read(address, len(expected)) != expected:
            raise ValueError(f"本地充值 GFx ABI 不匹配：{address:#x}")


def build_adapter(entry):
    """保留寄存器、标志和 RET 4 ABI；宿主暂停期间完成提交并填入返回值。"""
    if type(entry) is not int or not 0x10000 <= entry <= 0xFFFFE000:
        raise ValueError("充值适配地址无效")
    # PUSHFD/PUSHAD 后 [EBP+40] 为原回调参数，params+20 为参数数量。
    code = bytearray.fromhex("9c60 8bec 8b4528 83781401 0f8500000000")
    trap = entry + len(code)
    code.extend(bytes.fromhex("cc 619d c20400"))
    original = len(code)
    code.extend(bytes.fromhex("619d e9"))
    code.extend(struct.pack("<I", (HANDLER-entry-len(code)-4) & 0xFFFFFFFF))
    struct.pack_into("<i", code, 13, original-17)
    return {"code": bytes(code), "trap": trap, "patch_va": HANDLER_SLOT,
            "before": struct.pack("<I", HANDLER), "after": struct.pack("<I", entry)}


def result_slot(read, frame):
    """验证专用单参数请求和未托管的返回值槽，不借用网页对象布局。"""
    def word(address):
        return struct.unpack("<I", read(address, 4))[0]
    params = word(frame+40)
    if params < 0x10000 or word(params+20) != 1:
        raise ValueError("充值 GFx 参数数量不符")
    args = word(params+16)
    if args < 0x10000 or word(args+12) != 3 or word(args+16) != 1:
        raise ValueError("充值 GFx 请求标记不符")
    result = word(params)
    if result < 0x10000 or word(result+12) not in (0, 1):
        raise ValueError("充值 GFx 返回值槽不为空")
    return result


def reply(read, write, result, cp):
    payload = struct.pack("<Ii", 3, cp)
    write(result+12, payload)
    if read(result+12, len(payload)) != payload:
        raise ValueError("充值返回值回读失败")


def credit(read, write, plan, commit):
    """余额只通过已有原生接口读写；充值数量与上限由应用层 plan 计算，
    持久化由显式注入的档案端口负责，失败时恢复原生余额。"""
    before = currency_snapshot(read)
    after = plan(before)
    if set(after) != set(before):
        raise ValueError("充值结果必须保持原有钱币种类")
    try:
        publish_currency(read, write, after)
        commit(after)
    except Exception:
        publish_currency(read, write, before)
        raise
    return after


def replacements():
    # 复用充值事件字符串的 multiname，诊断方法名补空格保持字符串池等长。
    old_pool = b"\x11EVT_SHOW_RECHARGE\x3bcodo.component:PlayerAccountInfo/private:onReplenishCPClick"
    new_pool = b"\x0bOpenWebPage\x41codo.component:PlayerAccountInfo/private:onReplenishCPClick      "
    old_click = bytes.fromhex(
        "f105f0ac02d030f0ae025d87016687015d8801668801668901468a010129"
        "f0af025d8b015d616661668c01468b010129f0b00247")
    # 返回 -1 时保留原显示；成功后使用已提交余额刷新当前组件并重新排版。
    new_click = bytes.fromhex(
        "d03060522401468c01012a2400150f0000"
        "d066352b4675004f7601d04f7c00472947").ljust(len(old_click), b"\x02")
    return [
        (2212941, old_pool, new_pool),
        (2217483, b"\x02", b"\x03"),  # max_stack: 2 -> 3，locals/scope 不变。
        (2217488, old_click, new_click),
        # bindAccountInfo：本地按钮不再依赖在线充值 URL 的有效性。
        (2216674, bytes.fromhex("d05d3e663e66715d726672667324005602463b02"),
         b"\x26" + b"\x02"*19),
    ]
