"""大小喇叭发送确认的仓库发布与原生通知回归。"""
import struct
import unittest
from unittest.mock import Mock

from codol_rebuild.adapters.game.v1649370.accounts import profile
from codol_rebuild.adapters.game.v1649370.accounts.profile_bridge import LocalProfileBridge
from codol_rebuild.adapters.game.v1649370.chat import broadcast
from test_broadcast import server_message
from test_purchase import Memory


class BroadcastConsumptionTests(unittest.TestCase):
    def setUp(self):
        self.memory = Memory()
        self.pool = 0x100000
        self.memory.write(self.pool - 16, b'LOCAL-INVENTORY!')
        self.memory.write(self.pool + profile.CAPACITY * profile.STRIDE, b'LOCAL-INVENTORY!')
        self.small_slot = self.pool + 0x40
        self.large_slot = self.pool + profile.STRIDE + 0x40
        for index, (item_id, quantity) in enumerate(((50002, 2), (50003, 3))):
            address = self.pool + index * profile.STRIDE
            self.memory.word(address, 0xD59C78)
            self.memory.write(address + 0x40, struct.pack('<III', item_id, quantity, 0))
        self.memory.word(profile.layout.INVENTORY_COUNT, 2)
        profile.publish_currency(self.memory.read, self.memory.write, {'gp': 50, 'cp': 75})
        self.process = Mock(read=self.memory.read, write=self.memory.write, main_thread_id=1)
        self.process.context.return_value = (100, bytearray(256))
        self.profiles = LocalProfileBridge(self.process, Mock(), {}, Mock())
        self.profiles._ready = True
        self.profiles._inventory = {'pool': hex(self.pool), 'items': 2, 'weapon_experience': 1}
        self.profiles._catalog = {item_id: {'type_bytes': [0, 8]} for item_id in (50002, 50003)}
        self.profiles._confirmed_inventory = self.snapshot(1, 2, 3)
        self.port = Mock()
        self.port.poll_sent.return_value = None
        self.port.poll_message.return_value = None
        self.port.available.return_value = True
        self.bridge = broadcast.BroadcastBridge(self.process, self.port, self.profiles.apply_inventory)
        self.bridge.state = 0x21000000
        self.bridge.player_id = 'pl_sender'
        self.poll_address = 0x20000000
        self.bridge.traps = {self.poll_address: 'poll'}

    @staticmethod
    def snapshot(revision, small_quantity, large_quantity):
        items = [{'itemId': item_id, 'quantity': quantity}
                 for item_id, quantity in ((50002, small_quantity), (50003, large_quantity)) if quantity]
        return {'revision': revision, 'items': items, 'gp': 50, 'cp': 75}

    def confirmed(self, snapshot, speaker_type='large'):
        return {'messageId': 'broadcast_one', 'speakerType': speaker_type, 'inventory': snapshot}

    def poll(self):
        self.bridge.handle(Mock(tid=1), self.poll_address)

    def word(self, address):
        return struct.unpack('<I', self.memory.read(address, 4))[0]

    def result_field(self, offset):
        return self.word(self.bridge.state + offset)

    def test_confirmation_applies_snapshot_before_success_and_ignores_late_inventory(self):
        final_snapshot = self.snapshot(3, 2, 0)
        success_notifications = []

        def record_write(address, data):
            if address == self.bridge.state + broadcast.SEND_COMPLETE and data == struct.pack('<I', 1):
                success_notifications.append((self.profiles.log['online_inventory_revision'],
                                              self.result_field(broadcast.SEND_SUCCESS),
                                              self.word(self.large_slot)))
            self.memory.write(address, data)

        self.process.write = record_write
        for snapshot in (final_snapshot, final_snapshot, self.snapshot(2, 2, 2)):
            self.port.poll_sent.return_value = self.confirmed(snapshot)
            self.poll()
        self.assertEqual(success_notifications, [(3, 1, 0)] * 3)
        self.assertEqual(self.word(self.small_slot + 4), 2)
        self.assertEqual(self.memory.read(self.large_slot, 12), bytes(12))
        self.assertEqual(self.profiles._confirmed_inventory, final_snapshot)

    def test_duplicate_received_messages_do_not_consume_a_speaker(self):
        self.port.poll_message.return_value = server_message('small')['payload']
        self.poll()
        self.poll()
        self.assertEqual(self.word(self.small_slot + 4), 2)
        self.assertEqual(self.result_field(broadcast.MESSAGE_READY), 1)
        self.assertEqual(self.result_field(broadcast.SEND_COMPLETE), 0)
        self.assertEqual(self.profiles._confirmed_inventory['revision'], 1)

    def test_small_confirmation_and_large_incoming_message_same_frame(self):
        self.port.poll_message.return_value = server_message('large')['payload']
        self.port.poll_sent.return_value = self.confirmed(self.snapshot(2, 1, 3), 'small')
        self.poll()
        self.assertEqual(self.word(self.small_slot + 4), 1)
        self.assertEqual(self.word(self.large_slot + 4), 3)
        self.assertEqual(self.result_field(broadcast.MESSAGE_READY), 1)
        self.assertEqual(self.result_field(broadcast.SENT_SPEAKER_TYPE), 1)
        self.assertEqual(self.result_field(broadcast.SEND_SUCCESS), 1)
        self.assertEqual(self.result_field(broadcast.SEND_COMPLETE), 1)

    def test_rejected_small_send_and_incoming_message_same_frame(self):
        self.memory.word(self.bridge.state + broadcast.SENT_SPEAKER_TYPE, 1)
        self.port.poll_message.return_value = server_message('large')['payload']
        self.port.poll_sent.side_effect = OSError('喇叭数量不足')
        self.poll()
        self.assertEqual(self.result_field(broadcast.SEND_COMPLETE), 1)
        self.assertEqual(self.result_field(broadcast.SEND_SUCCESS), 0)
        self.assertEqual(self.result_field(broadcast.SENT_SPEAKER_TYPE), 1)
        self.assertEqual(self.result_field(broadcast.MESSAGE_READY), 1)
        self.assertEqual(self.word(self.small_slot + 4), 2)
        error_message = self.memory.read(self.bridge.state + broadcast.CAPACITY, 128).split(b'\0')[0]
        self.assertEqual(error_message.decode('utf-8'), '喇叭数量不足')

    def test_native_publication_failure_returns_error_without_advancing_revision(self):
        self.memory.write(self.pool - 16, bytes(16))
        self.port.poll_sent.return_value = self.confirmed(self.snapshot(2, 2, 2))
        self.poll()
        self.assertEqual(self.word(self.large_slot + 4), 3)
        self.assertEqual(self.profiles._confirmed_inventory['revision'], 1)
        self.assertEqual(self.result_field(broadcast.SEND_SUCCESS), 0)
        self.assertEqual(self.result_field(broadcast.SEND_COMPLETE), 1)
        error_message = self.memory.read(self.bridge.state + broadcast.CAPACITY, 128).split(b'\0')[0]
        self.assertIn('原生库存', error_message.decode('utf-8'))
