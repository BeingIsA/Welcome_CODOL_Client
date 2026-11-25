"""使用本机 Cloud Files 将给定文件集合呈现为按需完整填充的只读数据视图。"""
from collections import defaultdict
import ctypes
import json
import os
from pathlib import Path
import shutil
import stat
from tempfile import NamedTemporaryFile, TemporaryDirectory


ReadFileBytes = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32, ctypes.c_int64,
                                  ctypes.c_uint32, ctypes.c_void_p)
RecordTransfer = ctypes.WINFUNCTYPE(None, ctypes.c_char_p, ctypes.c_uint32, ctypes.c_int64,
                                   ctypes.c_int64, ctypes.c_int64, ctypes.c_int32, ctypes.c_int)


class ViewHandlers(ctypes.Structure):
    _fields_ = [('read', ReadFileBytes), ('complete', RecordTransfer)]


class Placeholder(ctypes.Structure):
    _fields_ = [('name', ctypes.c_wchar_p), ('identity', ctypes.c_char_p), ('size', ctypes.c_int64)]


class SyncRootRegistration(ctypes.Structure):
    """Windows SDK 的 CF_SYNC_ROOT_STANDARD_INFO，当前提供者使用空 SyncRootIdentity。"""
    _fields_ = [('root_file_id', ctypes.c_uint64), ('hydration_policy', ctypes.c_uint32),
                ('population_policy', ctypes.c_uint32), ('in_sync_policy', ctypes.c_uint32),
                ('hard_link_policy', ctypes.c_uint32), ('provider_status', ctypes.c_uint32),
                ('provider_name', ctypes.c_wchar * 256), ('provider_version', ctypes.c_wchar * 256),
                ('identity_length', ctypes.c_uint32), ('identity', ctypes.c_ubyte * 1)]


def check_status(operation, status):
    if status < 0:
        raise OSError(f'{operation} 失败，HRESULT 0x{status & 0xffffffff:08X}')


