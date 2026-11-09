"""单箱随机候选、保底优先级及原子持久化回归。"""
import unittest
import copy
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import patch

from codol_rebuild.application.accounts.supply_drop import draw_reward


class SupplyDropTests(unittest.TestCase):
    def setUp(self):
        self.state = {'version': 1, 'owned_ids': [60216], 'gp': 0, 'cp': 0,
                      'quantities': {'60216': 5}}
        self.pool = [{'item_id': 100 + tier, 'tier': tier, 'stackable': False}
                     for tier in range(1, 5)]

    def test_every_unowned_item_has_one_selection_index(self):
        for selected_index in range(4):
            with patch('codol_rebuild.application.accounts.supply_drop.secrets.randbelow',
                       return_value=selected_index) as random_choice:
                next_state, reward = draw_reward(self.state, self.pool, 60216)
            random_choice.assert_called_once_with(4)
            self.assertEqual(reward['item_id'], 101 + selected_index)
            self.assertEqual(next_state['quantities']['60216'], 4)
        self.assertEqual(self.state['owned_ids'], [60216])

    def test_owned_items_excluded(self):
        self.state['owned_ids'] += [101, 102, 103]
        next_state, reward = draw_reward(self.state, self.pool, 60216)
        self.assertEqual(reward['tier'], 4)
        self.assertEqual(next_state['supply_drop_progress'], {'since_third': 0, 'since_fourth': 0})

    def test_tenth_guarantee_and_hundredth_priority(self):
        for fourth_count, expected_tier in ((12, 3), (99, 4)):
            self.state['supply_drop_progress'] = {'since_third': 9, 'since_fourth': fourth_count}
            next_state, reward = draw_reward(self.state, self.pool, 60216)
            self.assertEqual(reward['tier'], expected_tier)
            self.assertEqual(next_state['supply_drop_progress']['since_third'], 0)

    def test_guarantee_falls_to_next_available_lower_tier(self):
        self.state['owned_ids'] += [103, 104]
        self.state['supply_drop_progress'] = {'since_third': 9, 'since_fourth': 99}
        next_state, reward = draw_reward(self.state, self.pool, 60216)
        self.assertEqual(reward['tier'], 2)
        self.assertEqual(next_state['supply_drop_progress'], {'since_third': 0, 'since_fourth': 0})

    def test_exhausted_pool_does_not_change_input(self):
        self.state['owned_ids'] += [101, 102, 103, 104]
        with self.assertRaises(ValueError):
            draw_reward(self.state, self.pool, 60216)
        self.assertEqual(self.state['quantities']['60216'], 5)

    def test_progress_and_reward_survive_reload_and_failed_commit(self):
        from codol_rebuild.adapters.persistence.sqlite_accounts import AccountRepository
        from codol_rebuild.application.accounts.service import AccountSession
        from codol_rebuild.application.accounts.contracts import RevisionConflict
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            repository = AccountRepository(Path(directory) / 'account.sqlite3')
            repository.identity()
            identities = {'public': {'test': 1}, 'secure': {'test': 2}}
            repository.save('local', 0, {'public': b'public', 'secure': b'secure'}, identities,
                            initialization=self.state)
            session = AccountSession(repository, SimpleNamespace(identities=identities))
            session.account = repository.load('local', identities)
            with patch('codol_rebuild.application.accounts.supply_drop.secrets.randbelow', return_value=0):
                plan = session.prepare_supply_drop(self.pool, 60216)
            session.commit_supply_drop(plan)
            restored = repository.load('local', identities)
            self.assertEqual(restored, session.account)
            self.assertEqual(restored['initialization']['supply_drop_progress'],
                             {'since_third': 1, 'since_fourth': 1})
            with self.assertRaises(RevisionConflict):
                session.commit_supply_drop(plan)
            before = copy.deepcopy(session.account)
            plan = session.prepare_supply_drop(self.pool, 60216)
            with closing(sqlite3.connect(repository.path)) as database, database:
                database.execute("CREATE TRIGGER reject_reward BEFORE UPDATE ON account_initialization BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
            with self.assertRaises(sqlite3.Error):
                session.commit_supply_drop(plan)
            self.assertEqual(session.account, before)
            self.assertEqual(repository.load('local', identities), before)

    def test_native_contract_and_adapter_instruction_boundaries(self):
        import capstone
        import pefile
        from codol_rebuild.adapters.game.v1649370.accounts import supply_drop
        image = pefile.PE(str(Path(__file__).resolve().parents[1] / 'bin/v1649370/codol1649370-offline.exe'))
        for address, expected in supply_drop.CONTRACT.items():
            self.assertEqual(image.get_data(address - image.OPTIONAL_HEADER.ImageBase, len(expected)), expected)
        entry = 0x10000000
        adapter = supply_drop.build_adapter(entry, 0x11000000, 0x12000000)
        instructions = list(capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32).disasm(adapter['code'], entry))
        self.assertEqual(sum(instruction.size for instruction in instructions), len(adapter['code']))
        addresses = {instruction.address for instruction in instructions}
        for instruction in instructions:
            if instruction.mnemonic.startswith('j') and instruction.op_str.startswith('0x'):
                target = int(instruction.op_str, 16)
                self.assertTrue(target in addresses or target == supply_drop.HANDLER)
        self.assertEqual(set(adapter['traps'].values()), {'request', 'commit', 'completed'})

    def test_last_crate_slot_reuse_rolls_back_on_database_failure(self):
        from unittest.mock import Mock
        import struct
        from codol_rebuild.adapters.game.v1649370.accounts.profile_bridge import LocalProfileBridge
        from codol_rebuild.adapters.game.v1649370.accounts import purchase
        from test_purchase import Memory
        memory, pool_address = Memory(), 0x100000
        memory.write(pool_address - 16, b'LOCAL-INVENTORY!')
        memory.write(pool_address + purchase.CAPACITY * purchase.STRIDE, b'LOCAL-INVENTORY!')
        memory.word(pool_address, 0xD59C78)
        memory.write(pool_address + 0x40, struct.pack('<III', 60216, 1, 0))
        memory.word(pool_address + 0x90, 0xffffffff)
        memory.word(pool_address + 0x98, 0xffffffff)
        memory.word(0x82C4A8C, 1)
        purchase.publish_currency(memory.read, memory.write, self.state)
        process = Mock(read=memory.read, write=memory.write)
        bridge = LocalProfileBridge(process, None, {}, lambda value: None)
        bridge._inventory = {'pool': hex(pool_address), 'items': 1, 'weapon_experience': 99}
        bridge._catalog = {101: {'id': 101, 'type_bytes': [0, 0]}}
        bridge._service = Mock()
        bridge._service.commit_supply_drop.side_effect = ValueError('保存失败')
        self.state['quantities']['60216'] = 1
        next_state, reward = draw_reward(self.state, self.pool[:1], 60216)
        plan = {'state': next_state, 'reward': reward, 'crate_item_id': 60216}
        with self.assertRaises(ValueError):
            bridge.commit_supply_drop(plan)
        self.assertEqual(memory.read(pool_address + 0x40, 12), struct.pack('<III', 60216, 1, 0))
        self.assertEqual(bridge._inventory['items'], 1)
        bridge._service.commit_supply_drop.side_effect = None
        bridge._service.commit_supply_drop.return_value = next_state
        bridge.commit_supply_drop(plan)
        self.assertEqual(memory.read(pool_address + 0x40, 12), struct.pack('<III', 101, 1, 99))
        self.assertEqual(bridge._inventory['items'], 1)


if __name__ == '__main__':
    unittest.main()
