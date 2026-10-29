"""原生退出诊断的恢复、采集和导出过滤回归；不替代虚拟机验收。"""
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from codol_rebuild.bootstrap.config import parse_args
from codol_rebuild.runtime.termination_trace import TerminationTrace


class TerminationTraceTests(unittest.TestCase):
    def test_capture_restores_instruction_and_records_exit(self):
        process = Mock()
        process.read.side_effect = [struct.pack('<3I', 0x1100, 0xffffffff, 0xc0000417), b'\0' * 4096]
        trace = TerminationTrace(process, Mock())
        trace.modules[0x1000] = (0x1000, 'game.exe')
        trace.breakpoints[0x2000] = (b'\x55', 'NtTerminateProcess')
        context = bytearray(716)
        struct.pack_into('<I', context, 196, 0x8000)
        row = {'tid': 7}
        trace.capture(0x2000, 9, context, row)
        process.write.assert_called_once_with(0x2000, b'\x55')
        process.set_context.assert_called_once_with(9, context)
        self.assertEqual(struct.unpack_from('<I', context, 184)[0], 0x2000)
        self.assertEqual(row['termination_status'], '0xc0000417')
        self.assertEqual(row['caller']['module'], 'game.exe')
        self.assertNotIn(0x2000, trace.breakpoints)

    def test_unreadable_stack_still_restores_execution(self):
        process = Mock()
        process.read.side_effect = OSError('unreadable')
        trace = TerminationTrace(process, Mock())
        trace.breakpoints[0x2000] = (b'\x55', '_invalid_parameter')
        row = {}
        trace.capture(0x2000, 9, bytearray(716), row)
        self.assertIn('capture_error', row)
        process.set_context.assert_called_once()
        process.write.assert_called_once_with(0x2000, b'\x55')

    def test_install_skips_forwarders_aliases_and_unloads(self):
        process = Mock()
        process.read.return_value = b'\x55'
        trace = TerminationTrace(process, Mock())
        image = Mock()
        image.FILE_HEADER.Machine = 0x14c
        image.OPTIONAL_HEADER.SizeOfImage = 4096
        image.DIRECTORY_ENTRY_EXPORT.symbols = [
            SimpleNamespace(name=b'_invalid_parameter', address=32, forwarder=None),
            SimpleNamespace(name=b'_invalid_parameter_noinfo', address=32, forwarder=None),
            SimpleNamespace(name=b'NtTerminateProcess', address=64, forwarder=b'other.export')]
        with patch('codol_rebuild.runtime.termination_trace.pefile.PE') as factory:
            factory.return_value.__enter__.return_value = image
            trace.module_loaded('test.dll', 0x1000)
            process.write.assert_called_once_with(0x1020, b'\xcc')
            trace.module_unloaded(0x1000)
            self.assertFalse(trace.breakpoints)
            trace.module_loaded('native64.dll', 0x100000000)
            factory.assert_called_once()

    def test_flag_defaults_on_and_can_be_disabled(self):
        self.assertTrue(parse_args(['--name', 'diagnostic']).trace_native_exit)
        self.assertTrue(parse_args(['--name', 'diagnostic', '--trace-native-exit']).trace_native_exit)
        self.assertFalse(parse_args(['--name', 'diagnostic', '--no-trace-native-exit']).trace_native_exit)
