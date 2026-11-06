"""将旧设置数据库一次性迁移为原生文件；后续读写由原生客户端负责。"""
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3

FILES = ('config_mp.cfg', 'settings_c.zip.iw4', 'settings_m.zip.iw4')
MAX_BYTES = 4 * 1024 * 1024


def migrate_settings(database_path, players_directory):
    """只读旧库并迁移缺失文件；保留旧库，绝不覆盖原生已保存的设置。"""
    database_path = Path(database_path)
    players_directory = Path(players_directory)
    players_directory.mkdir(parents=True, exist_ok=True)
    if not database_path.exists() or all((players_directory / name).exists() for name in FILES):
        return []
    with closing(sqlite3.connect(database_path.resolve().as_uri() + '?mode=ro', uri=True)) as connection:
        setting_rows = connection.execute('SELECT name, data, sha256 FROM settings').fetchall()
    pending_files = {}
    for name, data, digest in setting_rows:
        if name not in FILES:
            raise ValueError('未知设置文件: ' + name)
        if (players_directory / name).exists():
            continue
        if not 0 < len(data) <= MAX_BYTES or hashlib.sha256(data).hexdigest() != digest:
            raise ValueError('已保存设置校验失败: ' + name)
        pending_files[name] = data
    for name, data in pending_files.items():
        with (players_directory / name).open('xb') as stream:
            stream.write(data)
    return sorted(pending_files)
