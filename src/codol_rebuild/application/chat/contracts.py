"""普通聊天端口：不依赖具体传输或游戏对象。"""
from typing import Protocol


class MessageSender(Protocol):
    """向当前连接非阻塞交接消息，不保留离线待发送任务。"""

    def send_message(self, message: dict) -> None:
        """交接一次发送，不向业务返回成功或失败确认。"""
        ...


class MessageInbox(Protocol):
    """由唯一实时分派器填充的聊天收件端口。"""

    def poll_event(self) -> dict | None:
        """消费一条服务端展示消息。"""
        ...


class ChatPort(Protocol):
    """原生适配提交用户命令及获取可展示的普通聊天。"""

    def begin(self, text: str, scope: str, room_id: str | None) -> None:
        """发送一次；房间范围必须提供当前已确认的房间。"""
        ...

    def poll_message(self, room_id: str | None) -> dict | None:
        """房间消息只交给仍在对应房间的客户端。"""
        ...
