"""仅适配 TAB 比赛信息发布的就绪调用，保留原生组装和通知流程。"""
import struct


TITLE_READY_CALL = 0x5EC2D1
TITLE_READY_WINDOW = bytes.fromhex('e82a48140084c00f84be000000')


def install(process, local_profile_ready_entry):
    """由装配层传入本地档案检查入口；只写调用位移，不修改公共在线检查。"""
    if process.read(TITLE_READY_CALL, len(TITLE_READY_WINDOW)) != TITLE_READY_WINDOW:
        raise ValueError('Scoreboard title readiness call does not match native image')
    displacement = struct.pack('<I', (local_profile_ready_entry - TITLE_READY_CALL - 5) & 0xFFFFFFFF)
    process.write(TITLE_READY_CALL + 1, displacement)
    expected_window = TITLE_READY_WINDOW[:1] + displacement + TITLE_READY_WINDOW[5:]
    if process.read(TITLE_READY_CALL, len(expected_window)) != expected_window:
        raise ValueError('Scoreboard title readiness call readback mismatch')
    return {'call_address': hex(TITLE_READY_CALL),
            'ready_entry': hex(local_profile_ready_entry),
            'changed_range': [hex(TITLE_READY_CALL + 1), hex(TITLE_READY_CALL + 4)]}
