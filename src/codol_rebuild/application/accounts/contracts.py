"""原生档案格式与历史归档解码端口。"""
from typing import Protocol


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
