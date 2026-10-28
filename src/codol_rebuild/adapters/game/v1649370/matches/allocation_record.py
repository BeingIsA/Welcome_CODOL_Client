"""原生分配记录的字段转换；不创建原生对象、不执行连接或修改房间状态。

字段证据：806F20调用B95D70解码，806B50负责构造0x238字节对象。
调用者须使用原生构造器/原有已构造槽，不得把下面字段片段当作完整对象。
"""
from ipaddress import IPv4Address
import struct

RECORD_SIZE = 0x238
CONSTRUCTOR = 0x806B50
DESERIALIZER = 0x806F20
RESULT_CALLBACK = 0x637930


def allocation_fields(address: str, port: int, security_id: bytes,
                      security_key: bytes, server_user_id: int,
                      server_name: str, maximum_players: int) -> tuple[tuple[int, bytes], ...]:
    """转换服务端确认数据为有证据的字段片段，保留对象虚表和生命周期字段。"""
    packed_address = IPv4Address(address).packed
    if not 1 <= port <= 65535:
        raise ValueError('游戏服务器端口无效')
    if type(maximum_players) is not int or not 1 <= maximum_players <= 18:
        raise ValueError('本机验收人数上限无效')
    if len(security_id) != 8 or len(security_key) != 16:
        raise ValueError('原生security ID/key长度不符')
    if not 0 < server_user_id < 1 << 64:
        raise ValueError('原生服务器用户标识无效')
    encoded_name = server_name.encode('ascii')
    if not encoded_name or len(encoded_name) >= 64 or b'\0' in encoded_name:
        raise ValueError('原生服务器名称必须为1～63字节ASCII')
    # bdCommonAddr：十个本地地址、一个公网地址和NAT类型；端口为小端。
    endpoint = packed_address + struct.pack('<H', port)
    common_address = endpoint + bytes(54) + endpoint + b'\x01'
    name_field = encoded_name + bytes(64 - len(encoded_name))
    return (
        (0x10, security_id),
        (0x18, common_address),
        (0x118, struct.pack('<I', len(common_address))),
        (0x11C, struct.pack('<III', 0, maximum_players, 0)),
        (0x128, name_field),
        # 这里是即时消息接收者的64位用户ID，不能误写成IP/端口。
        (0x168, struct.pack('<Q', server_user_id)),
        (0x170, name_field),
        (0x1CC, security_key),
    )