class CloudFilesView:
    """持有一个独占槽位中的同步根；释放前保持回调、原生库及上下文有效。"""

    def __init__(self, root, library_path, sizes, read_bytes, record_transfer, *, index_sha256):
        self.root = Path(root).absolute()
        self.marker = self.root / '.codol-packed-view-owner.json'
        self.sizes = sizes
        self.index_sha256 = index_sha256
        self.reused = False
        self.building = False
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
        self.system.CfGetSyncRootInfoByPath.argtypes = [
            ctypes.c_wchar_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
        self.handlers = ViewHandlers(ReadFileBytes(read_bytes), RecordTransfer(record_transfer))

    @staticmethod
    def _identity(directory):
        if directory.resolve() != directory or directory.is_symlink() or directory.is_junction():
            raise ValueError('资源视图位置不可重定向：' + str(directory))
        attributes = directory.stat()
        return {'provider': 'CODOL packed resources', 'volume': attributes.st_dev, 'directory': attributes.st_ino}

    def _has_our_registration(self, identity):
        """按路径查询可能命中外层同步根，必须同时核对根文件 ID 和提供者。"""
        registration = SyncRootRegistration()
        status = self.system.CfGetSyncRootInfoByPath(
            str(self.root), 1, ctypes.byref(registration), ctypes.sizeof(registration), None)
        if (status & 0xffffffff) in (0x80070178, 0x80070186):  # 非云文件或不在同步根下
            return False
        check_status('读取按需资源注册', status)
        if (registration.root_file_id != identity['directory']
                or registration.provider_name != identity['provider'] or registration.provider_version != '1'):
            raise ValueError('按需资源注册与拥有者不符：' + str(self.root))
        return True

    @staticmethod
    def _read_owner(marker):
        if (marker.resolve() != marker or not marker.is_file()
                or marker.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError('资源视图缺少独立的所有权记录：' + str(marker))
        return json.loads(marker.read_bytes())

    def _publish_owner(self, record):
        """先写完临时文件，再在同一槽位原子替换；中断时旧记录仍表明树未完成。"""
        temporary_file = NamedTemporaryFile(dir=self.root.parent, prefix=self.marker.name + '.',
                                           mode='w', encoding='utf-8', delete=False)
        temporary_path = Path(temporary_file.name)
        try:
            with temporary_file as stream:
                json.dump(record, stream)
                stream.flush()
                os.fsync(stream.fileno())
            temporary_path.replace(self.marker)
        finally:
            temporary_path.unlink(missing_ok=True)

    def _create_owned_root(self):
        """所有权与新目录一起发布，中途退出不会在正式位置留下无主目录。"""
        with TemporaryDirectory(dir=self.root.parent, prefix=self.root.name + '.new-') as temporary:
            new_root = Path(temporary) / self.root.name
            new_root.mkdir()
            identity = self._identity(new_root)
            with (new_root / self.marker.name).open('x', encoding='utf-8') as owner:
                json.dump(identity, owner)
                owner.flush()
                os.fsync(owner.fileno())
            new_root.rename(self.root)
        return identity

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
                    if path == self.marker:
                        continue  # 清理前已单独校验，此记录始终保留在根中。
                    relative = path.relative_to(self.root).as_posix()
                    attributes = entry.stat(follow_symlinks=False)
                    tag = attributes.st_reparse_tag
                    if tag and (tag & 0xffff0fff) != 0x9000001a:
                        raise ValueError('遗留视图含外部重解析项：' + relative)
                    if stat.S_ISDIR(attributes.st_mode) and relative in valid_directories:
                        pending.append(path)
                    elif not stat.S_ISREG(attributes.st_mode) or relative not in self.sizes:
                        raise ValueError('遗留视图含非索引文件：' + relative)

    def _clear_incomplete_root(self):
        identity = self._identity(self.root)
        if self._read_owner(self.marker) != identity:
            raise ValueError('拒绝清理所有权不符的资源视图：' + str(self.root))
        self._check_existing_tree()
        if self._has_our_registration(identity):
            check_status('撤销按需资源注册', self.adapter.UnregisterGameRoot(str(self.root)))
        for entry in self.root.iterdir():
            if entry != self.marker:
                if entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
        self.building = False

    def _connect(self):
        check_status('连接按需资源目录', self.adapter.ConnectGameView(
            str(self.root), ctypes.byref(self.handlers), ctypes.byref(self.connection)))
        self.connected = True

    def open(self):
        if self.root.parent.resolve() != self.root.parent:
            raise ValueError('资源视图父目录不可重定向')
        legacy_marker = self.root.with_name(self.root.name + '.owner.json')
        if os.path.lexists(self.root):
            identity = self._identity(self.root)
            completed_owner = {**identity, 'index_sha256': self.index_sha256}
            if os.path.lexists(legacy_marker):
                legacy_owner = self._read_owner(legacy_marker)
                if os.path.lexists(self.marker) or legacy_owner not in (identity, completed_owner):
                    raise ValueError('旧版资源视图所有权与目录不符：' + str(self.root))
                legacy_marker.rename(self.marker)
            owner = self._read_owner(self.marker)
            if owner == completed_owner:
                if self._has_our_registration(identity):
                    self._connect()
                    self.reused = True
                    return
                # 注册被外部撤销后，占位树不再可用。先降级为未完成再走受控清理。
                self._publish_owner(identity)
            self._clear_incomplete_root()
        else:
            if os.path.lexists(legacy_marker):
                raise ValueError('旧版资源视图所有权记录缺少对应目录：' + str(legacy_marker))
            identity = self._create_owned_root()
        self.reused = False
        check_status('注册按需资源目录', self.adapter.RegisterGameRoot(str(self.root)))
        self.building = True
        self._connect()
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
        self._publish_owner({**identity, 'index_sha256': self.index_sha256})
        self.building = False

    def close(self):
        if self.connected:
            check_status('断开按需资源目录', self.system.CfDisconnectSyncRoot(self.connection))
            self.connected = False
        if self.building:
            self._clear_incomplete_root()
