"""档案保存表示边界回归；模拟进程不代替原生连接验收。"""
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from codol_rebuild.adapters.game.v1649370.accounts.profile_bridge import LocalProfileBridge
from codol_rebuild.adapters.game.v1649370.accounts.profile import PUBLIC, SECURE


class ProfileSaveBoundaryTests(unittest.TestCase):
    """编码态不可落盘，明文仍按既有双档案事务保存。"""
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
        self.flags = {PUBLIC: [0, 0], SECURE: [0, 0]}
        self.process.read.side_effect = self.read_memory

    def read_memory(self, address, length):
        for base in (PUBLIC, SECURE):
            if address == base+0x1112C:
                return bytes([self.flags[base].pop(0)])
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

    def test_encoded_either_profile_blocks_normal_and_forced_save(self):
        for base in (PUBLIC, SECURE):
            for force in (False, True):
                with self.subTest(base=base, force=force):
                    self.flags = {PUBLIC: [0, 0], SECURE: [0, 0]}
                    self.flags[base] = [1, 1]
                    self.save(force)
        self.bridge._service.save.assert_not_called()
        self.bridge._service.inspect_snapshot.assert_not_called()

    def test_encoding_transition_during_sampling_blocks_save(self):
        self.flags[SECURE] = [0, 1]
        self.save(True)
        self.bridge._service.save.assert_not_called()
        self.bridge._service.inspect_snapshot.assert_not_called()

    def test_save_resumes_when_native_returns_to_plaintext(self):
        self.flags[PUBLIC] = [1]
        self.save(True)
        self.flags = {PUBLIC: [0, 0], SECURE: [0, 0]}
        self.save(True)
        self.bridge._service.save.assert_called_once()


if __name__ == '__main__':
    unittest.main()
