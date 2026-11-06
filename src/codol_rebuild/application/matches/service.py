"""交付服务端已准备的本局连接资料。"""
from copy import deepcopy
from dataclasses import dataclass
from codol_rebuild.application.errors import OnlineError


@dataclass(frozen=True)
class MatchCompletion:
    """一次原生阶段的确认结果，不等同于游戏连接成功。"""
    operation: str
    room_id: str
    data: dict | None = None
    error: OnlineError | None = None


class MatchSession:
    """保留本局已确认连接资料及原生阶段结果。"""
    def __init__(self, current_room, inbox=None):
        self._inbox = inbox
        self._current_room = current_room
        self._player_id = None
        self._pending = None
        self._observed_room = None
        self._consumed_starts = set()
        self._available_start = None
        self._waiting_start = None
        self._shown_waits = set()
        self._prepared_start = None

    def connected(self, player_id: str):
        """由既有登录完成路径传入身份，不创建新身份或重复登录。"""
        self._player_id = player_id

    def begin(self, operation: str, room_id: str, allocation_id=None, security_id=None, start_id=None) -> bool:
        """按原生阶段交付已确认资料；不重新分配、预订或发送开局请求。"""
        if not self._player_id or room_id != self._current_room() or self._pending is not None:
            return False
        if operation not in ('member_connect', 'allocate', 'book', 'start'):
            raise ValueError('未知对局请求')
        prepared_start = self._prepared_start
        if prepared_start is None or prepared_start['roomId'] != room_id:
            return False
        connection = prepared_start['connection']
        matches = (operation != 'member_connect' or start_id == prepared_start['startId'])
        if operation in ('book', 'start'):
            matches = allocation_id == connection['allocationId']
        if operation == 'book':
            matches = matches and security_id == connection.get('securityId')
        if not matches:
            completion = MatchCompletion(operation, room_id,
                error=OnlineError('START_CHANGED', '原生请求与已确认开局不匹配'))
        elif operation in ('member_connect', 'allocate'):
            completion = MatchCompletion(operation, room_id,
                data=dict(deepcopy(connection), startId=prepared_start['startId']))
        else:
            # 原生仍生成 mmstart/mmban；规则及准入已由服务端在通知前完成。
            confirmation = 'booked' if operation == 'book' else 'started'
            completion = MatchCompletion(operation, room_id,
                data={'allocationId': connection['allocationId'], confirmation: True})
        self._pending = (prepared_start['startId'], completion)
        return True

    def poll(self) -> MatchCompletion | None:
        """在既有原生轮询时机消费一次结果，拒绝离房或换局后的旧阶段。"""
        if self._pending is None:
            return None
        start_id, completion = self._pending
        self._pending = None
        if completion.room_id != self._current_room():
            return MatchCompletion(completion.operation, completion.room_id,
                error=OnlineError('ROOM_CHANGED', '对局请求期间房间已改变'))
        if self._prepared_start is None or self._prepared_start['startId'] != start_id:
            return MatchCompletion(completion.operation, completion.room_id,
                error=OnlineError('START_CHANGED', '对局请求期间开局已改变'))
        return completion

    def _accept_start(self, room_id, start):
        """只保留当前房间未消费的终态；原生执行点确认前保持可交付。"""
        if not isinstance(start, dict):
            return
        start_id = start.get('startId')
        if (not room_id or room_id != self._current_room()
                or not isinstance(start_id, str) or not start_id
                or start.get('status') not in ('starting', 'ready', 'failed')):
            return
        if start['status'] == 'starting':
            identity = (room_id, start_id)
            if (identity not in self._shown_waits and identity not in self._consumed_starts
                    and not (self._available_start is not None
                             and self._available_start['startId'] == start_id)):
                self._waiting_start = deepcopy(dict(start, roomId=room_id))
            return
        prepared_start = self._prepared_start
        # 房主在 StartMatch 前已确认通知；其后的失败仍须撤销尚未交付的成功。
        revokes_prepared_start = (start['status'] == 'failed' and prepared_start is not None
                                 and prepared_start['startId'] == start_id)
        if (room_id, start_id) in self._consumed_starts and not revokes_prepared_start:
            return
        accepted_start = deepcopy(dict(start, roomId=room_id))
        connection = accepted_start.get('connection')
        if accepted_start['status'] == 'ready' and (not isinstance(connection, dict)
                or not isinstance(connection.get('allocationId'), str) or not connection['allocationId']):
            return
        if prepared_start is not None and prepared_start['startId'] != start_id:
            self._consumed_starts.add((room_id, prepared_start['startId']))
            self._pending = None
        self._prepared_start = accepted_start if accepted_start['status'] == 'ready' else None
        self._available_start = accepted_start
        return True


    def poll_start(self):
        """仅消费首次广播的完整开局资料；离房时清理本局状态。"""
        room_id = self._current_room()
        if room_id != self._observed_room:
            self._observed_room = room_id
            self._available_start = None
            self._prepared_start = None
            self._pending = None
            self._consumed_starts.clear()
            self._shown_waits.clear()
            self._waiting_start = None
        if self._inbox is not None:
            message = self._inbox.poll_event()
            while message is not None:
                if message.get('type') == 'room_start_updated':
                    start = message.get('payload', {})
                    if isinstance(start, dict) and start.get('roomId') == room_id:
                        self._accept_start(room_id, start)
                message = self._inbox.poll_event()
        return self._waiting_start or self._available_start

    def acknowledge_wait(self, room_id, start_id):
        """等待提示已交给原生；不消费同局后续的就绪通知。"""
        self._shown_waits.add((room_id, start_id))
        if (self._waiting_start is not None
                and (self._waiting_start['roomId'], self._waiting_start['startId']) == (room_id, start_id)):
            self._waiting_start = None

    def acknowledge_start(self, room_id, start_id):
        """原生已接收或显示失败后才消费；重复推送不重复启动。"""
        self._consumed_starts.add((room_id, start_id))
        if (self._available_start is not None
                and (self._available_start['roomId'], self._available_start['startId']) == (room_id, start_id)):
            self._available_start = None
