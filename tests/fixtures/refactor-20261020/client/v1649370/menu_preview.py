"""固定新版的受控离线 UI 激活调用桩；只生成机器码，不安装或执行。

替换 0x5EBFF3 的原 CALL。先执行原 getter，保留其 EAX、通用寄存器及 EFLAGS，
仅在全部本地前置条件满足时调用原生 cppActivate。没有登录、档案或库存状态写入。
原生 UI 调用仍可能触发 ActionScript 回调，必须由外层受控调试宿主验证。

宿主协议：两个 int3 必须仅在登记的主线程处理；继续时 EIP=trap+1。
前置陷阱用于复查对象与主线程，后置陷阱前仅 MOV 恢复 ESP，EAX/AL 和 flags 是原生调用返回值。
原函数无论调用成功与否都可能置 once，不能以 once 代替 AL，也不能清除后重试。
EBP 指向保存帧：EDI+0、ESI+4、原EBP+8、PUSHAD的ESP+12、EBX+16、EDX+20、
ECX+24、getterEAX+28、getterEFLAGS+32、原返回地址+36。savedESP 是原入口ESP-4，
POPAD 会跳过该字段。前后陷阱 ESP 均等于 EBP 保存帧；实际 CALL 前另向下16字节对齐。
"""
from __future__ import annotations

import hashlib
import struct

IMAGE_SHA256 = '55f067ac24e7d5ea9bea5acd46a1514eb1b7780a627b73c2094f41133dce3ffd'
PATCH_VA = 0x5EBFF3
PATCH_BEFORE = bytes.fromhex('e828280000')
GETTER_VA = 0x5EE820
ACTIVATE_VA = 0x5EBE40
RETURN_VA = PATCH_VA + 5
ROOT_VALUE_VA = 0xE69738
# 每个固定地址均来自当前恢复镜像与原生消费者，而非旧版地址迁移。
FIELDS = {
    'dw_dvar_pointer': 0x849B874,
    'catalog_initialized': 0x84ABB2F,
    'catalog_count': 0x7795840,
    'catalog_records': 0x779583C,
    'movie_definition': 0xC7AE9A0,
    'movie_view': 0xC77BB98,
    'movie_init_tail': 0xE7B630B,
    'cpp_activate_once': 0x82E1344,
    'root_interface': ROOT_VALUE_VA + 8,
    'root_type': ROOT_VALUE_VA + 12,
    'root_object': ROOT_VALUE_VA + 16,
    'weapon_definition_count': 0x827F738,
    'viewer_initialized': 0xC7341D8,
}
SAVED_FRAME = {'edi': 0, 'esi': 4, 'ebp': 8, 'pushad_esp': 12, 'ebx': 16,
               'edx': 20, 'ecx': 24, 'getter_eax': 28, 'getter_eflags': 32,
               'original_return': 36}


def validate_menu_preview_image(image: bytes) -> dict:
    """安装前验证独立基底指纹及关键调用字节；已打补丁运行副本应另行核对补丁链。"""
    if type(image) is not bytes or hashlib.sha256(image).hexdigest() != IMAGE_SHA256:
        raise ValueError('离线 UI 预览基底指纹不符')
    import pefile
    pe = pefile.PE(data=image, fast_load=True)
    if pe.OPTIONAL_HEADER.ImageBase != 0x400000 or pe.FILE_HEADER.Machine != 0x14c:
        raise ValueError('离线 UI 预览仅支持固定 x86 镜像')
    for va, expected in ((PATCH_VA, PATCH_BEFORE),
                         (ACTIVATE_VA, bytes.fromhex('803d44132e0800'))):
        if pe.get_data(va - 0x400000, len(expected)) != expected:
            raise ValueError(f'离线 UI 调用边字节不符: {va:#x}')
    return {'image_sha256': IMAGE_SHA256, 'patch_va': PATCH_VA,
            'getter_va': GETTER_VA, 'activate_va': ACTIVATE_VA}


