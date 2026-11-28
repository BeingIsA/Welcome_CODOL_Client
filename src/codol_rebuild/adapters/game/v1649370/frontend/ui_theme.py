"""终版大厅的主题选择、主题提示与根时间轴署名。"""
from __future__ import annotations

import struct

from .. import movie_identity


LOBBY_WATERMARK = 'Welcome CODOL 完全免费，严禁用于商业用途'


def _movie_tag(tag_type: int, payload: bytes) -> bytes:
    """构造一个 Scaleform 主时间轴标签。"""
    length = len(payload)
    header = struct.pack('<H', tag_type << 6 | min(length, 63))
    return header + (struct.pack('<I', length) if length >= 63 else b'') + payload


def _watermark_tags() -> bytes:
    """在 1920×1080 原版画布上定义单行只读文字，留出右下截图标记的位置。"""
    text_identifier = 6194
    button_identifier = 6195
    # 文本框从画布左上角开始，RECT 坐标使用 twip。
    coordinates = (0, 642 * 20, 0, 32 * 20)
    bit_count = max(coordinates).bit_length() + 1
    rectangle_bits = f'{bit_count:05b}' + ''.join(
        f'{coordinate:0{bit_count}b}' for coordinate in coordinates)
    rectangle_bits += '0' * (-len(rectangle_bits) % 8)
    bounds = int(rectangle_bits, 2).to_bytes(len(rectangle_bits) // 8, 'big')
    # 半透明白字，布局值 0 表示左对齐。
    text_definition = (struct.pack('<H', text_identifier) + bounds
                       + b'\x8c\xb1$font_3\0' + struct.pack('<H', 22 * 20)
                       + bytes((255, 255, 255, 190))
                       + struct.pack('<BHHHh', 0, 0, 0, 0, 0)
                       + b'\0' + LOBBY_WATERMARK.encode('utf-8') + b'\0')
    # 只定义可见状态，不定义命中区域和动作，避免文字拦住后面的原版按钮。
    text_record = struct.pack('<BHH', 7, text_identifier, 1) + b'\x00\x00'
    button_definition = (struct.pack('<HBH', button_identifier, 0, 0)
                         + text_record + b'\x00')
    placement = struct.pack('<BHH', 0x06, 1, button_identifier) + b'\x00'
    return (_movie_tag(37, text_definition) + _movie_tag(34, button_definition)
            + _movie_tag(26, placement))


def watermark_replacements() -> list:
    """只在主电影首帧放置一次；页面容器位于文字下面，截图仍可临时追加子项。"""
    frame_tags = _watermark_tags()
    original_size = movie_identity.RESOURCE_SIZE
    # BaseMain.onLoad 将唯一的页面容器放到索引 0。用原 debugline 的两个字节
    # 容纳 pushbyte 0；调用前的栈深为 3，原方法声明的 max_stack 也是 3。
    return [
        (4, struct.pack('<I', original_size), struct.pack('<I', original_size + len(frame_tags))),
        (767044 + 123, bytes.fromhex('46380129f06f'), bytes.fromhex('2400462a0229')),
        (original_size - 4, movie_identity.FINAL_FRAME, frame_tags + movie_identity.FINAL_FRAME),
    ]

# 唯一主题选择处：只维护原生参数求值的八个字节，不复制整个方法。
THEME_VALUE_OFFSET = 4906697 + 13
THEME_VALUE_BEFORE = bytes.fromhex('5d0f2c19460f0173')
THEME_VALUE_AFTER = bytes.fromhex('2405020202020202')

# LobbyTeamMember.showMiniClientTag：只将 visible 的输入改为 false。
# 等长替换 getlocal1 为 pushfalse，保留成员 DLC 数据及所有准入判断。
MINI_TAG_OFFSET = 6649182
MINI_TAG_BEFORE = bytes.fromhex('f10bf067d030ef01540067f069d0661ad1613bf06a47')
MINI_TAG_AFTER = bytes.fromhex('f10bf067d030ef01540067f069d0661a27613bf06a47')

# checkDLCMembers：等长移除独立的 showMsg 表达式（含参数及返回值 pop）。
# 保留短路条件的全部跳转落点、提醒标志和成员计数，避免改变分支栈状态。
MINI_WARNING_OFFSET = 5101244 + 166
MINI_WARNING_BEFORE = bytes.fromhex('5dfe025dc80266c80266d9035dff0266ff0266800346fe020229')
MINI_WARNING_AFTER = b'\x02' * len(MINI_WARNING_BEFORE)


def replacements() -> list:
    """在主题控制器初始化时选择原版 SpaceStation（索引5）。

    仅替换 getDvarInt("ui_theme") 的求值：pushbyte 5 后填充 nop。
    保留原局部变量、主题范围检查和资源完整性断言；不改全局 Dvar。
    """
    return [(THEME_VALUE_OFFSET, THEME_VALUE_BEFORE, THEME_VALUE_AFTER),
            (MINI_TAG_OFFSET, MINI_TAG_BEFORE, MINI_TAG_AFTER),
            (MINI_WARNING_OFFSET, MINI_WARNING_BEFORE, MINI_WARNING_AFTER)]
