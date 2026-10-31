"""近战快捷键手势：短按松开攻击，长按只请求原生武器切换。"""
from dataclasses import dataclass
import struct


@dataclass
class MeleeGesture:
    """仅保存一次按住的状态，不维护背包或玩家持有权威。"""
    pressed_at: int | None = None
    consumed: bool = False
    suppressed: bool = False
    threshold_ms: int = 400

    def reset(self):
        """死亡、失去输入或切换对局时取消未完成手势。"""
        self.pressed_at = None
        self.consumed = False
        self.suppressed = False

    def update(self, now_ms, held, pulse, pressed_at, target_weapon, enabled=True):
        """返回（短按攻击脉冲，长按目标）；时间使用原生32位毫秒时钟。"""
        if not enabled:
            self.reset()
            self.suppressed = bool(held)
            return False, 0
        if self.suppressed:
            if not held:
                self.reset()
            return False, 0
        if self.pressed_at is None and (held or pulse):
            self.pressed_at = pressed_at & 0xffffffff
            self.consumed = False
        if self.pressed_at is None:
            return False, 0
        elapsed_ms = (now_ms - self.pressed_at) & 0xffffffff
        if elapsed_ms >= 0x80000000:
            self.reset()
            return False, 0
        if held:
            if elapsed_ms >= self.threshold_ms and not self.consumed:
                self.consumed = True
                return False, target_weapon
            return False, 0
        attack = not self.consumed and elapsed_ms < self.threshold_ms
        target = target_weapon if not self.consumed and elapsed_ms >= self.threshold_ms else 0
        self.reset()
        return attack, target


class MeleeInputBridge:
    """在原生主线程解析专用可靠消息，并仅接管近战键的命令组装段。"""
    INPUT_SITE = 0x5B1EC4
    INPUT_BYTES = bytes.fromhex('80be2402000000')
    MESSAGE_SITE = 0x613A84
    MESSAGE_BYTES = bytes.fromhex('8b15583b2c08')

    def __init__(self, process):
        self.process = process
        self.gesture = MeleeGesture()
        self.target_weapon = 0
        self.traps = {}
        self.was_connected = False

    def integer(self, address):
        return struct.unpack('<I', self.process.read(address, 4))[0]

    def install(self):
        """只替换两个已核验的完整指令，其他命令及原生收拔枪保持原样。"""
        for address, expected in ((self.INPUT_SITE, self.INPUT_BYTES), (self.MESSAGE_SITE, self.MESSAGE_BYTES)):
            if self.process.read(address, len(expected)) != expected:
                raise ValueError(f'近战输入原生指令不匹配：{address:#x}')
        self.code = self.process.allocate(4096)
        self.traps = {self.code: 'input', self.code + 32: 'message'}
        self.process.write(self.code, b'\xcc')
        self.process.write(self.code + 32, b'\xcc' + self.MESSAGE_BYTES + b'\xe9' +
                           struct.pack('<i', self.MESSAGE_SITE + 6 - (self.code + 44)))
        self.process.executable(self.code, 4096)
        for address, expected, entry in ((self.INPUT_SITE, self.INPUT_BYTES, self.code),
                                         (self.MESSAGE_SITE, self.MESSAGE_BYTES, self.code + 32)):
            patch = b'\xe9' + struct.pack('<i', entry - address - 5) + b'\x90' * (len(expected) - 5)
            self.process.write(address, patch)
            if self.process.read(address, len(patch)) != patch:
                raise ValueError('近战输入补丁回读失败')

    def handle(self, event, address):
        """消息只更新目标副本；手势才调用原生选择入口，不从网络线程操作对象。"""
        if event.tid != self.process.main_thread_id:
            raise ValueError('近战输入适配必须在原生主线程执行')
        thread, context = self.process.context(event.tid)
        try:
            if self.traps[address] == 'message':
                token_pointer = struct.unpack_from('<I', context, 176)[0]
                token = self.process.read(token_pointer, 13)
                resume = address + 1
                if token == b'~codol_melee\x00':
                    resume = 0x61491C
                    command_level = self.integer(0x3306910)
                    argument_count = self.integer(0x3306994 + command_level * 4)
                    arguments = self.integer(0x33069D4 + command_level * 4)
                    self.target_weapon = 0
                    self.gesture.reset()
                    local_client = self.integer(0x82C3B58)
                    self.gesture.suppressed = bool(self.process.read(0x750605C + local_client * 0x378, 1)[0])
                    if argument_count == 2:
                        number_pointer = self.integer(arguments + 4)
                        number = self.process.read(number_pointer, 12).split(b'\x00', 1)[0]
                        if number.isdigit() and 0 < int(number) < 2048:
                            self.target_weapon = int(number)
            else:
                input_state = struct.unpack_from('<I', context, 160)[0]
                command_pointer = struct.unpack_from('<I', context, 156)[0]
                client_index = self.integer(0x82C3B58)
                player = self.integer(0x1C1D188 + client_index * 4) ^ 0xFBB2C97A
                connected = self.integer(player + 0x3BD4) != 0
                enabled = connected and self.integer(player + 4) < 8 and self.integer(0x76446A8) == 0
                if not connected and self.was_connected:
                    self.target_weapon = 0
                self.was_connected = connected
                held, pulse = self.process.read(input_state + 0x224, 2)
                pressed_at = self.integer(input_state + 0x218)
                now_ms = self.integer(0x765877C) if held else self.integer(input_state + 0x21C)
                attack, target = self.gesture.update(now_ms, bool(held), bool(pulse), pressed_at,
                                                     self.target_weapon, enabled)
                if attack:
                    self.process.write(command_pointer + 4, struct.pack('<I', self.integer(command_pointer + 4) | 4))
                resume = 0x5B1EDA
                if target and target != self.integer(player + 0xC7228):
                    # 调用仍执行原生持有、有效性检查；保留调用现场寄存器与标志。
                    entry = self.code + 64
                    instructions = b'\x9c\x60\xbf' + struct.pack('<I', target) + b'\xe8'
                    instructions += struct.pack('<i', 0x59B8D0 - entry - len(instructions) - 4)
                    instructions += b'\x61\x9d\xe9'
                    instructions += struct.pack('<i', resume - entry - len(instructions) - 4)
                    self.process.write(entry, instructions)
                    resume = entry
            struct.pack_into('<I', context, 184, resume)
            self.process.set_context(thread, context)
        finally:
            self.process.close_thread(thread)
