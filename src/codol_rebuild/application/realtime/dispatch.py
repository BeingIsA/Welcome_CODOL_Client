"""应用层事件分派组件：唯一读取传输队列，按显式注册关系交接消息。"""
from collections import deque


class EventInbox:
    """业务收件队列；房间等待期间不影响其他业务取消息。"""

    def __init__(self):
        self._messages = deque()

    def receive(self, message: dict) -> None:
        """接收分派器交接的一条事件。"""
        self._messages.append(message)

    def poll_event(self) -> dict | None:
        """按原始顺序消费本业务的一条事件。"""
        return self._messages.popleft() if self._messages else None


class RoutedConnection:
    """兼容现有房间连接接口的过渡包装。

    生命周期操作委托原连接，收消息只读房间收件队列；不拥有连接管理策略。
    房间用例的连接生命周期与事件消费接口分离后，可移除这层包装。
    """

    def __init__(self, connection, inbox: EventInbox):
        self._connection = connection
        self._inbox = inbox

    def start(self, player_id: str, fingerprint: str) -> None:
        """委托既有连接启动机制。"""
        self._connection.start(player_id, fingerprint)

    def close(self) -> None:
        """委托既有连接关闭机制。"""
        self._connection.close()

    def status(self):
        """返回既有连接状态。"""
        return self._connection.status()

    def poll_event(self) -> dict | None:
        """不直接访问传输接收队列。"""
        return self._inbox.poll_event()


class RealtimeDispatcher:
    """由游戏调度点调用；不执行原生调用或业务规则。"""

    def __init__(self, connection):
        self._connection = connection
        self._receivers = {}

    def register(self, event_types: tuple[str, ...], inbox: EventInbox) -> None:
        """为每种事件登记唯一业务收件人，避免重复分派。"""
        for event_type in event_types:
            if event_type in self._receivers:
                raise ValueError('实时事件已注册：' + event_type)
            self._receivers[event_type] = inbox

    def poll(self) -> None:
        """每次分派一条消息，不受房间用例的等待状态约束。"""
        message = self._connection.poll_event()
        if message is not None:
            receiver = self._receivers.get(message['type'])
            if receiver is not None:
                receiver.receive(message)
