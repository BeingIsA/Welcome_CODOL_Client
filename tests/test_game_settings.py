"""验证原生设置跨启动恢复、稳定保存、事务冲突与异常输入保护。"""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from codol_rebuild.adapters.persistence.game_settings import FILES, SettingsStore, SettingsSession, SettingsConflict


class GameSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = SettingsStore(self.root/'account/settings.sqlite3')

    def session(self, name):
        session = SettingsSession(self.store, self.root/name)
        session.restore()
        return session

    def test_native_binary_roundtrip_and_private_files(self):
        first = self.session('first')
        expected = {name: b'\x00\xffnative-' + name.encode() for name in FILES}
        for name, data in expected.items():
            (first.directory/name).write_bytes(data)
        self.assertEqual(first.checkpoint(), [])
        self.assertEqual(set(first.checkpoint()), set(FILES))
        second = self.session('second')
        self.assertEqual({name: (second.directory/name).read_bytes() for name in FILES}, expected)
        (first.directory/FILES[0]).write_bytes(b'new')
        self.assertEqual((second.directory/FILES[0]).read_bytes(), expected[FILES[0]])
        self.assertEqual(first.checkpoint(final=True), [FILES[0]])
        self.assertEqual(self.store.load()[FILES[0]], b'new')
        with closing(sqlite3.connect(self.store.path)) as db, db:
            self.assertEqual(db.execute('SELECT data FROM history').fetchone()[0], expected[FILES[0]])

    def test_in_progress_and_empty_file_do_not_replace_saved_data(self):
        self.store.save({FILES[0]: b'previous'}, {})
        session = self.session('run')
        target = session.directory/FILES[0]
        target.write_bytes(b'partial')
        self.assertEqual(session.checkpoint(), [])
        target.write_bytes(b'complete')
        self.assertEqual(session.checkpoint(), [])
        self.assertEqual(self.store.load()[FILES[0]], b'previous')
        self.assertEqual(session.checkpoint(), [FILES[0]])
        target.write_bytes(b'')
        with self.assertRaises(ValueError):
            session.checkpoint(final=True)
        self.assertEqual(self.store.load()[FILES[0]], b'complete')

    def test_conflict_is_atomic_and_old_session_does_not_overwrite(self):
        self.store.save({FILES[0]: b'initial'}, {})
        old, new = self.session('old'), self.session('new')
        (new.directory/FILES[0]).write_bytes(b'newer')
        new.checkpoint(final=True)
        (old.directory/FILES[0]).write_bytes(b'stale')
        (old.directory/FILES[1]).write_bytes(b'other')
        with self.assertRaises(SettingsConflict):
            old.checkpoint(final=True)
        self.assertEqual(self.store.load(), {FILES[0]: b'newer'})
        self.assertEqual(old.checkpoint(final=True), [])

    def test_missing_files_do_not_delete_saved_settings(self):
        self.store.save({FILES[0]: b'initial'}, {})
        session = self.session('run')
        (session.directory/FILES[0]).unlink()
        self.assertEqual(session.checkpoint(final=True), [])
        self.assertEqual(self.store.load(), {FILES[0]: b'initial'})

    def test_corrupt_database_and_unknown_filename_rejected(self):
        with self.assertRaises(ValueError):
            self.store.save({'../escape': b'x'}, {})
        self.store.save({FILES[0]: b'initial'}, {})
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute('UPDATE settings SET data=?', (b'tampered',))
        with self.assertRaises(ValueError):
            self.session('run')
