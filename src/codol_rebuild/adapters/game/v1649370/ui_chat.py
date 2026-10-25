"""大厅聊天界面局部解锁：只调整入口与提示，不伪造频道状态或接管发送。"""

from __future__ import annotations


# 仅覆盖必要指令；原电影身份、原字节及重叠检查由 movie_identity 执行。
_REPLACEMENTS = (
    # enableData：初始化时启用小喇叭入口。
    (1530974, bytes.fromhex('665a'),
     bytes.fromhex('2926')),
    # enableData：初始化时启用大喇叭入口；保留前面的状态查询。
    (1531016, bytes.fromhex('615bf09b01'),
     bytes.fromhex('2926615b02')),
    # onGlobalChatChannelStateChanged：在短路分支汇合处隐藏加入提示，保持调用与栈平衡。
    (1532540, bytes.fromhex('46a1010129f09802'),
     bytes.fromhex('292746a101012902')),
    # onGlobalChatChannelStateChanged：频道未加入时不再自动关闭聊天输入框。
    (1532549, bytes.fromhex('665a'),
     bytes.fromhex('2926')),
    # onGlobalChatChannelStateChanged：频道状态刷新后继续启用小喇叭入口。
    (1532590, bytes.fromhex('665a'),
     bytes.fromhex('2926')),
    # onBigSpeakerChannelStateChanged：频道状态刷新后继续启用大喇叭入口。
    (1532759, bytes.fromhex('615bf0b002'),
     bytes.fromhex('2926615b02')),
    # refreshHistoryList：跳过未加入提示，沿原有空历史处理刷新列表。
    (1533917, bytes.fromhex('665a'),
     bytes.fromhex('2926')),
    # checkCanChangeMainChatPanel：允许原有展开按钮和快捷键打开大厅聊天框。
    (1534781, bytes.fromhex('665a'),
     bytes.fromhex('2926')),
)


def replacements() -> list[tuple[int, bytes, bytes]]:
    """返回局部 UI 替换；频道状态、发送、库存和冷却逻辑保持原样。"""
    return list(_REPLACEMENTS)
