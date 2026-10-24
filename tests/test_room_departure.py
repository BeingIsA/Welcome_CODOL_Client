"""被动离房应用回归；不等同于原生页面验收。"""
from concurrent.futures import Future
import unittest
from unittest.mock import Mock

from codol_rebuild.application.room.contracts import Completion, IdentityRecord
from codol_rebuild.application.room.service import RoomSession


class RoomDepartureTests(unittest.TestCase):
    def test_active_and_passive_departures_clear_same_record_with_distinct_requests(self):
        for operation in ('leave', 'room_kicked', 'room_closed'):
            with self.subTest(operation=operation):
                gateway, store = Mock(), Mock()
                session = RoomSession(gateway, store, 'http://localhost', 'test')
                session._record = IdentityRecord('pl_me', 'rm_current', uncertain_join=True)
                session._is_owner = True
                gateway.room_detail.return_value = {
                    'roomId': 'rm_current', 'members': [{'playerId': 'pl_me'}]}
                try:
                    if operation == 'leave':
                        self.assertEqual(session._leave(), {'left': True})
                        gateway.leave.assert_called_once_with('pl_me', 'rm_current')
                    else:
                        result = session._complete_room_departure(operation, 'rm_current')
                        self.assertEqual(result.operation, operation)
                        gateway.leave.assert_not_called()
                        gateway.room_detail.assert_not_called()
                    self.assertEqual(session._record, IdentityRecord('pl_me'))
                    self.assertIsNone(session._is_owner)
                    store.save.assert_called_once_with('http://localhost', IdentityRecord('pl_me'))
                    request_count = gateway.leave.call_count
                    session.close()
                    self.assertEqual(gateway.leave.call_count, request_count)
                finally:
                    session.close()

    def test_active_departure_save_failure_still_propagates(self):
        gateway, store = Mock(), Mock()
        store.save.side_effect = OSError('disk failure')
        session = RoomSession(gateway, store, 'http://localhost', 'test')
        session._record = IdentityRecord('pl_me', 'rm_current')
        session._is_owner = True
        try:
            with self.assertRaisesRegex(OSError, 'disk failure'):
                session._leave()
            gateway.leave.assert_called_once_with('pl_me', 'rm_current')
            self.assertEqual(session._record, IdentityRecord('pl_me'))
            # 保留原主动退出的顺序：保存抛错时尚未执行房主状态清理。
            self.assertTrue(session._is_owner)
        finally:
            session.close()

    def test_both_events_clear_identity_without_http_leave_and_ignore_duplicates(self):
        for event_type in ('room_kicked', 'room_closed'):
            with self.subTest(event_type=event_type):
                gateway, store, realtime = Mock(), Mock(), Mock()
                session = RoomSession(gateway, store, 'http://localhost', 'test', realtime)
                session._record = IdentityRecord('pl_me', 'rm_current')
                session._is_owner = True
                realtime.poll_event.return_value = {'type': event_type,
                                                    'payload': {'roomId': 'rm_current'}}
                try:
                    self.assertIsNone(session.poll_event())
                    session._departure_pending.result(timeout=3)
                    self.assertEqual(session.poll_event(), Completion(event_type, {'roomId': 'rm_current'}))
                    self.assertEqual(session._record, IdentityRecord('pl_me'))
                    self.assertIsNone(session._is_owner)
                    store.save.assert_called_once_with('http://localhost', IdentityRecord('pl_me'))
                    self.assertIsNone(session.poll_event())
                    session.close()
                    gateway.leave.assert_not_called()
                finally:
                    session.close()

    def test_other_room_does_not_clear_current_membership(self):
        session = RoomSession(Mock(), Mock(), 'http://localhost', 'test', Mock())
        session._record = IdentityRecord('pl_me', 'rm_current')
        try:
            session._handle_room_departure('room_closed', {'roomId': 'rm_old'})
            self.assertIsNone(session._departure_pending)
            self.assertEqual(session._record.room_id, 'rm_current')
            session._store.save.assert_not_called()
        finally:
            session._record = None
            session.close()

    def test_pending_kick_and_settings_results_are_preserved(self):
        session = RoomSession(Mock(), Mock(), 'http://localhost', 'test', Mock())
        session._record = IdentityRecord('pl_me', 'rm_current')
        session._kick_pending = Future()
        session._settings_pending = Future()
        session._kick_pending.set_result(Completion('kick', {'roomId': 'rm_current'}))
        session._settings_pending.set_result(Completion('settings'))
        try:
            session._handle_room_departure('room_kicked', {'roomId': 'rm_current'})
            session._departure_pending.result(timeout=3)
            self.assertEqual(session.poll_kick().operation, 'kick')
            self.assertEqual(session.poll().operation, 'settings')
            self.assertFalse(session.begin('join', '成员', room_id='rm_other'))
            self.assertIsNotNone(session.poll_event())
        finally:
            session.close()

    def test_save_failure_still_delivers_departure_and_keeps_memory_cleared(self):
        store = Mock()
        store.save.side_effect = OSError('disk failure')
        session = RoomSession(Mock(), store, 'http://localhost', 'test', Mock())
        session._record = IdentityRecord('pl_me', 'rm_current')
        session._is_owner = True
        try:
            with self.assertLogs('codol_rebuild.application.room.service', level='ERROR'):
                result = session._complete_room_departure('room_closed', 'rm_current')
            self.assertEqual(result.operation, 'room_closed')
            self.assertIsNone(session._record.room_id)
            self.assertIsNone(session._is_owner)
        finally:
            session.close()
