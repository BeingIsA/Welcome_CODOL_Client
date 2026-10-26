"""大厅与房间聊天：WS 单次上行，服务端推送负责唯一展示。"""
from uuid import uuid4
from collections.abc import Callable
from codol_rebuild.application.chat.contracts import MessageSender, MessageInbox


class ChatSession:
    """沿用既有登录连接；不维护发送结果、历史、重试或房间副本。"""

    def __init__(self, sender: MessageSender, inbox: MessageInbox,
                 current_room: Callable[[], str | None]):
        self._sender = sender
        self._inbox = inbox
        self._current_room = current_room

    def begin(self, text: str, scope: str, room_id: str | None) -> None:
        """提交普通聊天；不生成本地回显，也不等待 chat_result。"""
        if scope not in ('lobby', 'room') or (scope == 'room' and (room_id is None or room_id != self._current_room())):
            return
        payload = {'scope': scope, 'text': text}
        if scope == 'room':
            payload['roomId'] = room_id
        self._sender.send_message({'type': 'chat_send', 'requestId': str(uuid4()),
                                   'payload': payload})

    def poll_message(self, room_id: str | None) -> dict | None:
        """依据消费时的成员身份丢弃旧房间消息，不随当前选中标签改归属。"""
        event = self._inbox.poll_event()
        if event is None:
            return None
        message = event['payload']
        if message['scope'] == 'lobby':
            return message
        if (message['scope'] == 'room' and room_id is not None
                and message.get('roomId') == room_id == self._current_room()):
            return message
        return None
