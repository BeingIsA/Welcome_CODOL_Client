"""联机原生桥回归：ABI、分支边界、配置提取与失败时状态保留。"""
from codol_rebuild.adapters.game.v1649370.session.session_tick import build_tick
from codol_rebuild.adapters.game.v1649370.native_state import IDENTITY
from codol_rebuild.application.errors import OnlineError
from pathlib import Path
import struct
from types import SimpleNamespace
import unittest
from unittest import mock
from unittest.mock import Mock
import capstone
import pefile
from codol_rebuild.adapters.game.v1649370.rooms import room_session_bridge as bridge
from codol_rebuild.adapters.game.v1649370.rooms.room import build_room_adapter
from codol_rebuild.application.room.contracts import Completion


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


def prepare_profile(memory, adapter):
    """为房间请求提供合法的固定版本双档案及已采集展示包。"""
    from codol_rebuild.adapters.game.v1649370.rooms import member_profile
    from codol_rebuild.adapters.game.v1649370 import layout
    adapter.profile_buffer = 0x28000000
    memory.write(adapter.profile_buffer, bytes(0x140))
    for address, identity in ((member_profile.PUBLIC, layout.PUBLIC_IDENTITY),
                              (member_profile.SECURE, layout.SECURE_IDENTITY)):
        memory.write(address+0x90, struct.pack('<II', identity['version'], identity['checksum'])
                     +bytes(layout.BUFFER_CAPACITY-8))


def created_snapshot():
    """创建完成替身使用完整服务端契约。"""
    settings = {key: 0 for key in bridge.SETTING_SCALARS}
    settings.update(mode='td', map='mp_crash')
    return {'roomId': 'rm_test', 'ownerPlayerId': 'pl_me', 'ownerDisplayName': '测试玩家',
            'maxPlayers': 4, 'playerCount': 1, 'settings': settings,
            'members': [{'playerId': 'pl_me', 'displayName': '测试玩家', 'isOwner': True,
                         'team': 'tf141', 'readyState': False}]}


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
    """按 RoomPort 契约模拟：主流程优先，设置结果未消费前 begin 拒绝。"""
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
            return Completion('create', data=created_snapshot())
        if not self.busy:
            return None
        self.busy = False
        return Completion('settings', data={'revision': 2})


