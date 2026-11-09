"""离线解锁器回归：事务、备份、分页搜索与跨页选择。"""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch
from contextlib import closing
from codol_rebuild.adapters.persistence.sqlite_accounts import AccountRepository
from codol_rebuild.application.accounts.item_unlock import ItemUnlockService
from codol_rebuild.application.accounts.contracts import RevisionConflict


class ItemUnlockTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'local.sqlite3'
        self.repository = AccountRepository(self.path)
        self.repository.identity('local')
        self.initial = {'version': 1, 'owned_ids': [100], 'gp': 123, 'cp': 456, 'quantities': {'100': 8}, 'custom': 'preserve'}
        self.repository.save('local', 0, {'public': b'unchanged'}, {'public': {'version': 1}}, self.initial)
        self.guard = Mock()
        self.service = ItemUnlockService(self.repository, [100, 104, 105, 15400], self.guard)

    def tearDown(self):
        self.directory.cleanup()

    def test_batch_preserves_balance_quantities_profiles_and_backup(self):
        result = self.service.unlock('local', [100, 104, 104, 105])
        self.assertEqual((result['added'], result['skipped']), (2, 1))
        snapshot = self.repository.inventory_snapshot('local')
        self.assertEqual(snapshot['state'], {**self.initial, 'owned_ids': [100, 104, 105]})
        self.assertEqual(snapshot['revision'], 2)
        self.assertEqual(AccountRepository(result['backup']).inventory_snapshot('local')['state'], self.initial)
        with closing(sqlite3.connect(self.path)) as database:
            self.assertEqual(database.execute('SELECT blob FROM profiles').fetchone()[0], b'unchanged')

    def test_repeat_is_noop(self):
        result = self.service.unlock('local', [100])
        self.assertEqual(result['added'], 0)
        self.assertEqual(result['backup'], '')
        self.assertEqual(self.repository.inventory_snapshot('local')['revision'], 1)

    def test_reserved_and_unknown_rejected(self):
        for identifier in [15400, 20000, 99999]:
            with self.assertRaises(ValueError):
                self.service.unlock('local', [104, identifier])
        self.assertEqual(self.repository.inventory_snapshot('local')['state'], self.initial)

    def test_running_game_and_backup_failure_no_write(self):
        self.guard.side_effect = ValueError('game running')
        with self.assertRaises(ValueError):
            self.service.unlock('local', [104])
        self.guard.side_effect = None
        self.repository.backup_inventory_database = Mock(side_effect=OSError('backup failed'))
        with self.assertRaises(OSError):
            self.service.unlock('local', [104])
        self.assertEqual(self.repository.inventory_snapshot('local')['revision'], 1)

    def test_conflict_preserves_concurrent_state(self):
        original_backup = self.repository.backup_inventory_database
        def competing_backup():
            backup = original_backup()
            self.repository.save_inventory('local', 1, {**self.initial, 'cp': 789})
            return backup
        self.repository.backup_inventory_database = competing_backup
        with self.assertRaises(RevisionConflict):
            self.service.unlock('local', [104, 105])
        self.assertEqual(self.repository.inventory_snapshot('local')['state'], {**self.initial, 'cp': 789})

    def test_database_failure_rolls_back_revision(self):
        with closing(sqlite3.connect(self.path)) as database, database:
            database.execute("CREATE TRIGGER fail_inventory BEFORE UPDATE ON account_initialization BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        with self.assertRaises(sqlite3.Error):
            self.service.unlock('local', [104])
        self.assertEqual(self.repository.inventory_snapshot('local')['revision'], 1)
        self.assertEqual(self.repository.inventory_snapshot('local')['state'], self.initial)

    def test_missing_database_and_legacy_state_are_not_created(self):
        absent = Path(self.directory.name) / 'absent.sqlite3'
        with self.assertRaises(ValueError):
            AccountRepository(absent).inventory_accounts()
        self.assertFalse(absent.exists())
        self.repository.identity('legacy')
        with self.assertRaises(ValueError):
            self.service.unlock('legacy', [104])


class ItemUnlockGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtWidgets import QApplication
        cls.application = QApplication.instance() or QApplication([])

    def test_search_filter_pagination_selection_and_clear(self):
        from codol_rebuild.tools.item_unlocker.window import UnlockWindow
        catalog = json.loads((Path(__file__).resolve().parents[1] / 'resources/item_catalog.json').read_text(encoding='utf8'))
        repository = Mock()
        repository.inventory_accounts.return_value = ['local']
        repository.inventory_snapshot.return_value = {'revision': 1, 'state': {'version': 1, 'owned_ids': [], 'gp': 0, 'cp': 0}}
        window = UnlockWindow(catalog, repository, Mock(), 'test.sqlite3')
        self.assertEqual(window.catalog_table.rowCount(), 100)
        window.add_row(0, 0)
        window.add_row(0, 2)
        self.assertEqual(len(window.pending_ids), 1)
        window.page_number.setValue(2)
        window.add_row(0, 0)
        self.assertEqual(len(window.pending_ids), 2)
        window.search.setText('沙漠网巾')
        window.apply_filters()
        self.assertTrue(window.filtered_rows)
        self.assertTrue(all('沙漠网巾' in row[2] for row in window.filtered_rows))
        self.assertEqual(len(window.pending_ids), 2)
        window.search.clear()
        window.type_filter.setCurrentIndex(window.type_filter.findData('wea'))
        self.assertTrue(all(row[4] == 'wea' for row in window.filtered_rows))
        window.category_filter.setCurrentIndex(1)
        self.assertTrue(all(row[5] == window.category_filter.currentData() for row in window.filtered_rows))
        window.search.setText('不存在的名称987654321')
        window.apply_filters()
        self.assertEqual(window.catalog_table.rowCount(), 0)
        window.clear_pending()
        self.assertFalse(window.unlock_button.isEnabled())
        window.close()


class ItemUnlockProcessTests(unittest.TestCase):
    def test_editor_itself_is_not_treated_as_game(self):
        from codol_rebuild.tools.item_unlocker.environment import ensure_game_stopped
        with patch('codol_rebuild.tools.item_unlocker.environment.subprocess.run', return_value=Mock(stdout='"CODOL_ItemUnlocker.exe","1"\n')):
            ensure_game_stopped()
        with patch('codol_rebuild.tools.item_unlocker.environment.subprocess.run', return_value=Mock(stdout='"codol1649370-offline.exe","2"\n')):
            with self.assertRaises(ValueError):
                ensure_game_stopped()


if __name__ == '__main__':
    unittest.main()
