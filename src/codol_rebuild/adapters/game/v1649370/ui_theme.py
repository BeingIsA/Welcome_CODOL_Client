"""固定版本离线大厅主题；只提供主电影的等长方法替换规则。"""
from __future__ import annotations

METHOD_OFFSET = 4906697
ORIGINAL_METHOD = bytes.fromhex(
    'f103f02dd030ef0117002ff02f5d0f2c19460f0173d5f030d1d0660766100c290000'
    'f032d0d06607d166116812f0335d136613d0661246140066105d1566156616ab5d174a'
    '170046180229f03547')

# LobbyTeamMember.showMiniClientTag：只将 visible 的输入改为 false。
# 等长替换 getlocal1 为 pushfalse，保留成员 DLC 数据及所有准入判断。
MINI_TAG_OFFSET = 6649182
MINI_TAG_BEFORE = bytes.fromhex('f10bf067d030ef01540067f069d0661ad1613bf06a47')
MINI_TAG_AFTER = bytes.fromhex('f10bf067d030ef01540067f069d0661a27613bf06a47')


def replacements() -> list:
    """在主题控制器初始化时选择原版 SpaceStation（索引5）。

    仅替换 getDvarInt("ui_theme") 的求值：pushbyte 5 后填充 nop。
    保留原局部变量、主题范围检查和资源完整性断言；不改全局 Dvar。
    """
    replacement = (ORIGINAL_METHOD[:13] + b'\x24\x05' + b'\x02' * 6
                   + ORIGINAL_METHOD[21:])
    return [(METHOD_OFFSET, ORIGINAL_METHOD, replacement),
            (MINI_TAG_OFFSET, MINI_TAG_BEFORE, MINI_TAG_AFTER)]
