"""验证新档授权、余额保存与旧档兼容，不接入游戏运行目录。"""
import copy
import csv
import json
import re
from pathlib import Path
import struct
import tempfile
import unittest

from codol_rebuild.application.accounts.policy import new_account_policy
from codol_rebuild.application.accounts.service import AccountSession
from codol_rebuild.adapters.persistence.sqlite_accounts import AccountRepository, RevisionConflict
from codol_rebuild.adapters.game.v1649370.accounts.profile import publish_currency, currency_snapshot, populate_inventory, CAPACITY, STRIDE

ROOT = Path(__file__).resolve().parents[2]


class InitializationTests(unittest.TestCase):
    def test_initial_perks_and_wildcards_follow_native_id_encoding(self):
        """使用终版默认配装，验证技能读回及已有配装不被二次加一。"""
        from codol_rebuild.adapters.game.v1649370.accounts.profile_codec import AccountCodec, LOADOUT_DDL_FIELDS
        from codol_rebuild.adapters.game.profile_format.fields import Fields
        captured = json.loads((ROOT/'research/account-initialization/capture/market-memory.json').read_text(encoding='utf-8'))
        schemas = {kind: json.loads(Path(captured['schemas'][label]['schema']).read_text(encoding='utf-8'))
                   for kind, label in [('public', 'playerdata'), ('secure', 'playersecuredata')]}
        codec = AccountCodec(schemas, captured['tables'], captured['catalog'])
        policy = new_account_policy(codec.initialization_data())
        profiles, _ = codec.initialize({'profiles': {}, 'uid': 1}, policy)
        fields = Fields(schemas['public'])
        for loadout_index, loadout in enumerate(policy['loadouts']):
            for field_name, read_value in LOADOUT_DDL_FIELDS:
                item_id = read_value(loadout)
                stored = fields.read(profiles['public'], f'weaponClasses.{loadout_index}.{field_name}')
                if field_name.startswith(('perks.', 'wildcards.')):
                    self.assertEqual(stored - 1 if stored > 0 else 0, item_id)
                else:
                    self.assertEqual(stored, item_id)
        self.assertEqual(policy['loadouts'][0]['perks'][2], 60137)
        self.assertEqual(fields.read(profiles['public'], 'weaponClasses.0.perks.2'), 60138)
        self.assertEqual(fields.read(profiles['public'], 'weaponClasses.0.wildcards.0'), 0)
        self.assertEqual(fields.read(profiles['public'], 'characterClasses.0.slots.0'), 545)
        preserved, _ = codec.initialize({'profiles': profiles, 'uid': 1}, policy)
        self.assertEqual(preserved['public'], profiles['public'])

    def test_camo_grants_and_existing_archive_preserved(self):
        from codol_rebuild.adapters.game.v1649370.accounts.profile_codec import AccountCodec
        captured = json.loads((ROOT/'research/account-initialization/capture/market-memory.json').read_text(encoding='utf-8'))
        with (ROOT/'client/resources/迷彩表.csv').open(encoding='utf-8-sig', newline='') as resource_file:
            expected_ids = sorted(int(item_row['物品ID']) for item_row in csv.DictReader(resource_file))
        self.assertEqual(len(expected_ids), 2860)
        tables = {**captured['tables'], 'mp/coStartingLoadoutSetting.csv': []}
        schemas = {kind: json.loads(Path(captured['schemas'][label]['schema']).read_text(encoding='utf-8'))
                   for kind, label in [('public', 'playerdata'), ('secure', 'playersecuredata')]}
        codec = AccountCodec(schemas, tables, captured['catalog'])
        folder = ROOT/'verification/client/workspaces'
        with tempfile.TemporaryDirectory(dir=folder) as tmp:
            repo = AccountRepository(Path(tmp)/'honor.sqlite3')
            session = AccountSession(repo, codec)
            created = session.initialize()
            self.assertTrue(created['new_account'])
            self.assertEqual(created['initialization']['gp'], 2000000000)
            self.assertEqual(created['initialization']['cp'], 100000)
            selected_policy = new_account_policy(codec.initialization_data())
            self.assertTrue(selected_policy['fill_unlock_flags'])
            self.assertEqual(selected_policy['unlock_threshold'], 32767)
            self.assertEqual(created['progression'], selected_policy['progression'])
            inventory_memory = bytearray(CAPACITY * STRIDE)
            for inventory_index in range(CAPACITY):
                struct.pack_into('<I', inventory_memory, inventory_index * STRIDE, 0xD59C78)
            def read_inventory(address, size):
                return bytes(inventory_memory[address:address + size])
            def write_inventory(address, content):
                inventory_memory[address:address + len(content)] = content
            inventory_report = populate_inventory(read_inventory, write_inventory, 0,
                captured['catalog'], 0, created['initialization']['owned_ids'])
            self.assertEqual(inventory_report['items'], len(expected_ids))
            inventory_items = [struct.unpack_from('<II', inventory_memory, inventory_index * STRIDE + 0x40)
                               for inventory_index in range(len(expected_ids))]
            self.assertEqual(sorted(inventory_items), [(item_id, 1) for item_id in expected_ids])

            self.assertEqual(created['initialization']['owned_ids'], expected_ids)
            self.assertEqual(codec.validate_snapshot(created['profiles']), 545)
            # 现有档案启动不重新执行新档授权策略；即使新策略不同也不得迁移。
            def forbidden(*args):
                raise AssertionError('Existing archive must not regenerate entitlements')
            codec.initialization_data = forbidden
            reopened = AccountSession(repo, codec).initialize()
            self.assertFalse(reopened['new_account'])
            self.assertEqual(reopened['initialization'], created['initialization'])
            self.assertEqual(reopened['profiles'], created['profiles'])
            from codol_rebuild.adapters.game.profile_format.fields import Fields
            fields = Fields(schemas['public'])
            for field, value in (('cardIcon', 30000), ('cardBackground', 30060),
                                 ('cardTitle', 30061), ('cardNameplate', 0), ('cardFrame', 0)):
                self.assertEqual(fields.read(created['profiles']['public'], field), value)
            self.assertEqual(fields.read(created['profiles']['public'], 'loadoutVersion'), 3)
            old = bytearray(created['profiles']['public'])
            fields.write(old, 'loadoutVersion', 0)
            old_profiles = {**created['profiles'], 'public': bytes(old)}
            revision = repo.save('local', reopened['revision'], old_profiles, codec.identities,
                                 initialization=created['initialization'])
            migrated = AccountSession(repo, codec).initialize()
            self.assertEqual(migrated['revision'], revision+1)
            self.assertEqual(migrated['profiles'], created['profiles'])
            self.assertEqual(migrated['initialization'], created['initialization'])
            self.assertEqual(AccountSession(repo, codec).initialize()['revision'], migrated['revision'])
            # 老账号既保留未选择的零值，也保留非默认选择，启动不补写装扮。
            existing = bytearray(migrated['profiles']['public'])
            fields.write(existing, 'cardIcon', 0)
            fields.write(existing, 'cardBackground', 0)
            fields.write(existing, 'cardTitle', 12345)
            existing_profiles = {**migrated['profiles'], 'public': bytes(existing)}
            revision = repo.save('local', migrated['revision'], existing_profiles, codec.identities,
                                 initialization=created['initialization'])
            reopened = AccountSession(repo, codec).initialize()
            self.assertEqual(reopened['profiles'], existing_profiles)
            self.assertEqual(reopened['revision'], revision)
            # 无初始化标记的历史档案同样不能因 initialize 调用而补写默认装扮。
            legacy, _ = codec.initialize({'profiles': existing_profiles, 'uid': 1},
                {'progression': {'rank': 70, 'rank_version': 0, 'prestige': 4, 'experience': 0},
                 'fill_unlock_flags': True, 'unlock_threshold': 32767})
            self.assertEqual(legacy['public'], existing_profiles['public'])

    def test_native_contract_matches_image_and_rejects_mismatch(self):
        import pefile
        from codol_rebuild.adapters.game.v1649370.accounts.profile import validate_account_native_contract
        image = pefile.PE(str(ROOT/'client/bin/v1649370/codol1649370-offline.exe'))
        validate_account_native_contract(lambda a, n: image.get_data(a-0x400000, n))
        with self.assertRaises(ValueError):
            validate_account_native_contract(lambda a, n: bytes(n))

    def test_new_account_uses_explicit_grants(self):
        from codol_rebuild.application.accounts.policy import validate_initialization
        data = {'progression': {'rank': 70}, 'default_character': 545, 'starting_loadouts': [], 'initial_item_ids': [570, 2]}
        policy = new_account_policy(data)
        self.assertEqual(policy['initialization'], {'version': 1, 'owned_ids': [2, 570], 'gp': 2000000000, 'cp': 100000})
        self.assertEqual(validate_initialization(policy['initialization']), policy['initialization'])
        self.assertTrue(policy['fill_unlock_flags'])
        self.assertEqual(policy['unlock_threshold'], 32767)

    def test_policy_layer_free_of_native_format(self):
        # 架构断言：应用层策略不得出现 DDL 字段路径或原生分类数字。
        source = Path('codol_rebuild/application/accounts/policy.py')
        for parent in (Path(__file__).resolve().parents[:4]):
            candidate = parent/'client/src'/source
            if candidate.exists():
                text = candidate.read_text(encoding='utf-8')
                break
        else:
            self.fail('policy.py not found')
        # validate_initialization 属持久化授权状态校验，按方案保留预留 ID 检查。
        strategy = text.split('def validate_initialization')[0]
        for token in ('primaryBase', 'primaryAttach', 'secondaryAttach', 'perks.', 'wildcards.',
                      'kind', 'gp_cp_sale', '545'):
            self.assertNotIn(token, strategy)
        for number in ('15400', '20000', '545'):
            self.assertIsNone(re.search(rf'(?<!\d){number}(?!\d)', strategy))

    def test_currency_preserves_native_objects_and_other_currencies(self):
        memory = bytearray(b'\xA5' * (11*32))
        for offset in range(0, len(memory), 32):
            memory[offset] = 0
        memory[64] = 7
        before = bytes(memory)
        def read(a, n):
            return bytes(memory[a-0xC8E4078:a-0xC8E4078+n])
        def write(a, data):
            memory[a-0xC8E4078:a-0xC8E4078+len(data)] = data
        self.assertEqual(publish_currency(read, write, {'gp': 2000000000, 'cp': 100000}), {'gp': 2000000000, 'cp': 100000})
        for offset in (0, 32):
            self.assertEqual(memory[offset+8:offset+32], before[offset+8:offset+32])
        self.assertEqual(memory[64:], before[64:])
        struct.pack_into('<i', memory, 4, 85000)
        self.assertEqual(currency_snapshot(read)['gp'], 85000)

    def test_inventory_excludes_market_without_corrupting_objects(self):
        memory = bytearray(CAPACITY*STRIDE)
        for i in range(CAPACITY):
            struct.pack_into('<I', memory, i*STRIDE, 0xD59C78)
        def read(a, n):
            return bytes(memory[a:a+n])
        def write(a, data):
            memory[a:a+len(data)] = data
        catalog = [{'id': 1, 'type_bytes': [0, 0]}, {'id': 2, 'type_bytes': [0, 0]}]
        result = populate_inventory(read, write, 0, catalog, 100, owned_ids=[2])
        self.assertEqual(result['items'], 1)
        self.assertEqual(struct.unpack_from('<III', memory, 64), (2, 1, 100))
        self.assertEqual(struct.unpack_from('<I', memory, STRIDE+64)[0], 0)

    def test_atomic_state_reload_balance_only_save_and_legacy(self):
        folder = ROOT/'verification/client/workspaces'
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=folder) as tmp:
            repo = AccountRepository(Path(tmp)/'test.sqlite3')
            identities = {'public': {}, 'secure': {}}
            blobs = {'public': b'pub', 'secure': b'sec'}
            state = {'version': 1, 'owned_ids': [570], 'gp': 2000000000, 'cp': 100000}
            repo.identity('new')
            repo.save('new', 0, blobs, identities, initialization=state)
            with self.assertRaises(RevisionConflict):
                repo.save('new', 0, blobs, identities, initialization={**state, 'gp': 0})
            self.assertEqual(repo.load('new', identities)['initialization'], state)
            class Codec:
                def migrate(self, profiles):
                    return dict(profiles)
                def validate_snapshot(self, value):
                    pass
            codec = Codec()
            codec.identities = identities
            session = AccountSession(repo, codec, 'new')
            session.account = repo.load('new', identities)
            self.assertTrue(session.save(blobs, balances={'gp': 123, 'cp': 456}))
            reopened = AccountSession(AccountRepository(Path(tmp)/'test.sqlite3'), codec, 'new')
            self.assertEqual(reopened.initialize()['initialization'], {**state, 'gp': 123, 'cp': 456})
            self.assertFalse(reopened.save(blobs, balances={'gp': 123, 'cp': 456}))
            repo.identity('old')
            repo.save('old', 0, blobs, identities)
            self.assertNotIn('initialization', repo.load('old', identities))


if __name__ == '__main__':
    unittest.main()
