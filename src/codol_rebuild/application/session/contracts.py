"""会话端口与身份记录；房间只接收已确认身份，不执行登录。"""
from dataclasses import dataclass
from typing import Protocol
from codol_rebuild.application.errors import OnlineError


@dataclass(frozen=True)
class IdentityRecord:
    """按服务器保存的身份及待恢复房间；不保存硬件原始信息或密码。"""
    player_id: str
    room_id: str | None = None
    uncertain_create: bool = False
    uncertain_join: bool = False


class IdentityStore(Protocol):
    """服务器身份的原子持久化端口。"""
    def load(self, server: str) -> IdentityRecord | None:
        """读取身份；损坏时拒绝自动创建新身份。"""
        ...

    def save(self, server: str, record: IdentityRecord) -> None:
        """保存已确认身份及房间恢复标记。"""
        ...


@dataclass(frozen=True)
class LoginCompletion:
    """启动登录的完成结果，与房间操作结果分开消费。"""
    data: dict | None = None
    error: OnlineError | None = None


class LoginGateway(Protocol):
    """获取服务端身份与当前昵称。"""
    def login(self, fingerprint: str, username: str, player_id: str | None) -> dict:
        """认证已有身份或创建首次身份。"""
        ...


class RoomLifecycle(Protocol):
    """会话通过公开入口交接身份并等待房间恢复、关闭完成。"""
    def bind_identity(self, identity: IdentityRecord) -> None:
        """在房间工作线程接收确认身份与原有恢复记录。"""
        ...

    def restore(self) -> None:
        """等待清理上次未正常退出的房间；仅供后台启动流程调用。"""
        ...

    def close(self) -> None:
        """完成房间清理并释放房间工作线程。"""
        ...


class SessionPort(Protocol):
    """原生会话协调使用的登录用例接口。"""
    def begin_login(self, username: str) -> bool:
        """启动一次后台登录，有在途结果时拒绝重复提交。"""
        ...

    def poll_login(self) -> LoginCompletion | None:
        """非阻塞消费一次登录结果。"""
        ...


# 权限类错误码：登录或鉴权被服务端以身份/权限理由拒绝。
PERMISSION_ERROR_CODES = frozenset({'BANNED', 'FORBIDDEN', 'UNAUTHENTICATED'})


