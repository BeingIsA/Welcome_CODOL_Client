"""保护共享资源的文件权限，避免低完整性客户端或会话继承规则写入缓存。"""
import subprocess
import ctypes as C
from ctypes import wintypes as W
from pathlib import Path

from codol_rebuild.platform.win32 import current_sid, api, A, K, PTR, check


def protect_cache(path):
    """使用受保护 DACL 和显式中完整性标签，随后才允许建立硬链接。"""
    for options in [('/grant:r', '*' + current_sid() + ':(OI)(CI)F', '/T'),
                    ('/setintegritylevel', '(OI)(CI)M', '/T'),
                    ('/inheritance:d', '/T')]:
        result = subprocess.run(['icacls', str(path), *options], capture_output=True,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise RuntimeError('共享资源保护失败: ' +
                               (result.stdout + result.stderr).decode('mbcs', errors='replace'))

    # icacls /T 对目录标签主要通过继承传播；硬链接文件须有显式标签。
    descriptor = PTR()
    check(api(A, 'ConvertStringSecurityDescriptorToSecurityDescriptorW', W.BOOL,
              W.LPCWSTR, W.DWORD, C.POINTER(PTR), PTR)(
                  'S:(ML;;NW;;;ME)', 1, C.byref(descriptor), None))
    try:
        present, defaulted, sacl = W.BOOL(), W.BOOL(), PTR()
        check(api(A, 'GetSecurityDescriptorSacl', W.BOOL, PTR, C.POINTER(W.BOOL),
                  C.POINTER(PTR), C.POINTER(W.BOOL))(
                      descriptor, C.byref(present), C.byref(sacl), C.byref(defaulted)))
        setter = api(A, 'SetNamedSecurityInfoW', W.DWORD, W.LPWSTR, W.DWORD,
                     W.DWORD, PTR, PTR, PTR, PTR)
        for file in Path(path).rglob('*'):
            if file.is_file():
                error = setter(str(file), 1, 0x10, None, None, None, sacl)
                if error:
                    raise C.WinError(error)
    finally:
        api(K, 'LocalFree', PTR, PTR)(descriptor)
