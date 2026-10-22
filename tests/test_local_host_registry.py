"""核查只读注册表遍历的异常拒绝，防止损坏或不完整列表被当作功能缺失证据。"""
from pathlib import Path
import struct
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'client/tools'))
TOOLS = Path(__file__).resolve().parents[2] / 'verification/client/tools'
sys.path.insert(0, str(TOOLS))
from inspect_local_host import registries, COMMAND_HEAD, SECONDARY_COMMAND_HEAD, DVAR_BUCKETS


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.memory = {}
        self.put(COMMAND_HEAD, struct.pack('<I', 0x10000))
        self.put(0x10000, struct.pack('<3I', 0, 0x20000, 0x400000))
        self.put(0x20000, b'connect\0')
        name = '1964'
        bucket = sum(ord(ch)*(119+i) for i, ch in enumerate(name)) & 1023
        self.put(DVAR_BUCKETS+bucket*4, struct.pack('<I', 0x30000))
        self.put(0x30000, struct.pack('<I', 0x20020))
        self.put(0x20020, name.encode()+b'\0')

    def put(self, address, data):
        self.memory.update({address+i: value for i, value in enumerate(data)})

    def read(self, address, size):
        return bytes(self.memory.get(address+i, 0) for i in range(size))

    def test_missing_command_does_not_erase_present_dvar(self):
        result = registries(self.read)
        self.assertEqual([r['name'] for r in result['commands']], ['connect'])
        self.assertEqual([r['name'] for r in result['dvars']], ['1964'])

    def test_command_cycle_is_rejected(self):
        self.put(0x10000, struct.pack('<I', 0x10000))
        with self.assertRaisesRegex(ValueError, 'cycle'):
            registries(self.read)

    def test_wrong_dvar_bucket_is_rejected(self):
        self.put(0x20020, b'1965\0')
        with self.assertRaisesRegex(ValueError, 'bucket mismatch'):
            registries(self.read)

    def test_unterminated_command_name_is_rejected(self):
        self.put(0x20000, b'x'*256)
        with self.assertRaisesRegex(ValueError, 'terminator'):
            registries(self.read)

    def test_null_handler_remains_registered_not_executable(self):
        self.put(0x10008, struct.pack('<I', 0))
        result = registries(self.read)
        self.assertEqual(result['commands'][0]['handler'], '0x0')
        self.assertEqual(len(result['commands']), 1)

    def test_second_dispatch_table_is_not_treated_as_empty_stub(self):
        self.put(0x10008, struct.pack('<I', 0x8435d0))
        self.put(SECONDARY_COMMAND_HEAD, struct.pack('<I', 0x31000))
        self.put(0x31000, struct.pack('<3I', 0, 0x20000, 0x470750))
        result = registries(self.read)
        self.assertEqual(result['secondary_commands'][0]['handler'], '0x470750')


if __name__ == '__main__':
    unittest.main()
