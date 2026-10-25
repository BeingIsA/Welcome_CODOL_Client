"""本地大厅频道初始化与展示状态适配；保留原生显示检查。"""

from __future__ import annotations

_REPLACEMENTS = (
    # 复用两个相邻的纯调试局部变量名槽位，常量池总长度及索引不变。
    # 第一项作为大厅对象名称；第二项仅扩展调试名称以保持等长。
    (4929052, b'\x1drejoinGlobalChatRetryInterval\x18afterBrokenRetryInterval',
     b'\x0f' + '大厅聊天室'.encode('utf-8')
     + b'\x26afterBrokenRetryInterval_OriginalLocal'),
    # getChannelNameByID 的 GLOBAL 分支直接返回名称，供 addChatChannel 写入 name。
    # 其他频道分支和原有频道创建、去重、显示路径不变。
    (4951165, bytes.fromhex('5d9901669901669c02'),
     bytes.fromhex('2c1a48020202020202')),
    # 初始化末尾：保留原绑定，在依赖注册完成后创建本地大厅频道。
    (4944445, bytes.fromhex('f078d05d1266126660d0666146620229f07947'),
     bytes.fromhex('d060126660d066614f6202d024004f8a010147')),
    # 大厅展示就绪来自 CHAT_CHANNEL_OBJ(0)，不修改旧服务加入状态。
    (1532433, bytes.fromhex('ef01c701009002f09202d05d255d266626669c01462501685a'),
     bytes.fromhex('d05d2560266680022400560146250276685a02020202020202')),
    # 本地频道就绪后刷新真实频道标签，不读取旧服务频道编号。
    (1532637, bytes.fromhex('d0661d5d255d26662666a50146250146a6010129'),
     bytes.fromhex('d0661dd066144fdd010102020202020202020202')),
    # 沿原频道刷新路径读取首个大厅对象的 name，不替换文案常量。
    (2168416, bytes.fromhex('ef014d008301ef015c018501ef015d028601ef015e038b01ef015f048c01ef0160058d01f08501'),
     bytes.fromhex('d0664011200000d1664b12190000d12400663e664c110e0000d0d12400663e2ca201663e4f5e01')),
)


def replacements() -> list[tuple[int, bytes, bytes]]:
    """返回局部等长替换，不伪造加入成功或强制启用控件。"""
    return list(_REPLACEMENTS)
