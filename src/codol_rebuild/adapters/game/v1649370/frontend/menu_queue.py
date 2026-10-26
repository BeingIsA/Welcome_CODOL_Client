"""1649370 菜单命令的纯校验与写入计划；本模块不读写目标进程、不执行命令。

来源：evidence/deferred-menu-command-plan.json 的本版本原生消费者与持锁调用链。
只允许正常帧的主线程在 0x4f2ec0 入口暂停时使用。宿主还必须确认调试事件已暂停
所有目标线程、原生临界区仍由当前调用持有，并用本轮证据验证客户端对象已创建；
这里的 state 由宿主提交，纯函数不能替代这些运行时核对。

提交顺序：再次核对状态、队列长度和 current_data；写 payload 并回读一致；最后写
length_bytes 并回读；标记本轮已排队，然后恢复断点入口继续原消费者。失败就结束
本轮，不能带部分写入继续。不得使用新线程或直接调用菜单处理器替代原生命令队列。
"""
from __future__ import annotations

import hashlib
import struct

CONSUMER_VA = 0x4f2ec0
FRAME_RETURN_ADDRESSES = frozenset((0x4fe615, 0x4fe672))
BUFFER_POINTER_VA = 0x76595a4
BUFFER_CAPACITY_VA = 0x76595a8
BUFFER_LENGTH_VA = 0x76595ac
EXPECTED_POINTER = 0x33821b0
EXPECTED_CAPACITY = 0x20000
COMMAND = b'\nswitchtomainmenu\n'


def build_queue_plan(ptr: int, cap: int, length: int, current_data: bytes,
                     state: dict) -> dict:
    """校验输入快照，仅返回追加字节与长度更新；不改变 existing queue 或 state。

    state 必须显式包含 thread_id/main_thread_id/return_address/local_client_index/
    controller_index/engine_returned/dw_enabled/ui_resource_count/client_object_ready/
    already_queued。client_object_ready 只能来自实际指针解码及对象有效性验证，不能
    由 ui_resource_count>0 推断。payload 含额外 NUL，但 new_length 不计该 NUL。
    local_client_index 是 consumer 的第一个栈参数，不是对象表随机槽号；后者
    0..7、allocation_complete 与解码对象可读性由宿主核对后传 client_object_ready。
    """
    if type(state) is not dict:
        raise ValueError('state 必须为显式运行时快照字典')
    integer_fields = ('thread_id', 'main_thread_id', 'return_address', 'local_client_index',
                      'controller_index', 'dw_enabled', 'ui_resource_count')
    for name in integer_fields:
        if type(state.get(name)) is not int or not 0 <= state[name] <= 0xffffffff:
            raise ValueError(f'无效或缺失的状态字段：{name}')
    for name in ('engine_returned', 'client_object_ready', 'already_queued'):
        if type(state.get(name)) is not bool:
            raise ValueError(f'状态字段必须是 bool：{name}')
    if not state['thread_id'] or state['thread_id'] != state['main_thread_id']:
        raise ValueError('仅允许本次创建的主线程')
    if state['return_address'] not in FRAME_RETURN_ADDRESSES:
        raise ValueError('仅允许两个已验证的正常帧 consumer 调用点')
    if state['local_client_index'] != 0 or state['controller_index'] != 0:
        raise ValueError('要求本地客户端索引和控制器索引均为0')
    if not state['engine_returned'] or state['dw_enabled'] != 0:
        raise ValueError('引擎须完成初始化且原生 DW dvar 为0')
    if state['ui_resource_count'] <= 0 or not state['client_object_ready']:
        raise ValueError('UI资源或已验证的本地客户端对象尚未就绪')
    if state['already_queued']:
        raise ValueError('本轮菜单命令已经排队，拒绝重复提交')
    if type(ptr) is not int or ptr != EXPECTED_POINTER:
        raise ValueError('命令缓冲指针不符合固定版本证据')
    if type(cap) is not int or cap != EXPECTED_CAPACITY:
        raise ValueError('命令缓冲容量不符合固定版本证据')
    if type(length) is not int or not 0 <= length < cap:
        raise ValueError('命令缓冲长度越界')
    if type(current_data) is not bytes or len(current_data) != length:
        raise ValueError('当前队列快照必须恰为已报告的length字节')
    payload = COMMAND + b'\0'
    if length + len(payload) > cap:
        raise ValueError('命令缓冲不足以容纳菜单命令及额外NUL')
    new_length = length + len(COMMAND)
    return {
        'payload_address': ptr + length,
        'payload': payload,
        'length_address': BUFFER_LENGTH_VA,
        'expected_length': length,
        'new_length': new_length,
        'length_bytes': struct.pack('<I', new_length),
        'expected_prefix_sha256': hashlib.sha256(current_data).hexdigest(),
        'consumer_va': CONSUMER_VA,
        'return_address': state['return_address'],
        'main_thread_id': state['main_thread_id'],
    }
