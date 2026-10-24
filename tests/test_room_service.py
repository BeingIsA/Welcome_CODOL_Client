"""联机用例回归：异步、重登、写入不确定恢复和身份文件完整性。"""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from codol_rebuild.application.room.contracts import IdentityRecord, RoomError
from codol_rebuild.application.room.service import RoomSession
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
            raise RoomError('BANNED', '已封禁')
        return {'playerId': player_id or 'pl_test', 'displayName': username}

    def create(self, player_id, username, max_players, password, settings=None):
        self.calls.append(('create', player_id, max_players, password, settings))
        if self.rooms:
            raise RoomError('ALREADY_IN_OTHER_ROOM', '已有房间')
        self.serial += 1
        room = {'roomId': 'rm_'+str(self.serial), 'ownerPlayerId': player_id,
                'settings': settings or {}}
        self.rooms[room['roomId']] = room
        if self.fail_create:
            self.fail_create = False
            raise RoomError('NETWORK_ERROR', '响应丢失', uncertain=True)
        return room

    def update_settings(self, player_id, room_id, settings):
        self.calls.append(('update_settings', room_id, settings))
        room = self.rooms.get(room_id)
        if room is None:
            raise RoomError('ROOM_NOT_FOUND', '房间不存在')
        if getattr(self, 'settings_error', None):
            raise self.settings_error
        room['settings'] = settings
        return dict(room)

    def room_detail(self, player_id, room_id):
        self.calls.append(('detail', room_id))
        room = self.rooms.get(room_id)
        if room is None:
            raise RoomError('ROOM_NOT_FOUND', '房间不存在')
        return dict(room)

    def member_profile(self, player_id, room_id, member_id):
        """成员资料总是存在；格式验证由游戏适配层测试覆盖。"""
        return {'testMember': member_id}

    def list_rooms(self, player_id, cursor):
        self.calls.append(('list', player_id))
        return {'rooms': list(self.rooms.values()), 'nextCursor': None}

    def leave(self, player_id, room_id):
        self.calls.append(('leave', room_id))
        if self.fail_leave:
            raise RoomError('NETWORK_ERROR', '网络断开', uncertain=True)
        self.rooms.pop(room_id, None)
        return {'left': True, 'roomClosed': True}


