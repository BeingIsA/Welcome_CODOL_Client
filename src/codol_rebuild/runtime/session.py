"""运行会话：进程、句柄、受控内存和守卫生命周期。"""
from __future__ import annotations
import ctypes as C
from ctypes import wintypes as W
import hashlib, json, os, struct, subprocess, time, traceback
from pathlib import Path
from codol_rebuild.platform.win32 import (K,A,PTR,SIZE,api,check,close,wait,vquery,SI,PI,EVENT,MBI,SHELLINFO,low_token,token_info)
from codol_rebuild.runtime.scheduler import PeriodicTask

class ProcessSession:
    """只拥有运行机制；版本处理器通过装配注入。"""
    def __init__(self, args, run_dir, stage, log, record, phase_done, guard_path):
        self.args=args; self.run_dir=run_dir; self.stage=stage
        self.log=log; self.record=record; self.phase_done=phase_done
        self.guard_path=guard_path
        self.info=PI(); self.token=None; self.guard=None; self.pending=None
        self.closed=False
        self.settings_errors=(OSError,ValueError)
        self.resumed=False
        self.settings=None
        self.settings_task=PeriodicTask(2)

    @staticmethod
    def periodic(interval):
        return PeriodicTask(interval)

    @property
    def main_thread_id(self):
        return self.info.tid

    def is_running(self):
        return bool(self.info.process) and not self.closed and wait(self.info.process,0)==258

    def ensure_active(self):
        if self.closed or not self.info.process:
            raise ValueError('进程会话已关闭或尚未创建')

    def allocate(self, size=4096):
        self.ensure_active()
        return check(api(K,'VirtualAllocEx',PTR,PTR,PTR,SIZE,W.DWORD,W.DWORD)(self.info.process,None,size,0x3000,4))

    def executable(self, address, size=4096):
        self.ensure_active()
        old=W.DWORD()
        check(api(K,'VirtualProtectEx',W.BOOL,PTR,PTR,SIZE,W.DWORD,C.POINTER(W.DWORD))(self.info.process,address,size,32,C.byref(old)))

    def set_context(self, thread, ctx):
        check(api(K,'Wow64SetThreadContext',W.BOOL,PTR,PTR)(thread,ctx))

    @staticmethod
    def close_thread(thread):
        close(thread)

    def read(self, address, size):
        self.ensure_active()
        buf = C.create_string_buffer(size)
        count = SIZE()
        check(api(K, 'ReadProcessMemory', W.BOOL, PTR, PTR, PTR, SIZE, C.POINTER(SIZE))(self.info.process, address, buf, size, C.byref(count)))
        if count.value != size:
            raise ValueError('部分内存读取')
        return buf.raw

    def write(self, address, data):
        self.ensure_active()
        old = W.DWORD()
        count = SIZE()
        check(api(K, 'VirtualProtectEx', W.BOOL, PTR, PTR, SIZE, W.DWORD, C.POINTER(W.DWORD))(self.info.process, address, len(data), 64, C.byref(old)))
        try:
            check(api(K, 'WriteProcessMemory', W.BOOL, PTR, PTR, PTR, SIZE, C.POINTER(SIZE))(self.info.process, address, C.c_char_p(data), len(data), C.byref(count)))
            if count.value != len(data):
                raise ValueError('部分内存写入')
            check(api(K, 'FlushInstructionCache', W.BOOL, PTR, PTR, SIZE)(self.info.process, address, len(data)))
        finally:
            ignored = W.DWORD()
            check(api(K, 'VirtualProtectEx', W.BOOL, PTR, PTR, SIZE, W.DWORD, C.POINTER(W.DWORD))(self.info.process, address, len(data), old.value, C.byref(ignored)))

    def context(self, tid):
        thread = check(api(K, 'OpenThread', PTR, W.DWORD, W.BOOL, W.DWORD)(26, False, tid))
        ctx = C.create_string_buffer(716)
        struct.pack_into('<I', ctx, 0, 65599)
        try:
            check(api(K, 'Wow64GetThreadContext', W.BOOL, PTR, PTR)(thread, ctx))
        except Exception:
            close(thread)
            raise
        return (thread, ctx)

    def cstring(self, address, wide=False, strict=False):
        if not address:
            return ''
        data = bytearray()
        step = 2 if wide else 1
        for index in range(2048):
            part = self.read(address + index * step, step)
            if part == bytes(step):
                break
            data.extend(part)
        else:
            if strict:
                raise ValueError('字符串超过读取上限且未遇到终止符')
        return data.decode('utf-16-le' if wide else 'gbk', errors='replace')

    def regions(self):
        """只读枚举目标已提交、可读的内存区，供 schema 指纹扫描；不改动任何页。"""
        regions = []
        address = 65536
        while address < 4294967296:
            mbi = MBI()
            if not vquery(self.info.process, C.c_void_p(address), C.byref(mbi), C.sizeof(mbi)):
                break
            base = mbi.BaseAddress or 0
            size = mbi.RegionSize
            if size == 0:
                address += 65536
                continue
            if mbi.State == 4096 and mbi.Protect and (not mbi.Protect & 257):
                regions.append((base, size))
            address = base + size if base + size > address else address + size
        return regions

    def start(self, exe, settings_session):
        """守卫确认后才恢复低权限主线程。"""
        self.settings=settings_session
        self.token = low_token()
        si = SI()
        si.cb = C.sizeof(si)
        display_defaults = [] if 'config_mp.cfg' in settings_session.baseline else ['+set', 'r_displayMode', 'windowed']
        self.log['command_line'] = subprocess.list2cmdline([str(exe), '-q', *display_defaults, *self.args.argument])
        command = C.create_unicode_buffer(self.log['command_line'])
        env = {key: os.environ[key] for key in ('SystemRoot', 'WINDIR', 'COMSPEC', 'SystemDrive', 'NUMBER_OF_PROCESSORS', 'PROCESSOR_ARCHITECTURE') if key in os.environ}
        env.update({'PATH': str(self.stage) + ';' + os.environ['SystemRoot'] + '\\System32', 'TEMP': str(self.stage / 'temp'), 'TMP': str(self.stage / 'temp'), 'USERPROFILE': str(self.stage), 'LOCALAPPDATA': str(self.stage / 'localappdata'), 'APPDATA': str(self.stage / 'appdata')})
        environment = C.create_unicode_buffer('\x00'.join((k + '=' + v for k, v in sorted(env.items()))) + '\x00\x00')
        check(api(A, 'CreateProcessAsUserW', W.BOOL, PTR, W.LPCWSTR, W.LPWSTR, PTR, PTR, W.BOOL, W.DWORD, PTR, W.LPCWSTR, C.POINTER(SI), C.POINTER(PI))(self.token, str(exe), command, None, None, False, 1030, environment, str(self.stage), C.byref(si), C.byref(self.info)))
        self.log['pid'] = self.info.pid
        self.log['token'] = token_info(self.info.process)
        if self.log['token'] != {'elevated': 0, 'appcontainer': 0, 'integrity': 4096}:
            raise RuntimeError('目标令牌不符合低权限隔离约束')
        times = [C.c_uint64() for _ in range(4)]
        check(api(K, 'GetProcessTimes', W.BOOL, PTR, *[PTR] * 4)(self.info.process, *(C.byref(t) for t in times)))
        self.log['creation_time'] = times[0].value
        guard_path = self.guard_path
        self.log['guard_sha256'] = hashlib.sha256(guard_path.read_bytes()).hexdigest()
        request = SHELLINFO()
        request.size = C.sizeof(request)
        request.mask = 320
        request.verb = 'runas'
        request.file = str(guard_path)
        expected_bound = 0 if self.args.until_close else max(120, self.args.seconds + 60)
        request.parameters = subprocess.list2cmdline([str(self.run_dir), str(self.info.pid), str(exe), str(times[0].value), str(expected_bound)])
        request.show = 0
        check(api(C.WinDLL('shell32', use_last_error=True), 'ShellExecuteExW', W.BOOL, C.POINTER(SHELLINFO))(C.byref(request)))
        self.guard = request.process
        until = time.monotonic() + 30
        while time.monotonic() < until and (not (self.run_dir / 'guard-ready.json').exists()) and (wait(self.guard, 100) != 0):
            pass
        if not (self.run_dir / 'guard-ready.json').exists():
            raise RuntimeError('断网守卫未确认ready；客户端保持挂起')
        while True:
            try:
                ready = json.loads((self.run_dir / 'guard-ready.json').read_text())
                break
            except json.JSONDecodeError:
                if time.monotonic() >= until:
                    raise
                time.sleep(0.05)
        if ready['scope'] != 'offline-deny-all' or len(ready['filter_ids']) != 4 or (not ready['job_kill_on_close']) or (ready['max_processes'] != 1) or (ready['child_pid'] != self.info.pid) or (int(ready['creation_filetime']) != times[0].value) or (not ready['dynamic']):
            raise ValueError('断网守卫状态不符')
        if ready.get('bound_seconds') != expected_bound:
            raise ValueError('断网守卫持续时限不符合本次运行模式')
        self.log['guard'] = ready
        self.phase_done('process-and-uac-guard')
        check(api(K, 'DebugSetProcessKillOnExit', W.BOOL, W.BOOL)(True))
        if api(K, 'ResumeThread', W.DWORD, PTR)(self.info.thread) == 4294967295:
            raise C.WinError(C.get_last_error())
        self.resumed=True
        self.log['resumed'] = True
        self.log['lifetime'] = 'until_client_exit' if self.args.until_close else 'bounded_observation'

    def save_settings(self, final=False):
        if self.settings is None or not self.resumed:
            return
        if not self.settings_task.due(force=final):
            return
        try:
            changed = self.settings.checkpoint(final=final)
            if changed:
                self.log['game_settings_saved'] = {'files': changed, 'time': time.time(), 'final': final}
                self.record({'game_settings_saved': self.log['game_settings_saved']})
                self.log.pop('game_settings_save_error', None)
        except self.settings_errors as error:
            message = str(error)
            if self.log.get('game_settings_save_error') != message:
                self.record({'game_settings_save_error': message})
            self.log['game_settings_save_error'] = message

    def close(self):
        """终止并排空待处理事件，回收令牌与守卫。"""
        if self.info.process:
            if wait(self.info.process, 0) != 0:
                api(K, 'TerminateProcess', W.BOOL, PTR, W.UINT)(self.info.process, 53670)
            if self.pending:
                api(K, 'ContinueDebugEvent', W.BOOL, W.DWORD, W.DWORD, W.DWORD)(self.pending.pid, self.pending.tid, 2147549185)
            until = time.monotonic() + 4
            while wait(self.info.process, 0) != 0 and time.monotonic() < until:
                event = EVENT()
                if api(K, 'WaitForDebugEvent', W.BOOL, C.POINTER(EVENT), W.DWORD)(C.byref(event), 100):
                    api(K, 'ContinueDebugEvent', W.BOOL, W.DWORD, W.DWORD, W.DWORD)(event.pid, event.tid, 65538)
            self.log['child_terminated'] = wait(self.info.process, 1000) == 0
            if self.log['child_terminated']:
                self.save_settings(final=self.log.get('exit_code') == 0)
            close(self.info.thread)
            close(self.info.process)
        if self.token:
            close(self.token)
        if self.guard:
            (self.run_dir / 'guard-done.json').write_text('{}')
            if wait(self.guard, 8000) != 0:
                self.log['guard_cleanup_timeout'] = True
            if (self.run_dir / 'guard-result.json').exists():
                self.log['guard_cleanup'] = json.loads((self.run_dir / 'guard-result.json').read_text())
            close(self.guard)
        self.closed=True


