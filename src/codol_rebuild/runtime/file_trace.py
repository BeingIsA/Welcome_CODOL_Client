"""版本无关的 x86 CreateFileA/W 失败诊断门；默认只记录，允许宿主请求一次重试。

参考旧 Client/src/codol/v2697/resource_gate.py 的参数转发及错误码保存方式，
来源 SHA256: f872b8a30d980ab4d11295da1efa5c324bb0f4abae7451c329e7546d0df9da6b。
本模块独立实现，不导入旧工程，也不包含任何游戏版本地址或资源过滤规则。

安装协议：在 WOW64 初始断点完成系统导入解析后，从目标 IAT 读取真正的
CreateFileA/W 地址；GetLastError/SetLastError 也必须是目标进程中的已解析地址。
先分配目标代码区，再用其入口生成机器码；写入、设置可执行权限并刷新指令缓存，
最后替换相关 IAT 槽。不要把宿主的 64 位函数地址截断后传入。

陷阱协议（仅处理已登记的 trap 地址及本目标进程/线程的 breakpoint 事件）：
  EBP-8: 原 API 调用后的 GetLastError（2 或 3）；EBP-4: 原 HANDLE (0xffffffff)
  EBP-12: retry 标志，初始为 0；只有宿主明确写 1 才重试一次原 API。
  EBP+0: 调用者 EBP；EBP+4: 调用者返回地址；ESP == EBP-12
  EBP+8,+12,+16,+20,+24,+28,+32: 原七个 CreateFile 参数，首项为文件名指针。
  EAX == 原错误码；文件名 A 用目标 ACP 解码，W 用 UTF-16LE。宿主应有读取长度
  上限、页边界和不可读指针处理；无法读取必须记录原因，不能当作空文件名。

门内已有 0xCC。记录后将该线程 EIP 明确设为 trap+1，DBG_CONTINUE；不要把 EIP
退回 trap，也不要走移除一次性断点的通用路径。保留 0xCC 即可再次捕获后续失败，
不需要单步重装。若只需一次，则在暂停期间把该字节改成 NOP、刷新指令缓存后仍
从 trap+1 继续。除可选 retry 标志外，两种路径都不修改其余寄存器/栈。
宿主只有确认所需文件已经补齐后才可写 retry=1；门桩不自行补件，不伪造句柄。
重试结果无论成功失败都不再触发本门的陷阱，尾部返回最后一次原 API 的真实句柄
和错误码并 ret 28。retry 非 1（包括 0）均不重试，返回第一次原 API 的结果。
调试宿主不得用自己的 GetLastError 代替上述栈中的目标错误码。
"""
from __future__ import annotations

import struct


def build_file_gate(entry: int, original: int, get_error: int, set_error: int,
                    wide: bool = False) -> tuple[bytes, int]:
    """返回 (x86 机器码, int3 地址)，保留 stdcall 栈、非易失寄存器和 API 结果。

    只有原句柄为 INVALID_HANDLE_VALUE 且原错误码为 ERROR_FILE_NOT_FOUND / 
    ERROR_PATH_NOT_FOUND 才暂停；不限制访问模式。wide 仅供调用方关联解码方式，
    A/W 的调用约定完全相同，因此生成相同机器码。只接受非零 32 位整数地址，
    拒绝跨地址空间尾部的代码或指向门内的 API 地址。
    """
    addresses = {'entry': entry, 'original': original,
                 'get_error': get_error, 'set_error': set_error}
    for name, address in addresses.items():
        if type(address) is not int or not 0 < address <= 0xffffffff:
            raise ValueError(f'{name} 必须是非零 32 位整数地址')
    if type(wide) is not bool:
        raise ValueError('wide 必须是 bool')

    code = bytearray.fromhex('55 8b ec 83 ec 0c')  # EBP 帧及句柄、错误码、retry 三个局部值。
    branches: list[tuple[int, str]] = []

    def call(address: int) -> None:
        # x86 EIP 加法按 32 位回绕；不用 x64 的有符号 ±2GB 距离限制。
        relative = (address - entry - len(code) - 5) & 0xffffffff
        code.extend(b'\xe8' + struct.pack('<I', relative))

    def branch(opcode: str, label: str) -> None:
        code.extend(bytes.fromhex(opcode))
        branches.append((len(code), label))
        code.extend(bytes(4))

    def open_file() -> None:
        # 原始栈参数仍在 EBP+8..32；原 API 自行清理参数副本的 28 字节。
        for offset in range(32, 7, -4):
            code.extend(bytes((0xff, 0x75, offset)))
        call(original)
        code.extend(bytes.fromhex('89 45 fc'))  # 先保存句柄，避免 GetLastError 覆盖 EAX。
        call(get_error)
        code.extend(bytes.fromhex('89 45 f8'))  # 所有返回路径恢复最后一次调用的错误码。

    open_file()
    code.extend(bytes.fromhex('83 7d fc ff'))
    branch('0f85', 'finish')
    code.extend(bytes.fromhex('83 f8 02'))
    branch('0f84', 'missing')
    code.extend(bytes.fromhex('83 f8 03'))
    branch('0f85', 'finish')
    labels = {'missing': len(code)}
    code.extend(bytes.fromhex('c7 45 f4 00 00 00 00'))  # 每次缺失调用独立初始化 retry=0。
    trap = entry + len(code)
    code.append(0xcc)
    code.extend(bytes.fromhex('83 7d f4 01'))
    branch('0f85', 'finish')
    open_file()  # 宿主明确要求时只重试一次，不回到上述判断或 int3。
    labels['finish'] = len(code)
    code.extend(bytes.fromhex('ff 75 f8'))
    call(set_error)
    code.extend(bytes.fromhex('8b 45 fc 8b e5 5d c2 1c 00'))
    for offset, label in branches:
        struct.pack_into('<i', code, offset, labels[label] - offset - 4)
    if entry + len(code) > 0x100000000:
        raise ValueError('门桩越过 32 位地址空间末尾')
    for name in ('original', 'get_error', 'set_error'):
        if entry <= addresses[name] < entry + len(code):
            raise ValueError(f'{name} 不能指向门桩内部')
    return bytes(code), trap
