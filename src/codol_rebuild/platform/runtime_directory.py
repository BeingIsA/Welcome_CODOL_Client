"""Windows 可复用运行目录：独占租用、会话目录联接和进程生命周期绑定。

租用锁由系统句柄持有；多开时使用不同槽位。锁的副本交给游戏进程，
即使宿主提前退出，也要等游戏进程结束后才能再次租用该目录。
"""
from pathlib import Path
import ctypes
from ctypes import wintypes
import re

from codol_rebuild.platform.win32 import K, PTR, api, check, close
from codol_rebuild.platform.resource_mount import mount_resource


SESSION_DIRECTORIES = ('players', 'appdata', 'localappdata', 'temp')


class RuntimeDirectoryLease:
    """持有一个运行槽位；只重绑已知顶层联接，不删除其目标或会话数据。"""

    def __init__(self, slot, handle, sessions_root):
        self.slot = slot
        self.path = slot / 'runtime'
        self.handle = handle
        self.sessions_root = sessions_root

    def bind_session(self, session_directory):
        """把原生所需的可写顶层目录指向当前独立会话。"""
        if self.handle is None:
            raise ValueError('运行目录租用已释放')
        session_directory = Path(session_directory).resolve()
        if not session_directory.is_relative_to(self.sessions_root):
            raise ValueError('会话目录越界')
        for name in SESSION_DIRECTORIES:
            source = session_directory / name
            if not source.is_dir() or source.is_junction() or source.is_symlink():
                raise ValueError('会话目录必须为普通目录: ' + str(source))
            destination = self.path / name
            if destination.is_junction():
                if not destination.resolve().is_relative_to(self.sessions_root):
                    raise ValueError('既有会话联接目标越界: ' + str(destination))
                # Windows rmdir 仅移除联接本身；绝不递归删除旧会话数据。
                destination.rmdir()
            elif destination.exists() or destination.is_symlink():
                raise ValueError('会话联接位置被其他文件占用: ' + str(destination))
            mount_resource(source, destination, session_directory, self.path)

    def hold_for_process(self, process_handle):
        """给挂起的游戏进程保留无读写权限的锁句柄，随其退出自动关闭。"""
        if self.handle is None:
            raise ValueError('运行目录租用已释放')
        duplicate = PTR()
        current_process = api(K, 'GetCurrentProcess', PTR)()
        check(api(K, 'DuplicateHandle', wintypes.BOOL, PTR, PTR, PTR,
                  ctypes.POINTER(PTR), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)(
                      current_process, self.handle, process_handle, ctypes.byref(duplicate),
                      0, False, 0))

    def close(self):
        """释放宿主持有的租用；若游戏仍存活，其副本继续维持独占。"""
        if self.handle is not None:
            close(self.handle)
            self.handle = None


def acquire_runtime_directory(root, revision, sessions_root):
    """按内容版本租用空闲目录；已有槽位被占用时创建下一个，禁止路径重定向。"""
    if not re.fullmatch(r'[0-9a-f]{64}', revision):
        raise ValueError('运行程序版本指纹无效')
    root = Path(root).absolute()
    root.mkdir(parents=True, exist_ok=True)
    if root.resolve() != root:
        raise ValueError('运行目录根不可重定向')
    revision_root = root / revision
    revision_root.mkdir(exist_ok=True)
    if revision_root.resolve() != revision_root:
        raise ValueError('运行版本目录不可重定向')
    sessions_root = Path(sessions_root).resolve()
    slot_index = 1
    while True:
        slot_name = f'slot-{slot_index:04d}'
        lock_path = revision_root / (slot_name + '.lock')
        if lock_path.is_symlink() or lock_path.is_junction():
            raise ValueError('运行目录锁不可重定向')
        handle = api(K, 'CreateFileW', PTR, wintypes.LPCWSTR, wintypes.DWORD,
                     wintypes.DWORD, PTR, wintypes.DWORD, wintypes.DWORD, PTR)(
                         str(lock_path), 0x80000000, 0, None, 4, 0x80, None)
        if handle == ctypes.c_void_p(-1).value:
            error_code = ctypes.get_last_error()
            if error_code == 32:
                slot_index += 1
                continue
            raise ctypes.WinError(error_code)
        try:
            slot = revision_root / slot_name
            slot.mkdir(exist_ok=True)
            if slot.resolve() != slot:
                raise ValueError('运行槽位不可重定向')
            return RuntimeDirectoryLease(slot, handle, sessions_root)
        except Exception:
            close(handle)
            raise
