"""Windows SMBIOS 系统 UUID 摘要；不向服务端发送硬件原始字段。"""
import ctypes
import hashlib


def fingerprint() -> str:
    """读取固件 Type 1 系统 UUID；无有效硬件标识时明确失败，不随机换指纹。"""
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    get_table = kernel.GetSystemFirmwareTable
    get_table.argtypes = (ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32)
    get_table.restype = ctypes.c_uint32
    provider = int.from_bytes(b'RSMB', 'big')
    size = get_table(provider, 0, None, 0)
    if not 8 <= size <= 1024 * 1024:
        raise OSError('无法读取 SMBIOS 硬件信息')
    buffer = ctypes.create_string_buffer(size)
    if get_table(provider, 0, buffer, size) != size:
        raise OSError('SMBIOS 硬件信息读取不完整')
    raw, offset = buffer.raw, 8
    while offset + 4 <= size:
        kind, length = raw[offset:offset + 2]
        if length < 4 or offset + length > size:
            break
        if kind == 1 and length >= 24:
            value = raw[offset + 8:offset + 24]
            if value not in (bytes(16), b'\xff' * 16):
                return 'smbios-v1-' + hashlib.sha256(b'CODOL\0' + value).hexdigest()
        end = raw.find(b'\0\0', offset + length)
        if end < 0 or kind == 127:
            break
        offset = end + 2
    raise OSError('此设备没有有效的 SMBIOS 系统 UUID，无法生成稳定硬件指纹')
