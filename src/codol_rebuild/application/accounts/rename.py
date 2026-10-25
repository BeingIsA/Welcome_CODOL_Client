"""改名协调：后台通信及确认结果交接，不访问游戏进程。"""
from codol_rebuild.application.errors import OnlineError
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Protocol


class RenameGateway(Protocol):
    """改名通信端口。"""

    def rename(self, player_id: str, display_name: str) -> dict:
        """提交当前玩家的新昵称。"""
        ...


@dataclass(frozen=True)
class RenameCompletion:
    """交给游戏适配执行的结果；notify 区分用户改名和登录同步。"""

    display_name: str
    error: Exception | None = None
    notify: bool = False


class RenameSession:
    """调用线程管理状态，工作线程只执行一次 HTTP 请求。"""

    def __init__(self, gateway: RenameGateway):
        self.gateway = gateway
        self.player_id = None
        self.ready = False
        self._future = None
        self._result = None
        self._display_name = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='codol-rename')

    def connected(self, player_id, display_name):
        """登录后交接服务端昵称用于本地同步。"""
        self.player_id = player_id
        self.ready = False
        self._result = RenameCompletion(display_name)

    def begin(self, display_name):
        """提交一次改名，等待结果期间禁止再次提交。"""
        if not self.ready or self.player_id is None or self._future is not None or self._result is not None:
            raise ValueError('登录或改名尚未完成')
        self.ready = False
        self._display_name = display_name
        self._future = self._executor.submit(self._request, self.player_id, display_name)

    def _request(self, player_id, display_name):
        try:
            self.gateway.rename(player_id, display_name)
        except OnlineError as error:
            return error
        return None

    def poll(self):
        """取出网络结果；失败直接反馈，不自动重试或保存恢复记录。"""
        if self._result is not None:
            result, self._result = self._result, None
            return result
        if self._future is None or not self._future.done():
            return None
        completed_future, self._future = self._future, None
        error = completed_future.result()
        if error is not None:
            self.ready = not error.uncertain
        return RenameCompletion(self._display_name, error, True)

    def complete(self):
        """原生及本地事务均成功后，允许后续房间操作和改名。"""
        self.ready = True

    def close(self):
        """等待有界 HTTP 请求结束并关闭工作线程。"""
        self._executor.shutdown(wait=True)
