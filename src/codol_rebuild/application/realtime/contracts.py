"""实时通道公共契约；连接生命周期与业务收件接口分离。"""
from enum import Enum
from typing import Protocol


class EventSource(Protocol):
    """已经解码的事件来源，不暴露连接控制。"""
    def poll_event(self) -> dict | None:
        """非阻塞消费一条事件。"""
        ...


class RealtimeStatus(Enum):
    """实时连接状态；供应用层感知断线与身份失效。"""
    CONNECTED = 'connected'
    RECONNECTING = 'reconnecting'
    INVALIDATED = 'invalidated'
    CLOSED = 'closed'


class RealtimeConnection(Protocol):
    """实时连接生命周期端口；实现负责接收、保活和断线重连。"""
    def start(self, player_id: str, fingerprint: str) -> None:
        """建立首次连接，失败抛出联机错误；同一身份重复调用不重建连接。

        身份失效（INVALIDATED）后线程痕迹已清理，再次调用会真正重建连接。
        """
        ...

    def close(self) -> None:
        """停止重连并释放连接及后台线程。"""
        ...

    def status(self) -> RealtimeStatus:
        """返回当前连接状态；未启动或已关闭为 CLOSED。"""
        ...