class RoomTests(unittest.TestCase):
    def test_join_saves_target_and_blocks_guest_settings(self):
        self.result('login')
        from unittest.mock import Mock
        data = {'roomId': 'rm_target', 'ownerPlayerId': 'pl_owner', 
                'members': [{'playerId': 'pl_test'}]}
        self.gateway.join = Mock(return_value=data)
        result = self.result('join', room_id='rm_target', password='secret')
        self.assertEqual(result.data, data)
        self.gateway.join.assert_called_once_with('pl_test', 'rm_target', '测试玩家', 'secret')
        self.assertEqual(self.store.load('http://localhost:8080').room_id, 'rm_target')
        self.assertEqual(self.result('settings', settings={'mode': 'td'}).error.code, 'FORBIDDEN')
        self.assertEqual(self.result('join', room_id='rm_other').error.code, 'ALREADY_IN_OTHER_ROOM')
        self.gateway.join.assert_called_once()
        self.assertIsNone(self.result('leave').error)
        self.assertIsNone(self.store.load('http://localhost:8080').room_id)

    def test_join_failure_restores_record_without_retry(self):
        self.result('login')
        from unittest.mock import Mock
        self.gateway.join = Mock(side_effect=RoomError('INVALID_ROOM_PASSWORD', '密码错误'))
        self.assertEqual(self.result('join', room_id='rm_target').error.code, 'INVALID_ROOM_PASSWORD')
        self.assertIsNone(self.store.load('http://localhost:8080').room_id)
        self.gateway.join.assert_called_once()

    def test_join_lost_response_confirms_membership_and_retains_unknown_target(self):
        self.result('login')
        from unittest.mock import Mock
        self.gateway.join = Mock(side_effect=RoomError('NETWORK_ERROR', '超时', uncertain=True))
        self.gateway.room_detail = Mock(return_value={
            'roomId': 'rm_target', 'ownerPlayerId': 'pl_owner', 
            'members': [{'playerId': 'pl_test'}]})
        self.assertIsNone(self.result('join', room_id='rm_target').error)
        self.gateway.join.assert_called_once()
        self.result('leave')
        self.gateway.room_detail.return_value = {'roomId': 'rm_target', 'members': []}
        self.assertTrue(self.result('join', room_id='rm_target').error.uncertain)
        self.assertEqual(self.store.load('http://localhost:8080').room_id, 'rm_target')
        self.assertTrue(self.store.load('http://localhost:8080').uncertain_join)
        self.assertEqual(self.result('leave').error.code, 'RECOVERY_PENDING')
        # 服务端最终可见成员后才确认离房，避免先离房后迟到加入形成幽灵成员。
        self.gateway.room_detail.return_value = {'roomId': 'rm_target', 'members': [{'playerId': 'pl_test'}]}
        self.session.close()
        self.assertIn(('leave', 'rm_target'), self.gateway.calls)
        self.assertIsNone(self.store.load('http://localhost:8080').room_id)

    def wait_list(self):
        """仅等待独立测试工作线程，不接触游戏或外部服务器。"""
        for _ in range(300):
            result = self.session.poll_list()
            if result is not None:
                return result
            time.sleep(.005)
        self.fail('列表查询未完成')

    def test_list_collects_pages_deduplicates_without_leaving_current_room(self):
        self.assertIsNone(self.result('login').error)
        from test_room_list import room
        self.assertIsNone(self.result('create').error)
        calls = []
        def pages(player, cursor):
            calls.append(cursor)
            if cursor is None:
                return {'rooms': [room('rm_first','sd')], 'nextCursor': 'next'}
            return {'rooms': [room('rm_second'), room('rm_first','sd')], 'nextCursor': None}
        self.gateway.list_rooms = pages
        before = list(self.gateway.calls)
        self.session.begin_list('测试玩家')
        result = self.wait_list()
        self.assertIsNone(result.error)
        self.assertEqual(calls, [None, 'next'])
        self.assertEqual([r['roomId'] for r in result.data['rooms']], ['rm_first','rm_second'])
        self.assertEqual(self.gateway.calls, before)
        self.assertEqual(len(self.gateway.rooms), 1)

    def test_refresh_and_cancel_discard_old_batch_without_queue_growth(self):
        self.assertIsNone(self.result('login').error)
        from test_room_list import room
        entered, release = threading.Event(), threading.Event()
        calls = []
        def pages(player, cursor):
            calls.append(cursor)
            if len(calls) == 1:
                entered.set()
                release.wait(3)
                return {'rooms': [room('rm_stale')], 'nextCursor': 'must-not-fetch'}
            return {'rooms': [room('rm_latest')], 'nextCursor': None}
        self.gateway.list_rooms = pages
        self.addCleanup(release.set)
        self.session.begin_list('玩家')
        self.assertTrue(entered.wait(2))
        for _ in range(5):
            self.session.begin_list('玩家')
        release.set()
        result = self.wait_list()
        self.assertEqual(calls, [None, None])
        self.assertEqual(result.data['rooms'][0]['roomId'], 'rm_latest')
        self.session.begin_list('玩家')
        self.session.cancel_list()
        self.assertEqual(self.wait_list().operation, 'list_cancelled')

    def test_list_rejects_cyclic_cursor_and_reports_network_error(self):
        self.assertIsNone(self.result('login').error)
        self.gateway.list_rooms = lambda *_: {'rooms': [], 'nextCursor': 'same'}
        self.session.begin_list('玩家')
        self.assertEqual(self.wait_list().error.code, 'INVALID_RESPONSE')
        def broken(*_):
            raise RoomError('NETWORK_ERROR', '断网')
        self.gateway.list_rooms = broken
        self.session.begin_list('玩家')
        self.assertEqual(self.wait_list().error.code, 'NETWORK_ERROR')

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = JsonIdentityStore(Path(self.directory.name)/'identity.json')
        self.gateway = Gateway()
        self.session = RoomSession(self.gateway, self.store, 'http://localhost:8080', 'fp-test')
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
        self.session = RoomSession(self.gateway, self.store, 'http://localhost:8080', 'fp-other')
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
        self.assertIsNone(self.result('login').error)
        self.gateway.fail_create = True
        self.assertTrue(self.result('create').error.uncertain)
        self.assertTrue(self.store.load('http://localhost:8080').uncertain_create)
        self.assertIsNone(self.result('create').error)
        actions = [call[0] for call in self.gateway.calls]
        self.assertEqual(actions, ['login', 'create', 'list', 'leave', 'create'])
        self.assertEqual(len(self.gateway.rooms), 1)

    def test_failed_leave_keeps_identity_and_room_until_retry(self):
        self.assertIsNone(self.result('login').error)
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

    def test_business_operations_and_name_changes_never_login(self):
        self.assertIsNone(self.result('login').error)
        for operation in ('create', 'settings', 'leave'):
            self.assertTrue(self.session.begin(operation, '新昵称', settings={'mode': 'sd'}))
            self.assertIsNone(self.consume().error)
        self.session.begin_list('另一个昵称')
        self.assertIsNone(self.wait_list().error)
        self.assertEqual([c for c in self.gateway.calls if c[0] == 'login'],
                         [('login', None, '测试玩家')])

    def test_business_operations_without_identity_do_not_login(self):
        for operation in ('create', 'leave', 'settings'):
            self.assertEqual(self.result(operation).error.code, 'NO_IDENTITY')
        self.session.begin_list('测试玩家')
        self.assertEqual(self.wait_list().error.code, 'NO_IDENTITY')
        self.assertEqual(self.gateway.calls, [])

    def test_server_rejection_does_not_trigger_login(self):
        self.assertIsNone(self.result('login').error)
        def rejected(*args):
            raise RoomError('UNAUTHENTICATED', '服务端拒绝')
        self.gateway.list_rooms = rejected
        for _ in range(2):
            self.session.begin_list('新昵称')
            self.assertEqual(self.wait_list().error.code, 'UNAUTHENTICATED')
        self.assertEqual([c[0] for c in self.gateway.calls], ['login'])

