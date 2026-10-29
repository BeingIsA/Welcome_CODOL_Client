"""已确认身份/房间上的异步对局请求；完成结果由原生主线程消费。"""
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Protocol
from codol_rebuild.application.errors import OnlineError


class MatchGateway(Protocol):
    """对局通信端口；具体HTTP实现由启动层注入。"""
    def room_detail(self, player_id: str, room_id: str) -> dict:
        """读取当前开局结果，恢复丢失通知；不重复发起业务写入。"""
        ...

    def allocate_game(self, player_id: str, room_id: str) -> dict:
        """请求一个已完成地图初始化的实例。"""
        ...

    def book_game(self, player_id: str, room_id: str, allocation_id: str, security_id: str) -> dict:
        """把原生预订对应的分配和security ID交给服务端确认。"""
        ...

    def start_game(self, player_id: str, room_id: str, allocation_id: str, payload: dict) -> dict:
        """提交原生开局载荷，由服务端核对验收规则并确认实例就绪。"""
        ...


@dataclass(frozen=True)
class MatchCompletion:
    """一次后台请求的结果，不等同于游戏连接成功。"""
    operation: str
    room_id: str
    data: dict | None = None
    error: OnlineError | None = None


class MatchSession:
    """不自动重试；原生流程决定是否重新请求，服务端结果保持明确。"""
    def __init__(self, gateway: MatchGateway, current_room, inbox=None):
        self._inbox = inbox
        self._gateway = gateway
        self._current_room = current_room
        self._player_id = None
        self._pending = None
        self._start_query = None
        self._observed_room = None
        self._start_generation = 0
        self._reconcile_start = False
        self._consumed_starts = set()
        self._available_start = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='codol-match')

    def connected(self, player_id: str):
        """由既有登录完成路径传入身份，不创建新身份或重复登录。"""
        self._player_id = player_id

    def begin(self, operation: str, room_id: str, allocation_id=None, security_id=None, control_payload=None) -> bool:
        """只提交当前确认房间请求；存在在途结果时拒绝重复排队。"""
        if not self._player_id or room_id != self._current_room() or self._pending is not None:
            return False
        if operation == 'allocate':
            action = self._gateway.allocate_game
            arguments = (self._player_id, room_id)
        elif operation == 'book':
            action = self._gateway.book_game
            arguments = (self._player_id, room_id, allocation_id, security_id)
        elif operation == 'start':
            action = self._gateway.start_game
            arguments = (self._player_id, room_id, allocation_id, dict(control_payload))
        else:
            raise ValueError('未知对局请求')
        self._pending = (operation, room_id, self._executor.submit(action, *arguments))
        return True

    def poll(self) -> MatchCompletion | None:
        """消费一次结果；离房后的迟到结果不用于修改当前原生房间。"""
        if self._pending is None or not self._pending[2].done():
            return None
        operation, room_id, future = self._pending
        self._pending = None
        try:
            data = future.result()
        except OnlineError as error:
            return MatchCompletion(operation, room_id, error=error)
        if room_id != self._current_room():
            return MatchCompletion(operation, room_id, error=OnlineError('ROOM_CHANGED', '对局请求期间房间已改变'))
        return MatchCompletion(operation, room_id, data=data)

    def _accept_start(self, room_id, start):
        """只保留当前房间未消费的终态；原生执行点确认前保持可交付。"""
        if not isinstance(start, dict):
            return
        start_id = start.get('startId')
        if (room_id and room_id == self._current_room()
                and isinstance(start_id, str) and start_id
                and start.get('status') in ('ready', 'failed')
                and (room_id, start_id) not in self._consumed_starts):
            self._available_start = dict(start, roomId=room_id)

    def poll_start(self):
        """消费开局推送；仅入房及重连后查询一次，不定时请求服务端。"""
        room_id = self._current_room()
        if room_id != self._observed_room:
            self._observed_room = room_id
            self._start_generation += 1
            self._reconcile_start = bool(room_id)
            self._available_start = None
            self._consumed_starts.clear()
        if self._inbox is not None:
            message = self._inbox.poll_event()
            while message is not None:
                if message.get('type') == 'realtime_reconnected':
                    self._start_generation += 1
                    self._reconcile_start = bool(room_id)
                elif message.get('type') == 'room_start_updated':
                    start = message.get('payload', {})
                    if isinstance(start, dict) and start.get('roomId') == room_id:
                        self._start_generation += 1
                        self._accept_start(room_id, start)
                message = self._inbox.poll_event()
        if self._start_query is not None and self._start_query[2].done():
            queried_room, generation, future = self._start_query
            self._start_query = None
            try:
                snapshot = future.result()
                if queried_room == room_id and generation == self._start_generation:
                    self._accept_start(room_id, snapshot.get('start'))
            except OnlineError:
                logging.getLogger(__name__).warning('开局状态恢复查询失败，等待后续推送或重连', exc_info=True)
        if (self._reconcile_start and self._player_id and room_id
                and self._start_query is None and self._pending is None):
            self._reconcile_start = False
            self._start_query = (room_id, self._start_generation, self._executor.submit(
                self._gateway.room_detail, self._player_id, room_id))
        return self._available_start

    def acknowledge_start(self, room_id, start_id):
        """原生已接收或显示失败后才消费；重复推送不重复启动。"""
        self._consumed_starts.add((room_id, start_id))
        if (self._available_start is not None
                and (self._available_start['roomId'], self._available_start['startId']) == (room_id, start_id)):
            self._available_start = None

    def close(self):
        """等待有限超时的通信完成并释放线程，不执行原生操作。"""
        self._executor.shutdown(wait=True, cancel_futures=True)
