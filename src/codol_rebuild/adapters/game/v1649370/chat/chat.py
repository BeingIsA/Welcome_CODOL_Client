"""1649370 普通聊天桥：接管大厅/房间分派，保留原生输入及通知流程。"""
import struct
from codol_rebuild.application.chat.contracts import ChatPort
from codol_rebuild.adapters.game.v1649370.chat.chat_message import (
    CAPACITY, FIELDS, CONTRACT as MESSAGE_CONTRACT, read_word,
    native_sender_id, encode_message, build_message_dispatch,
)

SEND_DISPATCH = 0x726F2E
# 原生提前返回分支与分派点使用相同栈布局，释放 GFx 临时对象后正常返回。
# 日志尾部另有 28 字节参数栈，不能从分派点直接跳入其清理段。
SEND_CLEANUP = 0x726FA4
CHANNEL_SCOPES = {0: 'lobby', 3: 'room'}
NATIVE_CHANNELS = {scope: channel for channel, scope in CHANNEL_SCOPES.items()}
CONTRACT = {
    **MESSAGE_CONTRACT,
    SEND_DISPATCH: bytes.fromhex('8b44241083f809'),
    0x726F35: bytes.fromhex('0f8737020000'),
    # 原生 GFx 已完成参数提取；已接管发送只续行到原生对象清理。
    0xDB8060: b'message\0',
    0xDD4DDC: b'channelID\0',
}


def read_request(read, stack_address):
    """读取分派点已提取的频道和 UTF-8 正文，不覆盖其他频道。"""
    channel_id = read_word(read, stack_address + 0x10)
    if channel_id not in CHANNEL_SCOPES:
        return None
    text_pointer = read_word(read, stack_address + 0x14)
    text_bytes = bytearray()
    for character_offset in range(CAPACITY):
        character = read(text_pointer + character_offset, 1)
        if character == b'\0':
            return text_bytes.decode('utf-8'), CHANNEL_SCOPES[channel_id]
        text_bytes.extend(character)
    raise ValueError('原生聊天字符串超过适配缓冲区')


def build_adapter(entry, state):
    """仅替换分派处七字节；未接管频道恢复原比较标志并跳回。"""
    instructions = bytearray(b'\xcc' + CONTRACT[SEND_DISPATCH])
    instructions.extend(b'\xe9' + struct.pack('<i', SEND_DISPATCH + 7 - entry - len(instructions) - 5))
    poll_address = entry + len(instructions)
    # 暂停点不执行原生调用；恢复后共用消息发布桩保存全部寄存器。
    instructions.extend(b'\xcc')
    instructions.extend(build_message_dispatch(entry + len(instructions), state))
    patch = b'\xe9' + struct.pack('<i', entry - SEND_DISPATCH - 5) + b'\x90\x90'
    return {'code': bytes(instructions), 'patch': patch, 'poll': poll_address,
            'traps': {entry: 'send', poll_address: 'poll'}}


class ChatBridge:
    """主线程消费消息；通过显式房间查询端口确认自身当前成员身份。"""

    def __init__(self, process, port: ChatPort, current_room):
        self.process = process
        self.port = port
        self.current_room = current_room
        self.player_id = None
        self.state = None
        self.plan = None
        self.traps = {}

    def connected(self, player_id, display_name):
        """只使用既有登录身份，不自行登录或管理连接。"""
        self.player_id = player_id

    def _write(self, address, data):
        self.process.write(address, data)
        if self.process.read(address, len(data)) != data:
            raise ValueError('聊天适配写入回读失败')

    def install(self):
        """验证固定版本参数提取约定，安装最小分派补丁及独立展示区。"""
        for address, expected in CONTRACT.items():
            if self.process.read(address, len(expected)) != expected:
                raise ValueError(f'聊天 ABI 不匹配：{address:#x}')
        self.state = self.process.allocate(CAPACITY)
        entry = self.process.allocate(4096)
        self.plan = build_adapter(entry, self.state)
        self._write(self.state, bytes(FIELDS))
        self._write(entry, self.plan['code'])
        self.process.executable(entry, 4096)
        self._write(SEND_DISPATCH, self.plan['patch'])
        self.traps = self.plan['traps']
        return self.plan['poll']

    def handle(self, event, address):
        """发送不等待结果；展示只消费推送，迟到房间消息在编码前丢弃。"""
        if event.tid != self.process.main_thread_id:
            raise ValueError('聊天原生操作必须在主线程执行')
        thread, context = self.process.context(event.tid)
        try:
            next_instruction = address + 1
            if self.player_id is not None:
                room_id = self.current_room()
                if self.traps[address] == 'send':
                    stack_address = struct.unpack_from('<I', context, 196)[0]
                    request = read_request(self.process.read, stack_address)
                    if request is not None:
                        text, scope = request
                        self.port.begin(text, scope, room_id)
                        next_instruction = SEND_CLEANUP
                else:
                    message = self.port.poll_message(room_id)
                    if message is not None:
                        sender_id = native_sender_id(self.process.read, message['senderId'], self.player_id)
                        self._write(self.state, encode_message(
                            message, sender_id, self.state, NATIVE_CHANNELS[message['scope']], -1))
            struct.pack_into('<I', context, 184, next_instruction)
            self.process.set_context(thread, context)
        finally:
            self.process.close_thread(thread)
