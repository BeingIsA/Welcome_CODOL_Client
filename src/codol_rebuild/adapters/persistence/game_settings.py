"""原生操作、画面和声音设置的跨会话存储；保留客户端二进制格式。"""
from contextlib import contextmanager
import hashlib
from pathlib import Path
import sqlite3


FILES = ('config_mp.cfg', 'settings_c.zip.iw4', 'settings_m.zip.iw4')
MAX_BYTES = 4 * 1024 * 1024


def read_settings(directory):
    """只采集已知普通文件，拒绝空文件、越界链接及读取期间的变化。"""
    directory = Path(directory)
    root = directory.resolve()
    result = {}
    for name in FILES:
        path = directory / name
        if not path.exists():
            continue
        if path.is_symlink() or path.resolve().parent != root:
            raise ValueError('设置文件不可重定向: ' + name)
        before = path.stat()
        if not path.is_file() or not 0 < before.st_size <= MAX_BYTES:
            raise ValueError('设置文件大小无效: ' + name)
        with path.open('rb') as stream:
            data = stream.read(MAX_BYTES + 1)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or len(data) != before.st_size:
            raise ValueError('设置正在写入: ' + name)
        result[name] = data
    return result


class SettingsConflict(ValueError):
    """同一设置文件被另一会话更新，禁止旧会话覆盖。"""


class SettingsStore:
    """SQLite 事务保存设置和最近二十份文件备份，避免中断导致存档半写。"""
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, data BLOB NOT NULL, sha256 TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY, name TEXT NOT NULL, data BLOB NOT NULL, sha256 TEXT NOT NULL)')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def load(self):
        with self.connect() as db:
            rows = db.execute('SELECT name, data, sha256 FROM settings').fetchall()
        result = {}
        for name, data, digest in rows:
            if name not in FILES or not 0 < len(data) <= MAX_BYTES or hashlib.sha256(data).hexdigest() != digest:
                raise ValueError('已保存设置校验失败: ' + name)
            result[name] = data
        return result

    def save(self, changes, expected):
        for name, data in changes.items():
            if name not in FILES or not isinstance(data, bytes) or not 0 < len(data) <= MAX_BYTES:
                raise ValueError('无效设置记录')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for name, data in changes.items():
                old = db.execute('SELECT data, sha256 FROM settings WHERE name=?', (name,)).fetchone()
                previous = old[0] if old else None
                if previous != expected.get(name) and previous != data:
                    raise SettingsConflict('另一会话已更新设置，保留其存档及本次运行文件: ' + name)
                if previous == data:
                    continue
                if old:
                    db.execute('INSERT INTO history(name,data,sha256) VALUES(?,?,?)', (name, *old))
                db.execute('INSERT OR REPLACE INTO settings VALUES(?,?,?)',
                           (name, data, hashlib.sha256(data).hexdigest()))
            db.execute('DELETE FROM history WHERE id NOT IN (SELECT id FROM history ORDER BY id DESC LIMIT 20)')


class SettingsSession:
    """启动恢复、连续两次稳定采样后保存；退出时采集客户端关闭后的最终文件。"""
    def __init__(self, store, directory):
        self.store = store
        self.directory = Path(directory)
        self.baseline = store.load()
        self.observed = {}
        self.conflicted = False

    def restore(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        for name, data in self.baseline.items():
            # 只写入本次新建的运行目录，不与其他会话或数据库建立硬链接。
            with (self.directory / name).open('xb') as stream:
                stream.write(data)
        return sorted(self.baseline)

    def checkpoint(self, final=False):
        if self.conflicted:
            return []
        current = read_settings(self.directory)
        changes = {name: data for name, data in current.items()
                   if data != self.baseline.get(name) and (final or self.observed.get(name) == data)}
        self.observed = current
        if changes:
            try:
                self.store.save(changes, self.baseline)
            except SettingsConflict:
                self.conflicted = True
                raise
            self.baseline.update(changes)
        return sorted(changes)
