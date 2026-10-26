"""1649370 会话与房间共用原生状态区的固定布局和受控读写。"""
import struct

POLL, ACTION, ROOM, SESSION, LEAVE_ARG, LOGIN, IDENTITY = 0, 4, 8, 12, 16, 20, 24
UNDEFINED, MESSAGE = 64, 128


def u32(read, address):
    """按固定 ABI 读取一个小端整数。"""
    return struct.unpack('<I', read(address, 4))[0]


def checked_write(process, address, value):
    """仅由调试暂停点写入并回读通信区。"""
    process.write(address, value)
    if process.read(address, len(value)) != value:
        raise ValueError('联机桥状态回读失败')
