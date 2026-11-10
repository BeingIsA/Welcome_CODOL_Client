"""本地账号初始化策略：最高进度、初始角色与配装选择。

策略只操作业务概念（角色、武器、附件、技能、投掷物、余额），不认识
DDL 字段路径、原生分类数字或预留 ID；这些由版本适配层在数据进出时翻译。
"""
from copy import deepcopy


def initialization_policy(data):
    """输入为格式适配层解析后的业务数据；原生数值编码仍由格式层负责。"""
    return {'progression': dict(data['progression']),
            'character': data['default_character'],
            'loadouts': [{**deepcopy(data['starting_loadouts'][slot % 3]), 'name': f'Loadout {slot+1}'}
                         for slot in range(5)] if data['starting_loadouts'] else [],
            'fill_unlock_flags': True, 'unlock_threshold': 32767}


def new_account_policy(data):
    """新档使用适配层提供的初始授权；保留满级进度和初始余额。"""
    policy = initialization_policy(data)
    policy['initialization'] = {'version': 1, 'owned_ids': sorted(data['initial_item_ids']), 'gp': 2000000000, 'cp': 100000}
    return policy


def validate_initialization(state):
    """校验持久化授权状态，拒绝损坏数据而非恢复成全解锁。"""
    if not isinstance(state, dict) or state.get('version') != 1:
        raise ValueError('Unsupported account initialization version')
    ids = state.get('owned_ids')
    if not isinstance(ids, list) or any(type(v) is not int or not 0 < v < 65535 for v in ids):
        raise ValueError('Invalid account entitlement IDs')
    if len(ids) != len(set(ids)) or any(v in ids for v in (15400, 20000)):
        raise ValueError('Invalid duplicate or reserved account entitlement')
    for key in ('gp', 'cp'):
        if type(state.get(key)) is not int or not 0 <= state[key] <= 2147483647:
            raise ValueError('Invalid account currency balance')
    quantities = state.get('quantities', {})
    quantity_ids = {str(item) for item in ids}
    if not isinstance(quantities, dict) or any(
            key not in quantity_ids or type(value) is not int
            or not 1 <= value <= 2147483647 for key, value in quantities.items()):
        raise ValueError('Invalid account item quantities')
    return state
