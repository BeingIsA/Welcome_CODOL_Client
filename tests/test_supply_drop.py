"""单箱原生结果编码及异步任务通知回归。"""
from pathlib import Path
import struct
import unittest
from unittest.mock import Mock

from codol_rebuild.adapters.game.v1649370.accounts import supply_drop
from codol_rebuild.application.errors import OnlineError
from test_purchase import Memory


class SupplyDropTests(unittest.TestCase):
    def supply_drop_request(self, inventory_session):
        memory = Memory()
        frame = 0x13000000
        parameters = frame + 1024
        values = parameters + 128
        context = bytearray(208)
        for offset in (180, 196):
            struct.pack_into('<I', context, offset, frame)
        memory.word(frame + 40, parameters)
        memory.word(parameters + 20, 2)
        memory.word(parameters + 16, values)
        memory.word(values + 12, 3)
        memory.word(values + 16, 60216)
        memory.word(values + 44, 3)
        memory.word(values + 48, 1)
        process = Mock(read=memory.read, write=memory.write, main_thread_id=4)
        process.context.return_value = (5, context)
        bridge = supply_drop.SupplyDropBridge(process, Mock(), Mock(), (ValueError,), inventory_session)
        bridge.state = frame + 4096
        bridge.traps = {11: 'request', 12: 'poll', 13: 'reward_ready'}
        bridge.handle(Mock(tid=4), 11)
        return memory, bridge

    def test_supply_drop_waits_then_applies_only_confirmed_inventory(self):
        service = Mock()
        memory, bridge = self.supply_drop_request(service)
        service.begin_supply_drop.assert_called_once_with(60216)
        snapshot = {'revision': 4, 'items': [], 'gp': 10, 'cp': 5}
        service.poll.side_effect = [None, {'inventory': snapshot, 'reward': {
            'product_ids': [123], 'contents': [{'item_id': 55, 'quantity': 3, 'quantity_delta': 1}]}}]
        event = Mock(tid=4)
        bridge.handle(event, 12)
        bridge.profiles.apply_inventory.assert_not_called()
        self.assertEqual(supply_drop.u32(memory.read, bridge.state), 0)
        bridge.handle(event, 12)
        bridge.profiles.apply_inventory.assert_called_once_with(snapshot)
        self.assertEqual(supply_drop.u32(memory.read, bridge.state), 1)
        bridge.handle(event, 13)
        bridge.profiles.apply_inventory.assert_called_once_with(snapshot)
        service.begin_supply_drop.assert_called_once_with(60216)

    def test_offline_and_server_failure_publish_a_failed_task(self):
        memory, bridge = self.supply_drop_request(None)
        self.assertEqual(supply_drop.u32(memory.read, bridge.state), 0)
        self.assertIn('离线'.encode('utf-8'), memory.read(bridge.state + supply_drop.MESSAGE, 80))
        service = Mock()
        memory, bridge = self.supply_drop_request(service)
        service.poll.side_effect = OnlineError('MISSING_ITEM', '没有军火箱')
        bridge.handle(Mock(tid=4), 12)
        self.assertEqual(supply_drop.u32(memory.read, bridge.state), 0)
        self.assertEqual(supply_drop.u32(memory.read, bridge.state + supply_drop.ONLINE_TASK_STATE), 2)
        bridge.profiles.apply_inventory.assert_not_called()

    def test_three_largest_reward_bundles_fit_in_one_native_result(self):
        contents = [{'item_id': item_id, 'quantity': 1, 'quantity_delta': 1} for item_id in range(1, 60)]
        payload = supply_drop.encode_result(0x11000000, 60216, {'product_ids': [34, 840, 14121], 'contents': contents})
        self.assertEqual(struct.unpack_from('<I', payload, 4)[0], 3)
        self.assertLessEqual(len(payload), supply_drop.RESULT_CAPACITY)
        self.assertEqual(struct.unpack_from('<I', payload, len(payload) - 16)[0], 60216)

    def test_outfit_and_single_item_keep_draw_order(self):
        contents = [
            {'item_id': 553, 'quantity': 1, 'quantity_delta': 0},
            {'item_id': 556, 'quantity': 1, 'quantity_delta': 1},
            {'item_id': 55, 'quantity': 3, 'quantity_delta': 2},
        ]
        payload = supply_drop.encode_result(0x11000000, 60216, {'product_ids': [10012, 2], 'contents': contents})
        self.assertEqual(struct.unpack_from('<I', payload, 4)[0], 2)
        for index, expected in enumerate(((0, 10012, 1, 1), (0, 2, 1, 1))):
            start = supply_drop.FIELDS + index * len(supply_drop.REWARD_FIELDS) * 36
            self.assertEqual(tuple(struct.unpack_from('<I', payload, start + field * 36 + 20)[0]
                                   for field in (0, 1, 7, 8)), expected)

    def test_item_pool_displays_items_with_granted_quantities(self):
        import struct
        from codol_rebuild.adapters.game.v1649370.accounts import supply_drop
        reward = {'product_ids': [0], 'contents': [{'item_id': 101, 'quantity': 1, 'quantity_delta': 1},
                               {'item_id': 102, 'quantity': 3, 'quantity_delta': 3}]}
        payload = supply_drop.encode_result(0x11000000, 60216, reward)
        self.assertEqual(struct.unpack_from('<I', payload, 4)[0], 2)
        for content_index, item_id in enumerate((101, 102)):
            field_offset = supply_drop.FIELDS + content_index * len(supply_drop.REWARD_FIELDS) * 36
            self.assertEqual(struct.unpack_from('<I', payload, field_offset + 20)[0], item_id)
            self.assertEqual(struct.unpack_from('<I', payload, field_offset + 36 + 20)[0], 0)
            expected_quantity = 1 if item_id == 101 else 3
            for field_name in ('quantity', 'quantityDelta'):
                quantity_offset = supply_drop.REWARD_FIELDS.index(field_name) * 36
                self.assertEqual(struct.unpack_from('<I', payload, field_offset + quantity_offset + 20)[0],
                                 expected_quantity)
        self.assertLessEqual(len(payload), supply_drop.CAPACITY)

    def test_native_contract_and_adapter_instruction_boundaries(self):
        import capstone
        import pefile
        from codol_rebuild.adapters.game.v1649370.accounts import supply_drop
        image = pefile.PE(str(Path(__file__).resolve().parents[1] / 'bin/v1649370/codol1649370-offline.exe'))
        for address, expected in supply_drop.CONTRACT.items():
            self.assertEqual(image.get_data(address - image.OPTIONAL_HEADER.ImageBase, len(expected)), expected)
        entry = 0x10000000
        adapter = supply_drop.build_adapter(entry, 0x11000000, 0x12000000, deferred=True)
        instructions = list(capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32).disasm(adapter['code'], entry))
        self.assertEqual(sum(instruction.size for instruction in instructions), len(adapter['code']))
        addresses = {instruction.address for instruction in instructions}
        for instruction in instructions:
            if instruction.mnemonic.startswith('j') and instruction.op_str.startswith('0x'):
                target = int(instruction.op_str, 16)
                self.assertTrue(target in addresses or target == supply_drop.HANDLER)
        self.assertEqual(set(adapter['traps'].values()), {'request', 'poll', 'reward_ready', 'completed'})
