"""联机用例编排：后台串行请求、身份保存和不确定写入恢复。"""
import threading
import time
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
        # 最近已知房间修订号；None 表示未知，冲突时经详情自愈。
        self._revision = None
        # 设置同步与主流程互不阻塞，但共用单工作线程保证串行。
        self._settings_pending = None
        self._list_pending = None
        self._list_cancel = threading.Event()
        self._list_request = None
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
              password: str | None = None, settings: dict | None = None) -> bool:
        """提交单个用例，重复点击不会重复发送请求。

        设置同步与主流程（登录/创建/退出）互不阻塞：原生改设置在本地
        立即生效，这里只做异步补交，失败不影响房间状态。
        """
        if self._closed:
            return False
        if operation == 'settings':
            if self._settings_pending is not None:
                return False
            self._settings_pending = self._executor.submit(
                self._execute_settings, username, settings)
            return True
        if self._pending is not None:
            return False
        if operation not in ('login', 'create', 'leave'):
            raise ValueError('未知联机用例')
        self._pending = self._executor.submit(self._execute, operation, username,
                                              max_players, password, settings)
        return True

    def poll(self) -> Completion | None:
        """只在后台已完成时消费结果，不等待网络；主流程优先于设置同步。"""
        if self._pending is not None:
            if self._pending.done():
                pending, self._pending = self._pending, None
                return pending.result()
            return None
        if self._settings_pending is not None and self._settings_pending.done():
            pending, self._settings_pending = self._settings_pending, None
            return pending.result()
        return None

    def _save(self, record):
        # 即使磁盘写入失败，仍保留远端已生效的内存事实，供退出补偿使用。
        self._record = record
        self._store.save(self._server, record)

    def begin_list(self, username: str) -> bool:
        """只保留最新刷新意图；与写用例分开消费，共用串行工作线程。"""
        if self._closed:
            return False
        self.cancel_list()
        self._list_request = username
        self._start_list()
        return True

    def _start_list(self):
        if self._list_pending is None and self._list_request is not None:
            username, self._list_request = self._list_request, None
            self._list_cancel = threading.Event()
            self._list_pending = self._executor.submit(
                self._execute_list, username, self._list_cancel)

    def cancel_list(self) -> None:
        """使在途列表失效；不取消建房、离房、设置或登录请求。"""
        self._list_cancel.set()
        self._list_request = None

    def poll_list(self) -> Completion | None:
        """旧批次完成后才启动最新请求，避免连续刷新堆积后台任务。"""
        if self._list_pending is None or not self._list_pending.done():
            return None
        result = self._list_pending.result()
        cancelled = self._list_cancel.is_set()
        self._list_pending = None
        self._start_list()
        if cancelled:
            return None if self._list_pending is not None else Completion('list_cancelled')
        return result

    def _execute_list(self, username, cancelled):
        """收齐分页再发布；拒绝循环游标和过大列表，不把截断结果当作完整列表。"""
        try:
            if cancelled.is_set():
                return Completion('list')
            self._login(username)
            cursor, seen, rooms = None, set(), {}
            deadline = time.monotonic() + 20
            for _ in range(100):
                if cancelled.is_set():
                    return Completion('list')
                if time.monotonic() > deadline:
                    raise MultiplayerError('LIST_TIMEOUT', '房间列表查询超时，请刷新重试')
                page = self._gateway.list_rooms(self._record.player_id, cursor)
                for room in page['rooms']:
                    rooms[room['roomId']] = room
                if len(rooms) > 1000:
                    raise MultiplayerError('LIST_TOO_LARGE', '房间列表超过当前客户端容量')
                cursor = page['nextCursor']
                if cursor is None:
                    return Completion('list', data={'rooms': list(rooms.values())})
                if cursor in seen:
                    break
                seen.add(cursor)
            raise MultiplayerError('INVALID_RESPONSE', '房间分页游标没有前进')
        except MultiplayerError as error:
            return Completion('list', error=error)
        except (OSError, ValueError) as error:
            return Completion('list', error=MultiplayerError('LOCAL_IDENTITY_ERROR', str(error)))

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
        self._revision = None
        return {'left': True}

    def _execute(self, operation, username, max_players, password, settings=None):
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
                    data = self._gateway.create(self._record.player_id, username,
                                                max_players, password, settings)
                except MultiplayerError as error:
                    if not error.uncertain:
                        self._save(replace(self._record, uncertain_create=False))
                    raise
                self._save(replace(self._record, room_id=data['roomId'], uncertain_create=False))
                self._revision = data.get('revision')
            return Completion(operation, data=data)
        except MultiplayerError as error:
            return Completion(operation, error=error)
        except (OSError, ValueError) as error:
            return Completion(operation, error=MultiplayerError('LOCAL_IDENTITY_ERROR', str(error)))

    def _execute_settings(self, username, settings):
        """异步补交房内设置变更；冲突时拉取详情重试一次。"""
        try:
            self._login(username)
            if settings is None or not isinstance(settings, dict) or not settings:
                raise MultiplayerError('INVALID_SETTINGS', '设置数据无效')
            room_id = self._record.room_id if self._record else None
            if not room_id:
                raise MultiplayerError('NO_ROOM', '当前不在房间内，设置未同步')
            expected = self._revision if isinstance(self._revision, int) else 0
            try:
                data = self._gateway.update_settings(self._record.player_id, room_id,
                                                     settings, expected)
            except MultiplayerError as error:
                if error.code != 'REVISION_CONFLICT':
                    raise
                detail = self._gateway.room_detail(self._record.player_id, room_id)
                data = self._gateway.update_settings(
                    self._record.player_id, room_id, settings, detail['revision'])
            self._revision = data.get('revision')
            return Completion('settings', data={'revision': self._revision})
        except MultiplayerError as error:
            return Completion('settings', error=error)
        except (OSError, ValueError) as error:
            return Completion('settings', error=MultiplayerError('LOCAL_IDENTITY_ERROR', str(error)))

    def close(self) -> None:
        """进程结束后等待有限网络请求收尾；离房失败保留磁盘恢复标记。"""
        if self._closed:
            return
        self._closed = True
        self.cancel_list()
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
