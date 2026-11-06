"""原生对局边界的来源过滤与回调代码约定测试；不替代实机。"""
from pathlib import Path
import struct
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
import capstone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from codol_rebuild.adapters.game.v1649370.matches.session_bridge import MatchBridge, ROOM_POINTER, FIND_CALL, BINARY_SLOT, NOTIFICATION_SLOT, NOTIFICATION_FRAME_CALL, IDENTITY_CALLS


class MemoryProcess:
    """只模拟受控进程内存与上下文，不模拟原生业务成功。"""
    main_thread_id = 1
    def __init__(self):
        self.memory = {}
        self.next_address = 0x20000000
        self.registers = bytearray(716)
    def read(self, address, length):
        return bytes(self.memory.get(address+offset, 0) for offset in range(length))
    def write(self, address, value):
        if not isinstance(value, bytes):
            raise TypeError('原生进程写入接口仅接受bytes')
        self.memory.update((address+offset, byte) for offset, byte in enumerate(value))
    def allocate(self, length):
        address = self.next_address
        self.next_address += 4096
        return address
    def executable(self, address, length): pass
    def context(self, thread_id): return 1, self.registers
    def set_context(self, thread, context): self.registers = context
    def close_thread(self, thread): pass


class MatchBridgeTests(unittest.TestCase):
    def setUp(self):
        self.process = MemoryProcess()
        self.session = Mock()
        self.session.poll_start.return_value = None
        self.bridge = MatchBridge(self.process, self.session, lambda: 'room', '127.0.0.2', Mock())
        self.process.write(FIND_CALL, bytes.fromhex('e87cb11c00'))
        self.process.write(BINARY_SLOT, struct.pack('<I', 0x629730))
        self.process.write(NOTIFICATION_SLOT, struct.pack('<I', 0x62C580))
        self.process.write(NOTIFICATION_FRAME_CALL, bytes.fromhex('e8726efeff'))
        for call_address, original_bytes in IDENTITY_CALLS:
            self.process.write(call_address, original_bytes)
        self.bridge.install(0x42000)

    def test_starting_only_notifies_ui_without_consuming_ready(self):
        self.process.write(0x7CB210, bytes.fromhex('558bec83e4f883ec'))
        entry = self.bridge.apply_start_event(
            {'roomId': 'room', 'startId': 'first', 'status': 'starting'}, 0x500123)
        instructions = list(capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32).disasm(
            self.process.read(entry, 19), entry))
        self.assertEqual([instruction.op_str for instruction in instructions
                          if instruction.mnemonic == 'call'], ['0x7cb210'])
        self.assertEqual(instructions[-1].op_str, '0x500123')
        self.session.acknowledge_wait.assert_called_once_with('room', 'first')
        self.session.acknowledge_start.assert_not_called()
        self.session.begin.assert_not_called()

    def test_allocation_uses_client_config_address_and_validates_port(self):
        self.bridge.room_id = 'room'
        self.bridge.room_pointer = 0
        self.bridge.callback = Mock(return_value=123)
        for server_address in ('127.0.0.1', '47.114.209.151'):
            self.bridge.server_address = server_address
            for port, accepted in ((1, True), (27760, True), (65535, True), (0, False), (65536, False)):
                self.session.poll.return_value = SimpleNamespace(operation='allocate', error=None, data={
                    'port': port, 'securityId': bytes(8).hex(), 'securityKey': bytes(16).hex(),
                    'serverUserId': '5', 'serverName': 'fixture', 'maxPlayers': 6})
                self.bridge.poll(0x500123)
                self.bridge.callback.assert_called_with(0x500123, accepted)
                if accepted:
                    from ipaddress import IPv4Address
                    self.assertEqual(dict(self.bridge.callback_fields)[0x18][:6],
                        IPv4Address(server_address).packed + struct.pack('<H', port))

    def test_formal_ready_calls_original_start_match_with_preserved_return(self):
        self.session.poll_start.return_value = {'roomId': 'room', 'startId': 'first', 'status': 'ready'}
        self.process.write(ROOM_POINTER, struct.pack('<I', 0x40000))
        self.process.write(0x4001c, struct.pack('<I', 0x50000))
        self.process.write(0x40278, b'\x01')
        self.process.write(0x62F840, bytes.fromhex('558becb814200000'))
        entry = self.bridge.poll(0x500123)
        instructions = list(capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32).disasm(self.process.read(entry, 25), entry))
        self.assertEqual([(instruction.mnemonic, instruction.op_str) for instruction in instructions], [
            ('pushfd',''),('pushal',''),('mov','ecx, 0x40000'),('push','0'),('push','-1'),('push','1'),
            ('call','0x62f840'),('popal',''),('popfd',''),('jmp','0x500123')])
        self.session.acknowledge_start.assert_called_once_with('room', 'first')

    def test_member_ready_uses_connection_stage_without_calling_host_start(self):
        self.session.poll_start.return_value = {'roomId': 'room', 'startId': 'first', 'status': 'ready'}
        self.session.poll.return_value = None
        self.process.write(ROOM_POINTER, struct.pack('<I', 0x40000))
        self.process.write(0x4001c, struct.pack('<I', 0x50000))
        self.assertEqual(self.bridge.poll(0x500123), 0x500123)
        self.bridge.poll(0x500123)
        self.session.begin.assert_called_once_with('member_connect', 'room', start_id='first')
        self.session.acknowledge_start.assert_not_called()
        self.assertEqual(self.process.read(0x40278, 1), b'\x00')

    def test_member_serializer_uses_native_lifecycle_and_preserves_resume(self):
        self.bridge.callback_fields = ()
        entry = self.bridge.member_notification(0x500123)
        instructions = list(capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32).disasm(
            self.process.read(entry, 160), entry))
        calls = [instruction.op_str for instruction in instructions if instruction.mnemonic == 'call']
        self.assertEqual(calls, ['0x806b50', '0xb74220', '0xb778e0', 'dword ptr [0xca8128]', '0x625be0'])
        self.assertEqual(next(instruction.op_str for instruction in instructions if instruction.mnemonic == 'jmp'), '0x500123')

    def test_member_notification_executes_with_balanced_x86_stack(self):
        import os
        import subprocess
        import tempfile
        compiler = Path(os.environ.get('WINDIR', 'C:/Windows'))/'Microsoft.NET/Framework/v4.0.30319/csc.exe'
        if not compiler.exists():
            self.skipTest('需要 Windows x86 C# 编译器')
        project = Path(__file__).resolve().parents[2]
        workspace_root = project/'verification/client/workspaces/member-notification'
        workspace_root.mkdir(parents=True, exist_ok=True)
        self.bridge.constructed = True
        self.bridge.callback_fields = ()
        entry = self.bridge.member_notification(0x22002000)
        code = bytearray(self.process.read(entry, 160))
        relocated = {0xB74220: 0x29004220, 0xB778E0: 0x290078E0, 0x625BE0: 0x28005BE0}
        for instruction in capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32).disasm(code, entry):
            if instruction.mnemonic == 'call' and instruction.bytes[0] == 0xE8:
                target = int(instruction.op_str, 16)
                if target in relocated:
                    struct.pack_into('<i', code, instruction.address-entry+1, relocated[target]-instruction.address-5)
        code = code.replace(struct.pack('<I', 0xCA8128), struct.pack('<I', 0x2A008128))
        for address, phase in self.bridge.traps.items():
            if phase == 'member_serialized':
                code[address-entry] = 0x90
        with tempfile.TemporaryDirectory(dir=workspace_root) as directory:
            workspace = Path(directory)
            (workspace/'member.bin').write_bytes(code)
            executable = workspace/'member.exe'
            compiled = subprocess.run([str(compiler), '/nologo', '/platform:x86', '/out:'+str(executable),
                str(project/'client/tests/native/match_member_fixture.cs')], capture_output=True, timeout=30)
            self.assertEqual(compiled.returncode, 0, compiled.stdout)
            executed = subprocess.run([str(executable), str(workspace/'member.bin')], capture_output=True, timeout=15)
            self.assertEqual(executed.returncode, 0, executed.stdout+executed.stderr)

    def test_member_stale_result_does_not_construct_notification(self):
        self.bridge.member_start_id = 'first'
        self.bridge.room_id, self.bridge.room_pointer = 'room', 0x40000
        self.bridge.current_room = lambda: 'other'
        self.session.poll.return_value = SimpleNamespace(operation='member_connect', error=None, data={})
        self.assertEqual(self.bridge.poll(0x500123), 0x500123)
        self.assertIsNone(self.bridge.member_start_id)
        self.assertIsNone(self.bridge.start_notification)

    def test_member_failure_is_reported_without_native_delivery(self):
        self.bridge.member_start_id = 'first'
        self.bridge.room_id, self.bridge.room_pointer = 'room', 0x40000
        self.process.write(ROOM_POINTER, struct.pack('<I', 0x40000))
        self.session.poll.return_value = SimpleNamespace(operation='member_connect',
            error=ValueError('rejected'), data=None)
        self.bridge.notify = Mock()
        self.assertEqual(self.bridge.poll(0x500123), 0x500123)
        self.bridge.notify.assert_called_once()
        self.session.acknowledge_start.assert_called_once_with('room', 'first')
        self.assertIsNone(self.bridge.start_notification)

    def test_waiting_member_still_polls_events_and_consumes_results_once(self):
        self.bridge.member_start_id = 'first'
        self.bridge.room_id, self.bridge.room_pointer = 'room', 0x40000
        self.process.write(ROOM_POINTER, struct.pack('<I', 0x40000))
        self.session.poll.return_value = None
        self.assertEqual(self.bridge.poll(0x500123), 0x500123)
        self.session.poll_start.assert_called_once_with()
        self.session.poll.assert_called_once_with()
        self.session.begin.assert_not_called()

    def test_departure_clears_state_before_network_request_completes(self):
        self.bridge.member_start_id = 'first'
        self.bridge.room_id, self.bridge.room_pointer = 'room', 0x40000
        self.bridge.current_room = lambda: 'other'
        self.bridge.allocation = {'allocationId': 'old'}
        self.bridge.delivered_allocation = 'old'
        self.bridge.pending_start_phase = 'start_received'
        self.session.poll.return_value = None
        self.assertEqual(self.bridge.poll(0x500123), 0x500123)
        self.assertIsNone(self.bridge.member_start_id)
        self.assertIsNone(self.bridge.allocation)
        self.assertIsNone(self.bridge.delivered_allocation)
        self.assertIsNone(self.bridge.pending_start_phase)
        self.session.poll.assert_called_once_with()
        self.session.poll_start.assert_called_once_with()

    def test_departure_releases_notification_before_consuming_network_result(self):
        self.bridge.member_start_id = 'first'
        self.bridge.room_id, self.bridge.room_pointer = 'room', 0x40000
        self.bridge.current_room = lambda: 'other'
        self.bridge.start_notification = 0x63000
        self.assertNotEqual(self.bridge.poll(0x500123), 0x500123)
        self.session.poll.assert_not_called()
        self.assertIsNone(self.bridge.start_notification)
        self.assertIsNone(self.bridge.member_start_id)

    def test_failure_event_invalidates_pending_member_result_before_dispatch(self):
        self.bridge.member_start_id = 'first'
        self.bridge.room_id, self.bridge.room_pointer = 'room', 0x40000
        self.process.write(ROOM_POINTER, struct.pack('<I', 0x40000))
        self.session.poll_start.return_value = {'roomId': 'room', 'startId': 'first', 'status': 'failed'}
        self.session.poll.return_value = SimpleNamespace(operation='member_connect', error=None,
                                                        data={'startId': 'first'})
        self.bridge.member_notification = Mock()
        self.assertEqual(self.bridge.poll(0x500123), 0x500123)
        self.bridge.member_notification.assert_not_called()
        self.session.acknowledge_start.assert_called_once_with('room', 'first')
        self.session.poll.assert_called_once_with()

    def test_member_result_dispatch_does_not_depend_on_latest_owner_flag(self):
        self.bridge.member_start_id = 'first'
        self.bridge.room_id, self.bridge.room_pointer = 'room', 0x40000
        self.process.write(ROOM_POINTER, struct.pack('<I', 0x40000))
        self.process.write(0x4001c, struct.pack('<I', 0x50000))
        self.process.write(0x40278, b'\x01')
        self.session.poll_start.return_value = {'roomId': 'room', 'startId': 'first', 'status': 'ready'}
        completion = SimpleNamespace(operation='member_connect', error=None, data={})
        self.session.poll.return_value = completion
        self.bridge.apply_member_completion = Mock(return_value=0x600123)
        self.bridge.build_host_start_call = Mock()
        self.assertEqual(self.bridge.poll(0x500123), 0x600123)
        self.bridge.apply_member_completion.assert_called_once_with(completion, 0x500123)
        self.bridge.build_host_start_call.assert_not_called()

    def test_member_serialization_hands_one_reference_to_existing_frame_receiver(self):
        self.bridge.member_start_id = 'first'
        self.bridge.room_id, self.bridge.room_pointer = 'room', 0x40000
        self.bridge.callback_fields = ()
        self.bridge.member_notification(0x500123)
        self.process.write(self.bridge.member_message+12, struct.pack('<I', 0x63000))
        address = next(address for address, phase in self.bridge.traps.items() if phase == 'member_serialized')
        self.bridge.handle(SimpleNamespace(tid=1), address)
        self.assertEqual(self.bridge.start_notification, 0x63000)
        self.assertTrue(self.bridge.notification_ready)
        self.assertIsNone(self.bridge.member_start_id)
        self.session.acknowledge_start.assert_called_once_with('room', 'first')

    def test_other_message_recipient_falls_through(self):
        self.bridge.allocation = {'serverUserId': '5'}
        self.bridge.room_pointer = 0x40000
        struct.pack_into('<I', self.process.registers, 172, 0x40000)
        struct.pack_into('<I', self.process.registers, 196, 0x50000)
        self.process.write(0x50004, struct.pack('<6I', 0x60000, 8, 6, 0, 0, 0))
        self.process.write(0x60000, b'mmstart ')
        self.bridge.handle(SimpleNamespace(tid=1), self.bridge.code+32)
        self.assertEqual(struct.unpack_from('<I', self.process.registers, 184)[0], self.bridge.code+33)
        self.session.begin.assert_not_called()
        self.assertIsNone(self.bridge.pending_start_phase)

    def test_connection_identity_uses_existing_mapping_and_preserves_context(self):
        self.bridge.room_id, self.bridge.room_pointer = 'room', 0x40000
        self.bridge.allocation = {'allocationId': 'allocation'}
        self.bridge.delivered_allocation = 'allocation'
        self.process.write(ROOM_POINTER, struct.pack('<I', 0x40000))
        self.process.write(0x42000, struct.pack('<II', 0xE69165FD, 0x0A68AA58))
        self.process.registers[:] = bytes([0x55])*716
        expected = bytearray(self.process.registers)
        struct.pack_into('<I', expected, 168, 0x0A68AA58)
        struct.pack_into('<I', expected, 176, 0xE69165FD)
        struct.pack_into('<I', expected, 184, self.bridge.code+102)
        self.bridge.handle(SimpleNamespace(tid=1), self.bridge.code+96)
        self.assertEqual(self.process.registers, expected)
        # 原生格式化按高位 EDX、低位 EAX 输出两个 %08x；对照服务端真实准入值。
        rendered_identity = '%08x%08x' % (struct.unpack_from('<I', self.process.registers, 168)[0],
                                        struct.unpack_from('<I', self.process.registers, 176)[0])
        self.assertEqual(rendered_identity, '0a68aa58e69165fd')
        for call_address, original_bytes in IDENTITY_CALLS:
            target = call_address+5+struct.unpack('<i', self.process.read(call_address+1, 4))[0]
            self.assertEqual(target, self.bridge.code+96)
        self.assertFalse(any(0x807A40 <= address < 0x807A45 for address in self.process.memory))

    def test_identity_without_confirmed_current_allocation_uses_native_getter(self):
        self.bridge.room_id, self.bridge.room_pointer = 'room', 0x40000
        self.process.write(ROOM_POINTER, struct.pack('<I', 0x40000))
        self.process.write(0x42000, struct.pack('<II', 5, 6))
        for allocation, delivered, current_room in [(None, None, 'room'),
                ({'allocationId': 'allocation'}, None, 'room'),
                ({'allocationId': 'allocation'}, 'allocation', 'other')]:
            with self.subTest(allocation=allocation, delivered=delivered, current_room=current_room):
                self.bridge.allocation, self.bridge.delivered_allocation = allocation, delivered
                self.bridge.current_room = lambda: current_room
                expected = bytearray(self.process.registers)
                struct.pack_into('<I', expected, 184, self.bridge.code+97)
                self.bridge.handle(SimpleNamespace(tid=1), self.bridge.code+96)
                self.assertEqual(self.process.registers, expected)
        instructions = list(capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32).disasm(
            self.process.read(self.bridge.code+96, 7), self.bridge.code+96))
        self.assertEqual([(instruction.mnemonic, instruction.op_str) for instruction in instructions],
                         [('int3', ''), ('jmp', '0x807a40'), ('ret', '')])

    def test_identity_callsite_mismatch_refuses_all_patches(self):
        for changed_address, original_bytes in IDENTITY_CALLS:
            with self.subTest(changed_address=changed_address):
                process = MemoryProcess()
                for address, original in [(FIND_CALL, bytes.fromhex('e87cb11c00')),
                        (BINARY_SLOT, struct.pack('<I', 0x629730)),
                        (NOTIFICATION_SLOT, struct.pack('<I', 0x62C580)),
                        (NOTIFICATION_FRAME_CALL, bytes.fromhex('e8726efeff')), *IDENTITY_CALLS]:
                    process.write(address, original)
                process.write(changed_address, bytes(5))
                before = dict(process.memory)
                bridge = MatchBridge(process, Mock(), lambda: 'room', '127.0.0.2', Mock())
                with self.assertRaises(ValueError): bridge.install(0x42000)
                self.assertEqual(process.memory, before)

    def test_allocation_callback_constructs_record_and_preserves_resume_address(self):
        self.bridge.callback_fields = ()
        resume_address = 0x500123
        entry = self.bridge.callback(resume_address, True)
        decoder = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
        instructions = list(decoder.disasm(self.process.read(entry, 128), entry))
        calls = [instruction.op_str for instruction in instructions if instruction.mnemonic == 'call']
        self.assertEqual(calls, ['0x806b50', '0x637930'])
        jumps = [instruction.op_str for instruction in instructions if instruction.mnemonic == 'jmp']
        self.assertEqual(jumps, [hex(resume_address)])
        self.assertEqual([instruction.mnemonic for instruction in instructions[:2]], ['pushfd', 'pushal'])

    def test_changed_original_vtable_refuses_install_without_patch(self):
        process = MemoryProcess()
        process.write(FIND_CALL, bytes.fromhex('e87cb11c00'))
        bridge = MatchBridge(process, Mock(), lambda: 'room', '127.0.0.2', Mock())
        with self.assertRaises(ValueError): bridge.install(0x42000)
        self.assertEqual(process.read(FIND_CALL, 5), bytes.fromhex('e87cb11c00'))

    def test_notification_only_captured_at_booking_callsite(self):
        self.bridge.room_id, self.bridge.room_pointer = 'room', 0x40000
        self.bridge.allocation = {'allocationId': 'allocation'}
        self.bridge.pending_start_phase = 'start_received'
        struct.pack_into('<I', self.process.registers, 172, 0x40000)
        struct.pack_into('<I', self.process.registers, 196, 0x50000)
        self.process.write(0x50000, struct.pack('<II', 0x60000, 0x61000))
        self.bridge.handle(SimpleNamespace(tid=1), self.bridge.code+64)
        self.assertIsNone(self.bridge.start_notification)
        self.assertEqual(struct.unpack_from('<I', self.process.registers, 184)[0], self.bridge.code+65)
        self.process.write(0x50000, struct.pack('<I', 0x63280B))
        self.process.write(0x61000, struct.pack('<5I', 0xDC74F8, 1, 0x62000, 0x62000, 1))
        self.process.write(0x62000, struct.pack('<I', 0x63000))
        self.process.write(0x63000, struct.pack('<8I', 0xD59834, 1, 0x64000, 146, 105, 833, 833, 1))
        self.process.write(0x64000, bytes.fromhex('068600'))
        self.bridge.handle(SimpleNamespace(tid=1), self.bridge.code+64)
        self.assertEqual(self.bridge.start_notification, 0x63000)
        entry = struct.unpack_from('<I', self.process.registers, 184)[0]
        instructions = list(capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32).disasm(self.process.read(entry, 20), entry))
        self.assertTrue(any(instruction.mnemonic == 'call' and instruction.op_str == 'dword ptr [0xca8128]' for instruction in instructions))
        self.assertEqual(instructions[-1].op_str, '0x62c580')

    def test_start_success_schedules_native_receiver_once(self):
        self.bridge.room_id, self.bridge.room_pointer = 'room', 0x40000
        self.bridge.allocation = {'allocationId': 'allocation'}
        self.process.write(ROOM_POINTER, struct.pack('<I', 0x40000))
        self.process.write(0x4001C, struct.pack('<I', 0x41000))
        self.process.write(0x41020, struct.pack('<II', 5, 6))
        self.bridge.start_notification = 0x63000
        self.session.poll.return_value = SimpleNamespace(operation='start', error=None,
            data={'allocationId': 'allocation', 'started': True})
        self.assertEqual(self.bridge.poll(0x500123), 0x500123)
        self.assertEqual(self.bridge.start_notification, 0x63000)
        self.assertEqual(self.process.read(self.bridge.notification_flag, 4), struct.pack('<I', 1))
        self.bridge.handle(SimpleNamespace(tid=1), self.bridge.frame_trap)
        entry = struct.unpack_from('<I', self.process.registers, 184)[0]
        instructions = list(capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32).disasm(self.process.read(entry, 46), entry))
        self.assertEqual([instruction.op_str for instruction in instructions if instruction.mnemonic == 'call'], ['0x634870'])
        self.assertIsNone(self.bridge.start_notification)
        self.assertEqual(self.process.read(self.bridge.notification_flag, 4), bytes(4))
        self.assertEqual(self.bridge.poll(0x500123), 0x500123)

    def test_failed_or_stale_start_releases_without_connecting(self):
        for current_room, response in [('room', {'allocationId': 'wrong', 'started': True}),
                                       ('other', {'allocationId': 'allocation', 'started': True})]:
            with self.subTest(current_room=current_room):
                self.bridge.current_room = lambda: current_room
                self.bridge.room_id, self.bridge.room_pointer = 'room', 0x40000
                self.bridge.allocation = {'allocationId': 'allocation'}
                self.process.write(ROOM_POINTER, struct.pack('<I', 0x40000))
                self.bridge.start_notification = 0x63000
                self.session.poll.return_value = SimpleNamespace(operation='start', error=None, data=response)
                entry = self.bridge.poll(0x500123)
                instructions = list(capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32).disasm(self.process.read(entry, 35), entry))
                self.assertFalse(any(instruction.op_str == '0x634870' for instruction in instructions))
                release_branch = next(instruction for instruction in instructions if instruction.mnemonic == 'jne')
                restore = next(instruction for instruction in instructions if instruction.mnemonic == 'popal')
                self.assertEqual(int(release_branch.op_str, 16), restore.address)

    def test_frame_gate_preserves_native_call_and_only_traps_when_pending(self):
        entry = self.bridge.code+1024
        instructions = list(capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32).disasm(self.process.read(entry, 19), entry))
        self.assertEqual([instruction.mnemonic for instruction in instructions],
                         ['pushfd', 'pushal', 'cmp', 'je', 'int3', 'popal', 'popfd', 'jmp'])
        self.assertEqual(int(instructions[3].op_str, 16), instructions[5].address)
        self.assertEqual(instructions[-1].op_str, '0x7ef0d0')
        self.assertEqual(instructions[4].address, self.bridge.frame_trap)

    def test_leaving_between_confirmation_and_next_frame_does_not_connect(self):
        self.bridge.room_id, self.bridge.room_pointer = 'room', 0x40000
        self.bridge.allocation = {'allocationId': 'allocation'}
        self.bridge.start_notification = 0x63000
        self.bridge.notification_ready = True
        self.bridge.current_room = lambda: None
        self.bridge.handle(SimpleNamespace(tid=1), self.bridge.frame_trap)
        entry = struct.unpack_from('<I', self.process.registers, 184)[0]
        instructions = list(capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32).disasm(self.process.read(entry, 35), entry))
        self.assertFalse(any(instruction.op_str == '0x634870' for instruction in instructions))
        self.assertIsNone(self.bridge.start_notification)
        self.assertFalse(self.bridge.notification_ready)

    def test_failure_event_releases_queued_host_notification_without_connecting(self):
        self.bridge.room_id, self.bridge.room_pointer = 'room', 0x40000
        self.bridge.allocation = {'allocationId': 'allocation'}
        self.bridge.pending_start_phase = 'start_received'
        self.bridge.start_notification = 0x63000
        self.bridge.notification_ready = True
        self.process.write(ROOM_POINTER, struct.pack('<I', 0x40000))
        self.session.poll_start.return_value = {'roomId': 'room', 'startId': 'first', 'status': 'failed'}
        entry = self.bridge.poll(0x500123)
        instructions = list(capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32).disasm(
            self.process.read(entry, 35), entry))
        self.assertFalse(any(instruction.op_str == '0x634870' for instruction in instructions))
        self.assertIsNone(self.bridge.pending_start_phase)
        self.assertIsNone(self.bridge.start_notification)
        self.assertFalse(self.bridge.notification_ready)
        self.session.begin.assert_not_called()

    def test_prepared_broadcast_drives_host_native_stages_without_http(self):
        import base64
        from codol_rebuild.application.matches.service import MatchSession
        from codol_rebuild.application.realtime.dispatch import EventInbox
        inbox = EventInbox()
        session = MatchSession(lambda: 'room', inbox)
        session.connected('player')
        self.bridge.session = session
        connection = {'allocationId': 'a' * 32, 'port': 27760, 'securityId': '01' * 8,
                      'securityKey': '02' * 16, 'serverUserId': '5', 'serverName': 'fixture', 'maxPlayers': 6}
        inbox.receive({'type': 'room_start_updated', 'payload': {
            'roomId': 'room', 'startId': 'first', 'status': 'ready', 'connection': connection}})
        self.process.write(ROOM_POINTER, struct.pack('<I', 0x40000))
        self.process.write(0x4001c, struct.pack('<I', 0x41000))
        self.process.write(0x41020, struct.pack('<II', 5, 6))
        self.process.write(0x40278, b'\x01')
        self.process.write(0x62F840, bytes.fromhex('558becb814200000'))
        decoder = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)

        def native_calls(entry, length=128):
            """检查阶段交付仍使用原生入口，不把调用桩当成实际游戏执行。"""
            return [instruction.op_str for instruction in decoder.disasm(self.process.read(entry, length), entry)
                    if instruction.mnemonic == 'call']

        self.assertEqual(native_calls(self.bridge.poll(0x500123), 25), ['0x62f840'])
        struct.pack_into('<I', self.process.registers, 196, 0x50000)
        self.process.write(0x50004, struct.pack('<5I', 0x27, 0, 0, 0x47470, 0x637930))
        self.bridge.handle(SimpleNamespace(tid=1), self.bridge.code)
        self.assertEqual(native_calls(self.bridge.poll(0x500123)), ['0x806b50', '0x637930'])

        def native_send(body):
            """模拟既有房主专用消息边，不改动其他接收者和原生函数。"""
            struct.pack_into('<I', self.process.registers, 172, 0x40000)
            self.process.write(0x50004, struct.pack('<6I', 0x60000, len(body), 5, 0, 0, 0))
            self.process.write(0x60000, body)
            self.bridge.handle(SimpleNamespace(tid=1), self.bridge.code+32)
            return struct.unpack_from('<I', self.process.registers, 176)[0]

        native_send(b'mmbook 0 ' + base64.b64encode(bytes.fromhex(connection['securityId'])) + b' 0')
        self.assertEqual(native_calls(self.bridge.poll(0x500123)), ['0x632680'])
        self.assertEqual(native_send(b'mmban\0'), 0)
        self.assertEqual(native_send(b'mmstart native'), 1)
        self.assertEqual(native_send(b'mmstart duplicate'), 0)
        self.assertEqual(native_send(b'mmban\0'), 1)
        self.assertEqual(native_send(b'mmban\0'), 0)
        self.bridge.record.assert_any_call({'match_native_start': 'mmstart native'})
        self.bridge.record.assert_any_call({'match_native_item_bans': b'mmban\0'.hex()})
        self.bridge.start_notification = 0x63000
        self.assertEqual(self.bridge.poll(0x500123), 0x500123)
        self.assertEqual(self.bridge.poll(0x500123), 0x500123)
        self.assertTrue(self.bridge.notification_ready)
        self.bridge.handle(SimpleNamespace(tid=1), self.bridge.frame_trap)
        entry = struct.unpack_from('<I', self.process.registers, 184)[0]
        self.assertEqual(native_calls(entry, 46), ['0x634870'])
