"""业务 HTTP 契约回归：错误传播、身份不变和禁止隐式重试。"""
from io import BytesIO
import json
import unittest
from unittest.mock import Mock
from codol_rebuild.adapters.transport.http import HttpGateway
from codol_rebuild.application.multiplayer.contracts import MultiplayerError


class Response(BytesIO):
    """具有 HTTP 状态的独立响应流。"""
    def __init__(self, data, status=200):
        super().__init__(json.dumps(data).encode('utf-8'))
        self.status = status


class HttpGatewayTests(unittest.TestCase):
    def setUp(self):
        self.gateway = HttpGateway('http://127.0.0.1:8080')
        self.gateway._opener = Mock()

    def test_login_body_and_player_header(self):
        self.gateway._opener.open.return_value = Response({'apiVersion': '1', 'data': {
            'playerId': 'pl_saved', 'displayName': '玩家', 'firstLogin': False}})
        self.gateway.login('fp-test', '玩家', 'pl_saved')
        request = self.gateway._opener.open.call_args.args[0]
        self.assertEqual(json.loads(request.data), {'fingerprint': 'fp-test', 'username': '玩家', 'playerId': 'pl_saved'})
        self.gateway._opener.open.return_value = Response({'apiVersion': '1', 'data': {'left': True, 'roomClosed': True}})
        self.gateway.leave('pl_saved', 'rm_test')
        request = self.gateway._opener.open.call_args.args[0]
        self.assertEqual(request.get_header('X-player-id'), 'pl_saved')
        self.assertEqual(request.full_url, 'http://127.0.0.1:8080/api/v1/rooms/rm_test/leave')

    def test_ban_is_not_replaced_with_network_error(self):
        self.gateway._opener.open.return_value = Response({'error': {'code': 'BANNED', 'message': '已封禁'}}, 403)
        with self.assertRaises(MultiplayerError) as caught:
            self.gateway.login('fp-test', '玩家', 'pl_saved')
        self.assertEqual(caught.exception.code, 'BANNED')
        self.assertFalse(caught.exception.uncertain)
        self.gateway._opener.open.assert_called_once()

    def test_timeout_is_uncertain_and_not_retried(self):
        self.gateway._opener.open.side_effect = TimeoutError('timeout')
        with self.assertRaises(MultiplayerError) as caught:
            self.gateway.create('pl_saved', '玩家', 4, None)
        self.assertTrue(caught.exception.uncertain)
        self.gateway._opener.open.assert_called_once()

    def test_relogin_cannot_replace_saved_player_id(self):
        self.gateway._opener.open.return_value = Response({'apiVersion': '1', 'data': {
            'playerId': 'pl_other', 'displayName': '玩家', 'firstLogin': True}})
        with self.assertRaisesRegex(MultiplayerError, '不同身份'):
            self.gateway.login('fp-test', '玩家', 'pl_saved')

    def test_wrong_owner_response_is_uncertain(self):
        self.gateway._opener.open.return_value = Response({'apiVersion': '1', 'data': {
            'roomId': 'rm_other', 'ownerPlayerId': 'pl_other', 'maxPlayers': 4, 'members': []}})
        with self.assertRaises(MultiplayerError) as caught:
            self.gateway.create('pl_saved', '玩家', 4, None)
        self.assertEqual(caught.exception.code, 'INVALID_RESPONSE')
        self.assertTrue(caught.exception.uncertain)
