"""应用层事件分派组件：唯一读取传输队列，按显式注册关系交接消息。"""
from collections import deque
from codol_rebuild.application.realtime.contracts import EventSource


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


class RealtimeDispatcher:
    """由游戏调度点调用；不执行原生调用或业务规则。"""

    def __init__(self, connection: EventSource):
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
