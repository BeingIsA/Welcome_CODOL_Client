"""使用本机 Cloud Files 将给定文件集合呈现为按需完整填充的只读数据视图。"""
from collections import defaultdict
import ctypes
import json
import os
from pathlib import Path
import shutil
import stat


ReadFileBytes = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32, ctypes.c_int64,
                                  ctypes.c_uint32, ctypes.c_void_p)
RecordTransfer = ctypes.WINFUNCTYPE(None, ctypes.c_char_p, ctypes.c_uint32, ctypes.c_int64,
                                   ctypes.c_int64, ctypes.c_int64, ctypes.c_int32, ctypes.c_int)


class ViewHandlers(ctypes.Structure):
    _fields_ = [('read', ReadFileBytes), ('complete', RecordTransfer)]


class Placeholder(ctypes.Structure):
    _fields_ = [('name', ctypes.c_wchar_p), ('identity', ctypes.c_char_p), ('size', ctypes.c_int64)]


def check_status(operation, status):
    if status < 0:
        raise OSError(f'{operation} 失败，HRESULT 0x{status & 0xffffffff:08X}')


class CloudFilesView:
    """持有一个独占槽位中的同步根；释放前保持回调、原生库及上下文有效。"""

    def __init__(self, root, library_path, sizes, read_bytes, record_transfer):
        self.root = Path(root).absolute()
        self.marker = self.root.with_name(self.root.name + '.owner.json')
        self.sizes = sizes
        self.owned = False
        self.registered = False
        self.connected = False
        self.connection = ctypes.c_int64()
        self.adapter = ctypes.WinDLL(str(library_path))
        self.system = ctypes.WinDLL('CldApi.dll', winmode=0x800)
        self.adapter.RegisterGameRoot.argtypes = [ctypes.c_wchar_p]
        self.adapter.UnregisterGameRoot.argtypes = [ctypes.c_wchar_p]
        self.adapter.ConnectGameView.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ViewHandlers),
                                                 ctypes.POINTER(ctypes.c_int64)]
        self.adapter.AddGameMembers.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(Placeholder), ctypes.c_uint32]
        self.system.CfDisconnectSyncRoot.argtypes = [ctypes.c_int64]
        self.handlers = ViewHandlers(ReadFileBytes(read_bytes), RecordTransfer(record_transfer))

    def _identity(self):
        if self.root.resolve() != self.root or self.root.is_symlink() or self.root.is_junction():
            raise ValueError('资源视图位置不可重定向：' + str(self.root))
        attributes = self.root.stat()
        return {'provider': 'CODOL packed resources', 'volume': attributes.st_dev, 'directory': attributes.st_ino}

    def _check_existing_tree(self):
        """异常遗留目录须只包含此索引的子集；递归前排除外部联接。"""
        valid_directories = {relative.parent.as_posix() for relative in map(Path, self.sizes)}
        valid_directories.update(ancestor.as_posix() for path in tuple(valid_directories)
                                 for ancestor in Path(path).parents)
        pending = [self.root]
        while pending:
            directory = pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    relative = path.relative_to(self.root).as_posix()
                    attributes = entry.stat(follow_symlinks=False)
                    tag = attributes.st_reparse_tag
                    if tag and (tag & 0xffff0fff) != 0x9000001a:
                        raise ValueError('遗留视图含外部重解析项：' + relative)
                    if stat.S_ISDIR(attributes.st_mode) and relative in valid_directories:
                        pending.append(path)
                    elif not stat.S_ISREG(attributes.st_mode) or relative not in self.sizes:
                        raise ValueError('遗留视图含非索引文件：' + relative)

    def _remove_owned_root(self, *, recover=False):
        if self.marker.resolve() != self.marker or json.loads(self.marker.read_bytes()) != self._identity():
            raise ValueError('拒绝清理所有权不符的资源视图：' + str(self.root))
        if recover:
            self._check_existing_tree()
        if recover or self.registered:
            check_status('撤销按需资源注册', self.adapter.UnregisterGameRoot(str(self.root)))
            self.registered = False
        shutil.rmtree(self.root)
        self.marker.unlink()
        self.owned = False

    def open(self):
        if self.root.parent.resolve() != self.root.parent:
            raise ValueError('资源视图父目录不可重定向')
        if os.path.lexists(self.root):
            self._remove_owned_root(recover=True)
        if os.path.lexists(self.marker):
            raise ValueError('资源视图所有权记录缺少对应目录：' + str(self.marker))
        self.root.mkdir()
        try:
            with self.marker.open('x', encoding='utf-8') as owner:
                json.dump(self._identity(), owner)
        except BaseException:
            self.root.rmdir()
            raise
        self.owned = True
        check_status('注册按需资源目录', self.adapter.RegisterGameRoot(str(self.root)))
        self.registered = True
        check_status('连接按需资源目录', self.adapter.ConnectGameView(
            str(self.root), ctypes.byref(self.handlers), ctypes.byref(self.connection)))
        self.connected = True
        groups = defaultdict(list)
        for relative, size in self.sizes.items():
            path = Path(relative)
            groups[path.parent].append(Placeholder(path.name, relative.encode('utf-8'), size))
        for relative_directory, members in groups.items():
            directory = self.root / relative_directory
            directory.mkdir(parents=True, exist_ok=True)
            for offset in range(0, len(members), 500):
                batch = members[offset:offset + 500]
                array = (Placeholder * len(batch))(*batch)
                check_status('创建按需资源文件', self.adapter.AddGameMembers(str(directory), array, len(batch)))

    def close(self):
        if self.connected:
            check_status('断开按需资源目录', self.system.CfDisconnectSyncRoot(self.connection))
            self.connected = False
        if self.owned:
            self._remove_owned_root()
