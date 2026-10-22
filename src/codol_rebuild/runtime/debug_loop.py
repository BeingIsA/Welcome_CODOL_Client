"""调试事件循环：系统事件、断点恢复及注入处理器调度。"""
import ctypes as C
from ctypes import wintypes as W
import struct, time
from pathlib import Path
import pefile
from codol_rebuild.platform.win32 import K,PTR,SIZE,EVENT,api,check,close

class DebugLoop:
    """不解释版本地址或业务状态，游戏事件由处理器承接。"""
    def __init__(self, process, bridge, pe, supplier, forbidden):
        self.process=process;self.bridge=bridge;self.pe=pe;self.supplier=supplier
        self.args=process.args;self.log=process.log;self.record=process.record
        self.forbidden=forbidden;self.breaks={};self.file_traps={};self.tids=set();self.rearm={}
        bridge.bind_debugger(self)

    def arm(self, address, label):
        if address not in self.breaks:
            self.breaks[address] = (self.process.read(address, 1), label)
            self.process.write(address, b'\xcc')

    def install_file_trace(self):
        from codol_rebuild.runtime.file_trace import build_file_gate
        imports = {s.name.decode(): s.address for d in self.pe.DIRECTORY_ENTRY_IMPORT for s in d.imports if s.name}
        get_error = struct.unpack('<I', self.process.read(imports['GetLastError'], 4))[0]
        set_error = struct.unpack('<I', self.process.read(imports['SetLastError'], 4))[0]
        for name in ('CreateFileA', 'CreateFileW'):
            slot = imports[name]
            original = struct.unpack('<I', self.process.read(slot, 4))[0]
            entry = check(api(K, 'VirtualAllocEx', PTR, PTR, PTR, SIZE, W.DWORD, W.DWORD)(self.process.info.process, None, 4096, 12288, 4))
            code, trap = build_file_gate(entry, original, get_error, set_error, wide=name.endswith('W'))
            self.process.write(entry, code)
            old = W.DWORD()
            check(api(K, 'VirtualProtectEx', W.BOOL, PTR, PTR, SIZE, W.DWORD, C.POINTER(W.DWORD))(self.process.info.process, entry, 4096, 32, C.byref(old)))
            self.process.write(slot, struct.pack('<I', entry))
            self.file_traps[trap] = name
            self.record({'file_trace_installed': name, 'iat': hex(slot), 'original': hex(original), 'entry': hex(entry), 'trap': hex(trap)})

    def run(self):
        until=time.monotonic()+self.args.seconds
        while self.args.until_close or time.monotonic() < until:
            self.process.save_settings()
            event = EVENT()
            if not api(K, 'WaitForDebugEvent', W.BOOL, C.POINTER(EVENT), W.DWORD)(C.byref(event), 200):
                if C.get_last_error() == 121:
                    self.bridge.idle()
                    continue
                raise C.WinError(C.get_last_error())
            self.process.pending = event
            data = bytes(event.data)
            disposition = 65538
            self.tids.add(event.tid)
            if event.code in (3, 6):
                handle = struct.unpack_from('<Q', data)[0]
                base = struct.unpack_from('<Q', data, 24 if event.code == 3 else 8)[0]
                path = 'unknown'
                if handle:
                    text = C.create_unicode_buffer(32768)
                    if api(K, 'GetFinalPathNameByHandleW', W.DWORD, PTR, W.LPWSTR, W.DWORD, W.DWORD)(handle, text, len(text), 0):
                        path = text.value
                    close(handle)
                row = {'module': path, 'base': hex(base)}
                self.log['modules'].append(row)
                self.record(row)
                name = Path(path).name.lower()
                if name in self.forbidden:
                    self.log['stop_reason'] = 'forbidden_module_before_initialization'
                    break
                if event.code == 3:
                    self.bridge.module_loaded(base)
                if name == 'user32.dll':
                    local = Path(path.removeprefix('\\\\?\\'))
                    loaded = pefile.PE(str(local))
                    for symbol in loaded.DIRECTORY_ENTRY_EXPORT.symbols:
                        if symbol.name in (b'MessageBoxA', b'MessageBoxW') and (not symbol.forwarder):
                            self.arm(base + symbol.address, symbol.name.decode())
            elif event.code == 1:
                code = struct.unpack_from('<I', data)[0]
                address = struct.unpack_from('<Q', data, 16)[0]
                first = struct.unpack_from('<I', data, 152)[0]
                row = {'exception': hex(code), 'address': hex(address), 'first_chance': first, 'tid': event.tid}
                parameters = [struct.unpack_from('<Q', data, 32 + i * 8)[0] for i in range(min(struct.unpack_from('<I', data, 24)[0], 15))]
                row['parameters'] = [hex(v) for v in parameters]
                if code == 1080890248 and first and (len(parameters) == 4) and (parameters[0] == 4096) and (parameters[3] == 0):
                    row['debugger_notification'] = 'thread_name'
                    try:
                        row['thread_name'] = self.process.cstring(parameters[1])
                    except OSError:
                        row['thread_name_unreadable'] = True
                    self.record(row)
                elif self.bridge.handle_exception(event, code, address, row):
                    pass
                elif code in (2147483651, 1073741855) and address in self.file_traps:
                    thread, ctx = self.process.context(event.tid)
                    try:
                        ebp = struct.unpack_from('<I', ctx, 180)[0]
                        pointer = struct.unpack('<I', self.process.read(ebp + 8, 4))[0]
                        row['file_missing'] = {'api': self.file_traps[address], 'error': struct.unpack('<I', self.process.read(ebp - 8, 4))[0], 'caller': hex(struct.unpack('<I', self.process.read(ebp + 4, 4))[0])}
                        try:
                            path = self.process.cstring(pointer, self.file_traps[address].endswith('W'), strict=True)
                            row['file_missing']['path'] = path
                            if self.supplier:
                                supplied = self.supplier.supply(path)
                                row['resource_supply'] = supplied
                                if supplied.get('status') in ('supplied', 'already_present'):
                                    self.process.write(ebp - 12, struct.pack('<I', 1))
                        except (OSError, ValueError) as error:
                            row['file_missing']['error_detail'] = str(error)
                        struct.pack_into('<I', ctx, 184, address + 1)
                        check(api(K, 'Wow64SetThreadContext', W.BOOL, PTR, PTR)(thread, ctx))
                    finally:
                        close(thread)
                    self.record(row)
                elif code in (2147483651, 1073741855) and address in self.breaks:
                    original, label = self.breaks.pop(address)
                    thread, ctx = self.process.context(event.tid)
                    try:
                        self.bridge.breakpoint(event, address, original, label, row, thread, ctx)
                    finally:
                        close(thread)
                    self.record(row)
                    if self.log.get('stop_reason') == 'fatal_dialog':
                        break
                elif code in (2147483651, 1073741855):
                    self.record(row)
                else:
                    try:
                        thread, ctx = self.process.context(event.tid)
                        try:
                            eip = struct.unpack_from('<I', ctx, 184)[0]
                            esp = struct.unpack_from('<I', ctx, 196)[0]
                            row['registers'] = {n: hex(struct.unpack_from('<I', ctx, o)[0]) for n, o in [('eax', 176), ('ecx', 172), ('edx', 168), ('ebx', 164), ('esi', 160), ('edi', 156), ('ebp', 180), ('eip', 184), ('esp', 196)]}
                            row['instruction'] = self.process.read(eip, 32).hex()
                            for stack_size in (4096, 1024, 256, 64):
                                try:
                                    row['stack'] = self.process.read(esp, stack_size).hex()
                                    break
                                except OSError:
                                    pass
                            row['frames'] = []
                            frame = struct.unpack_from('<I', ctx, 180)[0]
                            seen = set()
                            for _ in range(20):
                                if not frame or frame in seen:
                                    break
                                seen.add(frame)
                                try:
                                    values = struct.unpack('<18I', self.process.read(frame, 72))
                                    entry = {'ebp': hex(frame), 'return': hex(values[1]), 'arguments': [hex(v) for v in values[2:]], 'strings': {}}
                                    for value in values[2:10]:
                                        try:
                                            string = self.process.cstring(value)
                                            if string and all((ch.isprintable() or ch in '\r\n\t' for ch in string)):
                                                entry['strings'][hex(value)] = string
                                        except (OSError, ValueError):
                                            pass
                                    row['frames'].append(entry)
                                    if values[0] <= frame or values[0] - frame > 1048576:
                                        break
                                    frame = values[0]
                                except OSError:
                                    break
                            row['parameters'] = [hex(struct.unpack_from('<Q', data, 32 + i * 8)[0]) for i in range(min(struct.unpack_from('<I', data, 24)[0], 15))]
                        finally:
                            close(thread)
                    except OSError as error:
                        row['context_error'] = str(error)
                    self.record(row)
                    disposition = 2147549185
                    if not first:
                        self.log['stop_reason'] = 'unhandled_exception'
                        break
            elif event.code == 5:
                self.log['exit_code'] = struct.unpack_from('<I', data)[0]
                self.record({'exit_code': self.log['exit_code']})
            elif event.code == 8:
                address = struct.unpack_from('<Q', data)[0]
                wide = struct.unpack_from('<H', data, 8)[0]
                try:
                    self.record({'debug_string': self.process.cstring(address, bool(wide))})
                except OSError:
                    pass
            check(api(K, 'ContinueDebugEvent', W.BOOL, W.DWORD, W.DWORD, W.DWORD)(event.pid, event.tid, disposition))
            self.process.pending = None
            if event.code == 5:
                break
        else:
            self.log['stop_reason'] = 'observation_timeout'
