"""离线房间名校验桥：只替换 UI 请求调用边，复用原生任务完成通知。

5D362B 的 cdecl 参数是 content/callback/type，EDI 是含 NUL 的字节数。
5CF310 接受 task/content/length/success/sanitized/type；task+28 指向状态，
927100 只读取状态+38 的错误码。桥使用栈内零错误描述，不创建在线任务。
原版 cppRefreshTask 只保存结果；AsyncTaskManager.update 才关闭提示并继续创建。
本地策略只检查非空、最多255字节及控制字符，不声称实现官方敏感词服务。
"""
from __future__ import annotations

import struct

PATCH_VA = 0x5D362B
ORIGINAL_VA = 0x63E970
CALLBACK_VA = 0x5CF310
DW_POINTER = 0x849B874
BEFORE = bytes.fromhex('e840b30600')


def build_room_name_adapter(entry: int, ready: int) -> dict:
    """生成独立代码；未就绪、在线状态及非房间名请求均走原生入口。"""
    for address in (entry, ready):
        if type(address) is not int or not 0x10000 <= address <= 0xffffe000:
            raise ValueError('适配地址必须为有效32位独立分配区')
    code = bytearray()
    labels = {}
    refs = []

    def emit(value):
        code.extend(bytes.fromhex(value))

    def branch(op, label):
        emit(op)
        refs.append((len(code), label))
        code.extend(bytes(4))

    emit('9c 60 8bec')  # 保存全部寄存器和标志；EBP 固定保存帧。
    emit('833d'); code.extend(struct.pack('<I', ready)); emit('01')
    branch('0f85', 'original')
    emit('a1'); code.extend(struct.pack('<I', DW_POINTER)); emit('85c0')
    branch('0f84', 'original')
    emit('83780c00'); branch('0f85', 'original')
    emit('837d3001'); branch('0f85', 'original')
    emit('817d2c'); code.extend(struct.pack('<I', CALLBACK_VA))
    branch('0f85', 'original')
    # ESI 持有原字符串；EBX 为本地检查结果。失败也完成任务，避免永久等待。
    emit('8b7528 8b4d00 31db 85f6'); branch('0f84', 'complete')
    emit('83f902'); branch('0f82', 'complete')
    emit('81f900010000'); branch('0f87', 'complete')
    emit('49 31d2')
    labels['scan'] = len(code)
    emit('8a0416 3c20'); branch('0f82', 'complete')
    emit('3c7f'); branch('0f84', 'complete')
    emit('42 3bd1'); branch('0f82', 'scan')
    emit('803c1600'); branch('0f85', 'complete')
    emit('43')
    labels['complete'] = len(code)
    # 栈内描述仅供原回调读错误码；不借用、修改任何原生在线任务。
    emit('81ec90000000 83e4f0 8bc4 8d5040 895028 c7423800000000')
    # 无内容时给静态空串（栈内），确保失败回调仍可安全构造 AS 字符串。
    emit('85f6'); branch('0f85', 'arguments')
    emit('c6407c00 8d707c')
    labels['arguments'] = len(code)
    emit('83ec08 6a01 6a00 53 ff7500 56 50')
    emit('e8'); code.extend(struct.pack('<I', (CALLBACK_VA-entry-len(code)-4) & 0xffffffff))
    emit('8be5 c6451c01 61 9d c3')
    labels['original'] = len(code)
    emit('8be5 61 9d e9')
    code.extend(struct.pack('<I', (ORIGINAL_VA-entry-len(code)-4) & 0xffffffff))
    for pos, label in refs:
        struct.pack_into('<i', code, pos, labels[label]-pos-4)
    return {'code': bytes(code), 'patch_va': PATCH_VA, 'before': BEFORE,
            'after': b'\xe8'+struct.pack('<I', (entry-PATCH_VA-5) & 0xffffffff)}


def validate_room_name_contract(read) -> None:
    """安装前核对调用边、回调特殊分支及状态读取 ABI，变化时拒绝安装。"""
    checks = {
        PATCH_VA: BEFORE,
        CALLBACK_VA: bytes.fromhex('558bec83e4f883ec44'),
        0x5CF39F: bytes.fromhex('8b7d1c83ff0175188b45088b4828e84e7d3500'),
        0x927100: bytes.fromhex('8b4138c3'),
    }
    for address, expected in checks.items():
        if read(address, len(expected)) != expected:
            raise ValueError(f'房间名校验 ABI 不匹配：{address:#x}')
