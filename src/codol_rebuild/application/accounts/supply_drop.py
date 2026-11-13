"""单箱奖励规则：按商品或物品候选等概率抽取并原子规划库存。"""
import secrets

from codol_rebuild.application.accounts.purchase import consume_entitlement
from codol_rebuild.application.accounts.policy import validate_initialization


def draw_reward(state, reward_pool, crate_item_id):
    """排除全部已拥有的单件奖励，规划单次扣箱与最多三份不同商品发奖。"""
    next_state = consume_entitlement(state, crate_item_id)
    owned_ids = set(state['owned_ids'])
    candidates = [reward for reward in reward_pool
                  if any((reward['product_id'] != 0 and content['stackable'])
                         or content['item_id'] not in owned_ids
                         for content in reward['contents'])]
    if not candidates:
        raise ValueError('奖池奖励已全部拥有')
    selection_count = min(3 if reward_pool[0]['product_id'] != 0 else 1, len(candidates))
    selected_rewards = []
    for selection_index in range(selection_count):
        selected_rewards.append(candidates.pop(secrets.randbelow(len(candidates))))
    granted_contents = []
    next_owned_ids = set(next_state['owned_ids'])
    quantities = dict(next_state.get('quantities', {}))
    for content in [content for reward in selected_rewards for content in reward['contents']]:
        item_id = content['item_id']
        previous_quantity = quantities.get(str(item_id), 1 if item_id in next_owned_ids else 0)
        quantity_delta = content['count'] if content['stackable'] else int(item_id not in next_owned_ids)
        quantity = previous_quantity + quantity_delta
        if content['stackable']:
            quantities[str(item_id)] = quantity
        next_owned_ids.add(item_id)
        granted_contents.append({**content, 'quantity': quantity, 'quantity_delta': quantity_delta})
    next_state.update(owned_ids=sorted(next_owned_ids), quantities=quantities)
    return validate_initialization(next_state), {'product_ids': [reward['product_id'] for reward in selected_rewards], 'contents': granted_contents}
