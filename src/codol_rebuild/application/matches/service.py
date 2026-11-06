"""交付服务端已准备的本局连接资料。"""
from copy import deepcopy
from collections import deque
from concurrent.futures import ThreadPoolExecutor
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
    def __init__(self, current_room, inbox=None, gateway=None):
        self._inbox = inbox
        self._current_room = current_room
        self._player_id = None
        self._pending = None
        self._observed_room = None
        self._consumed_starts = set()
        self._available_start = None
        self._gateway = gateway
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="match-commands")
        self._command_results = deque()
        self._countdowns = deque()
        self._allocation_request = None
        self._failed_start = None
        self._prepared_start = None

    def connected(self, player_id: str):
        """由既有登录完成路径传入身份，不创建新身份或重复登录。"""
        self._player_id = player_id

    def begin_start(self, room_id):
        """接管原生房主 DS 申请；请求开局后等待同一局的准备结果。"""
        if (not self._player_id or room_id != self._current_room() or self._gateway is None
                or self._allocation_request is not None or self._pending is not None):
            return False
        self._allocation_request = (room_id, self._executor.submit(
            self._gateway.start_match, self._player_id, room_id))
        return True

    def begin(self, operation: str, room_id: str, allocation_id=None, security_id=None, start_id=None) -> bool:
        """交付已确认的连接及后续阶段结果；开局请求由 begin_start 单独承担。"""
        if not self._player_id or room_id != self._current_room() or self._pending is not None:
            return False
        if operation not in ('member_connect', 'book', 'start'):
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
        elif operation == 'member_connect':
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
        if self._allocation_request is not None:
            requested_room, future = self._allocation_request
            if requested_room != self._current_room():
                self._allocation_request = None
                return MatchCompletion('allocate', requested_room,
                    error=OnlineError('ROOM_CHANGED', '开局期间房间已改变'))
            if future.done():
                try:
                    response = future.result()
                    requested_start_id = response['start']['startId']
                except OnlineError as error:
                    self._allocation_request = None
                    return MatchCompletion('allocate', requested_room, error=error)
                prepared = self._prepared_start
                if prepared is not None and prepared['startId'] == requested_start_id:
                    self._allocation_request = None
                    return MatchCompletion('allocate', requested_room,
                        data=dict(deepcopy(prepared['connection']), startId=prepared['startId']))
                failed = self._failed_start
                if failed is not None and failed['startId'] == requested_start_id and failed['status'] == 'failed':
                    self._allocation_request = None
                    return MatchCompletion('allocate', requested_room,
                        error=OnlineError('START_FAILED', failed.get('error') or '开局失败'))
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
                or start.get('status') not in ('ready', 'failed')):
            return
        prepared_start = self._prepared_start
        # 房主可能已确认就绪通知；其后的失败仍须撤销尚未交付的成功。
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
        self._failed_start = accepted_start if accepted_start['status'] == 'failed' else None
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
            self._countdowns.clear()
            self._failed_start = None
        if self._inbox is not None:
            message = self._inbox.poll_event()
            while message is not None:
                if message.get('type') == 'room_countdown_updated':
                    payload = message.get('payload', {})
                    if payload.get('roomId') == room_id:
                        self._countdowns.append(deepcopy(payload))
                if message.get('type') == 'room_start_updated':
                    start = message.get('payload', {})
                    if isinstance(start, dict) and start.get('roomId') == room_id:
                        self._accept_start(room_id, start)
                message = self._inbox.poll_event()
        return self._available_start

    def acknowledge_start(self, room_id, start_id):
        """原生已接收或显示失败后才消费；重复推送不重复启动。"""
        self._consumed_starts.add((room_id, start_id))
        if (self._available_start is not None
                and (self._available_start['roomId'], self._available_start['startId']) == (room_id, start_id)):
            self._available_start = None


    def broadcast_countdown(self, room_id, seconds):
        """原生房主命令排队发送；单线程保持开始、取消及开局请求顺序。"""
        if not self._player_id or room_id != self._current_room() or self._gateway is None:
            return False
        self._command_results.append((room_id, self._executor.submit(
            self._gateway.broadcast_countdown, self._player_id, room_id, seconds)))
        return True

    def poll_countdown(self):
        """只返回当前房间的广播，原生接收由适配层执行。"""
        while self._countdowns:
            payload = self._countdowns.popleft()
            if payload['roomId'] == self._current_room():
                return payload
        return None

    def poll_command_error(self):
        """消费倒计时请求结果，不重试或伪造广播成功。"""
        while self._command_results and self._command_results[0][1].done():
            room_id, future = self._command_results.popleft()
            try:
                future.result()
            except OnlineError as error:
                if room_id == self._current_room():
                    return error
        return None

    def close(self):
        """释放对局命令执行器。"""
        self._executor.shutdown(wait=False, cancel_futures=True)
