"""离线批量解锁用例：校验目录、去重及保留其他账号状态。"""
from copy import deepcopy
from codol_rebuild.application.accounts.contracts import InventoryEditorStore
from codol_rebuild.application.accounts.policy import validate_initialization


class ItemUnlockService:
    """通过显式存储端口和运行状态检查协作，不依赖图形界面。"""

    def __init__(self, store: InventoryEditorStore, catalog_ids, ensure_game_stopped):
        self.store = store
        self.catalog_ids = frozenset(catalog_ids) - {0, 15400, 20000}
        self.ensure_game_stopped = ensure_game_stopped

    def unlock(self, account_name, selected_ids):
        """一次提交所有选中物品；已拥有不增加数量，失败保留原事务。"""
        self.ensure_game_stopped()
        selected_ids = set(selected_ids)
        if not selected_ids or not selected_ids <= self.catalog_ids:
            raise ValueError('请选择有效物品；货币保留 ID 不能作为物品解锁。')
        snapshot = self.store.inventory_snapshot(account_name)
        state = validate_initialization(snapshot['state'])
        added_ids = selected_ids - set(state['owned_ids'])
        if not added_ids:
            return {'added': 0, 'skipped': len(selected_ids), 'backup': ''}
        next_state = deepcopy(state)
        next_state['owned_ids'] = sorted(set(state['owned_ids']) | added_ids)
        validate_initialization(next_state)
        backup_path = self.store.backup_inventory_database()
        self.ensure_game_stopped()
        self.store.save_inventory(account_name, snapshot['revision'], next_state)
        return {'added': len(added_ids), 'skipped': len(selected_ids)-len(added_ids), 'backup': backup_path}
