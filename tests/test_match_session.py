"""开局资料本地交付回归，不代替原生客户端验收。"""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from codol_rebuild.application.errors import OnlineError
from codol_rebuild.application.matches.service import MatchSession
from codol_rebuild.application.realtime.dispatch import EventInbox


class MatchSessionTests(unittest.TestCase):
    """验证完整推送、本地交付和消息先后顺序。"""
    def setUp(self):
        self.room_id = 'confirmed-room'
        self.inbox = EventInbox()
        self.connection = {'allocationId': 'a' * 32, 'port': 27760,
                           'securityId': '01' * 8, 'securityKey': '02' * 16,
                           'serverUserId': '5', 'serverName': 'fixture',
                           'map': 'mp_killhouse', 'mode': 'dm', 'maxPlayers': 6}
        self.session = MatchSession(lambda: self.room_id, self.inbox)
        self.session.connected('player')

    def push(self, start_id='first', status='ready', room_id=None, connection=True):
        """完整通知代表已准备的本人连接；不完整通知不能启动。"""
        payload = {'roomId': room_id or self.room_id, 'startId': start_id,
                   'status': status, 'error': '启动失败' if status == 'failed' else None}
        if connection and status == 'ready':
            payload['connection'] = dict(self.connection)
        self.inbox.receive({'type': 'room_start_updated', 'payload': payload})


    def test_wait_precedes_ready_and_duplicate_wait_cannot_restart_it(self):
        self.push(status='starting')
        self.push()
        self.assertEqual(self.session.poll_start()['status'], 'starting')
        self.session.acknowledge_wait(self.room_id, 'first')
        self.assertEqual(self.session.poll_start()['status'], 'ready')
        self.push(status='starting')
        self.assertEqual(self.session.poll_start()['status'], 'ready')
        self.session.acknowledge_start(self.room_id, 'first')
        self.push(status='starting')
        self.assertIsNone(self.session.poll_start())

    def test_wait_alone_does_not_supply_connection(self):
        self.push(status='starting')
        self.assertEqual(self.session.poll_start()['status'], 'starting')
        self.assertFalse(self.session.begin('allocate', self.room_id))
        self.session.acknowledge_wait(self.room_id, 'first')
        self.push(status='starting')
        self.assertIsNone(self.session.poll_start())

    def test_requires_identity_and_prepared_current_room(self):
        self.assertFalse(self.session.begin('allocate', self.room_id))
        self.push()
        self.session.poll_start()
        self.session.connected(None)
        self.assertFalse(self.session.begin('allocate', self.room_id))
        self.session.connected('player')
        self.assertFalse(self.session.begin('allocate', 'other-room'))
        self.assertTrue(self.session.begin('allocate', self.room_id))
        self.assertFalse(self.session.begin('allocate', self.room_id))
        self.assertEqual(self.session.poll().data['allocationId'], self.connection['allocationId'])
        self.assertIsNone(self.session.poll())

    def test_host_stages_survive_acknowledgment_without_http(self):
        self.push()
        self.session.poll_start()
        self.session.acknowledge_start(self.room_id, 'first')
        self.assertIsNone(self.session.poll_start())
        self.assertTrue(self.session.begin('allocate', self.room_id))
        self.assertEqual(self.session.poll().data, dict(self.connection, startId='first'))
        self.assertTrue(self.session.begin('book', self.room_id,
                        self.connection['allocationId'], self.connection['securityId']))
        self.assertEqual(self.session.poll().data,
                         {'allocationId': self.connection['allocationId'], 'booked': True})
        self.assertTrue(self.session.begin('start', self.room_id,
                        self.connection['allocationId']))
        self.assertEqual(self.session.poll().data,
                         {'allocationId': self.connection['allocationId'], 'started': True})

    def test_member_connect_delivers_same_start_without_http(self):
        self.push()
        self.session.poll_start()
        self.assertTrue(self.session.begin('member_connect', self.room_id, start_id='first'))
        self.assertFalse(self.session.begin('member_connect', self.room_id, start_id='first'))
        self.assertEqual(self.session.poll().data, dict(self.connection, startId='first'))

    def test_request_identity_mismatch_is_failure(self):
        self.push()
        self.session.poll_start()
        requests = [dict(operation='member_connect', start_id='wrong'),
                    dict(operation='book', allocation_id='wrong', security_id=self.connection['securityId']),
                    dict(operation='book', allocation_id=self.connection['allocationId'], security_id='wrong'),
                    dict(operation='start', allocation_id='wrong')]
        for request in requests:
            with self.subTest(request=request):
                self.assertTrue(self.session.begin(room_id=self.room_id, **request))
                completion = self.session.poll()
                self.assertIsNone(completion.data)
                self.assertIsInstance(completion.error, OnlineError)

    def test_departure_rejects_pending_and_clears_prepared_data(self):
        self.push()
        self.session.poll_start()
        self.session.begin('allocate', self.room_id)
        previous_room = self.room_id
        self.room_id = None
        self.assertIsInstance(self.session.poll().error, OnlineError)
        self.session.poll_start()
        self.room_id = previous_room
        self.assertFalse(self.session.begin('allocate', self.room_id))

    def test_new_start_replaces_pending_and_rejects_late_old_push(self):
        self.push()
        self.session.poll_start()
        self.session.begin('allocate', self.room_id)
        self.connection['allocationId'] = 'b' * 32
        self.push(start_id='second')
        self.assertEqual(self.session.poll_start()['startId'], 'second')
        self.assertIsNone(self.session.poll())
        self.assertTrue(self.session.begin('member_connect', self.room_id, start_id='second'))
        self.assertEqual(self.session.poll().data['allocationId'], 'b' * 32)
        self.push(start_id='first')
        self.assertEqual(self.session.poll_start()['startId'], 'second')

    def test_failure_after_host_acknowledgment_revokes_success(self):
        self.push()
        self.session.poll_start()
        self.session.acknowledge_start(self.room_id, 'first')
        self.session.begin('allocate', self.room_id)
        self.push(status='failed')
        self.assertEqual(self.session.poll_start()['status'], 'failed')
        self.assertIsInstance(self.session.poll().error, OnlineError)
        self.assertFalse(self.session.begin('allocate', self.room_id))

    def test_returned_connection_cannot_mutate_next_stage(self):
        self.push()
        self.session.poll_start()
        self.session.begin('allocate', self.room_id)
        self.session.poll().data['allocationId'] = 'changed'
        self.session.begin('book', self.room_id, self.connection['allocationId'], self.connection['securityId'])
        self.assertTrue(self.session.poll().data['booked'])

    def test_ready_waits_for_acknowledgment_and_duplicate_does_not_restart(self):
        self.push()
        first = self.session.poll_start()
        self.assertEqual(self.session.poll_start(), first)
        self.session.acknowledge_start(self.room_id, 'first')
        self.push()
        self.assertIsNone(self.session.poll_start())

    def test_failure_and_late_message_after_leaving(self):
        self.push(status='failed')
        self.assertEqual(self.session.poll_start()['error'], '启动失败')
        previous_room = self.room_id
        self.room_id = None
        self.push(room_id=previous_room)
        self.assertIsNone(self.session.poll_start())


    def test_entry_reconnect_and_status_only_notification_cannot_start(self):
        self.assertIsNone(self.session.poll_start())
        self.inbox.receive({'type': 'realtime_reconnected'})
        self.push(connection=False)
        self.assertIsNone(self.session.poll_start())
        self.assertFalse(self.session.begin('allocate', self.room_id))
        self.push()
        self.assertEqual(self.session.poll_start()['startId'], 'first')

    def test_notification_before_room_confirmation_is_not_recovered(self):
        self.room_id = None
        self.push(room_id='confirmed-room')
        self.assertIsNone(self.session.poll_start())
        self.room_id = 'confirmed-room'
        self.assertIsNone(self.session.poll_start())
        self.assertFalse(self.session.begin('allocate', self.room_id))
