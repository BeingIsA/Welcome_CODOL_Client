"""档案保存表示边界回归；模拟进程不代替原生连接验收。"""
from pathlib import Path
import sys
import struct
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from codol_rebuild.adapters.game.v1649370.accounts.profile_bridge import LocalProfileBridge
from codol_rebuild.adapters.game.v1649370.accounts.profile import PUBLIC, SECURE
from codol_rebuild.adapters.game.v1649370.accounts.profile_codec import AccountCodec, IDENTITIES
from codol_rebuild.adapters.game.v1649370.layout import BUFFER_CAPACITY
from codol_rebuild.adapters.game.profile_format.fields import Fields
from codol_rebuild.adapters.persistence.sqlite_accounts import AccountRepository
from codol_rebuild.application.accounts.service import AccountSession
from test_profile_plaintext import public_schema, encode_profile


class ProfileSaveBoundaryTests(unittest.TestCase):
    """仅稳定明文副本可落盘，安全档案编码态仍拒绝整个事务。"""
    def setUp(self):
        self.process = Mock()
        self.process.periodic.return_value.due.return_value = True
        self.bridge = LocalProfileBridge(self.process, Mock(), {}, Mock())
        self.bridge._ready = True
        self.bridge._service = Mock()
        self.bridge._service.account = {'profiles': {}, 'revision': 1}
        self.bridge._service.inspect_snapshot.return_value = 570
        self.bridge._service.save.return_value = True
        self.bridge._inventory = {'pool': '0x20000'}
        self.bridge._schemas = {'public': public_schema()}
        self.flags = {PUBLIC: [0, 0], SECURE: [0, 0]}
        self.keys = {PUBLIC: [123, 123], SECURE: [456, 456]}
        self.bodies = {PUBLIC: [bytes(BUFFER_CAPACITY)] * 2,
                       SECURE: [bytes(BUFFER_CAPACITY)] * 2}
        self.process.read.side_effect = self.read_memory

    def read_memory(self, address, length):
        for base in (PUBLIC, SECURE):
            if address == base+0x11128:
                return struct.pack('<IB', self.keys[base].pop(0), self.flags[base].pop(0))
            if address == base+0x90:
                return self.bodies[base].pop(0)
        if length == 16:
            return b'LOCAL-INVENTORY!'
        return bytes(length)

    def save(self, force=False):
        with patch('codol_rebuild.adapters.game.v1649370.accounts.profile_bridge.view_state', return_value={}):
            self.bridge.save(force=force)

    def test_plaintext_pair_is_saved(self):
        self.save()
        self.bridge._service.save.assert_called_once()
        self.assertEqual(set(self.bridge._service.save.call_args.args[0]), {'public', 'secure'})

    def test_encoded_secure_profile_blocks_normal_and_forced_save(self):
        for force in (False, True):
            with self.subTest(force=force):
                self.setUp()
                self.flags[SECURE] = [1]
                self.save(force)
                self.bridge._service.save.assert_not_called()
                self.bridge._service.inspect_snapshot.assert_not_called()

    def test_encoding_transition_during_sampling_blocks_save(self):
        for base in (PUBLIC, SECURE):
            for flags in ([0, 1], [1, 0]):
                with self.subTest(base=base, flags=flags):
                    self.setUp()
                    self.flags[base] = list(flags)
                    self.save(True)
                    self.bridge._service.save.assert_not_called()
                    self.bridge._service.inspect_snapshot.assert_not_called()

    def test_key_change_blocks_save(self):
        for base in (PUBLIC, SECURE):
            with self.subTest(base=base):
                self.setUp()
                self.flags[PUBLIC] = [1, 1]
                self.keys[base] = [123, 124]
                self.save(True)
                self.bridge._service.save.assert_not_called()

    def test_body_change_blocks_save(self):
        for base in (PUBLIC, SECURE):
            with self.subTest(base=base):
                self.setUp()
                self.bodies[base][1] = b'changed'
                self.save(True)
                self.bridge._service.save.assert_not_called()

    def test_save_resumes_after_unstable_sample(self):
        self.keys[PUBLIC] = [123, 124]
        self.save(True)
        self.flags = {PUBLIC: [0, 0], SECURE: [0, 0]}
        self.keys = {PUBLIC: [124, 124], SECURE: [456, 456]}
        self.bodies = {PUBLIC: [bytes(BUFFER_CAPACITY)] * 2,
                       SECURE: [bytes(BUFFER_CAPACITY)] * 2}
        self.save(True)
        self.bridge._service.save.assert_called_once()

    def test_encoded_loadout_change_is_persisted_as_plaintext_and_reopened(self):
        schema = self.bridge._schemas['public']
        fields = Fields(schema)
        plaintext = bytearray(BUFFER_CAPACITY)
        struct.pack_into('<II', plaintext, 0, IDENTITIES['public']['version'], IDENTITIES['public']['checksum'])
        fields.write(plaintext, 'characterClasses.0.slots.0', 570)
        secure = bytearray(BUFFER_CAPACITY)
        struct.pack_into('<II', secure, 0, IDENTITIES['secure']['version'], IDENTITIES['secure']['checksum'])
        profiles = {'public': bytes(plaintext), 'secure': bytes(secure)}
        temporary_root = Path(__file__).resolve().parents[2] / 'verification/client/workspaces/test-temp'
        temporary_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temporary_root) as directory:
            database_path = Path(directory) / 'accounts.sqlite3'
            repository = AccountRepository(database_path)
            repository.identity()
            repository.save('local', 0, profiles, IDENTITIES)
            service = AccountSession(repository, AccountCodec({'public': schema}, {}, []))
            service.account = repository.load('local', IDENTITIES)
            self.bridge._service = service
            fields.write(plaintext, 'weaponClasses.59.primaryBase', 3456)
            fields.write(plaintext, 'weaponClasses.59.name', 'After game')
            fields.write(plaintext, 'weaponClasses.59.lethalDoubleAmmo', True)
            encoded = encode_profile(plaintext, schema, 123)
            self.flags[PUBLIC] = [1, 1]
            self.bodies = {PUBLIC: [encoded, encoded], SECURE: [bytes(secure)] * 2}
            self.save()
            restored = AccountRepository(database_path).load('local', IDENTITIES)
            self.assertEqual(restored['profiles'], {'public': bytes(plaintext), 'secure': bytes(secure)})
            self.assertEqual(restored['revision'], 2)
            self.process.write.assert_not_called()


if __name__ == '__main__':
    unittest.main()
