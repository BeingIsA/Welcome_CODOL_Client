"""联机改名回归：单次请求、原子扣卡及原生延迟完成边界。"""
from codol_rebuild.application.errors import OnlineError
import struct
import unittest
from unittest.mock import ANY, Mock

import capstone
from codol_rebuild.application.accounts.rename import RenameSession, RenameCompletion
from codol_rebuild.application.accounts.service import AccountSession

from codol_rebuild.application.room.service import RoomSession
from codol_rebuild.application.session.service import OnlineSession
from codol_rebuild.adapters.transport.http import HttpGateway
from codol_rebuild.adapters.game.v1649370.accounts import player_name
from test_purchase import Codec, Memory


class RenameSessionTests(unittest.TestCase):
    def setUp(self):
        self.account = AccountSession(Codec())
        self.gateway = Mock()
        self.confirmed = {'revision': 2, 'gp': 1000, 'cp': 100,
                          'items': [{'itemId': 50001, 'quantity': 1}]}
        self.gateway.rename.return_value = {'displayName': '新昵称', 'inventory': self.confirmed}
        self.session = self.new_session()
        self.session.connected('pl_test')
        self.account.synchronize_name('原昵称')
        self.session.complete()

    def new_session(self):
        session = RenameSession(self.gateway)
        self.addCleanup(session.close)
        return session

    def finish_network(self, session=None):
        session = session or self.session
        session._future.result(timeout=3)
        return session.poll()

    def test_rename_returns_confirmed_card_count_and_saves_only_display_name(self):
        self.session.begin('新昵称')
        with self.assertRaises(ValueError):
            self.session.begin('另一个昵称')
        result = self.finish_network()
        self.assertTrue(result.notify)
        self.assertEqual(result.inventory, self.confirmed)
        self.assertEqual(self.account.name_snapshot['value'], '原昵称')
        self.account.synchronize_name(result.display_name)
        self.session.complete()
        self.gateway.rename.assert_called_once_with('pl_test', '新昵称')
        self.assertEqual(self.account.name_snapshot['value'], '新昵称')

    def test_response_lost_does_not_retry_or_recover(self):
        self.gateway.rename.side_effect = OnlineError('NETWORK_ERROR', '响应丢失', uncertain=True)
        self.session.begin('新昵称')
        self.assertIsNotNone(self.finish_network().error)
        self.gateway.rename.assert_called_once_with('pl_test', '新昵称')
        restarted = self.new_session()
        restarted.connected('pl_test')
        self.assertIsNone(restarted.poll())
        self.assertFalse(restarted.ready)
        self.assertEqual(self.gateway.rename.call_count, 1)

    def test_explicit_rejection_keeps_name_and_cards(self):
        self.gateway.rename.side_effect = OnlineError('INVALID_REQUEST', '拒绝')
        self.session.begin('新昵称')
        self.assertIsNotNone(self.finish_network().error)
        self.assertTrue(self.session.ready)
        self.assertEqual(self.account.name_snapshot['value'], '原昵称')
        self.assertIsNone(self.session.poll())

class DeferredNameBridgeTests(unittest.TestCase):
    def test_http_rename_preserves_online_error(self):
        gateway = HttpGateway('http://localhost')
        failure = OnlineError('NETWORK_ERROR', '响应丢失', uncertain=True)
        gateway._request = Mock(side_effect=failure)
        with self.assertRaises(OnlineError) as caught:
            gateway.rename('pl_test', '新昵称')
        self.assertIs(caught.exception, failure)

    def test_login_completion_forwards_server_name_instead_of_submitted_name(self):
        gateway = Mock()
        gateway.login.return_value = {'playerId': 'pl_test', 'displayName': '服务端昵称'}
        store = Mock()
        store.load.return_value = None
        rooms = RoomSession(gateway, store, 'http://localhost')
        session = OnlineSession(gateway, store, 'http://localhost', 'fingerprint', rooms)
        self.addCleanup(session.close)
        self.assertTrue(session.begin_login('旧本地昵称'))
        session._pending.result(timeout=3)
        self.assertEqual(session.poll_login().data['displayName'], '服务端昵称')

    def test_http_returns_confirmed_name_and_inventory(self):
        gateway = HttpGateway('http://localhost')
        result = {'playerId': 'pl_test', 'displayName': '新昵称',
                  'inventory': {'revision': 2, 'cp': 10, 'gp': 10, 'items': []}}
        gateway._request = Mock(return_value=result)
        self.assertEqual(gateway.rename('pl_test', '新昵称'), result)
        gateway._request.assert_called_once_with('POST', '/api/v1/players/me/rename',
            {'displayName': '新昵称', 'operationId': ANY}, 'pl_test')

    def test_request_returns_without_setter_and_tick_owns_completion(self):
        entry, state, ready = 0x21000000, 0x22000000, 0x23000000
        plan = player_name.build_adapter(entry, state, ready, 0x24000000, deferred=True)
        decoder = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
        instructions = list(decoder.disasm(plan['code'], entry))
        request_trap = next(address for address, phase in plan['traps'].items() if phase == 'request')
        after_request = [instruction for instruction in instructions if request_trap < instruction.address < plan['poll']]
        self.assertEqual([instruction.mnemonic for instruction in after_request], ['mov', 'popal', 'popfd', 'ret'])
        self.assertEqual(after_request[-1].op_str, '4')
        targets = {instruction.address for instruction in instructions}
        for instruction in instructions:
            if instruction.mnemonic.startswith('j') and instruction.op_str.startswith('0x'):
                target = int(instruction.op_str, 16)
                self.assertTrue(target in targets or target == player_name.HANDLER)
        self.assertLess(len(plan['code']), 4096)

    def test_poll_failure_sets_native_error_without_changing_name(self):
        memory = Memory()
        state = 0x22000000
        profiles = Mock()
        session = Mock()
        session.poll.return_value = RenameCompletion('新昵称', ValueError('服务端拒绝'), True)
        bridge = player_name.ServerNameBridge(memory, profiles, session, {'state': state},
                                               (OSError, ValueError), Mock())
        bridge.waiting_ui = True
        bridge.handle('poll', 0, {})
        self.assertEqual(struct.unpack('<II', memory.read(state+136, 8)), (2, 1))
        self.assertEqual(memory.read(state+144, 512).split(b'\0')[0].decode(), '服务端拒绝')
        profiles.apply_inventory.assert_not_called()
        profiles.synchronize_name.assert_not_called()


if __name__ == '__main__':
    unittest.main()
