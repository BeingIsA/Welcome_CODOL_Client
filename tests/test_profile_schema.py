"""只测试 DDL 结构定义导出；用合成内存，不访问进程、不启动游戏。"""
import json
import struct
import tempfile
import unittest
from pathlib import Path

from codol_rebuild.adapters.game.profile_format.schema import capture_schema
from codol_rebuild.adapters.game.v1649370.layout import BUFFER_CAPACITY

BASE = 0x10000000  # 页对齐、> 65536，使表指针有效性判定通过
IMAGE_SIZE = 0x20000


class FakeMemory:
    """按页对齐基址服务的只读内存；越界读抛 OSError 以覆盖跳过分支。"""

    def __init__(self, base=BASE, size=IMAGE_SIZE):
        self.base = base
        self.image = bytearray(size)

    def put_dwords(self, address, values):
        offset = address - self.base
        self.image[offset:offset + 4 * len(values)] = struct.pack('<' + 'I' * len(values), *values)

    def put_bytes(self, address, data):
        offset = address - self.base
        self.image[offset:offset + len(data)] = data

    def put_cstring(self, address, text):
        self.put_bytes(address, text.encode('utf-8') + b'\x00')

    def read(self, address, size):
        offset = address - self.base
        if offset < 0 or offset + size > len(self.image):
            raise OSError(f'越界读取 {address:#x}+{size}')
        return bytes(self.image[offset:offset + size])


def build_definition(mem, *, version=233, checksum=1892502725, size=9610,
                     enum_count=1, struct_count=1, indexed_count=1, enumed_count=0,
                     root_type=(5, 0)):
    """在合成内存里布下一份最小可用的 playerdata 定义对象，返回其地址。"""
    defobj = BASE + 0x0000
    enum_table = BASE + 0x1000
    enum_name_table = BASE + 0x1100
    enum_names = BASE + 0x1200
    struct_table = BASE + 0x2000
    prop_table = BASE + 0x2100
    prop_names = BASE + 0x2400
    indexed_table = BASE + 0x3000
    offset_table = BASE + 0x4000

    # 头部 16 字
    header = [0] * 16
    header[0], header[1] = version, checksum
    header[2], header[3] = enum_count, enum_table
    header[4], header[5] = struct_count, struct_table
    header[6], header[7] = indexed_count, indexed_table
    header[8], header[9] = enumed_count, BASE + 0x3800
    header[10], header[11] = root_type
    header[12] = size
    offsets = list(range(100, 100 + 64))  # flat -> byte offset
    header[13], header[14] = len(offsets), offset_table
    mem.put_dwords(defobj, header)
    mem.put_dwords(offset_table, offsets)

    # 一个枚举：两条目
    if enum_count:
        mem.put_dwords(enum_table, [2, BASE + 0x1080, enum_name_table])
        mem.put_cstring(enum_names, 'STANDING')
        mem.put_cstring(enum_names + 32, 'PRONE')
        mem.put_dwords(enum_name_table, [enum_names, 0, enum_names + 32, 1])

    # 一个结构体：rank(int) / unlockFlags(定长数组) / experience(int)
    if struct_count:
        names = ['rank', 'unlockFlags', 'experience']
        for i, text in enumerate(names):
            mem.put_cstring(prop_names + 32 * i, text)
        # prop: [name_addr, kind, index, offset(flat), extra_a, extra_b]
        mem.put_dwords(prop_table + 0, [prop_names + 0, 0, 0, 10, 0, 0])    # rank int
        mem.put_dwords(prop_table + 24, [prop_names + 32, 6, 0, 20, 0, 0])  # unlockFlags array idx0
        mem.put_dwords(prop_table + 48, [prop_names + 64, 0, 0, 11, 0, 0])  # experience int
        mem.put_dwords(struct_table, [3, prop_table, 0, 0, 0])

    # 定长数组 0：unlockFlags = 4096 位（[count, kind, index, extra]）
    if indexed_count:
        mem.put_dwords(indexed_table, [4096, 2, 0, 0])

    return defobj


class CaptureSchemaTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name) / 'secure-schema'
        self.addCleanup(self._tmp.cleanup)

    def test_exports_fields_arrays_and_offsets(self):
        mem = FakeMemory()
        defobj = build_definition(mem)
        summary = capture_schema(mem.read, defobj, self.folder, max_profile_size=BUFFER_CAPACITY)

        self.assertEqual(summary['size'], 9610)
        self.assertEqual(summary['root_type'], [5, 0])
        self.assertEqual(summary['counts'], {'enums': 1, 'structs': 1, 'indexed_arrays': 1,
                                             'enumed_arrays': 0, 'offsets': 64})

        schema = json.loads((self.folder / 'playerdata-schema.json').read_text(encoding='utf-8'))
        self.assertEqual([int(schema['header'][i], 16) for i in (0, 1)], [233, 1892502725])
        self.assertEqual(schema['offsets'][:3], [100, 101, 102])

        props = {p['name']: p for p in schema['structs'][0]['properties']}
        self.assertEqual(props['rank']['type'], [0, 0])
        self.assertEqual(props['rank']['offset'], 10)
        self.assertEqual(props['unlockFlags']['type'], [6, 0])
        self.assertEqual(props['experience']['offset'], 11)

        # 全解锁位域：4096 位的定长字节数组
        self.assertEqual(schema['indexed_arrays'][0][:3], [4096, 2, 0])

    def test_writes_raw_evidence_before_parse(self):
        mem = FakeMemory()
        defobj = build_definition(mem)
        capture_schema(mem.read, defobj, self.folder, max_profile_size=BUFFER_CAPACITY)
        raw = json.loads((self.folder / 'schema-raw-header.json').read_text(encoding='utf-8'))
        self.assertEqual(raw['address'], hex(defobj))
        self.assertEqual(int(raw['header'][12], 16), 9610)
        self.assertIn('structs', raw['tables'])
        self.assertTrue((self.folder / 'asset-pages.json').exists())

    def test_captures_profile_buffer_when_given(self):
        mem = FakeMemory()
        defobj = build_definition(mem)
        buffer_addr = BASE + 0x8000
        mem.put_bytes(buffer_addr, b'\xAB' * BUFFER_CAPACITY)
        capture_schema(mem.read, defobj, self.folder, max_profile_size=BUFFER_CAPACITY, profile_buffer=buffer_addr)
        before = (self.folder / 'profile-before.bin').read_bytes()
        self.assertEqual(len(before), BUFFER_CAPACITY)
        self.assertEqual(before[:4], b'\xAB\xAB\xAB\xAB')

    def test_rejects_size_out_of_range(self):
        for bad_size in (0, BUFFER_CAPACITY + 1):
            with self.subTest(size=bad_size):
                mem = FakeMemory()
                defobj = build_definition(mem, size=bad_size)
                with self.assertRaises(ValueError):
                    capture_schema(mem.read, defobj, self.folder, max_profile_size=BUFFER_CAPACITY)
                # 原始头部证据仍应在断言失败前落盘
                self.assertTrue((self.folder / 'schema-raw-header.json').exists())

    def test_rejects_implausible_table_count(self):
        mem = FakeMemory()
        defobj = build_definition(mem, struct_count=4096)
        with self.assertRaises(ValueError):
            capture_schema(mem.read, defobj, self.folder, max_profile_size=BUFFER_CAPACITY)

    def test_explicit_capacity_controls_size_check_and_buffer_capture(self):
        for capacity in (4096, 65536):
            with self.subTest(capacity=capacity):
                mem = FakeMemory()
                defobj = build_definition(mem, size=capacity)
                buffer_addr = BASE + 0x8000
                mem.put_bytes(buffer_addr, b'\xCD' * capacity)
                summary = capture_schema(mem.read, defobj, self.folder,
                                         max_profile_size=capacity, profile_buffer=buffer_addr)
                self.assertEqual(summary['size'], capacity)
                self.assertEqual((self.folder / 'profile-before.bin').read_bytes(), b'\xCD' * capacity)
                with self.assertRaises(ValueError):
                    capture_schema(mem.read, defobj, self.folder, max_profile_size=capacity - 1)


if __name__ == '__main__':
    unittest.main()
