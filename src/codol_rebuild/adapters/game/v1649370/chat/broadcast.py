"""1649370 大小喇叭桥：专用发送入口与原生成功、接收通知。

SendLoudSpeaker 的 GFx 参数取法来自 0x7273B0；接管类型 0（大）和 1（小）。
成功复用 0x7C4AB0(bool, error, type)，其 ret 12 清栈；接收沿
0x72F563 的控制器 3 / cppOnMessageReceived(Object) 约定发布。
所有进程访问均发生在主线程暂停点；网络结果由应用端口交接。
"""
import struct
from codol_rebuild.application.broadcast.contracts import BroadcastPort
from codol_rebuild.adapters.game.v1649370.chat.chat_message import (
    CAPACITY, MESSAGE_READY, MESSAGE_OBJECT, FIELDS, MESSAGE_FIELDS,
    CONTRACT as MESSAGE_CONTRACT, read_word, native_sender_id, build_message_dispatch,
    encode_message as encode_chat_message,
)

SEND_HANDLER = 0x7273B0
# 原版 ChatSpeakerType 与 ChattingController 的大小喇叭类型和库存物品。
SPEAKER_TYPES = {0: 'large', 1: 'small'}
NATIVE_SPEAKER_TYPES = {speaker_type: native_type for native_type, speaker_type in SPEAKER_TYPES.items()}
SPEAKER_ITEM_IDS = {'large': 50003, 'small': 50002}
SENT_SPEAKER_TYPE = 12
SEND_COMPLETE = 0
BROADCAST_AVAILABLE = 8
# IsInBigSpeakerChannel 只替换读取来源，保留原 GFx 布尔返回及对象释放逻辑。
AVAILABILITY_SOURCE = 0x5D2DE9
CONTRACT = {
    **MESSAGE_CONTRACT,
    0x5D2DE7: bytes.fromhex('8a1df44a2c08'),
    SEND_HANDLER: bytes.fromhex('558bec83e4f8'),
    0x7C4AB0: bytes.fromhex('558bec83ec64'),
    0x7C4B67: bytes.fromhex('c20c00'),
}


def read_request(read, stack_address):
    """读取 GFx 参数，不把其他聊天类型解释为大小喇叭命令。"""
    invocation = read_word(read, stack_address + 4)
    values = read_word(read, invocation + 0x10)
    speaker_type = read_word(read, values + 0x10)
    if speaker_type not in SPEAKER_TYPES:
        return None
    string_pointer = read_word(read, values + 0x30)
    if read_word(read, values + 0x2C) & 0x40:
        string_pointer = read_word(read, string_pointer)
    text_bytes = bytearray()
    for character_offset in range(CAPACITY):
        character = read(string_pointer + character_offset, 1)
        if character == b'\0':
            return text_bytes.decode('utf-8'), SPEAKER_TYPES[speaker_type]
        text_bytes.extend(character)
    raise ValueError('原生喇叭字符串超过适配缓冲区')


def encode_message(message, sender_id, state):
    """大小喇叭只负责选择原生类型，字段编码与普通聊天共用。"""
    return encode_chat_message(message, sender_id, state, 0,
                               NATIVE_SPEAKER_TYPES[message['speakerType']])


def build_adapter(entry, state):
    """只覆盖发送入口六字节；未接管的调用仍执行原方法。"""
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
    emit('ff35')
    word(state + SENT_SPEAKER_TYPE)
    emit('6a00 6a01')
    call(0x7C4AB0)
    labels['message'] = len(instructions)
    emit('8be5 619d')
    instructions.extend(build_message_dispatch(entry + len(instructions), state))
    for offset, target in branches:
        struct.pack_into('<i', instructions, offset, labels[target] - offset - 4)
    patch = b'\xe9' + struct.pack('<i', entry - SEND_HANDLER - 5) + b'\x90'
    return {'code': bytes(instructions), 'patch': patch, 'traps': traps,
            'return': return_address, 'poll': poll_address}


class BroadcastBridge:
    """游戏线程执行原生操作，应用端口负责后台发送与推送交接。"""

    def __init__(self, process, port: BroadcastPort, consume_item):
        self.process = process
        self.port = port
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
                    request = read_request(self.process.read, stack_address)
                    if request is not None:
                        self.port.begin(*request)
                        next_instruction = self.plan['return']
            else:
                message = self.port.poll_message() if self.player_id is not None else None
                if message is not None:
                    sender_id = native_sender_id(self.process.read, message['senderId'], self.player_id)
                    self._write(self.state, encode_message(message, sender_id, self.state))
                confirmation = self.port.poll_sent()
                if confirmation is not None:
                    # 仅 HTTP 确认消耗；原生成功通知随后负责冷却和库存刷新。
                    speaker_type = confirmation['speakerType']
                    self.consume_item(SPEAKER_ITEM_IDS[speaker_type])
                    self._write(self.state + SENT_SPEAKER_TYPE,
                                struct.pack('<I', NATIVE_SPEAKER_TYPES[speaker_type]))
                    self._write(self.state + SEND_COMPLETE, struct.pack('<I', 1))
                self._write(self.state + BROADCAST_AVAILABLE,
                            struct.pack('<I', int(self.port.available())))
            struct.pack_into('<I', context, 184, next_instruction)
            self.process.set_context(thread, context)
        finally:
            self.process.close_thread(thread)
