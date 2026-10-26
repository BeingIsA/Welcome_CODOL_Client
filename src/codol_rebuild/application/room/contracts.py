"""房间应用端口与数据契约；不包含 HTTP、原生内存或文件实现。"""
from codol_rebuild.application.errors import OnlineError
from dataclasses import dataclass
from typing import Protocol
import re
from codol_rebuild.application.session.contracts import IdentityRecord


def valid_room_id(value) -> bool:
    """接受非零 63 位数字房间号及旧 rm_ 记录，兼容未完成的恢复操作。"""
    return isinstance(value, str) and (
        re.fullmatch(r'rm_[A-Za-z0-9_-]{1,96}', value) is not None
        or (re.fullmatch(r'[1-9][0-9]{0,18}', value) is not None
            and int(value) <= (1 << 63) - 1))


@dataclass(frozen=True)
class Completion:
    """后台用例完成快照，由调用线程消费一次。"""
    operation: str
    data: dict | None = None
    error: OnlineError | None = None


class RoomStateStore(Protocol):
    """只持久化房间恢复记录；不提供身份加载或登录能力。"""
    def save(self, server: str, record: IdentityRecord) -> None:
        """保存同一确认身份下的房间状态，沿用原身份文件格式。"""
        ...


class Gateway(Protocol):
    """玩家业务通信端口；所有方法在后台串行执行。"""
    def update_profile(self, player_id: str, room_id: str, profile: dict) -> dict:
        """更新当前房内完整档案，返回房间和玩家身份确认。"""
        ...

    def change_ready(self, player_id: str, room_id: str, ready: bool) -> dict:
        """设置本人准备状态，返回服务端确认的房间快照。"""
        ...

    def change_team(self, player_id: str, room_id: str, team: str) -> dict:
        """提交本人的绝对目标阵营，返回服务端确认快照。"""
        ...
    def kick(self, player_id: str, room_id: str, target_player_id: str) -> dict:
        """以房主身份踢出目标，返回服务端确认的房间快照。"""
        ...
    def join(self, player_id: str, room_id: str, username: str,
             password: str | None, profile: dict | None = None) -> dict:
        """加入指定房间并返回服务端权威成员与设置快照。"""
        ...

    def create(self, player_id: str, username: str, max_players: int,
               password: str | None, settings: dict | None = None,
               profile: dict | None = None) -> dict:
        """创建房间并返回成员快照；settings 为可选的对局设置快照。"""
        ...

    def member_profile(self, player_id: str, room_id: str, member_id: str) -> dict | None:
        """读取同房成员最近提交的完整档案，缺少快照时返回 None。"""
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


class RoomPort(Protocol):
    """原生桥使用的异步用例接口；调用方不得直接访问工作线程状态。"""

    def begin_update_profile(self, room_id: str, profile: dict) -> bool:
        """异步上传当前档案；在途期间只保留最新待发送快照。"""
        ...

    def poll_update_profile(self) -> Completion | None:
        """消费上传结果，不自动重试失败请求。"""
        ...

    def begin_change_ready(self, room_id: str, ready: bool) -> bool:
        """异步提交一次准备或取消准备命令。"""
        ...

    def poll_change_ready(self) -> Completion | None:
        """消费准备命令结果，不自动恢复或重试。"""
        ...

    def begin_change_team(self, room_id: str, team: str) -> bool:
        """提交一次换队请求，不提前更新已确认阵营。"""
        ...

    def poll_change_team(self) -> Completion | None:
        """消费换队完成结果；失败仅收尾，不驱动 UI 或自动重试。"""
        ...
    def poll_event(self) -> Completion | None:
        """消费当前房间的设置、成员更新及被踢/关闭完成事件。"""
        ...

    def begin(self, operation: str, username: str, *, max_players: int = 4,
              password: str | None = None, settings: dict | None = None,
              room_id: str | None = None, profile: dict | None = None) -> bool:
        """发起创建、加入、退出或同步设置；有未消费操作时返回 False。"""
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

    def begin_kick(self, room_id: str, target_player_id: str) -> bool:
        """提交当前房间踢人命令；重复请求或主流程忙碌时拒绝。"""
        ...

    def poll_kick(self) -> Completion | None:
        """非阻塞消费一次踢人完成结果。"""
        ...
