"""终版原包按需供件。长期复用 x86 IFS2 读取进程，为普通文件视图提供验过摘要的正文。"""
import ctypes
from hashlib import sha256
import json
from pathlib import Path
import struct
import subprocess
import threading
import time

from codol_rebuild.resources.offline_resources import MAX_RESOURCE, RESOURCE_KEY, decode_loose


PACKED_READER_FILES = ('ifs_reader.exe', 'cloud_files.dll')
IFS_LIBRARY_SHA256 = '5b0324e0b2ba3545a6c02626287d4fbc6a3116594744f4647333982d85508421'


class ArchiveReader:
    """在 x86 子进程中打开固定版本的 IFS2.dll，每次请求返回一个原包成员。"""

    def __init__(self, packed_root, executable):
        library = packed_root / 'IIPS/IFS2.dll'
        if sha256(library.read_bytes()).hexdigest() != IFS_LIBRARY_SHA256:
            raise ValueError('IFS2.dll 与终版读取器不匹配')
        self.lock = threading.Lock()
        self.process = subprocess.Popen(
            [executable, library], cwd=packed_root / 'IIPS/IIPSDownload',
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW,
        )

    def read(self, archive_name, game_path):
        archive = archive_name.encode('mbcs', 'strict')
        member = game_path.replace('/', '\\').encode('mbcs', 'strict')
        with self.lock:
            self.process.stdin.write(struct.pack('<II', len(archive), len(member)) + archive + member)
            self.process.stdin.flush()
            content = bytearray()
            while True:
                header = self.process.stdout.read(4)
                if len(header) != 4:
                    raise OSError('原包读取进程意外退出')
                length, = struct.unpack('<I', header)
                if not length:
                    return bytes(content)
                if length == 0xffffffff:
                    raise FileNotFoundError(f'{archive_name} 中无法打开 {game_path}')
                if length > 65536 or len(content) + length > MAX_RESOURCE:
                    self.process.kill()
                    raise ValueError('原包读取器返回了越界的成员尺寸')
                chunk = self.process.stdout.read(length)
                if len(chunk) != length:
                    raise OSError('原包读取进程返回了不完整正文')
                content.extend(chunk)

    def close(self):
        try:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        finally:
            self.process.stdout.close()


class PackedView:
    """一个宿主会话拥有的原包读取进程、文件视图和交付审计。"""

    def __init__(self, packed_root, members, root):
        self.packed_root = Path(packed_root).resolve()
        self.root = Path(root).absolute()
        self.members = {path: member for path, member in members.items()
                        if path.startswith(('main/', 'hires/', 'client_shipretail/'))}
        self.reader = None
        self.view = None
        self.audit = None
        self.audit_lock = threading.Lock()
        self.successful_fetches = 0
        self.delivered_bytes = 0
        self.failed_fetches = 0

    @property
    def reused(self):
        """本次会话是否连接到了已完成的资源视图。"""
        return self.view.reused

    def _read_bytes(self, identity, process_id, offset, length, destination):
        try:
            path = identity.decode('utf-8')
            member = self.members[path]
            if offset != 0 or length != member['size']:
                raise ValueError('请求范围与完整供件大小不符')
            if 'archive' in member:
                content = self.reader.read(member['archive'], path)
                if not path.endswith('.ff'):
                    content = decode_loose(content, member['decode_filename'], RESOURCE_KEY)
            else:
                content = (self.packed_root / path).read_bytes()
            if len(content) != length or sha256(content).hexdigest() != member['sha256']:
                raise ValueError('资源正文与终版索引不符')
            ctypes.memmove(destination, content, length)
            return 1
        except Exception as error:
            with self.audit_lock:
                self.audit.write(json.dumps({'time': time.time(), 'pid': process_id,
                                             'path': identity.decode('utf-8', 'replace'), 'error': str(error)}) + '\n')
            return 0

    def _complete(self, identity, process_id, offset, length, size, status, success):
        path = identity.decode('utf-8', 'replace')
        member = self.members.get(path, {})
        event = {'time': time.time(), 'pid': process_id, 'path': path, 'source': member.get('archive', path),
                 'offset': offset, 'length': length, 'file_sha256': member.get('sha256'),
                 'cf_execute': f'0x{status & 0xffffffff:08x}', 'supplied': size if success else 0}
        with self.audit_lock:
            if success:
                self.successful_fetches += 1
                self.delivered_bytes += size
            else:
                self.failed_fetches += 1
            self.audit.write(json.dumps(event) + '\n')

    def close(self):
        if self.view is not None:
            try:
                self.view.close()
            finally:
                if not self.view.connected:
                    self._close_reader()
        else:
            self._close_reader()

    def _close_reader(self):
        try:
            if self.reader is not None:
                self.reader.close()
                self.reader = None
        finally:
            if self.audit is not None:
                self.audit.close()
                self.audit = None


def start_packed_view(packed_root, resource_entries, view_root, binaries_directory, audit_path, *, index_sha256, view_factory):
    """在已租用的槽中打开该索引的按需视图；每次连接独立记录实际供件。"""
    resource_view = PackedView(packed_root, resource_entries, view_root)
    try:
        resource_view.audit = Path(audit_path).open('x', encoding='utf-8', buffering=1)
        resource_view.reader = ArchiveReader(resource_view.packed_root, binaries_directory / 'ifs_reader.exe')
        sizes = {path: member['size'] for path, member in resource_view.members.items()}
        resource_view.view = view_factory(resource_view.root, binaries_directory / 'cloud_files.dll',
                                          sizes, resource_view._read_bytes, resource_view._complete,
                                          index_sha256=index_sha256)
        resource_view.view.open()
        return resource_view
    except BaseException:
        resource_view.close()
        raise
