"""玩家双档案的传输契约，覆盖空档、完整二进制和写入确认。"""
import unittest
from unittest.mock import Mock

from codol_rebuild.adapters.transport.http import HttpGateway
from codol_rebuild.application.errors import OnlineError


class PlayerProfileHttpTests(unittest.TestCase):
    def test_initial_response_and_binary_roundtrip(self):
        gateway = HttpGateway('http://localhost')
        gateway._request = Mock(return_value={'revision': 0, 'profile': None})
        self.assertEqual(gateway.load_player_profile('pl_test'), (0, {}))
        records = {'public': b'p' * 32764, 'secure': b's' * 32764}
        gateway._request.return_value = {'revision': 1}
        self.assertEqual(gateway.save_player_profile('pl_test', 0, records), 1)
        payload = gateway._request.call_args.args[2]
        self.assertEqual(payload['expectedRevision'], 0)
        gateway._request.return_value = {'revision': 1, 'profile': payload['profile']}
        self.assertEqual(gateway.load_player_profile('pl_test'), (1, records))
        for malformed in ({'revision': 1, 'profile': None}, {'revision': 0}, {
                'revision': 1, 'profile': {**payload['profile'], 'secure': ''}}):
            gateway._request.return_value = malformed
            with self.assertRaises(OnlineError):
                gateway.load_player_profile('pl_test')

    def test_retry_keeps_original_pair_and_revision(self):
        gateway = HttpGateway('http://localhost')
        records = {'public': b'p' * 32764, 'secure': b's' * 32764}
        gateway._request = Mock(side_effect=[OnlineError('NETWORK_ERROR', '回执丢失', uncertain=True), {'revision': 8}])
        self.assertEqual(gateway.save_player_profile('pl_test', 7, records), 8)
        self.assertEqual(gateway._request.call_args_list[0], gateway._request.call_args_list[1])
        gateway._request = Mock(side_effect=OnlineError('PROFILE_REVISION_CONFLICT', '版本冲突'))
        with self.assertRaises(OnlineError):
            gateway.save_player_profile('pl_test', 7, records)
        gateway._request.assert_called_once()
