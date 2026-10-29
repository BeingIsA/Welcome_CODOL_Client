"""上传局部补丁边界及ABI回归；不能代替真实客户端连续重进验收。"""
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock
import capstone

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from codol_rebuild.adapters.game.v1649370.accounts.profile import install_upload_checksum_order


class ProfileUploadOrderTests(unittest.TestCase):
    def test_only_upload_instruction_window_changes(self):
        memory = bytearray(bytes.fromhex('7400e889fbffff57e8f30e000083bf8400000000'))
        original_memory = bytes(memory)
        writes = []

        def read(address, length):
            offset = address - 0x7F6EE0
            return bytes(memory[offset:offset+length])

        def write(address, value):
            writes.append((address, len(value)))
            offset = address - 0x7F6EE0
            memory[offset:offset+len(value)] = value

        install_upload_checksum_order(read, write)
        self.assertEqual(writes, [(0x7F6EE2, 11)])
        self.assertEqual(memory[:2], original_memory[:2])
        self.assertEqual(memory[13:], original_memory[13:])
        decoder = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
        instructions = list(decoder.disasm(bytes(memory[2:13]), 0x7F6EE2))
        self.assertEqual([(instruction.mnemonic, instruction.op_str) for instruction in instructions],
                         [('push', 'edi'), ('call', '0x7f7de0'), ('call', '0x7f6a70')])
        self.assertEqual(sum(instruction.size for instruction in instructions), 11)
        # EDI先压入作为整理函数参数，原生ret4消费它；CRC以EDI取对象，不另压参数。
        self.assertEqual([instruction.op_str for instruction in instructions if instruction.mnemonic == 'push'], ['edi'])

    def test_wrong_version_does_not_write(self):
        writer = Mock()
        with self.assertRaisesRegex(ValueError, 'order mismatch'):
            install_upload_checksum_order(lambda address, length: bytes(length), writer)
        writer.assert_not_called()

    def test_failed_write_is_detected(self):
        original = bytes.fromhex('e889fbffff57e8f30e0000')
        with self.assertRaisesRegex(ValueError, 'readback mismatch'):
            install_upload_checksum_order(lambda address, length: original, Mock())


if __name__ == '__main__':
    unittest.main()
