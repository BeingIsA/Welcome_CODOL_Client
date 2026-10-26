"""联机会话编排：身份获取、WS 首次握手、房间恢复和退出顺序。"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from codol_rebuild.application.errors import OnlineError
from codol_rebuild.application.session.contracts import (
    IdentityRecord, IdentityStore, LoginGateway, LoginCompletion, RoomLifecycle,
)
from codol_rebuild.application.realtime.contracts import RealtimeConnection


class OnlineSession:
    """会话拥有登录任务；房间与传输分别通过公开生命周期端口协作。"""

    def __init__(self, gateway: LoginGateway, store: IdentityStore, server: str,
                 fingerprint: str, rooms: RoomLifecycle,
                 realtime: RealtimeConnection | None = None):
        self._gateway = gateway
        self._store = store
        self._server = server
        self._fingerprint = fingerprint
        self._rooms = rooms
        self._realtime = realtime
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='codol-login')
        self._player_id = None
        self._pending = None
        self._closed = False

    def begin_login(self, username: str) -> bool:
        """只提交一次登录；失败不自动重试，调用方可显式再次发起。"""
        if self._closed or self._pending is not None:
            return False
        self._pending = self._executor.submit(self._login, username)
        return True

    def poll_login(self) -> LoginCompletion | None:
        """只返回已完成的登录结果，不阻塞原生主线程。"""
        if self._pending is None or not self._pending.done():
            return None
        completed = self._pending
        self._pending = None
        return completed.result()

    def _login(self, username):
        try:
            saved_identity = self._store.load(self._server)
            player_id = self._player_id or (saved_identity.player_id if saved_identity else None)
            data = self._gateway.login(self._fingerprint, username, player_id)
            # 沿用原流程：保存或握手失败后也不创建替代身份。
            self._player_id = data['playerId']
            identity = (replace(saved_identity, player_id=data['playerId']) if saved_identity
                        else IdentityRecord(data['playerId']))
            # 先交接确认身份，保证保存/握手失败后退出仍能清理既有房间。
            self._rooms.bind_identity(identity)
            self._store.save(self._server, identity)
            if self._realtime is not None:
                self._realtime.start(identity.player_id, self._fingerprint)
            self._rooms.restore()
            return LoginCompletion(data={'playerId': identity.player_id, 'displayName': data['displayName']})
        except OnlineError as error:
            return LoginCompletion(error=error)
        except (OSError, ValueError) as error:
            return LoginCompletion(error=OnlineError('LOCAL_IDENTITY_ERROR', str(error)))

    def close(self) -> None:
        """等登录完成，再离房，最后关闭 WS；任何清理失败都释放线程。"""
        if self._closed:
            return
        self._closed = True
        try:
            if self._pending is not None:
                self._pending.result()
        finally:
            try:
                self._rooms.close()
            finally:
                try:
                    if self._realtime is not None:
                        self._realtime.close()
                finally:
                    self._executor.shutdown(wait=True)
