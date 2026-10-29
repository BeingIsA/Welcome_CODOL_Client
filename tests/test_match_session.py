"""对局协调的重复提交与离房迟到结果回归，不代替原生客户端验收。"""
from concurrent.futures import Future
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from codol_rebuild.application.matches.service import MatchSession
from codol_rebuild.application.errors import OnlineError


class MatchSessionTests(unittest.TestCase):
    def setUp(self):
        self.room_id = 'confirmed-room'
        self.future = Future()
        self.executor = Mock()
        self.executor.submit.return_value = self.future
        self.gateway = Mock()
        with patch('codol_rebuild.application.matches.service.ThreadPoolExecutor', return_value=self.executor):
            self.session = MatchSession(self.gateway, lambda: self.room_id)
        self.addCleanup(self.session.close)

    def test_requires_identity_and_confirmed_room_and_rejects_duplicate(self):
        self.assertFalse(self.session.begin('allocate', self.room_id))
        self.session.connected('existing-player')
        self.assertFalse(self.session.begin('allocate', 'other-room'))
        self.assertTrue(self.session.begin('allocate', self.room_id))
        self.assertFalse(self.session.begin('allocate', self.room_id))
        self.executor.submit.assert_called_once_with(self.gateway.allocate_game, 'existing-player', self.room_id)
        self.future.set_result({'allocationId': 'confirmed-allocation'})
        self.assertEqual(self.session.poll().data, {'allocationId': 'confirmed-allocation'})
        self.assertIsNone(self.session.poll())

    def test_late_result_after_leaving_does_not_publish_allocation(self):
        self.session.connected('existing-player')
        self.session.begin('allocate', self.room_id)
        self.room_id = None
        self.future.set_result({'allocationId': 'stale-allocation'})
        completion = self.session.poll()
        self.assertIsNone(completion.data)
        self.assertIsInstance(completion.error, OnlineError)

    def test_booking_failure_remains_failure(self):
        self.session.connected('existing-player')
        self.session.begin('book', self.room_id, 'allocation', 'security-id')
        failure = OnlineError('BOOKING_REJECTED', '预订被拒绝')
        self.future.set_exception(failure)
        completion = self.session.poll()
        self.assertIs(completion.error, failure)
        self.assertIsNone(completion.data)
        self.assertIsNone(self.session.poll())


class FormalStartNotificationTests(MatchSessionTests):
    """详情恢复只交付当前开局，原生确认前不会丢失结果。"""
    def test_ready_result_waits_for_native_acknowledgment(self):
        self.session.connected('player')
        self.assertIsNone(self.session.poll_start())
        self.future.set_result({'start': {'startId': 'start-one', 'status': 'ready', 'error': None}})
        first = self.session.poll_start()
        self.assertEqual(first['startId'], 'start-one')
        self.assertEqual(self.session.poll_start(), first)
        self.session.acknowledge_start(self.room_id, 'start-one')
        self.assertIsNone(self.session.poll_start())

    def test_old_room_result_cannot_start_new_room(self):
        self.session.connected('player')
        self.session.poll_start()
        self.room_id = 'new-room'
        self.future.set_result({'start': {'startId': 'old', 'status': 'ready'}})
        self.assertIsNone(self.session.poll_start())


class StartPushTests(unittest.TestCase):
    """开局推送与一次性恢复查询的顺序及请求次数。"""
    def setUp(self):
        MatchSessionTests.setUp(self)
        from codol_rebuild.application.realtime.dispatch import EventInbox
        self.inbox = EventInbox()
        self.session._inbox = self.inbox
        self.session.connected('player')

    def push(self, start_id='new', status='ready', room_id=None):
        self.inbox.receive({'type': 'room_start_updated', 'payload': {
            'roomId': room_id or self.room_id, 'startId': start_id,
            'status': status, 'error': '启动失败' if status == 'failed' else None}})

    def test_idle_and_starting_do_not_repeat_queries(self):
        self.session.poll_start()
        self.future.set_result({'start': {'startId': 'new', 'status': 'starting'}})
        for iteration in range(100):
            self.assertIsNone(self.session.poll_start())
        self.executor.submit.assert_called_once_with(self.gateway.room_detail, 'player', self.room_id)
        self.push()
        self.assertEqual(self.session.poll_start()['startId'], 'new')
        self.assertEqual(self.executor.submit.call_count, 1)

    def test_push_wins_over_late_query_and_duplicates(self):
        self.session.poll_start()
        self.push()
        self.assertEqual(self.session.poll_start()['startId'], 'new')
        self.future.set_result({'start': {'startId': 'old', 'status': 'ready'}})
        self.assertEqual(self.session.poll_start()['startId'], 'new')
        self.session.acknowledge_start(self.room_id, 'new')
        self.push()
        self.assertIsNone(self.session.poll_start())
        self.assertEqual(self.executor.submit.call_count, 1)

    def test_failure_and_late_message_after_leaving(self):
        self.push(status='failed')
        self.assertEqual(self.session.poll_start()['error'], '启动失败')
        previous_room = self.room_id
        self.room_id = None
        self.push(room_id=previous_room)
        self.assertIsNone(self.session.poll_start())
        self.future.set_result({'start': {'startId': 'old', 'status': 'ready'}})
        self.assertIsNone(self.session.poll_start())

    def test_reconnect_queries_once_and_recovers_missed_result(self):
        self.session.poll_start()
        self.future.set_result({'start': None})
        self.session.poll_start()
        recovery = Future()
        self.executor.submit.return_value = recovery
        self.inbox.receive({'type': 'realtime_reconnected', 'payload': {}})
        self.assertIsNone(self.session.poll_start())
        recovery.set_result({'start': {'startId': 'missed', 'status': 'ready'}})
        self.assertEqual(self.session.poll_start()['startId'], 'missed')
        self.session.acknowledge_start(self.room_id, 'missed')
        for iteration in range(100):
            self.assertIsNone(self.session.poll_start())
        self.assertEqual(self.executor.submit.call_count, 2)

    def test_notification_before_room_confirmation_is_recovered_on_entry(self):
        self.room_id = None
        self.push(room_id='joined')
        self.assertIsNone(self.session.poll_start())
        self.executor.submit.assert_not_called()
        self.room_id = 'joined'
        self.session.poll_start()
        self.future.set_result({'start': {'startId': 'new', 'status': 'ready'}})
        self.assertEqual(self.session.poll_start()['startId'], 'new')

    def test_query_failure_does_not_restart_polling(self):
        self.session.poll_start()
        self.future.set_exception(OnlineError('OFFLINE', '连接失败'))
        with self.assertLogs('codol_rebuild.application.matches.service', level='WARNING'):
            self.session.poll_start()
        for iteration in range(100):
            self.assertIsNone(self.session.poll_start())
        self.assertEqual(self.executor.submit.call_count, 1)
        self.push()
        self.assertEqual(self.session.poll_start()['startId'], 'new')
