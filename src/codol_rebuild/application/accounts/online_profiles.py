"""登录时读取账号双档案，按服务端修订号串行保存游戏线程采样的快照。"""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from threading import Lock
from typing import Protocol


class PlayerProfileGateway(Protocol):
    """本人原生双档案的通信端口，public 与 secure 同版本提交。"""

    def load_player_profile(self, player_id: str) -> tuple[int, dict[str, bytes]]:
        """读取修订号与双档案，尚未建档时返回零版本及空字典。"""
        ...

    def save_player_profile(self, player_id: str, expected_revision: int, profiles: dict[str, bytes]) -> int:
        """仅在修订号匹配时提交，返回服务端确认的修订号。"""
        ...


class OnlineProfiles:
    """一次登录的档案会话。一次写入在途时，仅保留最新的完整待写双档案。"""

    def __init__(self, gateway: PlayerProfileGateway):
        self._gateway = gateway
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='codol-profiles')
        self._lock = Lock()
        self._pending = None
        self._writing = False
        self._failure = None
        self._revision = 0
        self._confirmed = {}
        self.player_id = None
        self.display_name = None

    def load_for_login(self, player_id: str, display_name: str) -> None:
        """在登录工作线程完成首读，然后把已确认的身份、昵称和档案交给原生初始化。"""
        revision, profiles = self._gateway.load_player_profile(player_id)
        with self._lock:
            self._revision, self._confirmed = revision, dict(profiles)
            self.player_id, self.display_name = player_id, display_name

    def snapshot(self) -> dict:
        """返回已确认的修订号和档案副本；此前保存失败时交给调用方处理。"""
        with self._lock:
            if self._failure:
                raise self._failure
            if self.player_id is None:
                raise ValueError('尚未取得玩家档案')
            digest = sha256(self.player_id.encode('utf-8')).digest()
            uid = int.from_bytes(digest[:8], 'little') or 1
            return {'uid': uid, 'revision': self._revision, 'profiles': dict(self._confirmed)}

    @property
    def confirmed_revision(self) -> int:
        """主线程读取已确认版本，后台拒绝或未能提交时报告原错误。"""
        with self._lock:
            if self._failure:
                raise self._failure
            return self._revision

    def submit(self, profiles: dict[str, bytes]) -> None:
        """非阻塞接收稳定档案，副本归本次写入持有。"""
        with self._lock:
            if self._failure:
                raise self._failure
            self._pending = {kind: bytes(blob) for kind, blob in profiles.items()}
            if not self._writing:
                self._writing = True
                self._executor.submit(self._drain)

    def _drain(self):
        try:
            while True:
                with self._lock:
                    if self._pending is None:
                        self._writing = False
                        return
                    profiles, self._pending = self._pending, None
                    expected_revision = self._revision
                    if profiles == self._confirmed:
                        continue
                revision = self._gateway.save_player_profile(self.player_id, expected_revision, profiles)
                with self._lock:
                    self._revision, self._confirmed = revision, profiles
        except Exception as error:
            with self._lock:
                self._failure = error
                self._writing = False
                self._pending = None

    def close(self):
        """等待最后一次采样提交完毕，并把未能保存的原因交给退出流程。"""
        self._executor.shutdown(wait=True)
        with self._lock:
            if self._failure:
                raise self._failure
