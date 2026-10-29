"""玩家实时通道：后台接收、协议保活与有上限退避重连，不处理游戏界面事件。"""
from codol_rebuild.application.errors import OnlineError
import logging
import json
import queue
import threading
from urllib.parse import urlencode

from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus
from websockets.sync.client import connect

from codol_rebuild.application.realtime.contracts import RealtimeStatus
from codol_rebuild.adapters.transport.http import normalize_server, validate_room_snapshot

LOGGER = logging.getLogger(__name__)
# 房间协议人数上限 255；每人两份 32764 字节档案及展示数据经 Base64 后约 86 KiB。
# 32 MiB 覆盖完整成员快照，同时保留有限的单消息上限。
MAX_MESSAGE_BYTES = 32 * 1024 * 1024


class WebSocketConnection:
    """由联机会话串行调用 start/close；后台线程只管理网络连接。"""
    def __init__(self, server: str, timeout: float = 5.0):
        self._server = normalize_server(server)
        self._timeout = timeout
        self._stop = threading.Event()
        self._thread = None
        self._player_id = None
        self._machine_code = None
        self._status = RealtimeStatus.CLOSED
        self._events = queue.Queue(maxsize=256)
        self._send_lock = threading.Lock()
        self._outgoing = None

    def send_message(self, message: dict) -> None:
        """仅交给当前连接的发送队列；网络线程发送一次，断线不补发。"""
        encoded = json.dumps(message, ensure_ascii=False)
        with self._send_lock:
            if self._outgoing is None or self._stop.is_set():
                return
            try:
                self._outgoing.put_nowait(encoded)
            except queue.Full:
                pass

    def _activate_sender(self):
        """每次握手创建独立队列，重连不继承旧连接的未发送内容。"""
        with self._send_lock:
            self._outgoing = queue.Queue(maxsize=256)
            return self._outgoing

    def poll_event(self) -> dict | None:
        """非阻塞取出一条下行消息；内容语义由应用层解释。"""
        try:
            return self._events.get_nowait()
        except queue.Empty:
            return None

    def status(self) -> RealtimeStatus:
        """返回连接状态；身份被拒为 INVALIDATED，主动关闭后为 CLOSED。"""
        return self._status

    def _connect(self, player_id, machine_code):
        url = ('wss' if self._server.startswith('https:') else 'ws') + self._server[self._server.index(':'):]
        return connect(url + '/ws?' + urlencode({'playerId': player_id, 'machine_code': machine_code}),
                       open_timeout=self._timeout, close_timeout=1,
                       ping_interval=10, ping_timeout=10, max_size=MAX_MESSAGE_BYTES,
                       max_queue=16, proxy=None)

    def start(self, player_id: str, fingerprint: str) -> None:
        """首次握手携带登录机器码；玩家与机器码不变时复用连接及重连任务。"""
        if self._stop.is_set():
            raise OnlineError('WS_CLOSED', '实时通道已关闭')
        if self._thread is not None:
            if self._player_id != player_id or self._machine_code != fingerprint:
                raise OnlineError('WS_IDENTITY_CHANGED', '实时通道身份不一致')
            return
        try:
            connection = self._connect(player_id, fingerprint)
        except (OSError, TimeoutError, InvalidHandshake) as error:
            raise OnlineError('WS_CONNECT_FAILED', '实时连接失败，请检查服务端后重试') from error
        self._player_id = player_id
        self._machine_code = fingerprint
        self._status = RealtimeStatus.CONNECTED
        outgoing = self._activate_sender()
        self._thread = threading.Thread(target=self._run, args=(connection, outgoing),
                                        name='codol-websocket', daemon=True)
        self._thread.start()

    def _invalidate(self):
        """身份被服务端拒绝：清理线程痕迹使重启能真正重建，不触发重新登录。"""
        LOGGER.warning('实时连接身份被拒绝，停止重连')
        self._thread = None
        self._player_id = None
        self._machine_code = None
        self._status = RealtimeStatus.INVALIDATED

    def _run(self, connection, outgoing):
        delay = 1
        while not self._stop.is_set():
            try:
                # 只解码并交接消息，不在网络线程访问游戏对象。
                while not self._stop.is_set():
                    for message_index in range(outgoing.qsize()):
                        connection.send(outgoing.get_nowait())
                    try:
                        message = json.loads(connection.recv(timeout=.25))
                    except TimeoutError:
                        continue
                    delay = 1
                    if message.get('type') == 'room_members_updated':
                        try:
                            payload = message['payload']
                            validate_room_snapshot(payload, payload['roomId'])
                            if any('profile' not in member for member in payload['members']):
                                raise ValueError('成员推送缺少档案字段')
                        except (KeyError, TypeError, ValueError):
                            LOGGER.warning('拒绝格式无效的成员快照')
                            continue
                    self._enqueue_event(message)
            except ConnectionClosed as error:
                if error.rcvd is not None and error.rcvd.code == 4401:
                    self._invalidate()
                    return
                if not self._stop.is_set():
                    self._status = RealtimeStatus.RECONNECTING
            finally:
                with self._send_lock:
                    if self._outgoing is outgoing:
                        self._outgoing = None
                connection.close()
            while not self._stop.wait(delay):
                try:
                    connection = self._connect(self._player_id, self._machine_code)
                    outgoing = self._activate_sender()
                    self._status = RealtimeStatus.CONNECTED
                    self._enqueue_event({'type': 'realtime_reconnected', 'payload': {}})
                    break
                except InvalidStatus as error:
                    if error.response.status_code in (401, 403):
                        self._invalidate()
                        return
                except (OSError, TimeoutError, InvalidHandshake):
                    pass
                delay = min(delay * 2, 30)
            else:
                return
        # close 可能发生在握手期间；成功握手后也必须释放连接。
        connection.close()

    def _enqueue_event(self, message: dict) -> None:
        """有界交接消息及连接恢复事实；业务恢复由应用层决定。"""
        while not self._stop.is_set():
            try:
                self._events.put(message, timeout=.25)
                return
            except queue.Full:
                continue

    def close(self) -> None:
        """中止退避等待，等待有限超时的握手或接收结束并关闭连接。"""
        self._stop.set()
        self._status = RealtimeStatus.CLOSED
        if self._thread is not None:
            self._thread.join()