class SettingsSyncTests(RoomTests):
    """房内设置同步：异步补交、修订号冲突自愈与离房重置。"""

    def test_create_carries_settings_without_revision(self):
        self.assertIsNone(self.result('login').error)
        settings = {'mode': 'td', 'map': 'mp_crash', 'modeTimeLimit': 10.5}
        result = self.result('create', max_players=8, password='pw', settings=settings)
        self.assertIsNone(result.error)
        create_call = next(c for c in self.gateway.calls if c[0] == 'create')
        self.assertEqual(create_call[4], settings)

    def test_settings_sync_without_revision(self):
        self.assertIsNone(self.result('login').error)
        self.result('create', max_players=4)
        result = self.result('settings', settings={'mode': 'sd'})
        self.assertIsNone(result.error)
        update = next(c for c in self.gateway.calls if c[0] == 'update_settings')
        self.assertEqual(update[1], 'rm_1')
        self.assertEqual(update[2], {'mode': 'sd'})
        room = self.gateway.rooms['rm_1']
        self.assertEqual(room['settings'], {'mode': 'sd'})
        self.assertNotIn('revision', result.data)

    def test_identical_settings_requests_each_submit_once(self):
        """每次明确的同值设置意图仍分别提交，不依赖修订号。"""
        self.assertIsNone(self.result('login').error)
        settings = {'mode': 'td'}
        created = self.result('create', settings=settings)
        self.assertNotIn('revision', created.data)
        for _ in range(3):
            result = self.result('settings', settings=settings)
            self.assertIsNone(result.error)
            self.assertEqual(result.data['settings'], settings)
        self.assertEqual([c[2] for c in self.gateway.calls if c[0] == 'update_settings'],
                         [settings] * 3)

    def test_settings_errors_never_refetch_or_retry(self):
        """明确拒绝与不确定写入均向调用方返回，旧冲突码也不触发重试。"""
        self.assertIsNone(self.result('login').error)
        self.result('create', max_players=4)
        for code, uncertain in (('REVISION_CONFLICT', False), ('FORBIDDEN', False),
                                ('NETWORK_ERROR', True)):
            with self.subTest(code=code):
                self.gateway.calls.clear()
                self.gateway.settings_error = RoomError(code, code, uncertain=uncertain)
                result = self.result('settings', settings={'mode': 'sd'})
                self.assertEqual(result.error.code, code)
                self.assertEqual(result.error.uncertain, uncertain)
                self.assertEqual(self.gateway.calls,
                                 [('update_settings', 'rm_1', {'mode': 'sd'})])

    def test_settings_without_room_reports_error(self):
        self.assertIsNone(self.result('login').error)
        result = self.result('settings', settings={'mode': 'sd'})
        self.assertEqual(result.error.code, 'NO_ROOM')

    def test_settings_after_leave_target_new_room(self):
        self.assertIsNone(self.result('login').error)
        self.result('create', max_players=4)
        self.result('settings', settings={'mode': 'sd'})
        self.result('leave')
        # 重新建房后的设置必须提交到新房间。
        self.result('create', max_players=4)
        result = self.result('settings', settings={'mode': 'td'})
        self.assertIsNone(result.error)
        self.assertEqual(result.data['roomId'], 'rm_2')
        self.assertEqual(result.data['settings'], {'mode': 'td'})

    def test_settings_does_not_block_create(self):
        self.assertIsNone(self.result('login').error)
        self.assertTrue(self.session.begin('settings', '测试玩家', settings={'mode': 'sd'}))
        # 设置同步进行中，主流程仍可提交。
        self.assertTrue(self.session.begin('login', '测试玩家'))
        self.consume()
        self.consume()
