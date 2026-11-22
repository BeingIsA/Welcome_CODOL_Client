"""验证联机仓库命令、首次登录以及配装存档与线上权益的隔离。"""
from threading import Event
import unittest
from unittest.mock import Mock

from codol_rebuild.adapters.game.v1649370.accounts.profile_bridge import LocalProfileBridge
from codol_rebuild.application.accounts.online_inventory import OnlineInventory
from codol_rebuild.application.errors import OnlineError
from codol_rebuild.application.session.service import OnlineSession


class OnlineInventoryTests(unittest.TestCase):
    def test_name_card_check_uses_the_confirmed_online_inventory(self):
        profiles = LocalProfileBridge(Mock(), Mock(), {}, Mock())
        with self.assertRaisesRegex(ValueError, '尚未就绪'):
            profiles.check_name_card()
        profiles._confirmed_inventory = {'revision': 1, 'items': []}
        with self.assertRaisesRegex(ValueError, '没有改名卡'):
            profiles.check_name_card()
        server_item = {'itemId': 50001, 'quantity': 1}
        profiles._confirmed_inventory = {'revision': 2, 'items': [server_item]}
        profiles.check_name_card()
        self.assertEqual(server_item['quantity'], 1)

    def test_receives_each_user_action_once_and_only_after_login(self):
        gateway = Mock()
        gateway.inventory.return_value = {'items': [], 'gp': 50, 'cp': 50, 'revision': 1}
        purchase_started, release_purchase = Event(), Event()
        def purchase(player_id, sku_id, operation_id):
            purchase_started.set()
            if not release_purchase.wait(5):
                raise RuntimeError('测试未释放购买调用')
            return {'inventory': {**gateway.inventory.return_value, 'revision': 2}}
        gateway.purchase.side_effect = purchase
        session = OnlineInventory(gateway)
        try:
            with self.assertRaises(OnlineError):
                session.begin_purchase(56)
            session.load_for_login('pl_test')
            session.begin_purchase(56)
            self.assertTrue(purchase_started.wait(2))
            self.assertIsNone(session.poll('purchase'))
            with self.assertRaises(OnlineError):
                session.begin_purchase(56)
            session.begin_supply_drop(60216)
        finally:
            release_purchase.set()
            session.close()
        self.assertEqual(session.poll('purchase')['inventory']['revision'], 2)
        self.assertIsNone(session.poll('purchase'))
        self.assertIs(session.poll('supply_drop'), gateway.open_supply_drop.return_value)
        self.assertEqual(gateway.purchase.call_count, 1)
        self.assertEqual(gateway.open_supply_drop.call_count, 1)
        purchase_arguments = gateway.purchase.call_args.args
        crate_arguments = gateway.open_supply_drop.call_args.args
        self.assertEqual(purchase_arguments[:2], ('pl_test', 56))
        self.assertEqual(crate_arguments[:2], ('pl_test', 60216))
        self.assertNotEqual(purchase_arguments[2], crate_arguments[2])

    def test_inventory_load_failure_stops_login_before_realtime_and_room_restore(self):
        gateway, store, rooms, realtime = Mock(), Mock(), Mock(), Mock()
        store.load.return_value = None
        gateway.login.return_value = {'playerId': 'pl_test', 'displayName': '仓库验收'}
        gateway.inventory.side_effect = OnlineError('NETWORK_ERROR', '仓库暂不可用')
        inventory = OnlineInventory(gateway)
        online = OnlineSession(gateway, store, 'http://localhost', 'device', rooms, realtime, inventory)
        try:
            online.begin_login('仓库验收')
            online._pending.result(timeout=2)
            self.assertEqual(online.poll_login().error.code, 'NETWORK_ERROR')
            self.assertIsNone(inventory.initial_snapshot)
            gateway.inventory.assert_called_once_with('pl_test')
            realtime.start.assert_not_called()
            rooms.restore.assert_not_called()
            self.assertEqual(store.save.call_args.args[1].player_id, 'pl_test')
        finally:
            online.close()
            inventory.close()
