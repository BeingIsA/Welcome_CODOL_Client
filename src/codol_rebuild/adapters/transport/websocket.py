"""玩家实时通道：后台接收、协议保活与有上限退避重连，不处理游戏界面事件。"""
import logging
import threading
from urllib.parse import urlencode

from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus
from websockets.sync.client import connect

from codol_rebuild.application.multiplayer.contracts import MultiplayerError, RealtimeStatus
from codol_rebuild.adapters.transport.http import normalize_server

LOGGER = logging.getLogger(__name__)


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
        self._invalidated_handler = None

    def set_invalidation_handler(self, handler) -> None:
        """注册身份失效回调；由会话在启动前调用，实现须线程安全。"""
        self._invalidated_handler = handler

    def status(self) -> RealtimeStatus:
        """返回连接状态；身份被拒为 INVALIDATED，主动关闭后为 CLOSED。"""
        return self._status

    def _connect(self, player_id, machine_code):
        url = ('wss' if self._server.startswith('https:') else 'ws') + self._server[self._server.index(':'):]
        return connect(url + '/ws?' + urlencode({'playerId': player_id, 'machine_code': machine_code}),
                       open_timeout=self._timeout, close_timeout=1,
                       ping_interval=10, ping_timeout=10, max_size=1024 * 1024,
                       max_queue=16, proxy=None)

    def start(self, player_id: str, fingerprint: str) -> None:
        """首次握手携带登录机器码；玩家与机器码不变时复用连接及重连任务。"""
        if self._stop.is_set():
            raise MultiplayerError('WS_CLOSED', '实时通道已关闭')
        if self._thread is not None:
            if self._player_id != player_id or self._machine_code != fingerprint:
                raise MultiplayerError('WS_IDENTITY_CHANGED', '实时通道身份不一致')
            return
        try:
            connection = self._connect(player_id, fingerprint)
        except (OSError, TimeoutError, InvalidHandshake) as error:
            raise MultiplayerError('WS_CONNECT_FAILED', '实时连接失败，请检查服务端后重试') from error
        self._player_id = player_id
        self._machine_code = fingerprint
        self._status = RealtimeStatus.CONNECTED
        self._thread = threading.Thread(target=self._run, args=(connection,),
                                        name='codol-websocket', daemon=True)
        self._thread.start()

    def _invalidate(self):
        """身份被服务端拒绝：清理线程痕迹使重启能真正重建，并通知应用层。"""
        LOGGER.warning('实时连接身份被拒绝，停止重连')
        self._thread = None
        self._player_id = None
        self._machine_code = None
        self._status = RealtimeStatus.INVALIDATED
        if self._invalidated_handler is not None:
            try:
                self._invalidated_handler()
            except Exception:
                LOGGER.exception('实时身份失效回调执行失败')

    def _run(self, connection):
        delay = 1
        while not self._stop.is_set():
            try:
                # 持续消费消息，避免队列积压；事件到原生界面的映射不在本期范围内。
                while not self._stop.is_set():
                    try:
                        connection.recv(timeout=.25)
                    except TimeoutError:
                        continue
                    delay = 1
            except ConnectionClosed as error:
                if error.rcvd is not None and error.rcvd.code == 4401:
                    self._invalidate()
                    return
                if not self._stop.is_set():
                    self._status = RealtimeStatus.RECONNECTING
            finally:
                connection.close()
            while not self._stop.wait(delay):
                try:
                    connection = self._connect(self._player_id, self._machine_code)
                    self._status = RealtimeStatus.CONNECTED
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

    def close(self) -> None:
        """中止退避等待，等待有限超时的握手或接收结束并关闭连接。"""
        self._stop.set()
        self._status = RealtimeStatus.CLOSED
        if self._thread is not None:
            self._thread.join()
