"""1649370 聊天展示：大小喇叭与普通消息共用的原生字段、身份及 GFx 通知。"""
import hashlib
import struct
from datetime import datetime

CAPACITY = 65536
MESSAGE_READY, MESSAGE_OBJECT, FIELDS = 4, 32, 64
MESSAGE_FIELDS = ('channelID', 'senderID', 'senderName', 'message',
                  'formatedTime', 'loudSpeakerType')
CONTRACT = {
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


def native_sender_id(read, sender_id, local_player_id):
    """本人复用 GetPlayerID 的实际来源，其他玩家沿现有房间的显示 ID 映射。"""
    if sender_id == local_player_id:
        identity_object = read_word(read, 0x84ABB08) or read_word(read, 0x84ABB14)
        # GetPlayerID 在两指针均为空时返回十进制字符串 0。
        identity_number = struct.unpack('<Q', read(identity_object + 0xF8, 8))[0] if identity_object else 0
    else:
        identity_number = int.from_bytes(hashlib.sha256(sender_id.encode('utf-8')).digest()[:8], 'little') or 1
    return str(identity_number)


def encode_message(message, sender_id, state, channel_id, speaker_type):
    """转换原生既有字段；服务端消息 ID 不伪装成原生玩家 ID。"""
    time_text = datetime.fromisoformat(message['sentAt']).astimezone().strftime('%H:%M')
    values = (channel_id, sender_id, message['senderName'], message['text'],
              time_text, speaker_type)
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
        payload = append_string(value) if value_kind == 6 else value & 0xFFFFFFFF
        struct.pack_into('<IIIIII', data, FIELDS + field_index * 36,
                         field_pointer, 0xFFFFFFFF, 0xFFFFFFFF, 0, value_kind, payload)
    if len(data) > CAPACITY:
        raise ValueError('聊天内容超过原生发布缓冲区')
    struct.pack_into('<I', data, MESSAGE_READY, 1)
    return bytes(data)


def build_message_dispatch(entry, state):
    """构造并发布原生聊天对象，复用控制器接收及历史记录流程。"""
    instructions = bytearray()
    labels, branches = {}, []

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

    emit('9c60 8bec')
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
    return bytes(instructions)
