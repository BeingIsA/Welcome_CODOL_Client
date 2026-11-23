"""联网档案的首次建档、重登、并发冲突及纯离线临时会话回归。"""
from hashlib import sha256
from threading import Event
import unittest
from unittest.mock import Mock

from codol_rebuild.application.accounts.online_profiles import OnlineProfiles
from codol_rebuild.application.accounts.service import AccountSession
from codol_rebuild.application.errors import OnlineError


def pair(label):
    return {'public': b'public ' + label, 'secure': b'secure ' + label}


def codec():
    result = Mock()
    result.initialization_data.return_value = {
        'progression': {}, 'starting_loadouts': [], 'default_character': 545}
    result.initialize.return_value = pair(b'default'), {}
    result.migrate.side_effect = dict
    return result


class OnlineProfileTests(unittest.TestCase):
    def test_new_player_builds_once_and_reopens_latest_server_pair(self):
        gateway = Mock()
        gateway.load_player_profile.return_value = (0, {})
        gateway.save_player_profile.return_value = 1
        saves = OnlineProfiles(gateway)
        saves.load_for_login('pl_player', '服务端昵称')
        account = AccountSession(codec(), saves)
        initial = account.initialize()
        saves.close()
        self.assertTrue(account.initial_profile_ready)
        self.assertNotIn('initialization', initial)
        self.assertTrue(initial['new_account'])
        self.assertEqual(initial['uid'], int.from_bytes(sha256(b'pl_player').digest()[:8], 'little'))
        self.assertEqual(account.name_snapshot['value'], '服务端昵称')
        gateway.save_player_profile.assert_called_once_with('pl_player', 0, pair(b'default'))
        gateway.load_player_profile.return_value = (1, pair(b'default'))
        saves = OnlineProfiles(gateway)
        saves.load_for_login('pl_player', '已改昵称')
        account = AccountSession(codec(), saves)
        existing = account.initialize()
        saves.close()
        self.assertFalse(existing['new_account'])
        self.assertEqual(existing['revision'], 1)
        self.assertEqual(existing['uid'], initial['uid'])
        self.assertEqual(account.name_snapshot['value'], '已改昵称')
        account.codec.initialize.assert_not_called()
        self.assertEqual(gateway.save_player_profile.call_count, 1)

    def test_pending_edits_coalesce_after_server_confirmation(self):
        started, release = Event(), Event()
        gateway = Mock()
        gateway.load_player_profile.return_value = (0, {})
        def save(player_id, expected_revision, profiles):
            if not expected_revision:
                started.set()
                if not release.wait(3):
                    raise AssertionError('测试未释放首次保存')
            return expected_revision + 1
        gateway.save_player_profile.side_effect = save
        saves = OnlineProfiles(gateway)
        saves.load_for_login('pl_player', '测试玩家')
        account = AccountSession(codec(), saves)
        try:
            account.initialize()
            self.assertTrue(started.wait(1))
            self.assertFalse(account.initial_profile_ready)
            self.assertTrue(account.save(pair(b'older')))
            self.assertTrue(account.save(pair(b'latest')))
            self.assertFalse(account.save(pair(b'latest')))
        finally:
            release.set()
            saves.close()
        self.assertEqual([call.args for call in gateway.save_player_profile.call_args_list],
                         [('pl_player', 0, pair(b'default')), ('pl_player', 1, pair(b'latest'))])
        self.assertEqual(saves.confirmed_revision, 2)

    def test_conflict_stops_further_writes_and_is_reported_at_close(self):
        gateway = Mock()
        gateway.load_player_profile.return_value = (7, pair(b'existing'))
        failure = OnlineError('PROFILE_CONFLICT', '有其他会话保存了较新的档案')
        gateway.save_player_profile.side_effect = failure
        saves = OnlineProfiles(gateway)
        saves.load_for_login('pl_player', '测试玩家')
        account = AccountSession(codec(), saves)
        account.initialize()
        account.save(pair(b'change'))
        with self.assertRaises(OnlineError) as caught:
            saves.close()
        self.assertIs(caught.exception, failure)
        with self.assertRaises(OnlineError):
            account.save(pair(b'change'))
        self.assertEqual(account.account['revision'], 7)
        gateway.save_player_profile.assert_called_once_with('pl_player', 7, pair(b'change'))

    def test_offline_sessions_always_start_with_their_temporary_defaults(self):
        offline = AccountSession(codec())
        self.assertEqual(offline.initialize()['initialization']['owned_ids'], [])
        self.assertTrue(offline.initial_profile_ready)
        offline.save(pair(b'offline edit'))
        restarted = AccountSession(codec())
        self.assertEqual(restarted.initialize()['profiles'], pair(b'default'))
        self.assertIsNone(restarted.name_snapshot['value'])
