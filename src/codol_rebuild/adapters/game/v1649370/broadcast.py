"""1649370 大喇叭桥：专用发送入口与原生成功、接收通知。

SendLoudSpeaker 的 GFx 参数取法来自 0x7273B0；只接管类型 0。
成功复用 0x7C4AB0(bool, error, type)，其 ret 12 清栈；接收沿
0x72F563 的控制器 3 / cppOnMessageReceived(Object) 约定发布。
所有进程访问均发生在主线程暂停点；网络结果由应用端口交接。
"""
import hashlib
import struct
from datetime import datetime
from codol_rebuild.application.broadcast.contracts import BroadcastPort

SEND_HANDLER = 0x7273B0
# 原版 ChattingController.BIG_LOUD_SPEAKER_ID；类型 0 使用此库存物品。
BIG_LOUD_SPEAKER_ITEM_ID = 50003
CAPACITY = 65536
SEND_COMPLETE, MESSAGE_READY, MESSAGE_OBJECT, FIELDS = 0, 4, 32, 64
BROADCAST_AVAILABLE = 8
# IsInBigSpeakerChannel 只替换读取来源，保留原 GFx 布尔返回及对象释放逻辑。
AVAILABILITY_SOURCE = 0x5D2DE9
MESSAGE_FIELDS = ('channelID', 'senderID', 'senderName', 'message',
                  'formatedTime', 'loudSpeakerType')
CONTRACT = {
    0x5D2DE7: bytes.fromhex('8a1df44a2c08'),
    SEND_HANDLER: bytes.fromhex('558bec83e4f8'),
    0x7C4AB0: bytes.fromhex('558bec83ec64'),
    0x7C4B67: bytes.fromhex('c20c00'),
    0x72FD70: bytes.fromhex('558beca198bb770c53'),
    0x436750: bytes.fromhex('558bec518bc1'),
    0x436660: bytes.fromhex('568bf18b460c'),
    0x730570: bytes.fromhex('558bec83e4f883ec50'),
    0xDD601C: b'cppOnMessageReceived\0',
    0xB9A0F0: bytes.fromhex('8d81d8000000c3'),
}


def read_word(read, address):
    """读取已核对 ABI 的 32 位指针或整数。"""
    return struct.unpack('<I', read(address, 4))[0]


def read_request(read, stack_address):
    """读取 GFx 参数，不把其他聊天类型解释为大喇叭命令。"""
    invocation = read_word(read, stack_address + 4)
    values = read_word(read, invocation + 0x10)
    speaker_type = read_word(read, values + 0x10)
    if speaker_type != 0:
        return None
    string_pointer = read_word(read, values + 0x30)
    if read_word(read, values + 0x2C) & 0x40:
        string_pointer = read_word(read, string_pointer)
    text_bytes = bytearray()
    for character_offset in range(CAPACITY):
        character = read(string_pointer + character_offset, 1)
        if character == b'\0':
            return text_bytes.decode('utf-8')
        text_bytes.extend(character)
    raise ValueError('原生喇叭字符串超过适配缓冲区')


def native_sender_id(read, sender_id, local_player_id):
    """本人复用 GetPlayerID 的实际来源，其他玩家沿现有房间的显示 ID 映射。"""
    if sender_id == local_player_id:
        identity_object = read_word(read, 0x84ABB08) or read_word(read, 0x84ABB14)
        # GetPlayerID 在两指针均为空时返回十进制字符串 0。
        identity_number = struct.unpack('<Q', read(identity_object + 0xF8, 8))[0] if identity_object else 0
    else:
        identity_number = int.from_bytes(hashlib.sha256(sender_id.encode('utf-8')).digest()[:8], 'little') or 1
    return str(identity_number)


def encode_message(message, sender_id, state):
    """转换原生既有字段；服务端消息 ID 不伪装成原生玩家 ID。"""
    time_text = datetime.fromisoformat(message['sentAt']).astimezone().strftime('%H:%M')
    values = (0, sender_id, message['senderName'], message['text'], time_text, 0)
    data = bytearray(FIELDS + len(MESSAGE_FIELDS) * 36)
    struct.pack_into('<II', data, MESSAGE_OBJECT, 0xFFFFFFFF, 0xFFFFFFFF)

    def append_string(text):
        if '\0' in text:
            raise ValueError('原生 GFx 字符串不能包含空字符')
        pointer = state + len(data)
        data.extend(text.encode('utf-8') + b'\0')
        return pointer

    for field_index, (field_name, value) in enumerate(zip(MESSAGE_FIELDS, values)):
        field_pointer = append_string(field_name)
        value_kind = 6 if isinstance(value, str) else 3
        payload = append_string(value) if value_kind == 6 else value
        struct.pack_into('<IIIIII', data, FIELDS + field_index * 36,
                         field_pointer, 0xFFFFFFFF, 0xFFFFFFFF, 0, value_kind, payload)
    if len(data) > CAPACITY:
        raise ValueError('广播内容超过原生发布缓冲区')
    struct.pack_into('<I', data, MESSAGE_READY, 1)
    return bytes(data)


