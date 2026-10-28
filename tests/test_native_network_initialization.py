"""网络模式适配仅写获准字节；窗口不符时写入前失败。"""
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from codol_rebuild.adapters.game.v1649370.matches.network_initialization import CALL_WINDOWS, install


class MemoryProcess:
    def __init__(self):
        self.memory = {address+offset: value for address, expected in CALL_WINDOWS
                       for offset, value in enumerate(expected)}
        self.writes = []

    def read(self, address, size):
        return bytes(self.memory[address+offset] for offset in range(size))

    def write(self, address, data):
        self.writes.append((address, data))
        for offset, value in enumerate(data):
            self.memory[address+offset] = value


class NativeNetworkInitializationTests(unittest.TestCase):
    def test_only_two_immediate_bytes_change(self):
        process = MemoryProcess()
        before = dict(process.memory)
        install(process)
        changed = {address for address in before if before[address] != process.memory[address]}
        self.assertEqual(changed, {0x7E9912, 0x621ABF})
        self.assertEqual(process.writes, [(0x7E9912, b'\0'), (0x621ABF, b'\0')])

    def test_second_call_mismatch_leaves_first_call_untouched(self):
        process = MemoryProcess()
        process.memory[0x621AC6] ^= 1
        with self.assertRaises(ValueError):
            install(process)
        self.assertEqual(process.writes, [])
