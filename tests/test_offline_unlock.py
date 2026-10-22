"""offline_unlock 的单测：合成 schema 验证纯逻辑 + 真实 evidence schema 回归。

独立模块，不启动游戏、不访问进程。真实 schema 用例在缺少 evidence 文件时跳过。
"""
import base64
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'client' / 'src'))

from codol_rebuild.adapters.game.v1649370 import profile_unlock as ou
from codol_rebuild.adapters.persistence.profile_archive import ProfileStore
from codol_rebuild.adapters.game.v1649370.profile_codec import SecureArchiveFormat

EVIDENCE_SECURE = ROOT / 'client/workspaces/offline-runs/offline-20261020-040037-7938274a/local-profile/playersecuredata/playerdata-schema.json'


def synthetic_schema():
    """构造一个结构合法、size 较小的 secure 风格 schema（字段/数组/offsets 齐全）。"""
    offsets = [0] * 64
    offsets[10], offsets[11], offsets[12] = 16, 24, 32       # rank/experience/rankVersion 字节偏移
    for i, bit in enumerate([80, 81, 88, 89, 96, 97, 104, 105]):  # unlockFlags 8 个散布位
        offsets[20 + i] = bit
    for i in range(4):                                       # unlockFlagsNew 4 个 int16 槽
        offsets[30 + i] = 40 + i * 2
    return {
        'header': [hex(19), hex(0xa34378b4)] + [hex(0)] * 14,
        'size': 128, 'root_type': [5, 0],
        'structs': [{'header': [5, 0, 0, 0, 0], 'properties': [
            {'name': 'rank', 'type': [0, 0], 'offset': 10, 'extra': [32, 0]},
            {'name': 'experience', 'type': [0, 0], 'offset': 11, 'extra': [32, 0]},
            {'name': 'rankVersion', 'type': [0, 0], 'offset': 12, 'extra': [32, 0]},
            {'name': 'unlockFlags', 'type': [6, 0], 'offset': 20, 'extra': [0, 0]},
            {'name': 'unlockFlagsNew', 'type': [6, 1], 'offset': 30, 'extra': [0, 0]},
        ]}],
        'indexed_arrays': [[8, 2, 0, 0], [4, 9, 0, 0]],
        'offsets': offsets,
    }


