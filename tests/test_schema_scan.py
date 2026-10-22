"""schema_scan 的纯函数单测：不访问进程，用合成内存验证定位与形态判定。"""
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'client' / 'src'))

from codol_rebuild.adapters.game.profile_format import scan as schema_scan
from codol_rebuild.adapters.game.v1649370.layout import BUFFER_CAPACITY, KNOWN_DEFINITIONS


class FakeMemory:
    """把一段字节映射到固定基址；越界读取抛 OSError，模拟不可读页。"""

    def __init__(self, base, data):
        self.base = base
        self.data = data

    def read(self, address, size):
        start = address - self.base
        if start < 0 or start + size > len(self.data):
            raise OSError(f'不可读地址 {address:#x}')
        return self.data[start:start + size]


def header_words(version, checksum, size, *, pointers, counts):
    """构造 16 字定义对象头部；pointer 值由调用方给（0xffffffff 表示未重定位）。"""
    words = [0] * 16
    words[0] = version
    words[1] = checksum
    words[12] = size
    for label, count_index, pointer_index in schema_scan._TABLES:
        words[count_index] = counts.get(label, 0)
        words[pointer_index] = pointers.get(label, 0)
    return struct.pack('<16I', *words)


class SchemaScanTest(unittest.TestCase):
    def test_fingerprint_matches_version_checksum_bytes(self):
        self.assertEqual(schema_scan.fingerprint(233, 0x4a6cc345),
                         bytes.fromhex('e900000045c36c4a'))
        self.assertEqual(schema_scan.fingerprint(19, 0xa34378b4),
                         bytes.fromhex('13000000b47843a3'))

    def test_raw_form_is_unrelocated_and_not_selected(self):
        counts = {'enums': 1, 'structs': 8, 'indexed_arrays': 40, 'enumed_arrays': 0, 'offsets': 3927}
        raw = header_words(233, 0x4a6cc345, 15767,
                           pointers={k: 0xffffffff for k in counts}, counts=counts)
        mem = FakeMemory(0x10000000, b'\x00' * 16 + raw + b'\x00' * 16)
        matches = schema_scan.find_fingerprints(
            mem.read, [(0x10000000, len(mem.data))], {'playerdata': KNOWN_DEFINITIONS['playerdata']})
        self.assertEqual(matches['playerdata'], [0x10000010])
        info = schema_scan.classify_definition(mem.read, 0x10000010, max_profile_size=BUFFER_CAPACITY)
        self.assertTrue(info['plausible'])
        self.assertFalse(info['relocated'])
        self.assertIn('structs', info['raw_tables'])
        selected = schema_scan.select_definition(mem.read, matches['playerdata'], max_profile_size=BUFFER_CAPACITY)
        self.assertIsNone(selected['selected'])

    def test_relocated_form_is_selected(self):
        counts = {'enums': 1, 'structs': 8, 'indexed_arrays': 40, 'enumed_arrays': 0, 'offsets': 3927}
        pointers = {'enums': 0x11000000, 'structs': 0x11001000, 'indexed_arrays': 0x11002000,
                    'enumed_arrays': 0, 'offsets': 0x11003000}
        blob = header_words(233, 0x4a6cc345, 15767, pointers=pointers, counts=counts)
        mem = FakeMemory(0x10000000, b'\xaa' * 32 + blob + b'\xbb' * 32)
        matches = schema_scan.find_fingerprints(
            mem.read, [(0x10000000, len(mem.data))], {'playerdata': KNOWN_DEFINITIONS['playerdata']})
        self.assertEqual(matches['playerdata'], [0x10000020])
        selected = schema_scan.select_definition(mem.read, matches['playerdata'], max_profile_size=BUFFER_CAPACITY)
        self.assertEqual(selected['selected'], 0x10000020)
        self.assertTrue(selected['candidates'][0]['relocated'])

    def test_bad_size_marks_implausible(self):
        counts = {'structs': 1}
        blob = header_words(233, 0x4a6cc345, BUFFER_CAPACITY + 1,
                            pointers={'structs': 0x11000000}, counts=counts)
        mem = FakeMemory(0x10000000, blob)
        info = schema_scan.classify_definition(mem.read, 0x10000000, max_profile_size=BUFFER_CAPACITY)
        self.assertFalse(info['plausible'])
        self.assertFalse(info['relocated'])

    def test_unreadable_address_reports_error(self):
        mem = FakeMemory(0x10000000, b'\x00' * 8)
        info = schema_scan.classify_definition(mem.read, 0x20000000, max_profile_size=BUFFER_CAPACITY)
        self.assertFalse(info['readable'])
        self.assertFalse(info['relocated'])

    def test_multiple_matches_and_unreadable_region_skipped(self):
        needle = KNOWN_DEFINITIONS['playersecuredata']
        blob = b'\x00' * 4 + needle + b'\x00' * 4 + needle + b'\x00' * 4
        mem = FakeMemory(0x10000000, blob)
        regions = [(0x10000000, len(blob)), (0x90000000, 4096)]  # 第二区不可读
        matches = schema_scan.find_fingerprints(mem.read, regions, {'playersecuredata': needle})
        self.assertEqual(matches['playersecuredata'], [0x10000004, 0x10000010])

    def test_fingerprint_spanning_chunk_boundary_is_found(self):
        needle = KNOWN_DEFINITIONS['playerdata']
        chunk = schema_scan._CHUNK
        # 把指纹放在跨越两块边界处（前一块尾部留 3 字节）。
        prefix = b'\x00' * (chunk - 3)
        blob = prefix + needle + b'\x00' * 64
        mem = FakeMemory(0x10000000, blob)
        matches = schema_scan.find_fingerprints(
            mem.read, [(0x10000000, len(blob))], {'playerdata': needle})
        self.assertEqual(matches['playerdata'], [0x10000000 + chunk - 3])

    def test_explicit_capacity_controls_classification_and_selection(self):
        for capacity in (4096, 65536):
            with self.subTest(capacity=capacity):
                blob = header_words(233, 0x4a6cc345, capacity,
                                    pointers={'structs': 0x11000000}, counts={'structs': 1})
                mem = FakeMemory(0x10000000, blob)
                accepted = schema_scan.classify_definition(mem.read, 0x10000000, max_profile_size=capacity)
                self.assertTrue(accepted['relocated'])
                selected = schema_scan.select_definition(mem.read, [0x10000000], max_profile_size=capacity)
                self.assertEqual(selected['selected'], 0x10000000)
                rejected = schema_scan.select_definition(mem.read, [0x10000000], max_profile_size=capacity - 1)
                self.assertIsNone(rejected['selected'])
                self.assertFalse(rejected['candidates'][0]['plausible'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
