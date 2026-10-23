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


def write_full_config(memory, room, maximum=6, password=None, mode='', map_name='',
                      scalars=None):
    """构造一份完整的 0x70 原生房间配置（含字符串与标量）。"""
    config = room+0x105F0
    scalars = scalars or {}
    texts = {'mode': mode, 'map': map_name, 'password': password or ''}
    pointers = {}
    for index, key in enumerate(('mode', 'map', 'password')):
        pointer = 0x31000000+index*0x100
        memory.write(pointer, texts[key].encode('utf-8')+b'\0')
        pointers[key] = pointer
        continue
    buffer = bytearray(0x70)
    struct.pack_into('<BB', buffer, 0x0E, maximum, maximum)  # maxPlayer 及其镜像
    for key, (offset, form) in bridge.SETTING_SCALARS.items():
        value = scalars.get(key, 0)
        struct.pack_into(form, buffer, offset, int(value) if form != '<f' else value)
    memory.write(config, bytes(buffer))
    # 字符串指针在整块写入之后写回，避免被上面的裸块覆盖。
    for key, offset in {'mode': 0, 'map': 8, 'password': 0x18}.items():
        memory.word(config+offset, pointers[key])


class SettingsChannelPort:
    """按 MultiplayerPort 契约模拟：主流程优先，设置结果未消费前 begin 拒绝。"""
    def __init__(self):
        self.busy = False
        self.main = False
        self.submitted = []

    def begin(self, operation, username, **kwargs):
        if operation != 'settings':
            self.main = True
            return True
        if self.busy:
            return False
        self.busy = True
        self.submitted.append(kwargs['settings'])
        return True

    def poll(self):
        if self.main:
            self.main = False
            return Completion('create', data={'roomId': 'rm_test'})
        if not self.busy:
            return None
        self.busy = False
        return Completion('settings', data={'revision': 2})


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
        write_full_config(memory, room, maximum=6, password='房间密码',
                          mode='td', map_name='mp_crash_tropical',
                          scalars={'modeTimeLimit': 10.5, 'modeScoreLimit': 7500,
                                   'playerMaxHealth': 100, 'teamFFType': 2,
                                   'weaponRestrictPreset': 0xFF,
                                   'weaponRestrictMask': 0x8000})
        maximum, password, settings = bridge.room_options(memory.read, room)
        self.assertEqual((maximum, password), (6, '房间密码'))
        self.assertEqual(settings['mode'], 'td')
        self.assertEqual(settings['map'], 'mp_crash_tropical')
        self.assertEqual(settings['modeTimeLimit'], 10.5)
        self.assertEqual(settings['modeScoreLimit'], 7500)
        self.assertEqual(settings['playerMaxHealth'], 100)
        self.assertEqual(settings['teamFFType'], 2)
        self.assertEqual(settings['weaponRestrictPreset'], 0xFF)
        self.assertEqual(settings['weaponRestrictMask'], 0x8000)
        memory.write(room+0x105FE, b'\0')
        with self.assertRaises(ValueError):
            bridge.room_options(memory.read, room)

    def test_settings_detour_wraps_submit_method_entry(self):
        plan = bridge.build_settings_detours(0x50000000)
        decoder = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
        rows = list(decoder.disasm(plan['code'], 0x50000000))
        self.assertEqual(sum(row.size for row in rows), len(plan['code']))
        # 单陷阱在桩首；原序幕 6 字节原样保留后跳回方法体 0x6300A6。
        self.assertEqual(list(plan['traps'].values()), ['settings_submit'])
        trap = next(iter(plan['traps']))
        self.assertEqual(plan['code'][trap-0x50000000], 0xCC)
        self.assertEqual(plan['code'][trap-0x50000000+1:trap-0x50000000+7],
                         bytes.fromhex('558bec83ec10'))
        back = next(r for r in rows if r.mnemonic == 'jmp')
        self.assertEqual(int(back.op_str, 16), bridge.SETTINGS_SUBMIT+6)
        # 补丁：原序幕 6 字节替换为 E9 JMP + NOP（JMP 不压栈，入口栈与
        # 原方法一致），目标落在陷阱字节上。
        patch = plan['patches'][0]
        self.assertEqual(patch['va'], bridge.SETTINGS_SUBMIT)
        self.assertEqual(patch['before'], bytes.fromhex('558bec83ec10'))
        self.assertEqual(patch['after'][0], 0xE9)
        self.assertEqual(patch['after'][5], 0x90)
        target = patch['va']+5+struct.unpack('<i', patch['after'][1:5])[0]
        self.assertEqual(target, trap)

    def test_create_and_identical_lobby_settings_are_separate_requests(self):
        # 创建与后续设置是独立请求；即使配置相同，也不能按内容去重。
        value, process, port, room, session = self.setup_bridge()
        write_full_config(process, room, mode='td', map_name='mp_x')
        ctx = bytearray(716)
        struct.pack_into('<I', ctx, 184, 0x50000010)
        struct.pack_into('<I', ctx, 196, 0x30000000)
        process.word(0x30000010, room)  # frame+16 = room
        process.word(0x30000000, session)
        value.traps = {0x50000010: 'create'}
        event = SimpleNamespace(tid=1)
        process.context = Mock(return_value=(1, ctx))
        process.set_context = Mock()
        process.close_thread = Mock()
        process.main_thread_id = 1
        with mock.patch.object(bridge, 'username', lambda read: '测试玩家'):
            value.handle(event, 0x50000010)
        self.assertEqual(port.begin.call_args.args[0], 'create')
        baseline = port.begin.call_args.kwargs['settings']
        # 大厅再次应用同一份配置：仍产生 settings 提交。
        ctx2 = bytearray(716)
        struct.pack_into('<I', ctx2, 196, 0x30000000)
        struct.pack_into('<I', ctx2, 184, 0x50000020)
        process.context = Mock(return_value=(1, ctx2))
        process.word(0x30000004, room+0x105F0)
        value.traps = {0x50000020: 'settings_submit'}
        value.pending = None
        with mock.patch.object(bridge, 'username', lambda read: '测试玩家'):
            value.handle(event, 0x50000020)
        self.assertEqual(port.begin.call_count, 2)
        self.assertEqual(port.begin.call_args.args[0], 'settings')
        # 配置变化后同样提交。
        write_full_config(process, room, mode='sd', map_name='mp_x')
        with mock.patch.object(bridge, 'username', lambda read: '测试玩家'):
            value.handle(event, 0x50000020)
        self.assertEqual(port.begin.call_count, 3)
        self.assertEqual(port.begin.call_args.args[0], 'settings')
        self.assertEqual(baseline['mode'], 'td')

    def test_settings_trap_submits_without_blocking_native(self):
        value, process, port, room, session = self.setup_bridge()
        write_full_config(process, room)
        ctx = bytearray(716)
        struct.pack_into('<I', ctx, 196, 0x30000000)  # esp
        struct.pack_into('<I', ctx, 184, 0x50000010)  # eip = 陷阱地址
        process.word(0x30000004, room+0x105F0)        # [esp+4] = 配置指针
        value.traps = {0x50000010: 'settings_submit'}
        event = SimpleNamespace(tid=1)
        process.context = Mock(return_value=(1, ctx))
        process.set_context = Mock()
        process.close_thread = Mock()
        process.main_thread_id = 1
        with mock.patch.object(bridge, 'username', lambda read: '测试玩家'):
            value.handle(event, 0x50000010)
        port.begin.assert_called_once()
        self.assertEqual(port.begin.call_args.args[0], 'settings')

    def _fire(self, value, process, phase, address, esp=0x30000000):
        """按主线程陷阱约定派发一次 handle；ctx 带 esp 与待回写的 eip。"""
        ctx = bytearray(716)
        struct.pack_into('<I', ctx, 196, esp)
        struct.pack_into('<I', ctx, 184, address)
        process.context = Mock(return_value=(1, ctx))
        process.set_context = Mock()
        process.close_thread = Mock()
        process.main_thread_id = 1
        value.traps = {address: phase}
        with mock.patch.object(bridge, 'username', lambda read: '测试玩家'):
            value.handle(SimpleNamespace(tid=1), address)

    def test_settings_submit_rearms_polling_so_channel_is_not_wedged(self):
        """回归：设置补交必须重新武装轮询，否则通道被首个 future 永久占住。

        建房收尾会把 POLL 归 0，而设置结果只能由 _poll 消费；不武装轮询时
        通道再也腾不出来，房内后续改地图与改设置全部被丢成 busy
        （2026-10-23 实机连续四次 skipped 的成因）。
        """
        value, process, _unused, room, session = self.setup_bridge()
        port = SettingsChannelPort()
        value.port = port
        records = []
        value.record = records.append
        process.word(0x30000004, room+0x105F0)  # [esp+4] = 配置指针

        # 建房界面首次应用配置：占用通道，同时必须武装轮询让帧桩取走结果。
        write_full_config(process, room, mode='td', map_name='mp_crash')
        self._fire(value, process, 'settings_submit', 0x50000010)
        self.assertTrue(port.busy)
        self.assertEqual(bridge.u32(process.read, value.state+bridge.POLL), 1)

        # 帧桩据 POLL 轮询：取走结果后无主流程在途，停止轮询不空转。
        value._poll()
        self.assertFalse(port.busy)
        self.assertEqual(records[-1]['multiplayer_settings'], 'done')
        self.assertEqual(bridge.u32(process.read, value.state+bridge.POLL), 0)

        # 建房主流程收尾同样把 POLL 归 0。
        process.word(0x30000010, room)  # frame+16 = room
        process.word(0x30000000, session)
        self._fire(value, process, 'create', 0x50000010)
        self.assertEqual(value.pending[0], 'create')
        value._poll()  # 取走建房结果，ACTION 置 1
        self._fire(value, process, 'completed', 0x50000030)
        self.assertIsNone(value.pending)
        self.assertEqual(bridge.u32(process.read, value.state+bridge.POLL), 0)

        # 房内改地图：通道已腾空，必须真正提交而不是丢成 busy。
        write_full_config(process, room, mode='td', map_name='mp_crash_tropical')
        self._fire(value, process, 'settings_submit', 0x50000010)
        self.assertEqual(bridge.u32(process.read, value.state+bridge.POLL), 1)
        value._poll()
        self.assertEqual(port.submitted[-1]['map'], 'mp_crash_tropical')
        self.assertEqual(records[-1]['multiplayer_settings'], 'done')
        self.assertNotIn('skipped', [r.get('multiplayer_settings') for r in records])

    def test_create_completion_does_not_strand_outstanding_settings(self):
        """回归：设置补交在途时建房收尾不得停轮询，否则结果被搁浅。

        主流程占用 poll 期间设置结果取不出来；若收尾把 POLL 归 0，这个
        future 就再也没人消费，通道永久 busy。
        """
        value, process, _unused, room, session = self.setup_bridge()
        port = SettingsChannelPort()
        value.port = port
        records = []
        value.record = records.append
        process.word(0x30000004, room+0x105F0)
        process.word(0x30000010, room)
        process.word(0x30000000, session)

        # 改设置后立刻建房：补交在途时被主流程抢占。
        write_full_config(process, room, mode='td', map_name='mp_crash')
        self._fire(value, process, 'settings_submit', 0x50000010)
        self.assertTrue(port.busy)
        self._fire(value, process, 'create', 0x50000010)
        self.assertEqual(value.pending[0], 'create')
        value._poll()  # 主流程优先，设置结果仍取不出来
        self.assertTrue(port.busy)

        # 建房收尾必须因设置补交在途而保持轮询。
        self._fire(value, process, 'completed', 0x50000030)
        self.assertIsNone(value.pending)
        self.assertEqual(bridge.u32(process.read, value.state+bridge.POLL), 1)

        # 主流程腾出后取走搁浅结果，通道恢复可用并停止轮询。
        value._poll()
        self.assertFalse(port.busy)
        self.assertEqual(records[-1]['multiplayer_settings'], 'done')
        self.assertEqual(bridge.u32(process.read, value.state+bridge.POLL), 0)
        write_full_config(process, room, mode='sd', map_name='mp_crash_tropical')
        self._fire(value, process, 'settings_submit', 0x50000010)
        self.assertEqual(port.submitted[-1]['mode'], 'sd')
        self.assertNotIn('skipped', [r.get('multiplayer_settings') for r in records])

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

    def test_completed_login_does_not_request_reauthentication(self):
        value, process, port, room, session = self.setup_bridge()
        value.login_started, value.login_done = True, True
        port.poll.return_value = None
        for _ in range(3):
            value._poll()
        port.begin.assert_not_called()

    def test_login_permission_failure_shows_popup_without_retry(self):
        value, process, port, room, session = self.setup_bridge()
        value.login_started, value.login_done = False, False
        port.begin.return_value = True
        port.poll.return_value = Completion('login', error=MultiplayerError('BANNED', '身份已封禁'))
        with mock.patch.object(bridge, 'username', return_value='测试'):
            value._poll()
        self.assertTrue(value.login_started)
        self.assertEqual(process.read(value.state+bridge.MESSAGE, 64).split(b'\0')[0], '缺少文件'.encode('utf-8'))
        self.assertEqual(bridge.u32(process.read, value.state+bridge.ACTION), 4)
        port.poll.return_value = None
        value._poll()
        value._poll()
        port.begin.assert_called_once()