def build_menu_preview_helper(helper_va: int, *, initializer_va: int | None = None,
                              initializer_ready_va: int | None = None) -> dict:
    """返回独立 x86 调用桩、原 CALL 替换字节和宿主陷阱协议；不访问文件或进程。

    CALL 的 rel32 按32位回绕编码。GETTER 前额外留12字节，连同 helper 的额外
    返回地址保证 GETTER 入口对齐与原调用一致。LEA 不改变 GETTER 返回的 flags。
    之后 PUSHFD/PUSHAD 保存现场；激活调用前对齐 ESP，最终恢复原 getter 结果。
    """
    if type(helper_va) is not int or not 0 < helper_va <= 0xffffffff:
        raise ValueError('helper_va 必须是非零32位整数地址')
    if initializer_ready_va is not None and (initializer_va is None or
            type(initializer_ready_va) is not int or not 0 < initializer_ready_va <= 0xfffffffc):
        raise ValueError('初始化完成标志须配合有效初始化入口')
    code = bytearray()
    branches: list[tuple[int, str]] = []
    labels: dict[str, int] = {}

    def call(target: int) -> None:
        code.extend(b'\xe8' + struct.pack('<I', (target-helper_va-len(code)-5) & 0xffffffff))

    def jump(op: bytes, label: str) -> None:
        code.extend(op)
        branches.append((len(code), label))
        code.extend(bytes(4))

    def cmp_byte(address: int, value: int) -> None:
        code.extend(b'\x80\x3d' + struct.pack('<I', address) + bytes((value,)))

    def cmp_dword(address: int, value: int = 0) -> None:
        code.extend(b'\x83\x3d' + struct.pack('<I', address) + bytes((value,)))

    # 保存的是原 getter 的调用后现场，避免用 helper 的判断结果替代 menuType。
    code.extend(bytes.fromhex('8d6424f4'))
    call(GETTER_VA)
    code.extend(bytes.fromhex('8d64240c 9c60 8bec'))
    code.extend(bytes.fromhex('83f801'))  # 原 getter EAX 必须等于主菜单类型1。
    jump(b'\x0f\x85', 'finish')
    code.extend(b'\xa1' + struct.pack('<I', FIELDS['dw_dvar_pointer']))
    code.extend(bytes.fromhex('85c0'))
    jump(b'\x0f\x84', 'finish')
    code.extend(bytes.fromhex('83780c00'))
    jump(b'\x0f\x85', 'finish')
    for name in ('catalog_initialized', 'movie_init_tail'):
        cmp_byte(FIELDS[name], 1)
        jump(b'\x0f\x85', 'finish')
    cmp_dword(FIELDS['catalog_count'])
    jump(b'\x0f\x8e', 'finish')  # 使用有符号正数，拒绝负数/零目录规模。
    for name in ('catalog_records', 'movie_definition', 'movie_view'):
        cmp_dword(FIELDS[name])
        jump(b'\x0f\x84', 'finish')
    cmp_byte(FIELDS['cpp_activate_once'], 0)
    jump(b'\x0f\x85', 'finish')
    # GFxValue 仅接受已审查的 managed Object(8)/DisplayObject(10)。
    code.extend(b'\xa1' + struct.pack('<I', FIELDS['root_type']))
    code.extend(bytes.fromhex('a840'))
    jump(b'\x0f\x84', 'finish')
    code.extend(bytes.fromhex('258f000000 83f808'))
    jump(b'\x0f\x84', 'root_type_valid')
    code.extend(bytes.fromhex('83f80a'))
    jump(b'\x0f\x85', 'finish')
    labels['root_type_valid'] = len(code)
    for name in ('root_interface', 'root_object'):
        cmp_dword(FIELDS[name])
        jump(b'\x0f\x84', 'finish')
    if initializer_va is not None:
        # Native frontend frames initialize the viewer and weapon definitions.
        # cppActivate immediately queries IsWeaponDataReady (5CC9E0); do not
        # publish inventory/activate the movie before its real data is ready.
        # Return to the normal frame loop while waiting; never fabricate count.
        cmp_byte(FIELDS['viewer_initialized'], 1)
        jump(b'\x0f\x85', 'finish')
        cmp_dword(FIELDS['weapon_definition_count'])
        jump(b'\x0f\x8e', 'finish')
    before = len(code)
    code.append(0xcc)
    code.extend(bytes.fromhex('83e4f0'))  # 按标准 x86 ABI 在 CALL 前16字节对齐。
    if initializer_va is not None:
        if initializer_ready_va is not None:
            cmp_dword(initializer_ready_va, 1)
            jump(b'\x0f\x84', 'activate')
        call(initializer_va)
    labels['activate'] = len(code)
    call(ACTIVATE_VA)
    code.extend(bytes.fromhex('8be5'))  # 不改AL或flags；恢复保存帧以固定陷阱栈布局。
    after = len(code)
    code.append(0xcc)
    labels['finish'] = len(code)
    code.extend(bytes.fromhex('8be5 619d c3'))
    for offset, label in branches:
        struct.pack_into('<i', code, offset, labels[label]-offset-4)
    if helper_va + len(code) > 0x100000000:
        raise ValueError('调用桩越过32位地址空间末尾')
    # 宿主必须把 helper 放在独立代码页，绝不可覆盖输入代码/字段。
    protected = [(PATCH_VA, PATCH_VA+5), (GETTER_VA, GETTER_VA+1),
                 (ACTIVATE_VA, ACTIVATE_VA+1)] + [(a, a+4) for a in FIELDS.values()]
    if any(helper_va < end and start < helper_va + len(code) for start,end in protected):
        raise ValueError('调用桩与固定原生代码或状态字段重叠')
    patch_after = b'\xe8' + struct.pack('<I', (helper_va-RETURN_VA) & 0xffffffff)
    return {'entry_bytes': bytes(code), 'entry_va': helper_va,
            'before_trap_offset': before, 'after_trap_offset': after,
            'finish_offset': labels['finish'], 'patch_va': PATCH_VA,
            'patch_before': PATCH_BEFORE, 'patch_after': patch_after,
            'metadata': {'image_sha256': IMAGE_SHA256, 'getter_va': GETTER_VA,
                         'activate_va': ACTIVATE_VA, 'return_va': RETURN_VA,
                         'saved_frame': SAVED_FRAME.copy(), 'frame_register': 'ebp', 'trap_esp_equals_frame': True,
                         'trap_continue': 'EIP=trap+1', 'native_return_at_after_trap': 'AL',
                         'accepted_root_base_types': [8, 10], 'managed_required': True,
                         'writes_online_or_profile_state': initializer_va is not None,
                         'local_profile_initializer': initializer_va,
                         'initializer_ready_va': initializer_ready_va,
                         'requires_main_thread_verification': True,
                         'game_execution_verified': False}}


class PreviewActivationCycles:
    """每次原生界面生命周期仅允许一次激活；成功返回且原生标志复位才可再次进入。"""
    def __init__(self):
        self.pending = False
        self.succeeded = False
        self.count = 0

    def begin(self, native_called):
        if native_called != 0 or self.pending or (self.count and not self.succeeded):
            raise ValueError('离线 UI 激活尚未完成、失败或原生生命周期未复位')
        self.pending = True
        self.succeeded = False
        self.count += 1
        return self.count

    def complete(self, result, native_called):
        if not self.pending or native_called != 1:
            raise ValueError('离线 UI 激活返回状态不符')
        self.pending = False
        self.succeeded = result == 1
