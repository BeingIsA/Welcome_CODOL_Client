"""联机用例编排：后台串行请求、身份保存和不确定写入恢复。"""
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from codol_rebuild.application.multiplayer.contracts import (
    Completion, Gateway, IdentityRecord, IdentityStore, MultiplayerError, RealtimeConnection,
)


class MultiplayerSession:
    """原生线程只提交与消费结果；身份、房间状态只在单个工作线程中改变。"""
    def __init__(self, gateway: Gateway, store: IdentityStore, server: str, fingerprint: str,
                 realtime: RealtimeConnection | None = None):
        self._gateway, self._store = gateway, store
        self._server, self._fingerprint = server, fingerprint
        self._record = None
        self._username = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='codol-http')
        self._pending = None
        self._closed = False
        self._realtime = realtime
        self._invalidated = threading.Event()
        if realtime is not None:
            realtime.set_invalidation_handler(self._on_realtime_invalidated)

    def _on_realtime_invalidated(self):
        """实时身份被服务端拒绝：作废内存登录态等待重新鉴权；磁盘身份文件不动。"""
        self._invalidated.set()
        self._username = None

    def invalidated(self) -> bool:
        """实时身份是否已被拒绝且尚未重新鉴权成功。"""
        return self._invalidated.is_set()

    def begin(self, operation: str, username: str, *, max_players: int = 4,
              password: str | None = None) -> bool:
        """提交单个用例，重复点击不会重复发送请求。"""
        if self._closed or self._pending is not None:
            return False
        if operation not in ('login', 'create', 'leave'):
            raise ValueError('未知联机用例')
        self._pending = self._executor.submit(self._execute, operation, username, max_players, password)
        return True

    def poll(self) -> Completion | None:
        """只在后台已完成时消费结果，不等待网络。"""
        if self._pending is None or not self._pending.done():
            return None
        pending, self._pending = self._pending, None
        return pending.result()

    def _save(self, record):
        # 即使磁盘写入失败，仍保留远端已生效的内存事实，供退出补偿使用。
        self._record = record
        self._store.save(self._server, record)

    def _login(self, username):
        if self._record is None:
            self._record = self._store.load(self._server)
        if self._username == username:
            return
        data = self._gateway.login(self._fingerprint, username,
                                   self._record.player_id if self._record else None)
        self._save(replace(self._record, player_id=data['playerId']) if self._record
                   else IdentityRecord(data['playerId']))
        if self._realtime is not None:
            self._realtime.start(self._record.player_id)
        # 重新鉴权成功（含实时连接重建）后，失效状态不再成立。
        self._invalidated.clear()
        self._username = username

    def _recover_create(self):
        if not self._record.uncertain_create:
            return
        cursor, seen = None, set()
        for _ in range(100):
            page = self._gateway.list_rooms(self._record.player_id, cursor)
            owned = [room for room in page['rooms'] if room['ownerPlayerId'] == self._record.player_id]
            if owned:
                self._save(replace(self._record, room_id=owned[0]['roomId'], uncertain_create=False))
                return
            cursor = page['nextCursor']
            if cursor is None:
                self._save(replace(self._record, uncertain_create=False))
                return
            if cursor in seen:
                break
            seen.add(cursor)
        raise MultiplayerError('RECOVERY_FAILED', '无法确认上次建房结果，请稍后重试')

    def _leave(self):
        self._recover_create()
        if self._record.room_id:
            try:
                self._gateway.leave(self._record.player_id, self._record.room_id)
            except MultiplayerError as error:
                if error.code not in ('ROOM_NOT_FOUND', 'ROOM_CLOSED'):
                    raise
            self._save(replace(self._record, room_id=None))
        return {'left': True}

    def _execute(self, operation, username, max_players, password):
        try:
            self._login(username)
            if operation == 'login':
                self._leave()  # 重启后清理上次未正常退出的房间。
                data = {'playerId': self._record.player_id, 'displayName': username}
            elif operation == 'leave':
                data = self._leave()
            else:
                if type(max_players) is not int or not 1 <= max_players <= 255:
                    raise MultiplayerError('INVALID_MAX_PLAYERS', '房间人数无效')
                self._leave()
                self._save(replace(self._record, uncertain_create=True))
                try:
                    data = self._gateway.create(self._record.player_id, username, max_players, password)
                except MultiplayerError as error:
                    if not error.uncertain:
                        self._save(replace(self._record, uncertain_create=False))
                    raise
                self._save(replace(self._record, room_id=data['roomId'], uncertain_create=False))
            return Completion(operation, data=data)
        except MultiplayerError as error:
            return Completion(operation, error=error)
        except (OSError, ValueError) as error:
            return Completion(operation, error=MultiplayerError('LOCAL_IDENTITY_ERROR', str(error)))

    def close(self) -> None:
        """进程结束后等待有限网络请求收尾；离房失败保留磁盘恢复标记。"""
        if self._closed:
            return
        self._closed = True
        def cleanup():
            if self._record is not None:
                self._leave()
        future = self._executor.submit(cleanup)
        try:
            future.result()
        finally:
            try:
                if self._realtime is not None:
                    self._realtime.close()
            finally:
                self._executor.shutdown(wait=True)
