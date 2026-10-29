"""本地账号应用服务：策略编排、校验与版本化保存，不解释原生字节。"""
from codol_rebuild.application.accounts.contracts import AccountStore, ProfileCodec
from codol_rebuild.application.accounts.policy import initialization_policy, new_account_policy, validate_initialization
import re

def validate_display_name(value: str) -> str:
    """遵循原版输入框：字母、数字、下划线、汉字，折算英文长度4～16。"""
    if not isinstance(value, str) or not re.fullmatch(r'[_0-9a-zA-Z一-龥]+', value):
        raise ValueError('昵称仅支持汉字、英文字母、数字和下划线')
    if not 4 <= sum(2 if ord(ch) > 255 else 1 for ch in value) <= 16:
        raise ValueError('昵称长度须为4～16个英文字符，汉字计2个')
    return value


def load_display_name(repository: AccountStore, name='local') -> dict:
    """读取并复核保存的昵称；空记录保留客户端原有默认名。"""
    saved = repository.display_name(name)
    if saved['value'] is not None:
        validate_display_name(saved['value'])
    return saved


def change_display_name(repository: AccountStore, revision: int, value: str, name='local') -> dict:
    """独立昵称存储工具；游戏内改名由 AccountSession 同时提交道具消耗。"""
    value = validate_display_name(value)
    revision = repository.save_display_name(name, revision, value)
    return {'value': value, 'revision': revision}



def bootstrap(repository: AccountStore, codec: ProfileCodec, name='local'):
    """在格式端口与存储端口之间编排原子初始化。"""
    repository.identity(name)
    saved = repository.load(name, codec.identities)
    state = saved.get('initialization')
    if state is not None:
        validate_initialization(state)
        if not saved['profiles']:
            raise ValueError('Account initialization exists without profiles')
        codec.validate_snapshot(saved['profiles'])
        profiles = codec.migrate(saved['profiles'])
        if profiles != saved['profiles']:
            revision = repository.save(name, saved['revision'], profiles, codec.identities,
                                       initialization=state)
            saved = {**saved, 'profiles': profiles, 'revision': revision}
        return {**saved, 'new_account': False, 'unlock_report': {'preserved': True}}
    new_account = not bool(saved['profiles'])
    data = codec.initialization_data(new_account)
    policy = new_account_policy(data) if new_account else initialization_policy(data)
    blobs, report = codec.initialize(saved, policy)
    revision = saved['revision']
    if blobs != saved['profiles']:
        state = policy.get('initialization')
        if state is not None:
            validate_initialization(state)
            revision = repository.save(name, revision, blobs, codec.identities, initialization=state)
        else:
            revision = repository.save(name, revision, blobs, codec.identities)
    result = {'profiles': blobs, 'revision': revision, 'uid': saved['uid'],
            'progression': policy['progression'], 'unlock_report': report,
            'new_account': not bool(saved['profiles'])}
    if state is not None:
        result['initialization'] = state
    return result


class AccountSession:
    """账号会话拥有已提交快照和版本；失败不推进保存状态。"""
    def __init__(self, repository: AccountStore, codec: ProfileCodec, name='local'):
        self.repository = repository
        self.codec = codec
        self.name = name
        self.account = None
        self.name_snapshot = None

    def initialize(self):
        self.account = bootstrap(self.repository, self.codec, self.name)
        self.name_snapshot = load_display_name(self.repository, self.name)
        return self.account

    def inspect_snapshot(self, blobs):
        """验证外部快照并返回当前角色，无持久化副作用。"""
        return self.codec.validate_snapshot(blobs)

    def save(self, blobs, balances=None):
        self.codec.validate_snapshot(blobs)
        state = self.account.get('initialization')
        if state is not None and balances is not None:
            state = validate_initialization({**state, 'gp': balances['gp'], 'cp': balances['cp']})
        if blobs == self.account['profiles'] and state == self.account.get('initialization'):
            return False
        if state is not None:
            revision = self.repository.save(self.name, self.account['revision'], blobs, self.codec.identities, initialization=state)
        else:
            revision = self.repository.save(self.name, self.account['revision'], blobs, self.codec.identities)
        self.account = {**self.account, 'revision': revision, 'profiles': dict(blobs)}
        if state is not None:
            self.account['initialization'] = state
        return True

    @staticmethod
    def validate_name(value):
        return validate_display_name(value)

    def purchase_state(self, offer, quantity=1):
        from codol_rebuild.application.accounts.purchase import permanent_purchase
        state = self.account.get('initialization')
        if state is None:
            raise ValueError('旧版全解锁档案无需购买装备')
        return permanent_purchase(state, offer, quantity)

    def commit_purchase(self, offer, quantity=1):
        """原子提交扣款与物品授权，不接收或覆盖配装、角色和进度档案。"""
        state = self.purchase_state(offer, quantity)
        revision = self.repository.save_inventory(self.name, self.account['revision'], state)
        self.account = {**self.account, 'revision': revision,
                        'initialization': state}
        return state

    def consume_state(self, item):
        """返回消耗一件授权后的规划状态；不写存储，供原生适配发布结果。"""
        from codol_rebuild.application.accounts.purchase import consume_entitlement
        state = self.account.get('initialization')
        if state is None:
            raise ValueError('旧版全解锁档案没有可消耗物品')
        return consume_entitlement(state, item)

    def commit_consumption(self, item_id):
        """通过既有账号事务保存一次道具消耗；提交失败不推进已确认快照。"""
        next_state = self.consume_state(item_id)
        revision = self.repository.save(
            self.name, self.account['revision'], self.account['profiles'],
            self.codec.identities, initialization=next_state)
        self.account = {**self.account, 'revision': revision, 'initialization': next_state}
        return next_state

    def credit_state(self, currency, amount, current_balance):
        """返回充值后的规划状态；不写存储，供原生适配发布结果。"""
        from codol_rebuild.application.accounts.purchase import credit_currency
        state = self.account.get('initialization')
        if state is None:
            raise ValueError('旧版全解锁档案没有可充值余额')
        return credit_currency(state, currency, amount, current_balance)

    def rename(self, value, consumed_item):
        """按剩余数量消耗一张改名卡，与昵称一起提交；失败不推进任一快照。"""
        value = validate_display_name(value)
        state = self.consume_state(consumed_item)
        revision = self.repository.save_display_name(
            self.name, self.name_snapshot['revision'], value,
            account_revision=self.account['revision'], initialization=state)
        snapshot = {'value': value, 'revision': revision}
        self.account = {**self.account, 'revision': self.account['revision'] + 1, 'initialization': state}
        self.name_snapshot = snapshot
        return snapshot

    def synchronize_name(self, value):
        """保存登录确认的昵称副本，不消耗改名卡。"""
        if self.name_snapshot['value'] != value:
            revision = self.repository.save_display_name(
                self.name, self.name_snapshot['revision'], value)
            self.name_snapshot = {'value': value, 'revision': revision}
        return self.name_snapshot

