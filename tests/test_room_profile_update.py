"""房内配装异步上传、生命周期和专用原生调用边回归。"""
import struct
import threading
import time
import unittest
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import Mock

import capstone

from codol_rebuild.application.room.service import RoomSession
from codol_rebuild.application.room.contracts import IdentityRecord, RoomError
from codol_rebuild.adapters.transport.http import HttpGateway
from codol_rebuild.adapters.game.v1649370 import member_profile
from test_refactor_equivalence import MemoryProcess
from test_member_profile import sample_profile


class ProfileUpdateSessionTests(unittest.TestCase):
    def setUp(self):
        self.gateway = Mock()
        self.session = RoomSession(self.gateway, Mock(), 'http://localhost', 'fingerprint')
        self.session._record = IdentityRecord('pl_me', 'rm_test')

    def tearDown(self):
        self.session.close()

    def completed(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = self.session.poll_update_profile()
            if result is not None:
                return result
            time.sleep(0.001)
        self.fail('档案上传未完成')

    def test_coalesces_latest_and_copies_submitted_snapshot(self):
        release_request = threading.Event()
        request_started = threading.Event()

        def upload(*parameters):
            request_started.set()
            release_request.wait(3)
            return {'roomId': parameters[1]}

        self.gateway.update_profile.side_effect = upload
        try:
            self.assertTrue(self.session.begin_update_profile('rm_test', {'selection': 1}))
            self.assertTrue(request_started.wait(1))
            self.assertTrue(self.session.begin_update_profile('rm_test', {'selection': 2}))
            latest_profile = {'selection': 3}
            self.assertTrue(self.session.begin_update_profile('rm_test', latest_profile))
            latest_profile['selection'] = 99
            self.assertEqual(self.gateway.update_profile.call_count, 1)
        finally:
            release_request.set()
        self.assertIsNone(self.completed().error)
        self.assertIsNone(self.completed().error)
        self.assertEqual([call.args[2] for call in self.gateway.update_profile.call_args_list],
                         [{'selection': 1}, {'selection': 3}])

    def test_create_enables_upload_and_failed_leave_keeps_upload_available(self):
        self.session._record = IdentityRecord('pl_me')
        self.gateway.create.return_value = {'roomId': 'rm_test', 'ownerPlayerId': 'pl_me',
                                            'members': [{'playerId': 'pl_me'}]}
        self.assertTrue(self.session.begin('create', '玩家', profile=sample_profile()))
        self.session._pending.result(timeout=2)
        self.assertIsNone(self.session.poll().error)
        self.assertTrue(self.session.begin_update_profile('rm_test', {'selection': 1}))
        self.completed()
        self.assertEqual(self.gateway.update_profile.call_args.args[:2], ('pl_me', 'rm_test'))
        self.gateway.leave.side_effect = RoomError('NETWORK_ERROR', '离房失败', uncertain=True)
        self.assertTrue(self.session.begin('leave', '玩家'))
        self.assertFalse(self.session.begin_update_profile('rm_test', {'selection': 2}))
        self.session._pending.result(timeout=2)
        self.assertIsNotNone(self.session.poll().error)
        self.assertTrue(self.session.begin_update_profile('rm_test', {'selection': 3}))
        self.completed()
        self.gateway.leave.side_effect = None

    def test_uncertain_failure_is_not_retried_or_automatically_followed(self):
        self.gateway.update_profile.side_effect = RoomError('NETWORK_ERROR', '超时', uncertain=True)
        self.session.begin_update_profile('rm_test', {'selection': 1})
        self.session.begin_update_profile('rm_test', {'selection': 2})
        self.assertTrue(self.completed().error.uncertain)
        self.assertIsNone(self.session.poll_update_profile())
        self.assertEqual(self.gateway.update_profile.call_count, 1)
        self.assertFalse(self.session.begin_update_profile('rm_test', {'selection': 3}))

    def test_http_upload_checks_confirmation_identity_and_put_timeout(self):
        gateway = HttpGateway('http://localhost')
        gateway._opener.open = Mock(side_effect=TimeoutError('timeout'))
        with self.assertRaises(RoomError) as raised:
            gateway.update_profile('pl_me', 'rm_test', {})
        self.assertTrue(raised.exception.uncertain)

        gateway._request = Mock(return_value={'roomId': 'rm_test', 'playerId': 'pl_me'})
        gateway.update_profile('pl_me', 'rm_test', {'selection': 1})
        gateway._request.assert_called_once_with('PUT', '/api/v1/rooms/rm_test/members/me/profile',
            {'profile': {'selection': 1}}, 'pl_me')
        gateway._request.return_value['playerId'] = 'pl_other'
        with self.assertRaises(RoomError) as raised:
            gateway.update_profile('pl_me', 'rm_test', {})
        self.assertTrue(raised.exception.uncertain)


class ProfileUpdateBridgeTests(unittest.TestCase):
    def setUp(self):
        self.process = MemoryProcess()
        self.capture_entry = self.process.allocate()
        self.display_buffer = self.process.allocate()
        self.bridge = member_profile.ProfileUpdateBridge(self.process, self.capture_entry, self.display_buffer)
        self.traps = self.bridge.install()
        self.context = bytearray(716)

    def test_only_submit_edge_changes_and_outside_room_retains_native_call(self):
        native_writes = [(address, value) for address, value in self.process.writes if address < 0x10000000]
        self.assertEqual(native_writes, [(member_profile.PROFILE_SUBMIT_SITE, self.bridge.plan['patch'])])
        self.assertEqual(len(native_writes[0][1]), 8)
        self.assertEqual(self.process.read(0x7C657E, 3), bytes.fromhex('57ffd0'))
        self.bridge.handle('profile_submit', self.context, None, False)
        self.assertEqual(struct.unpack_from('<I', self.context, 184)[0], self.bridge.plan['fallback'])

    def test_echo_and_initialization_skip_upload_but_user_change_captures_fresh_data(self):
        self.bridge.handle('profile_submit', self.context, 'rm_test', True)
        self.assertEqual(struct.unpack_from('<I', self.context, 184)[0], member_profile.PROFILE_SUBMIT_RETURN)
        self.bridge.handle('profile_submit', self.context, 'rm_test', False)
        self.assertEqual(struct.unpack_from('<I', self.context, 184)[0], self.bridge.plan['capture'])
        snapshot = member_profile.decode_snapshot(sample_profile())
        for field_name, address in (('public', member_profile.PUBLIC+0x90),
                                    ('secure', member_profile.SECURE+0x90),
                                    ('tf141', self.display_buffer), ('scar', self.display_buffer+0xA0)):
            self.process.write(address, snapshot[field_name])
        self.assertEqual(self.bridge.handle('profile_captured', self.context, 'rm_test', False), sample_profile())
        self.assertIsNone(self.bridge.handle('profile_captured', self.context, 'rm_test', False))

    def test_generated_capture_preserves_registers_and_has_valid_continuations(self):
        decoder = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
        for entry in (0x20000000, 0xf0000000):
            plan = member_profile.build_profile_submit(entry, 0x21000000)
            instructions = list(decoder.disasm(plan['code'], entry))
            self.assertEqual(sum(instruction.size for instruction in instructions), len(plan['code']))
            self.assertEqual([int(instruction.op_str, 16) for instruction in instructions
                              if instruction.mnemonic == 'call'], [0x21000000])
            self.assertEqual([int(instruction.op_str, 16) for instruction in instructions
                              if instruction.mnemonic == 'jmp'], [0x7C657E, 0x7C6581])
            self.assertEqual(plan['code'][plan['capture']-entry:plan['capture']-entry+2], bytes.fromhex('9c60'))

    def test_host_dispatches_captured_snapshot_and_suppresses_member_echo(self):
        from types import SimpleNamespace
        from codol_rebuild.adapters.game.v1649370.server_session_bridge import ServerSessionBridge
        self.process.context = Mock(return_value=('thread_handle', self.context))
        self.process.set_context = Mock()
        self.process.close_thread = Mock()
        port = Mock()
        host = ServerSessionBridge(self.process, port, Mock())
        host.notifications = Mock()
        host.profile_update = self.bridge
        host.traps = self.traps
        host._team_room_state = Mock(return_value=('rm_test', 0x30000000, 0x31000000))
        event = SimpleNamespace(tid=self.process.main_thread_id)
        addresses = {phase: address for address, phase in self.traps.items()}
        host.applying_members = True
        host.handle(event, addresses['profile_submit'])
        self.assertEqual(struct.unpack_from('<I', self.context, 184)[0], member_profile.PROFILE_SUBMIT_RETURN)
        port.begin_update_profile.assert_not_called()
        host.applying_members = False
        host.handle(event, addresses['profile_submit'])
        snapshot = member_profile.decode_snapshot(sample_profile())
        for field_name, address in (('public', member_profile.PUBLIC+0x90),
                                    ('secure', member_profile.SECURE+0x90),
                                    ('tf141', self.display_buffer), ('scar', self.display_buffer+0xA0)):
            self.process.write(address, snapshot[field_name])
        host.handle(event, addresses['profile_captured'])
        port.begin_update_profile.assert_called_once_with('rm_test', sample_profile())
        self.assertEqual(struct.unpack_from('<I', self.context, 184)[0], addresses['profile_captured']+1)

    def test_generated_submit_executes_in_independent_x86_process(self):
        """执行真实生成指令；原生采集器用破坏寄存器的替身，不等于游戏验收。"""
        from test_room_list_native import relocate_fixture_calls
        compiler = Path(os.environ.get('WINDIR', 'C:/Windows'))/'Microsoft.NET/Framework/v4.0.30319/csc.exe'
        if not compiler.exists():
            self.skipTest('需要 Windows .NET Framework x86 编译器')
        project_root = Path(__file__).resolve().parents[2]
        workspace_root = project_root/'verification/client/workspaces/profile-update-tests'
        workspace_root.mkdir(parents=True, exist_ok=True)
        plan = member_profile.build_profile_submit(0x20000000, 0x20001000)
        instruction_bytes = bytearray(plan['code'])
        for address in plan['traps']:
            instruction_bytes[address-0x20000000] = 0x90
        relocate_fixture_calls(instruction_bytes, 0x20000000)

        def wrapper(entry, target):
            # 保存 CLR 调用方寄存器；在调用前设置可辨识值并记录 ESP。
            code = bytearray(bytes.fromhex('9c60'))
            for opcode, value in ((0xb8, 0x12345678), (0xb9, 0x21000200),
                                   (0xba, 0x34567812), (0xbb, 0x45678123),
                                   (0xbd, 0x56781234), (0xbe, 0x67812345), (0xbf, 0x78123456)):
                code.extend(bytes([opcode])+struct.pack('<I', value))
            code.extend(bytes.fromhex('8925')+struct.pack('<I', 0x21000000)+b'\xf9')
            code.extend(b'\xe8'+struct.pack('<I', (target-entry-len(code)-5) & 0xffffffff))
            code.extend(bytes.fromhex('8925')+struct.pack('<I', 0x21000004))
            for register_index, offset in ((0, 8), (1, 12), (2, 16), (3, 20), (5, 24), (6, 28), (7, 32)):
                code.extend(bytes([0x89, 0x05+register_index*8])+struct.pack('<I', 0x21000000+offset))
            code.extend(bytes.fromhex('9c8f05')+struct.pack('<I', 0x21000024))
            code.extend(bytes.fromhex('619dc3'))
            return code

        with tempfile.TemporaryDirectory(prefix='native-', dir=workspace_root) as directory:
            workspace = Path(directory)
            (workspace/'submit.bin').write_bytes(instruction_bytes)
            (workspace/'capture.bin').write_bytes(wrapper(0x20002000, plan['capture']))
            (workspace/'fallback.bin').write_bytes(wrapper(0x20004000, plan['fallback']))
            executable = workspace/'fixture.exe'
            compiled = subprocess.run([str(compiler), '/nologo', '/platform:x86', '/out:'+str(executable),
                str(project_root/'client/tests/native/profile_update_fixture.cs')], capture_output=True, timeout=30)
            self.assertEqual(compiled.returncode, 0, compiled.stdout.decode('mbcs', errors='replace'))
            executed = subprocess.run([str(executable), str(workspace/'submit.bin'),
                str(workspace/'capture.bin'), str(workspace/'fallback.bin')], capture_output=True, timeout=15)
            self.assertEqual(executed.returncode, 0,
                             (executed.stdout+executed.stderr).decode('mbcs', errors='replace'))