class MultiplayerBridgeTests(unittest.TestCase):
    def test_departure_message_is_queued_only_after_native_cleanup(self):
        adapter, memory, port, room, session = self.setup_bridge()
        adapter.current_room_id = 'rm_current'
        adapter.notifications = Mock()
        adapter._apply_room_departure('room_closed', {'roomId': 'rm_current'})
        with self.assertRaisesRegex(ValueError, '原生房间未清理'):
            self._fire(adapter, memory, 'completed', 0x50000030)
        adapter.notifications.message.assert_not_called()
        memory.word(room+0x1C, 0)
        self._fire(adapter, memory, 'completed', 0x50000030)
        adapter.notifications.message.assert_called_once_with('管理员已关闭房间。')
        self.assertIsNone(adapter.current_room_id)
        self.assertFalse(adapter.is_owner)
        self.assertIsNone(adapter.departure_message)
        self._fire(adapter, memory, 'completed', 0x50000030)
        adapter.notifications.message.assert_called_once()
        port.begin.assert_not_called()

    def test_passive_departure_reuses_cleanup_without_submitting_http(self):
        for event_type, message in (('room_kicked', '你已被移出房间。'),
                                    ('room_closed', '管理员已关闭房间。')):
            with self.subTest(event_type=event_type):
                memory, adapter, data, room, session = self.join_fixture()
                adapter.pending = None
                adapter.current_room_id = data['roomId']
                memory.word(room+0x1C, session)
                memory.word(session+0x25C8, room)
                memory.write(session, b'\x01')
                adapter.notifications = Mock()
                adapter._apply_room_departure(event_type, {'roomId': 'rm_other'})
                self.assertIsNone(adapter.pending)
                adapter._apply_room_departure(event_type, data)
                self.assertEqual(adapter.pending, ('room_departure', room, session))
                self.assertEqual(adapter.departure_message, message)
                self.assertEqual(bridge.u32(memory.read, adapter.state+bridge.ACTION), 7)
                self.assertEqual(bridge.u32(memory.read, adapter.state+bridge.POLL), 1)
                self.assertEqual(bridge.u32(memory.read, adapter.state+bridge.SETTINGS_READY_OFFSET), 0)
                adapter.port.begin.assert_not_called()
                adapter.notifications.message.assert_not_called()

    def test_join_hook_is_installed_against_fixed_image(self):
        from test_refactor_equivalence import MemoryProcess
        from codol_rebuild.adapters.game.v1649370.rooms.room import JOIN_INPUT
        process = MemoryProcess()
        adapter = bridge.RoomSessionBridge(process, Mock(), Mock())
        adapter.allocate()
        room_plan = build_room_adapter(0x30000000, 0x31000000, 0x32000000, deferred=True,
                                       identity_address=adapter.state+IDENTITY)
        adapter.install_adapters(room_plan)
        patch = next(capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32).disasm(
            process.read(JOIN_INPUT, 5), JOIN_INPUT))
        target = int(patch.op_str, 16)
        self.assertEqual(adapter.traps[target+5], 'join_input')
        capture_call = next(capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32).disasm(
            process.read(target, 5), target))
        self.assertEqual(int(capture_call.op_str, 16), adapter.profile_capture)
        self.assertEqual(adapter.ownership, 0x31000000)

    def join_fixture(self):
        """准备独立内存，模拟两个服务端成员而不访问游戏进程。"""
        memory = Memory()
        room, session, state = 0x22000000, 0x23000000, 0x24000000
        # 原生已构造的成员区具有可读的阵营字段。
        memory.write(room+0xE368, bytes(18*0x1D0))
        memory.write(state, bytes(4096))
        memory.word(bridge.ROOM_POINTER, room)
        memory.word(room+0x1C, 0)
        memory.word(bridge.LANGUAGE, 0x25000000)
        memory.word(bridge.DIRECT_STRING_MODE, 1)
        port = Mock()
        port.begin.return_value = True
        adapter = bridge.RoomSessionBridge(memory, port, Mock())
        prepare_profile(memory, adapter)
        adapter.state = state
        adapter.join_scratch = 0x26000000
        adapter.player_id = 'pl_me'
        adapter.identity_ready = True
        adapter.pending = ('join', room, 0)
        settings = {key: (0.0 if form == '<f' else 0)
                    for key, (offset, form) in bridge.SETTING_SCALARS.items()}
        settings.update(mode='td', map='mp_crash')
        data = {'roomId': 'rm_target', 'ownerPlayerId': 'pl_owner', 'ownerDisplayName': '房主',
                'revision': 1, 'maxPlayers': 4, 'playerCount': 2, 'settings': settings,
                'members': [{'playerId': 'pl_owner', 'displayName': '房主', 'isOwner': True},
                            {'playerId': 'pl_me', 'displayName': '队友', 'isOwner': False}]}
        return memory, adapter, data, room, session

    def test_join_failure_preserves_password_reason_and_resets_for_next_failure(self):
        memory, adapter, data, room, session = self.join_fixture()
        for code, expected in (('INVALID_ROOM_PASSWORD', 5), ('ROOM_NOT_FOUND', 3),
                               ('ROOM_JOIN_BLOCKED', bridge.JOIN_BLOCKED_ERROR),
                               ('INVALID_ROOM_PASSWORD', 5), ('ROOM_CLOSED', 3)):
            with self.subTest(code=code):
                adapter.port.poll.return_value = Completion('join', error=OnlineError(code, '入房失败'))
                adapter.port.begin.reset_mock()
                with mock.patch.object(bridge, 'username', return_value='队友'):
                    adapter._poll()
                self.assertEqual(bridge.u32(memory.read, adapter.state+bridge.JOIN_ERROR_OFFSET), expected)
                self.assertEqual(bridge.u32(memory.read, adapter.state+bridge.ACTION), 6)
                expected_message = '@SF_UI_NOTE_EVER_BE_KICKED' if code == 'ROOM_JOIN_BLOCKED' else '入房失败'
                self.assertEqual(bridge.native_string(memory.read, adapter.state+bridge.MESSAGE), expected_message)
                self.assertEqual(bridge.u32(memory.read, room+0x1C), 0)
                adapter.port.begin.assert_called_once_with('leave', '队友')
        adapter._failure('join', '本地初始化失败')
        self.assertEqual(bridge.u32(memory.read, adapter.state+bridge.JOIN_ERROR_OFFSET), 3)

    def test_join_snapshot_keeps_local_identity_and_remote_owner(self):
        import hashlib
        memory, adapter, data, room, session = self.join_fixture()
        adapter._prepare_join_snapshot(data)
        memory.word(room+0x1C, session)
        memory.word(session+0x25C8, room)
        memory.write(session, b'\x01')
        adapter.pending = ('join', room, session)
        adapter._initialize_join_room()
        adapter._publish_join_snapshot()
        self.assertEqual(memory.read(room+0xE368, 8), hashlib.sha256(b'pl_me').digest()[:8])
        self.assertEqual(memory.read(session+0x20, 8), hashlib.sha256(b'pl_owner').digest()[:8])
        self.assertEqual(memory.read(room+0x338, 8), memory.read(session+0x20, 8))
        self.assertEqual(memory.read(room+0x278, 2), b'\0\x01')
        self.assertEqual(bridge.u32(memory.read, room+0x105D8), 2)
        self.assertFalse(adapter.is_owner)
        adapter._submit_settings(room+0x105F0)
        adapter.port.begin.assert_not_called()

    def test_settings_event_updates_only_settings_without_submitting(self):
        memory, adapter, data, room, session = self.join_fixture()
        adapter.pending = None
        adapter.current_room_id = data['roomId']
        adapter.is_owner = False
        memory.word(room+0x1C, session)
        memory.word(session+0x25C8, room)
        memory.write(session, b'\x01')
        adapter.port.poll_event.return_value = Completion('room_settings_updated', data=data)
        adapter._apply_room_settings(data)
        adapter.port.poll_event.assert_not_called()
        self.assertEqual(bridge.u32(memory.read, adapter.state+bridge.SETTINGS_READY_OFFSET), 1)
        self.assertEqual(bridge.u32(memory.read, adapter.state+bridge.SETTINGS_ROOM_OFFSET), room)
        self.assertEqual(memory.read(adapter.join_scratch, 3), b'td\0')
        self.assertEqual(memory.read(adapter.join_scratch+256, 9), b'mp_crash\0')
        adapter._submit_settings(room+0x105F0)
        adapter.port.begin.assert_not_called()
        # 原生会话已销毁时，迟到事件不得写入游戏对象。
        memory.word(room+0x1C, 0)
        before = dict(memory.data)
        adapter._apply_room_settings(data)
        self.assertEqual(memory.data, before)

    def test_member_snapshot_uses_server_team_ready_and_owner(self):
        """本机排首槽不影响服务端阵营；回显不会产生用户设置命令。"""
        memory, adapter, data, room, session = self.join_fixture()
        data['members'][0].update(team='tf141', readyState=False)
        data['members'][1].update(team='scar', readyState=True)
        import base64
        from test_member_profile import sample_profile
        for index, member in enumerate(data['members']):
            payload = sample_profile()
            secure = bytearray(base64.b64decode(payload['secure']))
            public = bytearray(base64.b64decode(payload['public']))
            struct.pack_into('<ii', secure, 4879, index+3, 60+index)
            for offset, value in zip((3854, 3846, 3842, 3850, 15527), range(101+index*10, 106+index*10)):
                struct.pack_into('<i', public, offset, value)
            payload.update(secure=base64.b64encode(secure).decode(), public=base64.b64encode(public).decode())
            member['profile'] = payload
        adapter._prepare_join_snapshot(data)
        memory.word(room+0x1C, session)
        memory.word(session+0x25C8, room)
        memory.write(session, b'\x01')
        adapter.pending = ('join', room, session)
        adapter._publish_join_snapshot()
        local, owner = room+0xE368, room+0xE368+0x1D0
        self.assertEqual(bridge.u32(memory.read, local+0x88), 1)
        self.assertEqual(bridge.u32(memory.read, owner+0x88), 2)
        self.assertEqual(memory.read(local+0x90, 1), b'\x01')
        self.assertEqual(memory.read(owner+0x90, 1), b'\0')
        self.assertEqual(memory.read(local+0x92, 1), b'\0')
        self.assertEqual(memory.read(owner+0x92, 1), b'\x01')
        self.assertEqual(memory.read(local+0x98, 2), bytes((60, 4)))
        self.assertEqual(memory.read(owner+0x98, 2), bytes((59, 3)))
        self.assertEqual(memory.read(local+0xB2, 10), struct.pack('<5H', *range(111, 116)))
        self.assertEqual(memory.read(owner+0xB2, 10), struct.pack('<5H', *range(101, 106)))
        adapter.port.begin.assert_not_called()

    def test_spectator_profile_is_published_on_join_and_team_transitions(self):
        """首次遇到观众也发布模型资料，不依赖其曾加入参战阵营留下的缓存。"""
        import hashlib
        from test_member_profile import sample_profile
        from codol_rebuild.adapters.game.v1649370.rooms.member_profile import decode_snapshot
        memory, adapter, data, room, session = self.join_fixture()
        owner_profile = sample_profile()
        data['members'][0].update(team='spectator', profile=owner_profile)
        data['members'][1].update(team='tf141', profile=sample_profile())
        adapter._prepare_join_snapshot(data)
        memory.word(room+0x1C, session)
        memory.word(session+0x25C8, room)
        memory.write(session, b'\x01')
        adapter.members_buffer = 0x27000000
        adapter.pending = ('join', room, session)
        decoded_profile = decode_snapshot(owner_profile)
        for owner_team, local_team in (('spectator', 'tf141'), ('spectator', 'spectator'),
                                       ('scar', 'spectator'), ('spectator', 'spectator'),
                                       ('spectator', 'spectator')):
            with self.subTest(owner_team=owner_team, local_team=local_team):
                data['members'][0]['team'] = owner_team
                data['members'][1]['team'] = local_team
                adapter._prepare_join_snapshot(data, joining=False)
                adapter._publish_join_snapshot()
                self.assertEqual(bridge.u32(memory.read, adapter.members_buffer+4), 1)
                packet = memory.read(adapter.members_buffer+16, 0xA0)
                self.assertEqual(packet[:8], hashlib.sha256(b'pl_owner').digest()[:8])
                display_profile = 'tf141' if owner_team == 'spectator' else owner_team
                self.assertEqual(packet[8:], decoded_profile[display_profile][8:])
                self.assertEqual(bridge.u32(memory.read, room+0xE368+0x1D0+0x88),
                                 6 if owner_team == 'spectator' else 1)
                adapter.pending = ('members', room, session)
        adapter.port.begin.assert_not_called()

    def test_realtime_dispatch_routes_settings_and_ignores_other_events(self):
        memory, adapter, data, room, session = self.join_fixture()
        adapter.pending = None
        adapter._apply_room_settings = Mock()
        adapter.port.poll_event.side_effect = [
            Completion('room_settings_updated', data=data),
            Completion('unhandled', data={}), None]
        before = dict(memory.data)
        for _ in range(3):
            adapter._poll_realtime_event()
        adapter._apply_room_settings.assert_called_once_with(data)
        self.assertEqual(memory.data, before)
        adapter.pending = ('leave', room, session)
        adapter._poll_realtime_event()
        self.assertEqual(adapter.port.poll_event.call_count, 3)

    def test_join_snapshot_rejects_capacity_and_nonfinite_settings_before_writes(self):
        memory, adapter, data, room, session = self.join_fixture()
        for broken in ({**data, 'members': data['members']*10},
                       {**data, 'settings': {**data['settings'], 'modeTimeLimit': float('nan')}}):
            before = dict(memory.data)
            with self.assertRaises(ValueError):
                adapter._prepare_join_snapshot(broken)
            self.assertEqual(memory.data, before)

    def test_join_input_copies_string_identifier_and_password(self):
        memory, adapter, data, room, session = self.join_fixture()
        adapter.pending = None
        frame = 0x27000000
        memory.word(frame+4, frame+64)
        memory.word(frame+28, frame+128)
        memory.write(frame+64, b'rm_target\0')
        memory.write(frame+128, '密码'.encode()+b'\0')
        with mock.patch.object(bridge, 'username', return_value='队友'):
            adapter._request_join(frame)
            adapter._request_join(frame)
        adapter.port.begin.assert_called_once_with('join', '队友', room_id='rm_target', password='密码',
            profile=bridge.member_profile.capture_snapshot(memory.read, adapter.profile_buffer))

    def test_cancelled_join_cleans_remote_membership_without_native_callback(self):
        memory, adapter, data, room, session = self.join_fixture()
        adapter.identity_ready = True
        adapter.join_cancelled = True
        adapter.port.poll.return_value = Completion('join', data=data)
        with mock.patch.object(bridge, 'username', return_value='队友'):
            adapter._poll()
        self.assertIsNone(adapter.pending)
        self.assertEqual(bridge.u32(memory.read, adapter.state+bridge.ACTION), 0)
        adapter.port.begin.assert_called_once_with('leave', '队友')

    def test_offline_generators_remain_byte_identical_to_baseline(self):
        from codol_rebuild.adapters.game.v1649370.frontend.menu_preview import build_menu_preview_helper
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

    def test_settings_detours_preserve_ui_arguments_and_return_to_callers(self):
        """UI调用边保留参数、虚调用与清栈；公共配置函数完全不打补丁。"""
        decoder = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
        for entry in (0x20000000, 0xf0000000):
            plan = bridge.build_settings_detours(entry)
            self.assertEqual([patch['va'] for patch in plan['patches']], [0x7CFE34, 0x7CFEFC])
            self.assertNotIn(0x6300A0, [patch['va'] for patch in plan['patches']])
            self.assertEqual(list(plan['traps'].values()), ['settings_command'] * 2)
            for patch, displacement in zip(plan['patches'], (0x30, 8)):
                target = (patch['va'] + 5 + struct.unpack('<i', patch['after'][1:5])[0]) & 0xffffffff
                rows = list(decoder.disasm(plan['code'][target-entry:target-entry+13], target))
                self.assertEqual([row.mnemonic for row in rows], ['lea', 'push', 'int3', 'call', 'jmp'])
                self.assertEqual(rows[0].op_str, f'edx, [esp + {hex(displacement) if displacement >= 10 else displacement}]')
                self.assertEqual(rows[1].op_str, 'edx')
                self.assertEqual(rows[3].op_str, 'eax')
                self.assertEqual(int(rows[4].op_str, 16), patch['va'] + 7)
                self.assertIn(rows[2].address, plan['traps'])
                self.assertEqual(patch['after'][5:], b'\x90\x90')

    def test_default_initialization_command_is_not_suppressed_by_member_display(self):
        """初始化是合法UI命令，即使成员展示尚未收尾也必须提交。"""
        value, process, port, room, session = self.setup_bridge()
        write_full_config(process, room, scalars={'modeScoreLimit': 75, 'playerMaxHealth': 100})
        value.applying_members = True
        with mock.patch.object(bridge, 'username', lambda read: '测试玩家'):
            value._submit_settings(room + 0x105F0)
        port.begin.assert_called_once()
        self.assertEqual(port.begin.call_args.kwargs['settings']['modeScoreLimit'], 75)

    def test_settings_call_edges_execute_with_balanced_x86_stack(self):
        """实际执行生成的跳转桩；不把反汇编检查当作调用栈验证。"""
        import os
        import subprocess
        import tempfile
        compiler = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Microsoft.NET/Framework/v4.0.30319/csc.exe'
        if not compiler.exists():
            self.skipTest('需要Windows .NET x86编译器')
        root = Path(__file__).resolve().parents[2]
        workspace_root = root / 'verification/client/workspaces/room-settings-tests'
        workspace_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=workspace_root) as directory:
            workspace = Path(directory)
            plan = bridge.build_settings_detours(0x20000000)
            instruction_bytes = bytearray(plan['code'])
            for address in plan['traps']:
                instruction_bytes[address-0x20000000] = 0x90
            from test_room_list_native import relocate_fixture_calls
            relocate_fixture_calls(instruction_bytes, 0x20000000)
            paths = [workspace / 'adapter.bin']
            paths[0].write_bytes(instruction_bytes)
            for index, patch in enumerate(plan['patches']):
                paths.append(workspace / f'edge-{index}.bin')
                relocated_patch = bytearray(patch['after'])
                target = (patch['va'] + 5 + struct.unpack('<i', relocated_patch[1:5])[0]) & 0xffffffff
                relocated_address = 0x28000000 + (patch['va'] & 0xffff)
                struct.pack_into('<I', relocated_patch, 1, (target-relocated_address-5) & 0xffffffff)
                paths[-1].write_bytes(relocated_patch)
            executable = workspace / 'fixture.exe'
            compiled = subprocess.run([str(compiler), '/nologo', '/platform:x86', '/out:'+str(executable),
                str(root / 'client/tests/native/room_settings_fixture.cs')], capture_output=True, timeout=30)
            self.assertEqual(compiled.returncode, 0, compiled.stdout.decode('mbcs', errors='replace'))
            executed = subprocess.run([str(executable), *map(str, paths)], capture_output=True, timeout=15)
            self.assertEqual(executed.returncode, 0, (executed.stdout+executed.stderr).decode('mbcs', errors='replace'))

    def test_member_display_movie_patch_is_local_and_preserves_stack(self):
        """固定电影仅取消成员派生广播，初始化和用户设置代码保持原字节。"""
        from codol_rebuild.adapters.game.v1649370.rooms import room
        from codol_rebuild.adapters.game.v1649370 import movie_identity
        from codol_rebuild.adapters.game.v1649370.session import ui_movie
        root = Path(__file__).resolve().parents[2]
        original = (root / 'research/client/ui-investigation/codo.gfx').read_bytes()
        patched = movie_identity.apply_rules(original, room.replacements())
        self.assertEqual(len(room.replacements()), 1)
        self.assertEqual([index for index, (before, after) in enumerate(zip(original, patched))
                          if before != after], list(range(5101924, 5101928)))
        self.assertIn(room.replacements()[0], ui_movie.replacements())
        self.assertEqual(original[5101928], 0x29)
        # 保留广播调用之前的栈前缀，替换后连同原 pop 一起净消耗三个操作数。
        stack = ['preserved', 'event_manager', 'event_name', 'room']
        for instruction in patched[5101924:5101929]:
            if instruction == 0x29:
                stack.pop()
            elif instruction == 0x21:
                stack.append(None)
            else:
                self.fail(f'非预期AVM2指令：{instruction:#x}')
        self.assertEqual(stack, ['preserved'])

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
        struct.pack_into('<I', ctx2, 196, 0x30000100)
        struct.pack_into('<I', ctx2, 184, 0x50000020)
        process.context = Mock(return_value=(1, ctx2))
        process.word(0x30000100, room+0x105F0)
        value.traps = {0x50000020: 'settings_command'}
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
        struct.pack_into('<I', ctx, 196, 0x30000100)  # UI调用边栈顶
        struct.pack_into('<I', ctx, 184, 0x50000010)  # eip = 陷阱地址
        process.word(0x30000100, room+0x105F0)        # [esp] = 配置指针
        value.traps = {0x50000010: 'settings_command'}
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
        if phase == 'settings_command':
            esp = 0x30000100
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
        process.word(0x30000100, room+0x105F0)  # [esp] = 配置指针

        # 建房界面首次应用配置：占用通道，同时必须武装轮询让帧桩取走结果。
        write_full_config(process, room, mode='td', map_name='mp_crash')
        self._fire(value, process, 'settings_command', 0x50000010)
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
        self._fire(value, process, 'settings_command', 0x50000010)
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
        process.word(0x30000100, room+0x105F0)
        process.word(0x30000010, room)
        process.word(0x30000000, session)

        # 改设置后立刻建房：补交在途时被主流程抢占。
        write_full_config(process, room, mode='td', map_name='mp_crash')
        self._fire(value, process, 'settings_command', 0x50000010)
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
        self._fire(value, process, 'settings_command', 0x50000010)
        self.assertEqual(port.submitted[-1]['mode'], 'sd')
        self.assertNotIn('skipped', [r.get('multiplayer_settings') for r in records])

    def test_all_generated_jumps_have_valid_destinations(self):
        decoder = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
        for entry in (0x20000000, 0xf0000000):
            room = build_room_adapter(entry, 0x21000000, 0x22000000, deferred=True, identity_address=0x23000018)
            from codol_rebuild.adapters.game.v1649370.rooms.room import build_join_adapter
            joined = build_join_adapter(entry+8192, 0x23000000, 0x25000000)
            entries = {**room['entries'], **joined['entries']}
            tick = build_tick(entry+4096, 0x23000000, 0x22000000, 0x24000000, entries)
            for plan, base, externals in (
                (room, entry, {0x633fe0, 0x801600, 0x7e7e30, 0x62fde6, 0x63e620, 0x62f640,
                               0x62af10, 0x67ec54, 0x7cad60, 0x7c64c0, 0x7c6581, 0x7c64b5, 0x77ffb0}),
                (tick, entry+4096, {0x24000000, 0x633fe0, 0x7cb7a0, 0x730280, *entries.values()}),
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
            self.assertEqual(set(room['traps'].values()), {'create', 'leave', 'member_initialized'})

    def setup_bridge(self):
        process, port = Memory(), Mock()
        value = bridge.RoomSessionBridge(process, port, Mock())
        prepare_profile(process, value)
        value.player_id = 'pl_me'
        value.join_scratch = 0x29000000
        process.word(bridge.LANGUAGE, 0x25000000)
        process.word(bridge.DIRECT_STRING_MODE, 1)
        value.state = 0x20000000
        process.write(value.state, bytes(4096))
        value.identity_ready = True
        room, session = 0x21000000, 0x22000000
        process.word(bridge.ROOM_POINTER, room)
        process.word(room+0x1C, session)
        process.word(session+0x25C8, room)
        process.write(session, b'\x01')
        return value, process, port, room, session

    def test_failed_leave_preserves_native_room(self):
        value, process, port, room, session = self.setup_bridge()
        value.pending = ('leave', room, session)
        port.poll.return_value = Completion('leave', error=OnlineError('NETWORK_ERROR', '断线'))
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
        port.poll.return_value = Completion('leave', error=OnlineError('BANNED', '身份已封禁'))
        value._poll()
        self.assertEqual(process.read(value.state+bridge.MESSAGE, 64).split(b'\0')[0], '缺少文件'.encode('utf-8'))
        self.assertEqual(bridge.u32(process.read, value.state+bridge.ACTION), 4)
        value.record.assert_called_with({'multiplayer': 'leave', 'success': False, 'error': '缺少文件'})
        # 非权限失败仍显示原始错误。
        value.pending = ('leave', room, session)
        port.poll.return_value = Completion('leave', error=OnlineError('ROOM_FULL', '房间已满'))
        value._poll()
        value.record.assert_called_with({'multiplayer': 'leave', 'success': False, 'error': '房间已满'})

    def test_completed_login_does_not_request_reauthentication(self):
        value, process, port, room, session = self.setup_bridge()
        value.identity_ready = True
        port.poll.return_value = None
        for _ in range(3):
            value._poll()
        port.begin.assert_not_called()

    def test_login_permission_failure_shows_popup_without_retry(self):
        from codol_rebuild.adapters.game.v1649370.session import online_session
        from codol_rebuild.application.session.contracts import LoginCompletion
        value, process, port, room, session = self.setup_bridge()
        port.begin_login.return_value = True
        port.poll_login.side_effect = [LoginCompletion(error=OnlineError('BANNED', '身份已封禁')), None, None]
        login_bridge = online_session.OnlineSessionBridge(process, port, value.state, Mock(), Mock())
        with mock.patch.object(online_session, 'username', return_value='测试'):
            login_bridge.poll()
            login_bridge.poll()
            login_bridge.poll()
        self.assertTrue(login_bridge.started)
        self.assertEqual(process.read(value.state+bridge.MESSAGE, 64).split(b'\0')[0], '缺少文件'.encode('utf-8'))
        self.assertEqual(bridge.u32(process.read, value.state+bridge.ACTION), 4)
        port.begin_login.assert_called_once()
