"""业务 HTTP 契约回归：错误传播、身份不变和禁止隐式重试。"""
from io import BytesIO
import json
import unittest
from unittest.mock import Mock
from codol_rebuild.adapters.transport.http import HttpGateway
from codol_rebuild.application.room.contracts import RoomError


class Response(BytesIO):
    """具有 HTTP 状态的独立响应流。"""
    def __init__(self, data, status=200):
        super().__init__(json.dumps(data).encode('utf-8'))
        self.status = status


class HttpGatewayTests(unittest.TestCase):
    def test_join_request_snapshot_and_uncertain_response(self):
        data = {'roomId': 'rm_target', 'ownerPlayerId': 'pl_owner', 'revision': 2,
                'maxPlayers': 4, 'playerCount': 2, 'settings': {'map': 'mp_crash', 'mode': 'td'},
                'members': [{'playerId': 'pl_owner', 'displayName': '房主', 'isOwner': True},
                            {'playerId': 'pl_saved', 'displayName': '玩家', 'isOwner': False}]}
        self.gateway._opener.open.return_value = Response({'apiVersion': '1', 'data': data})
        self.assertEqual(self.gateway.join('pl_saved', 'rm_target', '玩家', '密码'), data)
        request = self.gateway._opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'http://127.0.0.1:8080/api/v1/rooms/rm_target/join')
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(request.get_header('X-player-id'), 'pl_saved')
        self.assertEqual(json.loads(request.data), {'displayName': '玩家', 'password': '密码'})
        for change in ({'roomId': 'rm_other'}, {'revision': True}, {'playerCount': 1},
                       {'ownerPlayerId': 'pl_absent'}, {'members': []},
                       {'members': [data['members'][0]]*2}, {'settings': {'map': 1, 'mode': 'td'}}):
            with self.subTest(change=change):
                self.gateway._opener.open.return_value = Response({'apiVersion': '1', 'data': {**data, **change}})
                with self.assertRaises(RoomError) as error:
                    self.gateway.join('pl_saved', 'rm_target', '玩家', None)
                self.assertTrue(error.exception.uncertain)

    def test_join_rejection_and_timeout_are_not_retried(self):
        for code in ('INVALID_ROOM_PASSWORD', 'ROOM_FULL', 'ROOM_CLOSED', 'ALREADY_IN_OTHER_ROOM'):
            self.gateway._opener.open.reset_mock()
            self.gateway._opener.open.return_value = Response({'error': {'code': code, 'message': code}}, 409)
            with self.assertRaises(RoomError) as error:
                self.gateway.join('pl_saved', 'rm_target', '玩家', None)
            self.assertEqual(error.exception.code, code)
            self.assertFalse(error.exception.uncertain)
            self.gateway._opener.open.assert_called_once()
        self.gateway._opener.open.side_effect = TimeoutError('timeout')
        with self.assertRaises(RoomError) as error:
            self.gateway.join('pl_saved', 'rm_target', '玩家', None)
        self.assertTrue(error.exception.uncertain)

    def test_list_contract_and_cursor_encoding(self):
        from test_room_list import room
        data = {'rooms': [room()], 'nextCursor': None}
        self.gateway._opener.open.return_value = Response({'apiVersion': '1', 'data': data})
        self.assertEqual(self.gateway.list_rooms('pl_saved', 'time+zone|rm_one'), data)
        request = self.gateway._opener.open.call_args.args[0]
        self.assertIn('cursor=time%2Bzone%7Crm_one', request.full_url)
        self.assertEqual(request.get_method(), 'GET')
        self.assertEqual(request.get_header('X-player-id'), 'pl_saved')
        for key, value in [('playerCount', True), ('maxPlayers', 0), ('playerCount', 1000),
                           ('hasPassword', 1), ('mode', None), ('ownerDisplayName', 'a\0b')]:
            broken = {**room(), key: value}
            self.gateway._opener.open.return_value = Response({'apiVersion': '1', 'data': {
                'rooms': [broken], 'nextCursor': None}})
            with self.subTest(key=key, value=value), self.assertRaises(RoomError):
                self.gateway.list_rooms('pl_saved', None)

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
        with self.assertRaises(RoomError) as caught:
            self.gateway.login('fp-test', '玩家', 'pl_saved')
        self.assertEqual(caught.exception.code, 'BANNED')
        self.assertFalse(caught.exception.uncertain)
        self.gateway._opener.open.assert_called_once()

    def test_timeout_is_uncertain_and_not_retried(self):
        self.gateway._opener.open.side_effect = TimeoutError('timeout')
        with self.assertRaises(RoomError) as caught:
            self.gateway.create('pl_saved', '玩家', 4, None)
        self.assertTrue(caught.exception.uncertain)
        self.gateway._opener.open.assert_called_once()

    def test_relogin_cannot_replace_saved_player_id(self):
        self.gateway._opener.open.return_value = Response({'apiVersion': '1', 'data': {
            'playerId': 'pl_other', 'displayName': '玩家', 'firstLogin': True}})
        with self.assertRaisesRegex(RoomError, '不同身份'):
            self.gateway.login('fp-test', '玩家', 'pl_saved')

    def test_create_carries_settings_and_validates_echo(self):
        settings = {'mode': 'td', 'modeTimeLimit': 10.5, 'weaponRestrictMask': 32768}
        self.gateway._opener.open.return_value = Response({'apiVersion': '1', 'data': {
            'roomId': 'rm_new', 'ownerPlayerId': 'pl_saved', 'maxPlayers': 4,
            'members': [{'playerId': 'pl_saved', 'displayName': '玩家', 'isOwner': True}],
            'revision': 0, 'settings': settings}})
        created = self.gateway.create('pl_saved', '玩家', 4, None, settings)
        self.assertEqual(created['revision'], 0)
        request = self.gateway._opener.open.call_args.args[0]
        self.assertEqual(json.loads(request.data)['settings'], settings)
        # 回显不一致时视为可疑响应，不静默接受。
        self.gateway._opener.open.return_value = Response({'apiVersion': '1', 'data': {
            'roomId': 'rm_new', 'ownerPlayerId': 'pl_saved', 'maxPlayers': 4,
            'members': [{'playerId': 'pl_saved', 'displayName': '玩家', 'isOwner': True}],
            'settings': {'mode': 'dm'}}})
        with self.assertRaises(RoomError) as caught:
            self.gateway.create('pl_saved', '玩家', 4, None, settings)
        self.assertEqual(caught.exception.code, 'INVALID_RESPONSE')

    def test_update_settings_patches_with_expected_revision(self):
        settings = {'mode': 'sd', 'map': 'mp_crash'}
        self.gateway._opener.open.return_value = Response({'apiVersion': '1', 'data': {
            'roomId': 'rm_new', 'revision': 1, 'settings': settings, 'members': []}})
        self.gateway.update_settings('pl_saved', 'rm_new', settings, 0)
        request = self.gateway._opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), 'PATCH')
        self.assertEqual(request.full_url, 'http://127.0.0.1:8080/api/v1/rooms/rm_new/settings')
        self.assertEqual(json.loads(request.data),
                         {'settings': settings, 'expectedRevision': 0})
        # 冲突错误码原样透传且非不确定写入。
        self.gateway._opener.open.return_value = Response(
            {'error': {'code': 'REVISION_CONFLICT', 'message': '修订号过期'}}, 409)
        with self.assertRaises(RoomError) as caught:
            self.gateway.update_settings('pl_saved', 'rm_new', settings, 0)
        self.assertEqual(caught.exception.code, 'REVISION_CONFLICT')
        self.assertFalse(caught.exception.uncertain)

    def test_room_detail_reads_revision(self):
        self.gateway._opener.open.return_value = Response({'apiVersion': '1', 'data': {
            'roomId': 'rm_new', 'revision': 5, 'members': []}})
        detail = self.gateway.room_detail('pl_saved', 'rm_new')
        self.assertEqual(detail['revision'], 5)
        request = self.gateway._opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'http://127.0.0.1:8080/api/v1/rooms/rm_new')

    def test_wrong_owner_response_is_uncertain(self):
        self.gateway._opener.open.return_value = Response({'apiVersion': '1', 'data': {
            'roomId': 'rm_other', 'ownerPlayerId': 'pl_other', 'maxPlayers': 4, 'members': []}})
        with self.assertRaises(RoomError) as caught:
            self.gateway.create('pl_saved', '玩家', 4, None)
        self.assertEqual(caught.exception.code, 'INVALID_RESPONSE')
        self.assertTrue(caught.exception.uncertain)
