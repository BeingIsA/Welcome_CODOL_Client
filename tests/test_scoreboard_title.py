"""验证标题适配的原生指令边界、最小写入范围及失败行为。"""
from pathlib import Path
import struct
import unittest

import pefile

from codol_rebuild.adapters.game.v1649370.matches.scoreboard_title import (
    TITLE_READY_CALL, TITLE_READY_WINDOW, install,
)


class MemoryProcess:
    """只允许访问标题调用窗口的内存替身。"""
    def __init__(self, window=TITLE_READY_WINDOW, drop_write=False):
        self.window = bytearray(window)
        self.writes = []
        self.drop_write = drop_write

    def read(self, address, size):
        offset = address - TITLE_READY_CALL
        return bytes(self.window[offset:offset + size])

    def write(self, address, byte_buffer):
        self.writes.append((address, byte_buffer))
        if not self.drop_write:
            offset = address - TITLE_READY_CALL
            self.window[offset:offset + len(byte_buffer)] = byte_buffer


class ScoreboardTitleTests(unittest.TestCase):
    def test_original_image_contract(self):
        image_path = Path(__file__).resolve().parents[1] / 'bin/v1649370/codol1649370-offline.exe'
        with pefile.PE(str(image_path)) as image:
            self.assertEqual(image.get_data(TITLE_READY_CALL - image.OPTIONAL_HEADER.ImageBase,
                                            len(TITLE_READY_WINDOW)), TITLE_READY_WINDOW)

    def test_only_call_displacement_changes(self):
        for ready_entry in (0x20000C00, 0xF0000C00):
            with self.subTest(ready_entry=hex(ready_entry)):
                process = MemoryProcess()
                install(process, ready_entry)
                self.assertEqual(len(process.writes), 1)
                self.assertEqual(process.writes[0][0], TITLE_READY_CALL + 1)
                self.assertEqual(len(process.writes[0][1]), 4)
                self.assertEqual(process.window[:1], TITLE_READY_WINDOW[:1])
                self.assertEqual(process.window[5:], TITLE_READY_WINDOW[5:])
                displacement = struct.unpack('<i', process.window[1:5])[0]
                self.assertEqual((TITLE_READY_CALL + 5 + displacement) & 0xFFFFFFFF, ready_entry)

    def test_mismatch_rejected_without_writes(self):
        process = MemoryProcess(b'\x90' + TITLE_READY_WINDOW[1:])
        with self.assertRaisesRegex(ValueError, 'does not match'):
            install(process, 0x20000C00)
        self.assertEqual(process.writes, [])

    def test_failed_write_detected(self):
        with self.assertRaisesRegex(ValueError, 'readback mismatch'):
            install(MemoryProcess(drop_write=True), 0x20000C00)