class OfflineUnlockSyntheticTest(unittest.TestCase):
    def test_resolve_scalar_and_bitfield(self):
        schema = synthetic_schema()
        self.assertEqual(ou.resolve_scalar(schema, 'rank'), ('<i', 16))
        self.assertEqual(ou.resolve_scalar(schema, 'experience'), ('<i', 24))
        self.assertEqual(ou.resolve_bitfield(schema, 'unlockFlags'), [80, 81, 88, 89, 96, 97, 104, 105])
        self.assertEqual(len(ou.resolve_int16_array(schema, 'unlockFlagsNew')), 4)

    def test_bitfield_rejects_non_bit_array(self):
        schema = synthetic_schema()
        with self.assertRaises(ou.UnlockError):
            ou.resolve_bitfield(schema, 'unlockFlagsNew')  # elemKind 9，不是位域

    def test_apply_full_unlock_sets_rank_and_all_bits(self):
        schema = synthetic_schema()
        blob, report = ou.apply_full_unlock(b'\x00' * ou.BUFFER_CAPACITY, schema, {'rank': 70})
        self.assertEqual(struct.unpack_from('<i', blob, 16)[0], 70)
        for bit in [80, 81, 88, 89, 96, 97, 104, 105]:
            self.assertTrue(blob[bit // 8] & (1 << bit % 8))
        self.assertEqual(report['fields']['unlockFlags']['bits'], 8)
        self.assertEqual(report['fields']['unlockFlags']['newly_set'], 8)
        self.assertEqual(len(blob), ou.BUFFER_CAPACITY)

    def test_apply_skips_none_fields_and_int16_by_default(self):
        schema = synthetic_schema()
        blob, report = ou.apply_full_unlock(b'\x00' * ou.BUFFER_CAPACITY, schema)
        self.assertIn('rank', report['fields'])           # 默认写 rank=MAX_RANK
        self.assertNotIn('experience', report['fields'])  # None → 跳过
        self.assertNotIn('rankVersion', report['fields'])
        self.assertNotIn('unlockFlagsNew', report['fields'])  # 默认不触碰 int16 数组
        self.assertEqual(report['fields']['rank']['value'], ou.MAX_RANK)

    def test_apply_writes_int16_array_when_configured(self):
        schema = synthetic_schema()
        blob, report = ou.apply_full_unlock(b'\x00' * ou.BUFFER_CAPACITY, schema,
                                            {'unlock_flags_new_value': 1})
        self.assertEqual(report['fields']['unlockFlagsNew']['slots'], 4)
        for i in range(4):
            self.assertEqual(struct.unpack_from('<h', blob, 40 + i * 2)[0], 1)

    def test_idempotent_second_pass_sets_no_new_bits(self):
        schema = synthetic_schema()
        blob1, _ = ou.apply_full_unlock(b'\x00' * ou.BUFFER_CAPACITY, schema)
        _, report2 = ou.apply_full_unlock(blob1, schema)
        self.assertEqual(report2['fields']['unlockFlags']['newly_set'], 0)

    def test_oversized_base_rejected(self):
        schema = synthetic_schema()
        with self.assertRaises(ou.UnlockError):
            ou.apply_full_unlock(b'\x00' * (ou.BUFFER_CAPACITY + 1), schema)


class LoadSchemaTest(unittest.TestCase):
    def test_identity_match_and_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / 'good.json'
            good.write_text(json.dumps({'header': [hex(19), hex(0xa34378b4)] + [hex(0)] * 14,
                                        'size': 15124, 'root_type': [5, 0], 'structs': [], 'offsets': []}),
                            encoding='utf-8')
            self.assertEqual(ou.load_schema(good)['size'], 15124)
            bad = Path(tmp) / 'bad.json'
            bad.write_text(json.dumps({'header': [hex(19), hex(0xdeadbeef)] + [hex(0)] * 14,
                                       'size': 15124}), encoding='utf-8')
            with self.assertRaises(ou.UnlockError):
                ou.load_schema(bad)


class ProfileStoreTest(unittest.TestCase):
    def test_roundtrip_backup_and_recovery(self):
        blob = (bytes(range(256)) * (ou.BUFFER_CAPACITY // 256 + 1))[:ou.BUFFER_CAPACITY]
        self.assertEqual(len(blob), ou.BUFFER_CAPACITY)
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(Path(tmp) / 'save.json', SecureArchiveFormat())
            store.save(blob, run='run-1')
            loaded, info = store.load()
            self.assertEqual(loaded, blob)
            self.assertFalse(info['recovered_from_backup'])
            # 第二次保存不同内容 → 生成 .previous 备份
            blob2 = bytes([0xAB]) * ou.BUFFER_CAPACITY
            store.save(blob2, run='run-2')
            self.assertTrue((Path(tmp) / 'save.previous.json').exists())
            loaded2, _ = store.load()
            self.assertEqual(loaded2, blob2)
            # 主档损坏 → 从备份恢复
            (Path(tmp) / 'save.json').write_text('{ not json', encoding='utf-8')
            recovered, info3 = store.load()
            self.assertEqual(recovered, blob)
            self.assertTrue(info3['recovered_from_backup'])

    def test_all_corrupt_raises_without_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'save.json'
            path.write_text('{"format":999}', encoding='utf-8')
            store = ProfileStore(path, SecureArchiveFormat())
            with self.assertRaises(ou.UnlockError):
                store.load()


@unittest.skipUnless(EVIDENCE_SECURE.is_file(), '缺少真实捕获的 evidence schema')
class RealEvidenceSchemaTest(unittest.TestCase):
    """回归锁定 E1 运行期捕获的新版偏移，防止将来误改解释逻辑。"""

    @classmethod
    def setUpClass(cls):
        cls.schema = ou.load_schema(EVIDENCE_SECURE, ou.SECURE_IDENTITY)

    def test_scalar_offsets_match_capture(self):
        self.assertEqual(ou.resolve_scalar(self.schema, 'experience'), ('<i', 4840))
        self.assertEqual(ou.resolve_scalar(self.schema, 'rank'), ('<i', 4883))
        self.assertEqual(ou.resolve_scalar(self.schema, 'rankVersion'), ('<i', 4887))

    def test_unlock_flags_is_4096_bitfield(self):
        bits = ou.resolve_bitfield(self.schema, 'unlockFlags')
        self.assertEqual(len(bits), 4096)
        self.assertEqual(min(bits), 39672)
        self.assertEqual(max(bits), 63543)

    def test_unlock_flags_new_is_int16_array(self):
        slots = ou.resolve_int16_array(self.schema, 'unlockFlagsNew')
        self.assertEqual(len(slots), 2048)
        self.assertTrue(all(fmt == '<h' for fmt, _ in slots))

    def test_full_unlock_on_zero_buffer(self):
        blob, report = ou.apply_full_unlock(b'\x00' * ou.BUFFER_CAPACITY, self.schema, {'rank': ou.MAX_RANK})
        self.assertEqual(struct.unpack_from('<i', blob, 4883)[0], ou.MAX_RANK)
        self.assertEqual(report['fields']['unlockFlags']['newly_set'], 4096)
        # 抽查最高位已置位
        top = 63543
        self.assertTrue(blob[top // 8] & (1 << top % 8))


if __name__ == '__main__':
    unittest.main(verbosity=2)
