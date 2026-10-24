"""房间应用端口与数据契约；不包含 HTTP、原生内存或文件实现。"""
from dataclasses import dataclass
from enum import Enum
from typing import Protocol
import re


def valid_room_id(value) -> bool:
    """接受非零 63 位数字房间号及旧 rm_ 记录，兼容未完成的恢复操作。"""
    return isinstance(value, str) and (
        re.fullmatch(r'rm_[A-Za-z0-9_-]{1,96}', value) is not None
        or (re.fullmatch(r'[1-9][0-9]{0,18}', value) is not None
            and int(value) <= (1 << 63) - 1))


class RoomError(Exception):
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
    uncertain_join: bool = False


@dataclass(frozen=True)
class Completion:
    """后台用例完成快照，由调用线程消费一次。"""
    operation: str
    data: dict | None = None
    error: RoomError | None = None


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
    def join(self, player_id: str, room_id: str, username: str,
             password: str | None) -> dict:
        """加入指定房间并返回服务端权威成员与设置快照。"""
        ...

    def login(self, fingerprint: str, username: str, player_id: str | None) -> dict:
        """获取或复用身份。"""
        ...

    def create(self, player_id: str, username: str, max_players: int,
               password: str | None, settings: dict | None = None) -> dict:
        """创建房间并返回成员快照；settings 为可选的对局设置快照。"""
        ...

    def update_settings(self, player_id: str, room_id: str, settings: dict) -> dict:
        """整表替换房间对局设置；不传修订号，返回服务端确认的房间详情。"""
        ...

    def room_detail(self, player_id: str, room_id: str) -> dict:
        """读取房间详情与设置，用于查询及不确定加入的状态确认。"""
        ...

    def leave(self, player_id: str, room_id: str) -> dict:
        """幂等退出指定房间。"""
        ...

    def list_rooms(self, player_id: str, cursor: str | None) -> dict:
        """分页读取大厅摘要，也用于恢复结果未知的创建请求。"""
        ...


class RealtimeStatus(Enum):
    """实时连接状态；供应用层感知断线与身份失效。"""
    CONNECTED = 'connected'
    RECONNECTING = 'reconnecting'
    INVALIDATED = 'invalidated'
    CLOSED = 'closed'


class RealtimeConnection(Protocol):
    """实时连接生命周期端口；实现负责接收、保活和断线重连。"""
    def poll_event(self) -> dict | None:
        """非阻塞消费一条已解码的下行消息。"""
        ...

    def start(self, player_id: str, fingerprint: str) -> None:
        """建立首次连接，失败抛出联机错误；同一身份重复调用不重建连接。

        身份失效（INVALIDATED）后线程痕迹已清理，再次调用会真正重建连接。
        """
        ...

    def close(self) -> None:
        """停止重连并释放连接及后台线程。"""
        ...

    def status(self) -> RealtimeStatus:
        """返回当前连接状态；未启动或已关闭为 CLOSED。"""
        ...


# 权限类错误码：登录或鉴权被服务端以身份/权限理由拒绝。
PERMISSION_ERROR_CODES = frozenset({'BANNED', 'FORBIDDEN', 'UNAUTHENTICATED'})


class RoomPort(Protocol):
    """原生桥使用的异步用例接口；调用方不得直接访问工作线程状态。"""
    def poll_event(self) -> Completion | None:
        """消费当前房间设置推送；不查询快照，也不比较修订号。"""
        ...

    def begin(self, operation: str, username: str, *, max_players: int = 4,
              password: str | None = None, settings: dict | None = None,
              room_id: str | None = None) -> bool:
        """发起登录、创建、加入、退出或同步设置；有未消费操作时返回 False。"""
        ...

    def poll(self) -> Completion | None:
        """非阻塞地消费一次完成结果。"""
        ...

    def begin_list(self, username: str) -> bool:
        """刷新大厅列表；替换旧批次，网络查询不改变当前房间。"""
        ...

    def poll_list(self) -> Completion | None:
        """非阻塞消费最新批次的完整列表，旧批次结果不向调用方发布。"""
        ...

    def cancel_list(self) -> None:
        """取消列表发布，并在当前分页请求结束后停止继续查询。"""
        ...

    def close(self) -> None:
        """串行收尾已提交请求并尝试离房，失败保留恢复记录。"""
        ...
