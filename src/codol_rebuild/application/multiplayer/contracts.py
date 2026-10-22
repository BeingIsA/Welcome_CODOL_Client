"""联机端口与数据契约；不包含 HTTP、原生内存或文件实现。"""
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol


class MultiplayerError(Exception):
    """可展示的联机错误；uncertain 表示写请求可能已在服务端执行。"""
    def __init__(self, code: str, message: str, *, uncertain: bool = False):
        super().__init__(message)
        self.code = code
        self.uncertain = uncertain


@dataclass(frozen=True)
class IdentityRecord:
    """按服务器保存的身份及待恢复房间；不保存硬件原始信息或密码。"""
    player_id: str
    room_id: str | None = None
    uncertain_create: bool = False


@dataclass(frozen=True)
class Completion:
    """后台用例完成快照，由调用线程消费一次。"""
    operation: str
    data: dict | None = None
    error: MultiplayerError | None = None


class IdentityStore(Protocol):
    """服务器身份的原子持久化端口。"""
    def load(self, server: str) -> IdentityRecord | None:
        """读取身份；损坏时拒绝自动创建新身份。"""
        ...

    def save(self, server: str, record: IdentityRecord) -> None:
        """保存已确认身份及房间恢复标记。"""
        ...


class Gateway(Protocol):
    """玩家业务通信端口；所有方法在后台串行执行。"""
    def login(self, fingerprint: str, username: str, player_id: str | None) -> dict:
        """获取或复用身份。"""
        ...

    def create(self, player_id: str, username: str, max_players: int, password: str | None) -> dict:
        """创建房间并返回成员快照。"""
        ...

    def leave(self, player_id: str, room_id: str) -> dict:
        """幂等退出指定房间。"""
        ...

    def list_rooms(self, player_id: str, cursor: str | None) -> dict:
        """分页读取房间，用于恢复结果未知的创建请求。"""
        ...


class RealtimeStatus(Enum):
    """实时连接状态；供应用层感知断线与身份失效。"""
    CONNECTED = 'connected'
    RECONNECTING = 'reconnecting'
    INVALIDATED = 'invalidated'
    CLOSED = 'closed'


class RealtimeConnection(Protocol):
    """实时连接生命周期端口；实现负责接收、保活和断线重连。"""
    def start(self, player_id: str) -> None:
        """建立首次连接，失败抛出联机错误；同一身份重复调用不重建连接。

        身份失效（INVALIDATED）后线程痕迹已清理，再次调用会真正重建连接。
        """
        ...

    def close(self) -> None:
        """停止重连并释放连接及后台线程。"""
        ...

    def set_invalidation_handler(self, handler: Callable[[], None]) -> None:
        """注册身份失效回调；连接被服务端以身份理由拒绝时在后台线程调用一次。"""
        ...

    def status(self) -> RealtimeStatus:
        """返回当前连接状态；未启动或已关闭为 CLOSED。"""
        ...


# 权限类错误码：登录或鉴权被服务端以身份/权限理由拒绝。
PERMISSION_ERROR_CODES = frozenset({'BANNED', 'FORBIDDEN', 'UNAUTHENTICATED'})


class MultiplayerPort(Protocol):
    """原生桥使用的异步用例接口；调用方不得直接访问工作线程状态。"""
    def begin(self, operation: str, username: str, *, max_players: int = 4,
              password: str | None = None) -> bool:
        """发起登录、创建或退出；有未消费操作时返回 False。"""
        ...

    def poll(self) -> Completion | None:
        """非阻塞地消费一次完成结果。"""
        ...

    def invalidated(self) -> bool:
        """实时身份是否已被服务端拒绝且尚未重新鉴权成功。"""
        ...

    def close(self) -> None:
        """串行收尾已提交请求并尝试离房，失败保留恢复记录。"""
        ...
