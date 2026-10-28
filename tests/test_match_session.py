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
