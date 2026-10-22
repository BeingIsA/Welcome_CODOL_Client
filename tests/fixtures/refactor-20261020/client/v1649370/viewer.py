"""固定版本本地人物预览入口适配；仅替换预览函数的在线档案门。"""
import struct

PATCH_VA = 0x618A23
BODY_VA = 0x618A54
EXIT_VA = 0x6193C8
GATE_BYTES = bytes.fromhex(
    'e8d880110084c00f8498090000e83b66efff84c0740ee852061e00'
    '83b89400000001750d833d60ba4a08040f8574090000')
VIEWER = 0xC7341D8
DW_POINTER = 0x849B874
INVENTORY_READY = 0x82C4A86
INVENTORY_COUNT = 0x82C4A8C
WEAPON_COUNT = 0x827F738
# 原生表 E59CE0：3=team，11=champion_select，依赖房间/队伍数据。
LOCAL_MODES = (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10)
ROOM_POINTER = 0x82E13FC
AVATAR_COUNT = 0x82E13A4


def build_local_viewer_adapter(entry, profile_ready):
    """保留寄存器/标志/栈；本地数据有效才进入原生模型更新，否则走原生退出。"""
    if any(type(x) is not int or not 0x10000 <= x < 0xfffff000 for x in (entry, profile_ready)):
        raise ValueError('本地预览适配地址无效')
    code = bytearray(b'\x9c\x60')
    branches = []

    def reject(op=b'\x0f\x85'):
        code.extend(op)
        branches.append(len(code))
        code.extend(b'\0'*4)

    code.extend(b'\x81\xfb'+struct.pack('<I', VIEWER))
    reject()
    for address in (profile_ready, INVENTORY_READY, VIEWER):
        code.extend(b'\x80\x3d'+struct.pack('<I', address)+b'\x01')
        reject()
    for address in (INVENTORY_COUNT, WEAPON_COUNT):
        code.extend(b'\x83\x3d'+struct.pack('<I', address)+b'\x00')
        reject(b'\x0f\x8e')
    code.extend(b'\xa1'+struct.pack('<I', DW_POINTER)+b'\x85\xc0')
    reject(b'\x0f\x84')
    code.extend(b'\x83\x78\x0c\x00')
    reject()
    # 原生主体从 viewer+84 的 dvar 同步模式，旧 viewer+8 不能作为切换门。
    code.extend(bytes.fromhex('8b8384000000 85c0'))
    reject(b'\x0f\x84')
    code.extend(bytes.fromhex('8b400c 83f80a'))
    reject(b'\x0f\x87')  # 同时拒绝负数、未知模式和 champion_select。
    code.extend(b'\x83\xf8\x03')
    code.extend(b'\x0f\x85')
    non_team = len(code)
    code.extend(bytes(4))
    # team 原生固定容量为18槽；只在房间/成员/槽数组就绪时进入。
    code.extend(b'\xa1'+struct.pack('<I',ROOM_POINTER)+b'\x85\xc0')
    reject(b'\x0f\x84')
    code.extend(bytes.fromhex('83781c00'))
    reject(b'\x0f\x84')
    code.extend(bytes.fromhex('83b8d805010001'))
    reject(b'\x0f\x8c')
    code.extend(bytes.fromhex('83b8d805010012'))
    reject(b'\x0f\x8f')
    code.extend(b'\xa1'+struct.pack('<I',AVATAR_COUNT)+bytes.fromhex('48 83f811'))
    reject(b'\x0f\x87')
    struct.pack_into('<i',code,non_team,len(code)-non_team-4)

    def resume(target):
        code.extend(b'\x61\x9d\xe9')
        code.extend(struct.pack('<I', (target-entry-len(code)-4)&0xffffffff))

    resume(BODY_VA)
    denied = len(code)
    resume(EXIT_VA)
    for offset in branches:
        struct.pack_into('<i', code, offset, denied-offset-4)
    return {'code':bytes(code), 'patch_va':PATCH_VA, 'gate_before':GATE_BYTES,
            'before':GATE_BYTES[:5], 'after':b'\xe9'+struct.pack('<I',(entry-PATCH_VA-5)&0xffffffff),
            'body_va':BODY_VA, 'exit_va':EXIT_VA, 'local_modes':LOCAL_MODES}
