"""单箱奖励规则：未拥有物品等概率抽取，十次和百次保底随库存保存。"""
import secrets

from codol_rebuild.application.accounts.purchase import consume_entitlement


def draw_reward(state, reward_pool, crate_item_id):
    """规划一次扣箱、发奖和进度变化；不写存储，也不接触原生对象。"""
    next_state = consume_entitlement(state, crate_item_id)
    owned_ids = set(state['owned_ids'])
    candidates = [reward for reward in reward_pool if reward['item_id'] not in owned_ids]
    if not candidates:
        raise ValueError('奖池物品已全部拥有')
    progress = state.get('supply_drop_progress', {})
    third_count = progress.get('since_third', 0)
    fourth_count = progress.get('since_fourth', 0)
    if (type(third_count) is not int or not 0 <= third_count <= 9
            or type(fourth_count) is not int or not 0 <= fourth_count <= 99):
        raise ValueError('抽箱进度无效')
    guarantee_tier = 4 if fourth_count == 99 else 3 if third_count == 9 else 0
    selection = candidates
    if guarantee_tier:
        for candidate_tier in range(guarantee_tier, 0, -1):
            selection = [reward for reward in candidates if reward['tier'] == candidate_tier]
            if selection:
                break
        else:
            raise ValueError('保底品质及以下物品已全部拥有')
    reward = dict(selection[secrets.randbelow(len(selection))])
    next_state['owned_ids'] = sorted(set(next_state['owned_ids']) | {reward['item_id']})
    if reward['stackable']:
        next_state['quantities'] = {**next_state.get('quantities', {}), str(reward['item_id']): 1}
    # 降级发放仍消耗此次保底；否则空品质会使之后每次都进入保底。
    next_state['supply_drop_progress'] = {
        'since_third': 0 if reward['tier'] >= 3 or guarantee_tier else third_count + 1,
        'since_fourth': 0 if reward['tier'] >= 4 or guarantee_tier == 4 else fourth_count + 1,
    }
    return next_state, reward
