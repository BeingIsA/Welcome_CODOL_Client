"""联机仓库用例：获取首份授权、提交单次交易，在游戏帧取回服务端结果。"""
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol
from uuid import uuid4

from codol_rebuild.application.errors import OnlineError


class InventoryGateway(Protocol):
    """登录身份下的个人仓库读写；每笔写入由调用方标识，重放返回原成交结果。"""

    def inventory(self, player_id: str) -> dict:
        """读取本人余额及完整物品快照。"""
        ...

    def purchase(self, player_id: str, sku_id: int, operation_id: str) -> dict:
        """购买一个服务端商品规格。"""
        ...

    def open_supply_drop(self, player_id: str, crate_id: int, operation_id: str) -> dict:
        """打开一个军火箱并获取本次奖励。"""
        ...


class OnlineInventory:
    """登录工作线程完成首份快照读取，后续交易由独立工作线程提交。"""

    def __init__(self, gateway: InventoryGateway):
        self.gateway = gateway
        self.player_id = None
        self.initial_snapshot = None
        self._pending = {}
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='codol-inventory')

    def load_for_login(self, player_id):
        """仅供登录工作线程使用，先取得并验证快照，再交接给原生适配。"""
        snapshot = self.gateway.inventory(player_id)
        self.player_id = player_id
        self.initial_snapshot = snapshot

    def begin_purchase(self, sku_id):
        """从原生购买命令开始一次后台扣款发物。"""
        self._begin('purchase', self.gateway.purchase, sku_id)

    def begin_supply_drop(self, crate_id):
        """从原生单箱命令开始一次后台扣箱发奖。"""
        self._begin('supply_drop', self.gateway.open_supply_drop, crate_id)

    def _begin(self, kind, operation, item_reference):
        if self.initial_snapshot is None or kind in self._pending:
            raise OnlineError('INVENTORY_BUSY', '仓库未就绪或前一笔操作尚未完成')
        self._pending[kind] = self._executor.submit(
            operation, self.player_id, item_reference, uuid4().hex)

    def poll(self, kind):
        """主线程非阻塞取得指定原生任务的确认结果。"""
        pending = self._pending.get(kind)
        if pending is None or not pending.done():
            return None
        del self._pending[kind]
        return pending.result()

    def close(self):
        """等待已发交易结束后释放工作线程。"""
        self._executor.shutdown(wait=True)
