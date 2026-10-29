"""可选原生退出诊断：一次性断点采集，恢复原指令后继续原生流程。"""
import logging
from pathlib import Path
import struct

import pefile


LOGGER = logging.getLogger(__name__)
TARGET_EXPORTS = {
    b'_invalid_parameter', b'_invalid_parameter_noinfo',
    b'_invalid_parameter_noinfo_noreturn', b'_invoke_watson',
    b'NtTerminateProcess',
}


class TerminationTrace:
    """仅观察 32 位目标模块；诊断状态独立于版本业务断点。"""

    def __init__(self, process, record):
        self.process = process
        self.record = record
        self.breakpoints = {}
        self.modules = {}

    def module_loaded(self, path, base):
        """从目标实际 DLL 解析导出；不加载 DLL 到宿主进程。"""
        if base > 0xffffffff or path == 'unknown':
            return
        try:
            with pefile.PE(str(Path(path.removeprefix('\\\\?\\'))), fast_load=True) as image:
                if image.FILE_HEADER.Machine != 0x14c:
                    return
                self.modules[base] = (image.OPTIONAL_HEADER.SizeOfImage, path)
                image.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_EXPORT']])
                for symbol in getattr(getattr(image, 'DIRECTORY_ENTRY_EXPORT', None), 'symbols', ()):
                    if symbol.name not in TARGET_EXPORTS or symbol.forwarder:
                        continue
                    address = base + symbol.address
                    if address in self.breakpoints:
                        continue
                    original = self.process.read(address, 1)
                    if original == b'\xcc':
                        continue
                    self.process.write(address, b'\xcc')
                    self.breakpoints[address] = (original, symbol.name.decode())
                    self.record({'native_exit_trace_installed': symbol.name.decode(), 'address': hex(address), 'module': path})
        except (OSError, pefile.PEFormatError) as error:
            self.record({'native_exit_trace_install_error': str(error), 'module': path})
            LOGGER.warning('原生退出诊断安装失败：%s', path, exc_info=True)

    def module_unloaded(self, base):
        """卸载时丢弃地址，避免模块地址复用命中旧断点记录。"""
        module = self.modules.pop(base, None)
        if module:
            for address in list(self.breakpoints):
                if base <= address < base + module[0]:
                    del self.breakpoints[address]

    def location(self, address):
        for base, (size, path) in self.modules.items():
            if base <= address < base + size:
                return {'address': hex(address), 'module': path, 'offset': hex(address - base)}
        return {'address': hex(address)}

    def capture(self, address, thread, context, row):
        """先恢复指令及 EIP，再只读采集；不可读栈不妨碍原流程继续。"""
        original, symbol = self.breakpoints.pop(address)
        self.process.write(address, original)
        struct.pack_into('<I', context, 184, address)
        self.process.set_context(thread, context)
        row['native_exit_trace'] = symbol
        row['trace_policy'] = 'one-shot per exported address'
        row['registers'] = {name: hex(struct.unpack_from('<I', context, offset)[0])
                            for name, offset in [('eax', 176), ('ecx', 172), ('edx', 168),
                                                 ('ebx', 164), ('esi', 160), ('edi', 156),
                                                 ('ebp', 180), ('eip', 184), ('esp', 196)]}
        stack_pointer = struct.unpack_from('<I', context, 196)[0]
        try:
            arguments = struct.unpack('<3I', self.process.read(stack_pointer, 12))
            row['caller'] = self.location(arguments[0])
            if symbol == 'NtTerminateProcess':
                row['target_process_handle'] = hex(arguments[1])
                row['termination_status'] = hex(arguments[2])
            for stack_size in (4096, 1024, 256, 64):
                try:
                    row['stack'] = self.process.read(stack_pointer, stack_size).hex()
                    break
                except OSError:
                    continue
            row['frames'] = []
            frame_pointer = struct.unpack_from('<I', context, 180)[0]
            for frame_index in range(20):
                if not frame_pointer:
                    break
                next_frame, return_address = struct.unpack('<2I', self.process.read(frame_pointer, 8))
                row['frames'].append(self.location(return_address))
                if not frame_pointer < next_frame <= frame_pointer + 1048576:
                    break
                frame_pointer = next_frame
        except OSError as error:
            row['capture_error'] = str(error)
        LOGGER.warning('原生退出诊断命中 %s，线程=%s，退出状态=%s；详细栈见 events.jsonl',
                       symbol, row.get('tid'), row.get('termination_status'))
