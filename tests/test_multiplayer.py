"""联机用例回归：异步、重登、写入不确定恢复和身份文件完整性。"""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from codol_rebuild.application.multiplayer.contracts import IdentityRecord, MultiplayerError
from codol_rebuild.application.multiplayer.service import MultiplayerSession
from codol_rebuild.adapters.persistence.server_identity import JsonIdentityStore
from codol_rebuild.adapters.transport.http import normalize_server


class Gateway:
    """可注入响应丢失与封禁的内存服务端替身。"""
    def __init__(self):
        self.calls = []
        self.rooms = {}
        self.serial = 0
        self.fail_create = False
        self.fail_leave = False
        self.banned = False
        self.wait = None

    def login(self, fingerprint, username, player_id):
        self.calls.append(('login', player_id, username))
        if self.wait:
            self.wait.wait(2)
        if self.banned:
            raise MultiplayerError('BANNED', '已封禁')
        return {'playerId': player_id or 'pl_test', 'displayName': username}

    def create(self, player_id, username, max_players, password):
        self.calls.append(('create', player_id, max_players, password))
        if self.rooms:
            raise MultiplayerError('ALREADY_IN_OTHER_ROOM', '已有房间')
        self.serial += 1
        room = {'roomId': 'rm_'+str(self.serial), 'ownerPlayerId': player_id}
        self.rooms[room['roomId']] = room
        if self.fail_create:
            self.fail_create = False
            raise MultiplayerError('NETWORK_ERROR', '响应丢失', uncertain=True)
        return room

    def list_rooms(self, player_id, cursor):
        self.calls.append(('list', player_id))
        return {'rooms': list(self.rooms.values()), 'nextCursor': None}

    def leave(self, player_id, room_id):
        self.calls.append(('leave', room_id))
        if self.fail_leave:
            raise MultiplayerError('NETWORK_ERROR', '网络断开', uncertain=True)
        self.rooms.pop(room_id, None)
        return {'left': True, 'roomClosed': True}


class MultiplayerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = JsonIdentityStore(Path(self.directory.name)/'identity.json')
        self.gateway = Gateway()
        self.session = MultiplayerSession(self.gateway, self.store, 'http://localhost:8080', 'fp-test')
        self.addCleanup(self.close)

    def close(self):
        self.gateway.fail_leave = False
        if self.gateway.wait:
            self.gateway.wait.set()
        self.session.close()

    def result(self, operation, **kwargs):
        self.assertTrue(self.session.begin(operation, '测试玩家', **kwargs))
        return self.consume()

    def consume(self):
        until = time.monotonic()+3
        while time.monotonic() < until:
            value = self.session.poll()
            if value is not None:
                return value
            time.sleep(.005)
        self.fail('后台用例没有完成')

    def test_login_create_leave_create_and_relogin_identity(self):
        self.assertIsNone(self.result('login').error)
        first = self.result('create', max_players=6, password='口令')
        self.assertIsNone(first.error)
        self.assertEqual(self.store.load('http://localhost:8080').room_id, first.data['roomId'])
        self.assertIsNone(self.result('leave').error)
        self.assertIsNone(self.store.load('http://localhost:8080').room_id)
        second = self.result('create')
        self.assertNotEqual(first.data['roomId'], second.data['roomId'])
        self.session.close()
        self.assertEqual(self.gateway.rooms, {})
        self.session = MultiplayerSession(self.gateway, self.store, 'http://localhost:8080', 'fp-other')
        self.assertEqual(self.result('login').data['playerId'], 'pl_test')
        self.assertEqual(self.gateway.calls[-1], ('login', 'pl_test', '测试玩家'))

    def test_inflight_requests_do_not_block_or_duplicate(self):
        self.gateway.wait = threading.Event()
        self.assertTrue(self.session.begin('login', '测试玩家'))
        self.assertIsNone(self.session.poll())
        self.assertFalse(self.session.begin('create', '测试玩家'))
        self.gateway.wait.set()
        self.assertIsNone(self.consume().error)
        self.assertEqual(len(self.gateway.calls), 1)

    def test_lost_create_response_recovers_before_next_create(self):
        self.gateway.fail_create = True
        self.assertTrue(self.result('create').error.uncertain)
        self.assertTrue(self.store.load('http://localhost:8080').uncertain_create)
        self.assertIsNone(self.result('create').error)
        actions = [call[0] for call in self.gateway.calls]
        self.assertEqual(actions, ['login', 'create', 'list', 'leave', 'create'])
        self.assertEqual(len(self.gateway.rooms), 1)

    def test_failed_leave_keeps_identity_and_room_until_retry(self):
        created = self.result('create')
        self.gateway.fail_leave = True
        self.assertIsNotNone(self.result('leave').error)
        self.assertEqual(self.store.load('http://localhost:8080').room_id, created.data['roomId'])
        self.gateway.fail_leave = False
        self.assertIsNone(self.result('leave').error)

    def test_ban_does_not_create_replacement_identity(self):
        self.store.save('http://localhost:8080', IdentityRecord('pl_saved'))
        self.gateway.banned = True
        self.assertEqual(self.result('login').error.code, 'BANNED')
        self.assertEqual(self.store.load('http://localhost:8080').player_id, 'pl_saved')
        self.assertEqual(self.gateway.calls, [('login', 'pl_saved', '测试玩家')])

    def test_invalid_file_is_not_overwritten_or_sent_to_server(self):
        self.store.path.write_text('{broken', encoding='utf-8')
        self.assertEqual(self.result('login').error.code, 'LOCAL_IDENTITY_ERROR')
        self.assertEqual(self.gateway.calls, [])
        self.assertEqual(self.store.path.read_text(encoding='utf-8'), '{broken')

    def test_identity_scoped_by_server(self):
        self.store.save('http://one', IdentityRecord('pl_one'))
        self.store.save('http://two', IdentityRecord('pl_two'))
        self.assertEqual(self.store.load('http://one').player_id, 'pl_one')
        self.assertEqual(self.store.load('http://two').player_id, 'pl_two')
        self.assertIsNone(self.store.load('http://three'))

    def test_server_address_validation(self):
        self.assertEqual(normalize_server('HTTP://LOCALHOST:80/'), 'http://localhost')
        self.assertEqual(normalize_server('https://example.com:443'), 'https://example.com')
        for address in ('file:///a', 'http://user:pass@host', 'http://host/path', 'http://host?q=1'):
            with self.subTest(address=address), self.assertRaises(ValueError):
                normalize_server(address)
