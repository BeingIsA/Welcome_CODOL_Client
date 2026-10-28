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
        self.bridge = MatchBridge(self.process, self.session, lambda: 'room', ('127.0.0.2', 27760), Mock())
        self.process.write(FIND_CALL, bytes.fromhex('e87cb11c00'))
        self.process.write(BINARY_SLOT, struct.pack('<I', 0x629730))
        self.process.write(NOTIFICATION_SLOT, struct.pack('<I', 0x62C580))
        self.process.write(NOTIFICATION_FRAME_CALL, bytes.fromhex('e8726efeff'))
        for call_address, original_bytes in IDENTITY_CALLS:
            self.process.write(call_address, original_bytes)
        self.bridge.install(0x42000)

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
        self.assertIsNone(self.bridge.pending_start)

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
                bridge = MatchBridge(process, Mock(), lambda: 'room', ('127.0.0.2', 27760), Mock())
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
        bridge = MatchBridge(process, Mock(), lambda: 'room', ('127.0.0.2', 27760), Mock())
        with self.assertRaises(ValueError): bridge.install(0x42000)
        self.assertEqual(process.read(FIND_CALL, 5), bytes.fromhex('e87cb11c00'))

    def test_notification_only_captured_at_booking_callsite(self):
        self.bridge.room_id, self.bridge.room_pointer = 'room', 0x40000
        self.bridge.allocation = {'allocationId': 'allocation'}
        self.bridge.pending_start = {'command': 'mmstart '}
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
