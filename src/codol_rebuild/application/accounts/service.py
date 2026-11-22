"""账号会话负责原生档案初始化、稳定快照交接及当前昵称，纯离线状态仅存于内存。"""
import re
import secrets

from codol_rebuild.application.accounts.contracts import ProfileCodec
from codol_rebuild.application.accounts.online_profiles import OnlineProfiles
from codol_rebuild.application.accounts.policy import initialization_policy, new_account_policy


def validate_display_name(value: str) -> str:
    """遵循原版输入框：字母、数字、下划线、汉字，折算英文长度4～16。"""
    if not isinstance(value, str) or not re.fullmatch(r'[_0-9a-zA-Z一-龥]+', value):
        raise ValueError('昵称仅支持汉字、英文字母、数字和下划线')
    if not 4 <= sum(2 if ord(ch) > 255 else 1 for ch in value) <= 16:
        raise ValueError('昵称长度须为4～16个英文字符，汉字计2个')
    return value


class AccountSession:
    """联机从本人的服务端双档案初始化，独立启动时使用一次性默认档案。"""

    def __init__(self, codec: ProfileCodec, online_profiles: OnlineProfiles | None = None):
        self.codec = codec
        self._online_profiles = online_profiles
        self.account = None
        self.name_snapshot = {'value': None}

    def initialize(self):
        """使用既有原生规则创建或读取配装，只将新建或迁移后的双档案提交服务器。"""
        if self._online_profiles is None:
            saved = {'uid': secrets.randbelow(2**62-1) + 1, 'revision': 0, 'profiles': {}}
        else:
            saved = self._online_profiles.snapshot()
            self.synchronize_name(self._online_profiles.display_name)
        self.account = {**saved, 'new_account': not bool(saved['profiles'])}
        if saved['profiles']:
            profiles = self.codec.migrate(saved['profiles'])
            self.account['unlock_report'] = {'preserved': True}
        else:
            data = self.codec.initialization_data(True)
            policy = initialization_policy(data) if self._online_profiles is not None else new_account_policy(data)
            profiles, report = self.codec.initialize(saved, policy)
            self.account.update(progression=policy['progression'], unlock_report=report)
            if 'initialization' in policy:
                self.account['initialization'] = policy['initialization']
        self.save(profiles)
        return self.account

    def inspect_snapshot(self, profiles):
        """检查原生双档案并返回当前角色。"""
        return self.codec.validate_snapshot(profiles)

    @property
    def initial_profile_ready(self) -> bool:
        """联机首份建档已获确认后就绪，纯离线初始化只使用本次会话。"""
        return self._online_profiles is None or self._online_profiles.confirmed_revision > 0

    def save(self, profiles):
        """接收当前帧的稳定快照；联机修订号只取服务端确认值。"""
        self.codec.validate_snapshot(profiles)
        if self._online_profiles is not None:
            self.account['revision'] = self._online_profiles.confirmed_revision
        if profiles == self.account['profiles']:
            return False
        if self._online_profiles is not None:
            self._online_profiles.submit(profiles)
        self.account['profiles'] = dict(profiles)
        return True

    @staticmethod
    def validate_name(value):
        return validate_display_name(value)

    def synchronize_name(self, value):
        """保存本次会话中服务端确认的显示名。"""
        self.name_snapshot = {'value': value}
        return self.name_snapshot
