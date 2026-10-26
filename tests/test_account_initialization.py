"""验证新档授权、余额保存与旧档兼容，不接入游戏运行目录。"""
import copy
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
    def test_bounty_hunter_new_archive_and_existing_archive_preserved(self):
        import bisect
        from codol_rebuild.adapters.game.v1649370.accounts.profile import gp_cp_market_items
        from codol_rebuild.adapters.game.v1649370.accounts.profile_codec import AccountCodec
        captured = json.loads((ROOT/'research/account-initialization/capture/market-memory.json').read_text(encoding='utf-8'))
        blocks = {int(a, 16): bytes.fromhex(v) for a, v in captured['memory_chunks'].items()}
        starts = sorted(blocks)
        def read(address, size):
            if not size:
                return b''
            start = starts[bisect.bisect_right(starts, address)-1]
            result = blocks[start][address-start:address-start+size]
            self.assertEqual(len(result), size)
            return result
        bounty = copy.deepcopy(next(r for r in captured['catalog'] if r['id'] == 2056))
        self.assertEqual(bounty['name'], 'cha_cowgirl')
        bounty['market_sale'] = True
        sales = bytearray(65536)
        sales[21039] = 1  # 实际普通 CP SKU。
        self.assertEqual(gp_cp_market_items(read, [bounty], sales), {2056})
        sales[28436] = 1  # 实际排位赛荣誉 SKU，位于另一个产品。
        self.assertEqual(gp_cp_market_items(read, [bounty], sales), set())
        bounty['gp_cp_sale'] = False
        tables = {**captured['tables'], 'mp/coStartingLoadoutSetting.csv': []}
        schemas = {kind: json.loads(Path(captured['schemas'][label]['schema']).read_text(encoding='utf-8'))
                   for kind, label in [('public', 'playerdata'), ('secure', 'playersecuredata')]}
        defaults = [dict(row, gp_cp_sale=False) for row in captured['catalog']
                    if row['id'] in (30000, 30060, 30061)]
        codec = AccountCodec(schemas, tables, [bounty, *defaults])
        folder = ROOT/'verification/client/workspaces'
        with tempfile.TemporaryDirectory(dir=folder) as tmp:
            repo = AccountRepository(Path(tmp)/'honor.sqlite3')
            session = AccountSession(repo, codec)
            created = session.initialize()
            self.assertTrue(created['new_account'])
            self.assertEqual(created['initialization']['owned_ids'], [2056, 30000, 30060, 30061])
            self.assertEqual(codec.validate_snapshot(created['profiles']), 2056)
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

    def test_gp_cp_skus_require_both_currency_and_native_sale(self):
        from codol_rebuild.adapters.game.v1649370.accounts.profile import gp_cp_market_items
        memory = {}
        def put(address, blob):
            memory.update((address+i, value) for i, value in enumerate(blob))
        def read(address, size):
            return bytes(memory[address+i] for i in range(size))
        def table(pointer_at, count_at, base, count, stride):
            put(pointer_at, struct.pack('<I', base))
            put(count_at, struct.pack('<I', count))
            put(base, bytes(count*stride))
        table(0x7797FE8, 0x7797FEC, 0x10000, 10, 28)
        table(0x7797FF8, 0x7797FFC, 0x20000, 10, 2)
        table(0x7798000, 0x7798004, 0x30000, 10, 24)
        table(0x7798008, 0x779800C, 0x40000, 10, 2)
        put(0xE57364, struct.pack('<H', 0x1234))
        catalog, sales = [], bytearray(65536)
        # GP, CP, other currency, expired GP, mixed, hidden GP, non-sale.
        for item_id, sku_ids, visible in [
            (1, [1], True), (2, [2], True), (3, [3], True),
            (4, [4, 3], True), (5, [3, 2], True),
            (6, [1], False), (7, [], False),
        ]:
            raw = bytearray(40)
            struct.pack_into('<HI', raw, 34, 1, 0x50000+item_id*2)
            put(0x50000+item_id*2, struct.pack('<H', item_id))
            put(0x20000+item_id*2, struct.pack('<H', item_id))
            put(0x10000+item_id*28+20, bytes([len(sku_ids)]))
            put(0x10000+item_id*28+24, struct.pack('<I', 0x60000+item_id*20))
            put(0x60000+item_id*20, struct.pack(f'<{len(sku_ids)}H', *sku_ids))
            catalog.append({'id': item_id, 'raw': raw.hex(), 'market_sale': visible})
        for sku_id, currency, active in [(1, 1, True), (2, 2, True), (3, 7, True), (4, 1, False)]:
            put(0x40000+sku_id*2, struct.pack('<H', sku_id))
            address = 0x30000+sku_id*24+6
            encoded = ((currency >> 4) | (currency << 12)) & 65535
            put(address, struct.pack('<H', encoded ^ (address & 65535) ^ 0x1234))
            sales[sku_id] = active
        self.assertEqual(gp_cp_market_items(read, catalog, sales), {1, 2, 5})
        data = {'progression': {}, 'default_character': 0, 'starting_loadouts': [],
                'items': [{'id': r['id'], 'category': 'character', 'slot_group': (2, 0),
                           'marketplace_only': r['id'] in {1, 2, 5}, 'reserved': False} for r in catalog]}
        self.assertEqual(new_account_policy(data)['initialization']['owned_ids'], [3, 4, 6, 7])
        # 原生动态 SKU 使用扩展 ID 映射和扩展记录池，不能误当成非卖品。
        put(0x60000+3*20, struct.pack('<H', 10))
        put(0x8301AE8, struct.pack('<H', 10))
        put(0x8301AE4, struct.pack('<H', 1))
        address = 0x7798918+6
        put(address, struct.pack('<H', 0x2000 ^ (address & 65535) ^ 0x1234))
        sales[10] = 1
        self.assertEqual(gp_cp_market_items(read, catalog, sales), {1, 2, 3, 5})
        # 与赏金猎人相同：普通 CP 产品在前，另一个产品提供排位荣誉兑换。
        raw = bytearray.fromhex(catalog[1]['raw'])
        struct.pack_into('<H', raw, 34, 2)
        catalog[1]['raw'] = raw.hex()
        put(0x50000+2*2, struct.pack('<2H', 2, 8))
        put(0x20000+8*2, struct.pack('<H', 8))
        put(0x10000+8*28+2, struct.pack('<H', 3))
        put(0x10000+8*28+20, b'\x01')
        put(0x10000+8*28+24, struct.pack('<I', 0x61000))
        put(0x61000, struct.pack('<H', 8))
        put(0x40000+8*2, struct.pack('<H', 8))
        address = 0x30000+8*24+6
        for currency in (8, 9):
            put(address, struct.pack('<H', (currency << 12) ^ (address & 65535) ^ 0x1234))
            sales[8] = 1
            self.assertNotIn(2, gp_cp_market_items(read, catalog[:2], sales))
            # 产品顺序不影响荣誉渠道优先级。
            put(0x50000+2*2, struct.pack('<2H', 8, 2))
            self.assertNotIn(2, gp_cp_market_items(read, catalog[:2], sales))
            put(0x50000+2*2, struct.pack('<2H', 2, 8))
        sales[8] = 0
        self.assertIn(2, gp_cp_market_items(read, catalog[:2], sales))
        sales[8] = 1
        put(0x10000+8*28+2, struct.pack('<H', 1))
        self.assertIn(2, gp_cp_market_items(read, catalog[:2], sales))
        with self.assertRaises(ValueError):
            gp_cp_market_items(read, catalog, sales[:-1])

    def test_native_contract_matches_image_and_rejects_mismatch(self):
        import pefile
        from codol_rebuild.adapters.game.v1649370.accounts.profile import validate_account_native_contract
        image = pefile.PE(str(ROOT/'client/bin/v1649370/codol1649370-offline.exe'))
        validate_account_native_contract(lambda a, n: image.get_data(a-0x400000, n))
        with self.assertRaises(ValueError):
            validate_account_native_contract(lambda a, n: bytes(n))

    def test_market_exclusion_and_loadout_dependencies(self):
        row = {'primary': {'base': 1, 'attachments': [3, 0]}, 'secondary': {'base': 0, 'attachments': [0]},
               'perks': [0, 0, 0], 'wildcards': [0], 'lethal': 1, 'tactical': 0,
               'lethal_double_ammo': True, 'tactical_double_ammo': False}
        data = {'progression': {'rank': 70}, 'default_character': 545, 'starting_loadouts': [row, row, row],
                'items': [{'id': i, 'category': 'character' if kind == 2 else 'equipment',
                           'slot_group': (kind, 0), 'marketplace_only': sale, 'reserved': False}
                          for i, kind, sale in [(1, 0, True), (2, 0, False), (3, 1, False),
                                                (545, 2, True), (570, 2, False)]]}
        before = copy.deepcopy(data)
        policy = new_account_policy(data)
        self.assertEqual(policy['initialization'], {'version': 1, 'owned_ids': [2, 3, 570], 'gp': 2000000000, 'cp': 100000})
        self.assertEqual(policy['character'], 570)
        self.assertEqual(policy['loadouts'][0]['primary'], {'base': 2, 'attachments': [0, 0]})
        self.assertFalse(policy['loadouts'][0]['lethal_double_ammo'])
        self.assertEqual(before, data)

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

    def test_unclassified_catalog_refused(self):
        with self.assertRaises(ValueError):
            new_account_policy({'items': [{'id': 5}]})

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
