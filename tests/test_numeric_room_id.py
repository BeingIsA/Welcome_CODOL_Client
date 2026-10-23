"""数字房间号跨应用、传输与恢复文件的契约回归。"""
import tempfile
import unittest
from pathlib import Path
from codol_rebuild.application.room.contracts import IdentityRecord, valid_room_id
from codol_rebuild.adapters.transport.http import _identifier
from codol_rebuild.adapters.persistence.server_identity import JsonIdentityStore


class NumericRoomIdTests(unittest.TestCase):
    def test_boundaries_and_legacy_records(self):
        for value in ('1', '9223372036854775807', 'rm_legacy'):
            self.assertTrue(valid_room_id(value))
            self.assertEqual(_identifier(value, 'rm'), value)
        for value in ('0', '-1', '01', '9223372036854775808', '1.0', '１', '', 123, None):
            self.assertFalse(valid_room_id(value))
            with self.assertRaises(ValueError):
                _identifier(value, 'rm')

    def test_numeric_recovery_roundtrip(self):
        root = Path(__file__).resolve().parents[2]/'verification/client/workspaces/test-temp'
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as folder:
            store = JsonIdentityStore(Path(folder)/'identity.json')
            record = IdentityRecord('pl_player', '9223372036854775807', uncertain_join=True)
            store.save('http://localhost:8080', record)
            self.assertEqual(store.load('http://localhost:8080'), record)
