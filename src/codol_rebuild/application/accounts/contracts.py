"""账号存储契约：业务依赖此接口，SQLite 等持久化实现负责满足契约。

公开档案和安全档案必须成对提交；revision 用于拒绝覆盖并发修改。
接口不暴露数据库连接、文件路径、进程句柄或客户端地址。
"""
from typing import Protocol


class RevisionConflict(ValueError):
    """保存基于过期版本，调用方必须重新读取而不能静默覆盖。"""


class AccountStore(Protocol):
    """账号身份、档案读取和原子保存的最小业务端口。"""

    def identity(self, name: str = 'local') -> dict:
        """确保账号存在，返回稳定身份和当前版本。"""
        ...

    def load(self, name: str, identities: dict) -> dict:
        """校验档案身份并返回一致快照，损坏或不完整时拒绝。"""
        ...

    def save(self, name: str, revision: int, profiles: dict, identities: dict, initialization: dict | None = None) -> int:
        """按预期版本原子保存完整档案对，返回新版本。"""
        ...

    def display_name(self, name: str) -> dict:
        """读取独立昵称版本；未改名时 value 为 None，不改变账号主键。"""
        ...

    def save_display_name(self, name: str, revision: int, value: str,
                          *, account_revision: int | None = None, initialization: dict | None = None) -> int:
        """提交昵称；附带库存时同时校验账号版本并原子保存，两种版本均递增。"""
        ...


class ProfileCodec(Protocol):
    """原生格式端口；应用层只传递不透明档案和明确策略。"""
    @property
    def identities(self) -> dict: ...
    def initialization_data(self, new_account: bool) -> dict: ...
    def initialize(self, saved: dict, policy: dict) -> tuple: ...
    def migrate(self, profiles: dict) -> dict:
        """返回符合当前格式的不透明档案对，保留账号业务状态；无保存副作用。"""
        ...
    def validate_snapshot(self, profiles: dict) -> int: ...


class ProfileArchiveFormat(Protocol):
    """历史归档需要的格式校验，不暴露具体编码实现。"""
    @property
    def identity(self) -> dict: ...
    def validate(self, blob: bytes) -> None: ...


class ProfileFormatError(ValueError):
    """档案身份、容量或字段格式不匹配。"""
