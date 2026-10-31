"""近战短长按边界、重复输入与取消回归。"""
import unittest
import struct
from types import SimpleNamespace
from codol_rebuild.adapters.game.v1649370.matches.melee_input import MeleeGesture, MeleeInputBridge


class MeleeGestureTests(unittest.TestCase):
    def test_release_before_threshold_attacks_once(self):
        gesture = MeleeGesture()
        self.assertEqual(gesture.update(1000, True, True, 1000, 42), (False, 0))
        self.assertEqual(gesture.update(1399, False, False, 1000, 42), (True, 0))
        self.assertEqual(gesture.update(1400, False, False, 1000, 42), (False, 0))

    def test_hold_selects_once_and_release_does_not_attack(self):
        gesture = MeleeGesture()
        for time_ms, expected in ((1000, 0), (1399, 0), (1400, 42), (1401, 0), (2000, 0)):
            self.assertEqual(gesture.update(time_ms, True, False, 1000, 42), (False, expected))
        self.assertEqual(gesture.update(2001, False, False, 1000, 42), (False, 0))

    def test_pulse_between_frames_and_clock_wrap(self):
        gesture = MeleeGesture()
        self.assertEqual(gesture.update(30, False, True, 0xfffffff0, 42), (True, 0))
        self.assertEqual(gesture.update(1400, False, True, 1000, 42), (False, 42))

    def test_cancel_and_missing_target_do_not_synthesize_attack(self):
        gesture = MeleeGesture()
        gesture.update(1000, True, True, 1000, 42)
        self.assertEqual(gesture.update(1200, False, False, 1000, 42, False), (False, 0))
        self.assertEqual(gesture.update(1300, False, False, 1000, 42), (False, 0))
        self.assertEqual(gesture.update(2000, True, True, 1500, 0), (False, 0))
        self.assertEqual(gesture.update(2010, False, False, 1500, 0), (False, 0))

    def test_disabled_hold_requires_release_before_new_gesture(self):
        gesture = MeleeGesture()
        gesture.update(1000, True, True, 1000, 42, False)
        self.assertEqual(gesture.update(2000, True, False, 1000, 42), (False, 0))
        self.assertEqual(gesture.update(2001, False, False, 1000, 42), (False, 0))
        self.assertEqual(gesture.update(2200, False, True, 2100, 42), (True, 0))


class MemoryProcess:
    """验证原生寄存器及消息契约，不模拟动画或实际游戏验收。"""
    main_thread_id = 1

    def __init__(self):
        self.memory = {}
        self.registers = bytearray(716)

    def read(self, address, size):
        return bytes(self.memory.get(address + offset, 0) for offset in range(size))

    def write(self, address, data):
        self.memory.update((address + offset, value) for offset, value in enumerate(data))

    def allocate(self, size):
        return 0x10000000

    def executable(self, address, size):
        pass

    def context(self, thread_id):
        return thread_id, self.registers

    def set_context(self, thread, context):
        self.registers = context

    def close_thread(self, thread):
        pass


class MeleeBridgeTests(unittest.TestCase):
    def setUp(self):
        self.process = MemoryProcess()
        self.bridge = MeleeInputBridge(self.process)
        for address, data in ((self.bridge.INPUT_SITE, self.bridge.INPUT_BYTES),
                              (self.bridge.MESSAGE_SITE, self.bridge.MESSAGE_BYTES)):
            self.process.write(address, data)
        self.bridge.install()
        self.event = SimpleNamespace(tid=1)

    def store(self, address, value):
        self.process.write(address, struct.pack('<I', value))

    def test_reliable_target_and_unrelated_message_passthrough(self):
        struct.pack_into('<I', self.process.registers, 176, 0x2000)
        self.process.write(0x2000, b'~codol_melee\x00')
        self.store(0x3306994, 2)
        self.store(0x33069D4, 0x3000)
        self.store(0x3004, 0x4000)
        self.process.write(0x4000, b'42\x00')
        self.bridge.handle(self.event, self.bridge.code + 32)
        self.assertEqual(self.bridge.target_weapon, 42)
        self.assertEqual(struct.unpack_from('<I', self.process.registers, 184)[0], 0x61491C)
        self.process.write(0x2000, b'other_message')
        self.bridge.handle(self.event, self.bridge.code + 32)
        self.assertEqual(self.bridge.target_weapon, 42)
        self.assertEqual(struct.unpack_from('<I', self.process.registers, 184)[0], self.bridge.code + 33)

    def test_input_preserves_other_buttons_and_does_not_write_player_weapon(self):
        self.store(0x1C1D188, 0x20000000 ^ 0xFBB2C97A)
        self.store(0x20003BD4, 1)
        struct.pack_into('<I', self.process.registers, 160, 0x7505E38)
        struct.pack_into('<I', self.process.registers, 156, 0x5000)
        self.store(0x5004, 0x100001)
        self.process.write(0x750605C, b'\x00\x01')
        self.store(0x7506050, 1000)
        self.store(0x7506054, 1100)
        self.bridge.handle(self.event, self.bridge.code)
        self.assertEqual(self.bridge.integer(0x5004), 0x100005)
        self.assertEqual(self.bridge.integer(0x200003F8), 0)
        self.assertEqual(struct.unpack_from('<I', self.process.registers, 184)[0], 0x5B1EDA)

    def test_hold_calls_native_selector_once_and_disconnect_clears_target(self):
        player = 0x20000000
        self.store(0x1C1D188, player ^ 0xFBB2C97A)
        self.store(player + 0x3BD4, 1)
        struct.pack_into('<I', self.process.registers, 160, 0x7505E38)
        struct.pack_into('<I', self.process.registers, 156, 0x5000)
        self.store(0x7506050, 1000)
        self.store(0x765877C, 1400)
        self.process.write(0x750605C, b'\x01\x01')
        self.bridge.target_weapon = 42
        self.bridge.handle(self.event, self.bridge.code)
        entry = self.bridge.code + 64
        self.assertEqual(struct.unpack_from('<I', self.process.registers, 184)[0], entry)
        instructions = self.process.read(entry, 19)
        self.assertEqual(instructions[:3], b'\x9c\x60\xbf')
        self.assertEqual(struct.unpack_from('<I', instructions, 3)[0], 42)
        self.assertEqual(entry + 12 + struct.unpack_from('<i', instructions, 8)[0], 0x59B8D0)
        self.assertEqual(entry + 19 + struct.unpack_from('<i', instructions, 15)[0], 0x5B1EDA)
        self.bridge.handle(self.event, self.bridge.code)
        self.assertEqual(struct.unpack_from('<I', self.process.registers, 184)[0], 0x5B1EDA)
        self.assertEqual(self.bridge.integer(0x5004), 0)
        self.store(player + 0x3BD4, 0)
        self.bridge.handle(self.event, self.bridge.code)
        self.assertEqual(self.bridge.target_weapon, 0)
        self.assertTrue(self.bridge.gesture.suppressed)
