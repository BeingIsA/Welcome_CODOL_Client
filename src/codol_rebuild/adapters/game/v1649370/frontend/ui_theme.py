"""固定版本离线大厅主题；只提供主电影的等长方法替换规则。"""
from __future__ import annotations

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