def build_adapter(entry, state):
    """只覆盖发送入口六字节；其余原方法及小喇叭路径保持不变。"""
    instructions = bytearray()
    labels, branches, traps = {}, [], {}

    def emit(hexadecimal):
        instructions.extend(bytes.fromhex(hexadecimal))

    def word(number):
        instructions.extend(struct.pack('<I', number & 0xFFFFFFFF))

    def call(address):
        emit('e8')
        word(address - entry - len(instructions) - 4)

    def branch(opcode, name):
        emit(opcode)
        branches.append((len(instructions), name))
        word(0)

    def push(address):
        emit('68')
        word(address)

    def clear(offset):
        emit('c705')
        word(state + offset)
        word(0)

    traps[entry] = 'send'
    emit('cc')
    instructions.extend(CONTRACT[SEND_HANDLER])
    emit('e9')
    word(SEND_HANDLER + 6 - entry - len(instructions) - 4)
    return_address = entry + len(instructions)
    emit('c20400')

    poll_address = entry + len(instructions)
    emit('9c60 8bec')
    traps[entry + len(instructions)] = 'poll'
    emit('cc')
    emit('833d')
    word(state + SEND_COMPLETE)
    emit('00')
    branch('0f84', 'message')
    clear(SEND_COMPLETE)
    emit('6a00 6a00 6a01')
    call(0x7C4AB0)
    labels['message'] = len(instructions)
    emit('833d')
    word(state + MESSAGE_READY)
    emit('00')
    branch('0f84', 'done')
    clear(MESSAGE_READY)
    push(state + MESSAGE_OBJECT)
    call(0x72FD70)
    emit('83c404 84c0')
    branch('0f84', 'cleanup')
    emit('be')
    word(state + FIELDS)
    emit('bf')
    word(len(MESSAGE_FIELDS))
    labels['field'] = len(instructions)
    emit('8d4604 50 ff36 b9')
    word(state + MESSAGE_OBJECT)
    call(0x436750)
    emit('84c0')
    branch('0f84', 'cleanup')
    emit('83c624 4f')
    branch('0f85', 'field')
    emit('6a01')
    push(state + MESSAGE_OBJECT)
    emit('6a00')
    push(0xDD601C)
    emit('bf')
    word(3)
    call(0x730570)
    emit('83c410')
    labels['cleanup'] = len(instructions)
    emit('b9')
    word(state + MESSAGE_OBJECT)
    call(0x436660)
    labels['done'] = len(instructions)
    emit('8be5 619d c3')
    for offset, target in branches:
        struct.pack_into('<i', instructions, offset, labels[target] - offset - 4)
    patch = b'\xe9' + struct.pack('<i', entry - SEND_HANDLER - 5) + b'\x90'
    return {'code': bytes(instructions), 'patch': patch, 'traps': traps,
            'return': return_address, 'poll': poll_address}


class BroadcastBridge:
    """游戏线程执行原生操作，应用端口负责后台发送与推送交接。"""

    def __init__(self, process, port: BroadcastPort, dispatcher, consume_item):
        self.process = process
        self.port = port
        self.dispatcher = dispatcher
        self.consume_item = consume_item
        self.state = None
        self.plan = None
        self.traps = {}
        self.player_id = None

    def connected(self, player_id, display_name):
        """交接身份，并在登录完成暂停点同步入口状态，避免多等一个调度周期。"""
        self.player_id = player_id
        self.port.connected(player_id, display_name)
        if self.state is not None:
            self._write(self.state + BROADCAST_AVAILABLE,
                        struct.pack('<I', int(self.port.available())))

    def install(self):
        """验证固定版本 ABI 后安装专用入口，返回独立轮询桩。"""
        for address, expected in CONTRACT.items():
            if self.process.read(address, len(expected)) != expected:
                raise ValueError(f'广播 ABI 不匹配：{address:#x}')
        self.state = self.process.allocate(CAPACITY)
        entry = self.process.allocate(4096)
        self.plan = build_adapter(entry, self.state)
        self._write(self.state, bytes(FIELDS))
        self._write(entry, self.plan['code'])
        self.process.executable(entry, 4096)
        self._write(SEND_HANDLER, self.plan['patch'])
        self._write(AVAILABILITY_SOURCE, struct.pack('<I', self.state + BROADCAST_AVAILABLE))
        self.traps = self.plan['traps']
        return self.plan['poll']

    def _write(self, address, data):
        self.process.write(address, data)
        if self.process.read(address, len(data)) != data:
            raise ValueError('广播适配写入回读失败')

    def handle(self, event, address):
        """只在主线程暂停点读参数、分派消息和准备下一次原生通知。"""
        if event.tid != self.process.main_thread_id:
            raise ValueError('广播原生操作必须在主线程执行')
        thread, context = self.process.context(event.tid)
        try:
            next_instruction = address + 1
            if self.traps[address] == 'send':
                stack_address = struct.unpack_from('<I', context, 196)[0]
                if self.player_id is not None:
                    text = read_request(self.process.read, stack_address)
                    if text is not None:
                        self.port.begin(text)
                        next_instruction = self.plan['return']
            else:
                self.dispatcher.poll()
                message = self.port.poll_message() if self.player_id is not None else None
                if message is not None:
                    sender_id = native_sender_id(self.process.read, message['senderId'], self.player_id)
                    self._write(self.state, encode_message(message, sender_id, self.state))
                if self.port.poll_sent() is not None:
                    # 仅 HTTP 确认消耗；原生成功通知随后负责冷却和库存刷新。
                    self.consume_item(BIG_LOUD_SPEAKER_ITEM_ID)
                    self._write(self.state + SEND_COMPLETE, struct.pack('<I', 1))
                self._write(self.state + BROADCAST_AVAILABLE,
                            struct.pack('<I', int(self.port.available())))
            struct.pack_into('<I', context, 184, next_instruction)
            self.process.set_context(thread, context)
        finally:
            self.process.close_thread(thread)
