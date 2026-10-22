"""联机原生桥回归：ABI、分支边界、配置提取与失败时状态保留。"""
from pathlib import Path
import struct
from types import SimpleNamespace
import unittest
from unittest import mock
from unittest.mock import Mock
import capstone
import pefile
from codol_rebuild.adapters.game.v1649370 import multiplayer_bridge as bridge
from codol_rebuild.adapters.game.v1649370.room import build_room_adapter
from codol_rebuild.application.multiplayer.contracts import Completion, MultiplayerError


class Memory:
    """严格的独立内存片段，拒绝访问未准备地址。"""
    def __init__(self):
        self.data = {}
    def write(self, address, data):
        self.data.update({address+i: value for i, value in enumerate(data)})
    def read(self, address, length):
        return bytes(self.data[address+i] for i in range(length))
    def word(self, address, value):
        self.write(address, struct.pack('<I', value))


class MultiplayerBridgeTests(unittest.TestCase):
    def test_offline_generators_remain_byte_identical_to_baseline(self):
        from codol_rebuild.adapters.game.v1649370.menu_preview import build_menu_preview_helper
        root = Path(__file__).resolve().parent/'fixtures/refactor-20261020/client/v1649370'
        for name, function, arguments, keys in (
            ('room.py', build_room_adapter, (0x20000000, 0x21000000, 0x22000000), ('code', 'patches', 'entries')),
            ('menu_preview.py', build_menu_preview_helper, (0x20000000,), ('entry_bytes', 'patch_after')),
        ):
            scope = {}
            exec(compile((root/name).read_text(encoding='utf-8'), str(root/name), 'exec'), scope)
            expected = scope[function.__name__](*arguments)
            actual = function(*arguments)
            for key in keys:
                self.assertEqual(expected[key], actual[key])

    def test_image_contract_and_password_capacity_offsets(self):
        image = Path(__file__).resolve().parents[1]/'bin/v1649370/codol1649370-offline.exe'
        pe = pefile.PE(str(image))
        bridge.validate_contract(lambda address, size: pe.get_data(address-0x400000, size))
        memory = Memory()
        room = 0x20000000
        memory.write(room+0x105FE, b'\x06')
        memory.word(room+0x10608, 0x21000000)
        memory.write(0x21000000, '房间密码'.encode('utf-8')+b'\0')
        self.assertEqual(bridge.room_options(memory.read, room), (6, '房间密码'))
        memory.write(room+0x105FE, b'\0')
        with self.assertRaises(ValueError):
            bridge.room_options(memory.read, room)

    def test_all_generated_jumps_have_valid_destinations(self):
        decoder = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
        for entry in (0x20000000, 0xf0000000):
            room = build_room_adapter(entry, 0x21000000, 0x22000000, deferred=True, identity_address=0x23000018)
            tick = bridge.build_tick(entry+4096, 0x23000000, 0x22000000, 0x24000000, room['entries'])
            for plan, base, externals in (
                (room, entry, {0x633fe0, 0x801600, 0x7e7e30, 0x62fde6, 0x63e620, 0x62f640,
                               0x62af10, 0x67ec54, 0x7cad60, 0x7c64c0, 0x7c6581, 0x7c64b5, 0x77ffb0}),
                (tick, entry+4096, {0x24000000, 0x633fe0, 0x7cb7a0, 0x730280, *room['entries'].values()}),
            ):
                rows = list(decoder.disasm(plan['code'], base))
                starts = {row.address for row in rows}
                self.assertEqual(sum(row.size for row in rows), len(plan['code']))
                for row in rows:
                    if row.mnemonic.startswith('j') or row.mnemonic == 'call':
                        self.assertIn(int(row.op_str, 16), starts | externals)
                for trap in plan['traps']:
                    self.assertIn(trap, starts)
                    self.assertEqual(plan['code'][trap-base], 0xcc)
            self.assertEqual(set(room['traps'].values()), {'create', 'leave'})

    def setup_bridge(self):
        process, port = Memory(), Mock()
        port.invalidated.return_value = False
        value = bridge.MultiplayerBridge(process, port, Mock())
        value.state = 0x20000000
        process.write(value.state, bytes(4096))
        value.login_started = value.login_done = True
        room, session = 0x21000000, 0x22000000
        process.word(bridge.ROOM_POINTER, room)
        process.word(room+0x1C, session)
        process.word(session+0x25C8, room)
        process.write(session, b'\x01')
        return value, process, port, room, session

    def test_failed_leave_preserves_native_room(self):
        value, process, port, room, session = self.setup_bridge()
        value.pending = ('leave', room, session)
        port.poll.return_value = Completion('leave', error=MultiplayerError('NETWORK_ERROR', '断线'))
        value._poll()
        self.assertEqual(bridge.u32(process.read, value.state+bridge.ACTION), 4)
        self.assertEqual(bridge.u32(process.read, room+0x1C), session)
        self.assertEqual(process.read(session, 1), b'\x01')

    def test_stale_session_rejects_late_completion(self):
        value, process, port, room, session = self.setup_bridge()
        value.pending = ('create', room, session)
        process.word(room+0x1C, 0)
        port.poll.return_value = Completion('create', data={'roomId': 'rm_test'})
        with self.assertRaisesRegex(ValueError, '原生房间已改变'):
            value._poll()
        port.poll.assert_not_called()
        self.assertEqual(bridge.u32(process.read, value.state+bridge.ACTION), 0)

    def test_duplicate_native_request_does_not_resubmit(self):
        value, process, port, room, session = self.setup_bridge()
        value.pending = ('create', room, session)
        value._request('create', 0)
        port.begin.assert_not_called()

    def test_busy_poll_does_not_wait_or_complete(self):
        value, process, port, room, session = self.setup_bridge()
        value.pending = ('create', room, session)
        port.poll.return_value = None
        value._poll()
        self.assertEqual(bridge.u32(process.read, value.state+bridge.ACTION), 0)
        self.assertEqual(value.pending, ('create', room, session))

    def test_permission_failures_display_missing_file(self):
        value, process, port, room, session = self.setup_bridge()
        value.pending = ('leave', room, session)
        port.poll.return_value = Completion('leave', error=MultiplayerError('BANNED', '身份已封禁'))
        value._poll()
        self.assertEqual(process.read(value.state+bridge.MESSAGE, 64).split(b'\0')[0], '缺少文件'.encode('utf-8'))
        self.assertEqual(bridge.u32(process.read, value.state+bridge.ACTION), 4)
        value.record.assert_called_with({'multiplayer': 'leave', 'success': False, 'error': '缺少文件'})
        # 非权限失败仍显示原始错误。
        value.pending = ('leave', room, session)
        port.poll.return_value = Completion('leave', error=MultiplayerError('ROOM_FULL', '房间已满'))
        value._poll()
        value.record.assert_called_with({'multiplayer': 'leave', 'success': False, 'error': '房间已满'})

    def test_invalidated_session_resets_login_and_discards_late_results(self):
        value, process, port, room, session = self.setup_bridge()
        value.pending = ('create', room, session)
        port.invalidated.return_value = True
        port.begin.return_value = True
        port.poll.return_value = None
        with mock.patch.object(bridge, 'username', return_value='测试'):
            value._poll()
        self.assertFalse(value.login_done)
        self.assertTrue(value.login_started)
        self.assertIsNone(value.pending)
        self.assertTrue(value.discard_results)
        self.assertEqual(bridge.u32(process.read, value.state+bridge.LOGIN), 0)
        self.assertEqual(process.read(value.state+bridge.MESSAGE, 64).split(b'\0')[0], '缺少文件'.encode('utf-8'))
        # 丢弃窗口内迟到的创建结果不再投递原生回调。
        port.poll.return_value = Completion('create', data={'roomId': 'rm_test'})
        value._poll()
        value.record.assert_called_with({'multiplayer_discarded': 'create', 'success': True})
        # 重新鉴权成功后关闭丢弃窗口并恢复登录标志。
        port.poll.return_value = Completion('login', data={'playerId': 'pl_test'})
        value._poll()
        self.assertTrue(value.login_done)
        self.assertFalse(value.discard_results)
        self.assertEqual(bridge.u32(process.read, value.state+bridge.LOGIN), 1)

    def test_login_permission_failure_shows_popup_without_retry(self):
        value, process, port, room, session = self.setup_bridge()
        value.login_started, value.login_done = False, False
        port.begin.return_value = True
        port.poll.return_value = Completion('login', error=MultiplayerError('BANNED', '身份已封禁'))
        with mock.patch.object(bridge, 'username', return_value='测试'):
            value._poll()
        self.assertFalse(value.login_started)
        self.assertEqual(process.read(value.state+bridge.MESSAGE, 64).split(b'\0')[0], '缺少文件'.encode('utf-8'))
        self.assertEqual(bridge.u32(process.read, value.state+bridge.ACTION), 4)
