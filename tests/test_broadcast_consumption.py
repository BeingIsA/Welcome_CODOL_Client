"""大喇叭扣除回归：隔离数据库与内存替身，不代表真实 UI 验收。"""
from contextlib import closing
from copy import deepcopy
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import Mock

from codol_rebuild.adapters.game.v1649370 import broadcast, profile, purchase
from codol_rebuild.adapters.game.v1649370.profile_bridge import LocalProfileBridge
from codol_rebuild.adapters.persistence.sqlite_accounts import AccountRepository
from codol_rebuild.application.accounts.service import AccountSession
from test_broadcast import server_message
from test_purchase import Memory, Codec


class BroadcastConsumptionTests(unittest.TestCase):
    def setUp(self):
        workspace = Path(__file__).resolve().parents[2] / 'verification/client/workspaces'
        workspace.mkdir(parents=True, exist_ok=True)
        directory = tempfile.TemporaryDirectory(dir=workspace)
        self.addCleanup(directory.cleanup)
        self.repository = AccountRepository(Path(directory.name) / 'account.db')
        self.repository.identity()
        self.initial_state = {'version': 1, 'owned_ids': [545, 50003],
                              'quantities': {'50003': 3}, 'gp': 1000, 'cp': 100}
        self.profile_blobs = {'public': b'public', 'secure': b'secure'}
        self.repository.save('local', 0, self.profile_blobs, Codec.identities,
                             initialization=self.initial_state)
        self.account_session = AccountSession(self.repository, Codec())
        self.account_session.account = self.repository.load('local', Codec.identities)
        self.memory = Memory()
        self.pool_address = 0x100000
        self.record_address = self.pool_address + profile.STRIDE + 0x40
        self.memory.write(self.pool_address - 16, b'LOCAL-INVENTORY!')
        self.memory.write(self.pool_address + profile.CAPACITY * profile.STRIDE, b'LOCAL-INVENTORY!')
        for item_index, item_id in enumerate((545, 50003)):
            slot_address = self.pool_address + item_index * profile.STRIDE
            self.memory.word(slot_address, 0xD59C78)
            self.memory.write(slot_address + 0x40, struct.pack('<III', item_id, 3 if item_id == 50003 else 1, 7))
        self.memory.word(profile.layout.INVENTORY_COUNT, 2)
        self.process = Mock(read=self.memory.read, write=self.memory.write, main_thread_id=1)
        self.process.context.return_value = (100, bytearray(256))
        self.profile_bridge = LocalProfileBridge(self.process, Mock(), {}, Mock())
        self.profile_bridge._service = self.account_session
        self.profile_bridge._ready = True
        self.profile_bridge._inventory = {'pool': hex(self.pool_address), 'items': 2}
        self.port = Mock(poll_sent=Mock(return_value=None), poll_message=Mock(return_value=None),
                         available=Mock(return_value=True))
        self.bridge = broadcast.BroadcastBridge(
            self.process, self.port, Mock(), self.profile_bridge.consume_item)
        self.bridge.state = 0x21000000
        self.bridge.player_id = 'pl_sender'
        self.bridge.traps = {0x20000000: 'poll'}

    def poll(self):
        self.bridge.handle(Mock(tid=1), 0x20000000)

    def remaining(self):
        return purchase.u32(self.memory.read, self.record_address + 4)

    def test_consecutive_confirmations_persist_and_last_item_clears_slot(self):
        other_record = self.memory.read(self.pool_address, profile.STRIDE)
        for remaining_count in (2, 1, 0):
            self.port.poll_sent.return_value = {'messageId': str(remaining_count), 'speakerType': 'large'}
            self.poll()
            reopened = AccountRepository(self.repository.path).load('local', Codec.identities)
            self.assertEqual(self.remaining(), remaining_count)
            self.assertEqual(reopened['profiles'], self.profile_blobs)
            self.assertEqual(reopened['initialization']['gp'], 1000)
            self.assertEqual(reopened['initialization']['cp'], 100)
            self.assertEqual(50003 in reopened['initialization']['owned_ids'], remaining_count > 0)
            self.assertEqual(reopened['initialization']['quantities'],
                             {'50003': remaining_count} if remaining_count else {})
            self.assertEqual(self.memory.read(self.pool_address, profile.STRIDE), other_record)
            self.port.poll_sent.return_value = None
            self.poll()
            self.assertEqual(self.remaining(), remaining_count)
        self.assertEqual(self.memory.read(self.record_address, 12), bytes(12))

    def test_push_before_after_and_duplicate_push_never_consume(self):
        for sender_id in ('pl_sender', 'pl_other'):
            message = {**server_message()['payload'], 'senderId': sender_id}
            self.port.poll_message.return_value = message
            self.poll()
        self.assertEqual(self.remaining(), 3)
        self.port.poll_sent.return_value = {'messageId': 'broadcast_one', 'speakerType': 'large'}
        self.poll()
        self.assertEqual(self.remaining(), 2)
        self.port.poll_sent.return_value = None
        self.poll()
        self.poll()
        self.assertEqual(self.remaining(), 2)

    def test_http_failure_never_consumes_or_signals_success(self):
        self.port.poll_sent.side_effect = OSError('HTTP failed')
        with self.assertRaises(OSError):
            self.poll()
        self.assertEqual(self.remaining(), 3)
        self.assertEqual(self.memory.read(self.bridge.state, 4), bytes(4))
        self.assertEqual(self.repository.load('local', Codec.identities)['initialization'], self.initial_state)

    def test_commit_failure_rolls_back_and_does_not_signal_success(self):
        before_account = deepcopy(self.account_session.account)
        before_record = self.memory.read(self.record_address, 12)
        with closing(sqlite3.connect(self.repository.path)) as database, database:
            database.execute("CREATE TRIGGER fail_consumption BEFORE UPDATE ON account_initialization "
                             "BEGIN SELECT RAISE(ABORT,'injected failure'); END")
        self.port.poll_sent.return_value = {'messageId': 'broadcast_one', 'speakerType': 'large'}
        with self.assertRaises(sqlite3.Error):
            self.poll()
        self.assertEqual(self.memory.read(self.record_address, 12), before_record)
        self.assertEqual(self.account_session.account, before_account)
        self.assertEqual(self.repository.load('local', Codec.identities), before_account)
        self.assertEqual(self.memory.read(self.bridge.state, 4), bytes(4))

    def test_success_flag_is_written_after_database_and_inventory_commit(self):
        def write_with_order_check(address, data):
            if address == self.bridge.state and data == struct.pack('<I', 1):
                self.assertEqual(self.remaining(), 2)
                self.assertEqual(self.repository.load('local', Codec.identities)
                                 ['initialization']['quantities']['50003'], 2)
            self.memory.write(address, data)
        self.process.write = write_with_order_check
        self.port.poll_sent.return_value = {'messageId': 'broadcast_one', 'speakerType': 'large'}
        self.poll()
        self.assertEqual(self.memory.read(self.bridge.state, 4), struct.pack('<I', 1))

    def test_small_confirmation_consumes_small_item_and_keeps_large_inventory(self):
        small_slot = self.pool_address + 2 * profile.STRIDE
        self.memory.word(small_slot, 0xD59C78)
        self.memory.write(small_slot + 0x40, struct.pack('<III', 50002, 2, 7))
        self.memory.word(profile.layout.INVENTORY_COUNT, 3)
        self.profile_bridge._inventory['items'] = 3
        initialization = deepcopy(self.initial_state)
        initialization['owned_ids'].append(50002)
        initialization['quantities']['50002'] = 2
        self.repository.save('local', self.account_session.account['revision'],
                             self.profile_blobs, Codec.identities, initialization=initialization)
        self.account_session.account = self.repository.load('local', Codec.identities)
        self.port.poll_sent.return_value = {'messageId': 'small_one', 'speakerType': 'small'}
        self.port.poll_message.return_value = server_message('large')['payload']
        self.poll()
        self.assertEqual(self.remaining(), 3)
        self.assertEqual(purchase.u32(self.memory.read, small_slot + 0x44), 1)
        saved = self.repository.load('local', Codec.identities)
        self.assertEqual(saved['initialization']['quantities'], {'50003': 3, '50002': 1})
        self.assertEqual(purchase.u32(self.memory.read, self.bridge.state + broadcast.SENT_SPEAKER_TYPE), 1)
        self.port.poll_sent.return_value = None
        self.port.poll_message.return_value = server_message('small')['payload']
        self.poll()
        self.assertEqual(purchase.u32(self.memory.read, small_slot + 0x44), 1)

    def test_missing_native_item_rejects_without_database_change(self):
        self.memory.write(self.record_address, bytes(12))
        self.port.poll_sent.return_value = {'messageId': 'broadcast_one', 'speakerType': 'large'}
        with self.assertRaisesRegex(ValueError, '库存中找不到消耗物品'):
            self.poll()
        self.assertEqual(self.memory.read(self.record_address, 12), bytes(12))
        self.assertEqual(self.memory.read(self.bridge.state, 4), bytes(4))
        self.assertEqual(self.repository.load('local', Codec.identities)['initialization'], self.initial_state)


if __name__ == '__main__':
    unittest.main()