class SessionLog:
    """独立运行目录与事件日志，数据路径由装配入口传入。"""
    def __init__(self, runs_root, name):
        allowed=Path(runs_root).resolve()
        self.run_dir=(allowed/name).resolve()
        if not self.run_dir.is_relative_to(allowed) or self.run_dir==allowed or self.run_dir.exists():
            raise ValueError('必须使用 offline-runs 下尚不存在的单次会话名')
        self.run_dir.mkdir(parents=True)
        self.stage=self.run_dir/'runtime';self.stage.mkdir()
        self.data={'scope':'offline client startup diagnostic','main_menu_verified':False,'modules':[], 'events':[], 'started_at':time.time()}
        self.phase_started=time.monotonic()
        self.stream=(self.run_dir/'events.jsonl').open('x',encoding='utf-8')
    def record(self,row):
        row['time']=time.time();self.data['events'].append(row)
        self.stream.write(json.dumps(row,ensure_ascii=False)+'\n');self.stream.flush()
    def phase_done(self,name):
        now=time.monotonic();row={'phase':name,'seconds':round(now-self.phase_started,3)}
        self.data.setdefault('startup_timings',[]).append(row);self.record({'startup_timing':row})
        print('[startup] '+name+': '+str(row['seconds'])+'s',flush=True)
        self.phase_started=now
    def close(self):
        self.data['finished_at']=time.time();self.stream.close()
        (self.run_dir/'result.json').write_text(json.dumps(self.data,ensure_ascii=False,indent=2),encoding='utf-8')
