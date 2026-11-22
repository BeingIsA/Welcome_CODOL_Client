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
    """交给游戏适配执行的改名结果。"""

    display_name: str
    error: Exception | None = None
    notify: bool = False
    inventory: dict | None = None


class RenameSession:
    """调用线程管理改名状态，工作线程提交服务端事务。"""

    def __init__(self, gateway: RenameGateway):
        self.gateway = gateway
        self.player_id = None
        self.ready = False
        self._future = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='codol-rename')

    def connected(self, player_id):
        """绑定登录身份，等待原生初始化应用服务端昵称。"""
        self.player_id = player_id
        self.ready = False

    def begin(self, display_name):
        """提交一次改名，等待结果期间禁止再次提交。"""
        if not self.ready or self.player_id is None or self._future is not None:
            raise ValueError('登录或改名尚未完成')
        self.ready = False
        self._future = self._executor.submit(self._request, self.player_id, display_name)

    def _request(self, player_id, display_name):
        try:
            result = self.gateway.rename(player_id, display_name)
        except OnlineError as error:
            return RenameCompletion(display_name, error, True)
        return RenameCompletion(result['displayName'], notify=True, inventory=result['inventory'])

    def poll(self):
        """取出网络结果；失败直接反馈，不自动重试或保存恢复记录。"""
        if self._future is None or not self._future.done():
            return None
        completed_future, self._future = self._future, None
        completion = completed_future.result()
        if completion.error is not None:
            self.ready = not completion.error.uncertain
        return completion

    def complete(self):
        """原生昵称已完成初始化或改名回显，允许后续房间操作和改名。"""
        self.ready = True

    def close(self):
        """等待有界 HTTP 请求结束并关闭工作线程。"""
        self._executor.shutdown(wait=True)
