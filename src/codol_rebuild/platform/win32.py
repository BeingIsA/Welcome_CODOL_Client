"""Windows 原生接口：系统调用签名、调试结构、句柄与低权限令牌管理。

只由 Windows 运行宿主使用，不依赖业务层或固定客户端地址。
"""
import ctypes as C
from ctypes import wintypes as W
import struct

K, A = C.WinDLL("kernel32", use_last_error=True), C.WinDLL("advapi32", use_last_error=True)
PTR, SIZE = C.c_void_p, C.c_size_t

def api(lib, name, result, *args):
    """明确 Win32 指针宽度，避免 64 位宿主句柄被截断。"""
    fn = getattr(lib, name)
    fn.restype, fn.argtypes = result, args
    return fn


def check(value):
    """把失败的系统调用转成包含真实错误码的异常。"""
    if not value:
        raise C.WinError(C.get_last_error())
    return value


close = api(K, 'CloseHandle', W.BOOL, PTR)
wait = api(K, 'WaitForSingleObject', W.DWORD, PTR, W.DWORD)
vquery = api(K, 'VirtualQueryEx', SIZE, PTR, PTR, PTR, SIZE)


class SI(C.Structure):
    """Windows 启动信息；不启动额外命令窗口。"""
    _fields_ = [('cb', W.DWORD), ('reserved', W.LPWSTR), ('desktop', W.LPWSTR), ('title', W.LPWSTR)] + [(n, W.DWORD) for n in ('x','y','cx','cy','xchars','ychars','fill','flags')] + [('show', W.WORD), ('reservedsize', W.WORD), ('reservedbytes', PTR), ('stdin', PTR), ('stdout', PTR), ('stderr', PTR)]


class PI(C.Structure):
    """仅记录本次创建的进程、线程及其句柄。"""
    _fields_ = [('process', PTR), ('thread', PTR), ('pid', W.DWORD), ('tid', W.DWORD)]


class EVENT(C.Structure):
    """64 位 DEBUG_EVENT，其联合体可容纳 WOW64 异常记录。"""
    _fields_ = [('code', W.DWORD), ('pid', W.DWORD), ('tid', W.DWORD), ('padding', W.DWORD), ('data', C.c_ubyte * 160)]


class MBI(C.Structure):
    """64 位 MEMORY_BASIC_INFORMATION；只用于枚举目标已提交可读内存区。"""
    _fields_ = [('BaseAddress', PTR), ('AllocationBase', PTR), ('AllocationProtect', W.DWORD),
                ('align1', W.DWORD), ('RegionSize', SIZE), ('State', W.DWORD),
                ('Protect', W.DWORD), ('Type', W.DWORD), ('align2', W.DWORD)]


class SIDATTR(C.Structure):
    """用于设置低完整性标签。"""
    _fields_ = [('sid', PTR), ('attributes', W.DWORD)]


class SHELLINFO(C.Structure):
    """通过系统 UAC 启动短期守卫；显示与审批由用户的 Windows 处理。"""
    _fields_=[('size',W.DWORD),('mask',W.ULONG),('hwnd',PTR),('verb',W.LPCWSTR),('file',W.LPCWSTR),('parameters',W.LPCWSTR),('directory',W.LPCWSTR),('show',C.c_int),('instance',PTR),('idlist',PTR),('cls',W.LPCWSTR),('classkey',PTR),('hotkey',W.DWORD),('icon',PTR),('process',PTR)]


def low_token():
    """从普通用户令牌派生禁特权、低完整性令牌；不继承管理员权限。"""
    old, new, linked, sid = PTR(), PTR(), PTR(), PTR()
    check(api(A,'OpenProcessToken',W.BOOL,PTR,W.DWORD,C.POINTER(PTR))(api(K,'GetCurrentProcess',PTR)(),395,C.byref(old)))
    try:
        get = api(A,'GetTokenInformation',W.BOOL,PTR,C.c_int,PTR,W.DWORD,C.POINTER(W.DWORD))
        kind, needed = W.DWORD(), W.DWORD()
        check(get(old,18,C.byref(kind),4,C.byref(needed)))
        source = old
        if kind.value == 2:
            check(get(old,19,C.byref(linked),C.sizeof(linked),C.byref(needed)))
            source = linked
        check(api(A,'CreateRestrictedToken',W.BOOL,PTR,W.DWORD,W.DWORD,PTR,W.DWORD,PTR,W.DWORD,PTR,C.POINTER(PTR))(source,1,0,None,0,None,0,None,C.byref(new)))
        check(api(A,'ConvertStringSidToSidW',W.BOOL,W.LPCWSTR,C.POINTER(PTR))('S-1-16-4096',C.byref(sid)))
        label = SIDATTR(sid,32)
        check(api(A,'SetTokenInformation',W.BOOL,PTR,C.c_int,PTR,W.DWORD)(new,25,C.byref(label),C.sizeof(label)+api(A,'GetLengthSid',W.DWORD,PTR)(sid)))
        return new
    except Exception:
        if new: close(new)
        raise
    finally:
        close(old)
        if linked: close(linked)
        if sid: api(K,'LocalFree',PTR,PTR)(sid)


def token_info(process):
    """实际复核目标令牌，禁止把创建选项当成隔离成功的证据。"""
    token, needed = PTR(), W.DWORD()
    check(api(A,'OpenProcessToken',W.BOOL,PTR,W.DWORD,C.POINTER(PTR))(process,8,C.byref(token)))
    get = api(A,'GetTokenInformation',W.BOOL,PTR,C.c_int,PTR,W.DWORD,C.POINTER(W.DWORD))
    result = {}
    try:
        for name,kind in [('elevated',20),('appcontainer',29),('integrity',25)]:
            get(token,kind,None,0,C.byref(needed))
            buf=C.create_string_buffer(needed.value)
            check(get(token,kind,buf,len(buf),C.byref(needed)))
            if kind == 25:
                sid=C.cast(buf,C.POINTER(PTR))[0]
                n=api(A,'GetSidSubAuthorityCount',C.POINTER(C.c_ubyte),PTR)(sid)[0]
                result[name]=api(A,'GetSidSubAuthority',C.POINTER(W.DWORD),PTR,W.DWORD)(sid,n-1)[0]
            else: result[name]=struct.unpack_from('<I',buf)[0]
    finally: close(token)
    return result


def current_sid():
    """只给当前用户授予新运行目录所需权限，不改变项目或系统目录。"""
    token,needed,text=PTR(),W.DWORD(),PTR()
    check(api(A,'OpenProcessToken',W.BOOL,PTR,W.DWORD,C.POINTER(PTR))(api(K,'GetCurrentProcess',PTR)(),8,C.byref(token)))
    try:
        get=api(A,'GetTokenInformation',W.BOOL,PTR,C.c_int,PTR,W.DWORD,C.POINTER(W.DWORD))
        get(token,1,None,0,C.byref(needed));buf=C.create_string_buffer(needed.value)
        check(get(token,1,buf,len(buf),C.byref(needed)))
        check(api(A,'ConvertSidToStringSidW',W.BOOL,PTR,C.POINTER(PTR))(C.cast(buf,C.POINTER(PTR))[0],C.byref(text)))
        return C.wstring_at(text)
    finally:
        close(token)
        if text:api(K,'LocalFree',PTR,PTR)(text)


