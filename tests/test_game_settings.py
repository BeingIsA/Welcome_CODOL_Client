"""验证原生设置迁移、旧库只读和后续原生文件优先。"""
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from codol_rebuild.adapters.persistence.game_settings import FILES, migrate_settings

class GameSettingsTests(unittest.TestCase):
    def test_migration_preserves_native_changes_and_database(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database = root / 'settings.sqlite3'
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute('CREATE TABLE settings(name TEXT, data BLOB, sha256 TEXT)')
                for name in FILES:
                    data = name.encode()
                    connection.execute('INSERT INTO settings VALUES(?,?,?)', (name, data, hashlib.sha256(data).hexdigest()))
            original_database = database.read_bytes()
            players = root / 'players'
            players.mkdir()
            (players / FILES[0]).write_bytes(b'native-existing')
            self.assertEqual(migrate_settings(database, players), sorted(FILES[1:]))
            self.assertEqual((players / FILES[0]).read_bytes(), b'native-existing')
            for name in FILES[1:]:
                self.assertEqual((players / name).read_bytes(), name.encode())
            (players / FILES[1]).write_bytes(b'native-updated')
            self.assertEqual(migrate_settings(database, players), [])
            self.assertEqual((players / FILES[1]).read_bytes(), b'native-updated')
            self.assertEqual(database.read_bytes(), original_database)

    def test_first_launch_without_database(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.assertEqual(migrate_settings(root / 'missing.sqlite3', root / 'players'), [])
            self.assertTrue((root / 'players').is_dir())
            self.assertFalse((root / 'missing.sqlite3').exists())
