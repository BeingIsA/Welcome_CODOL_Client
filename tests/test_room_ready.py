"""准备命令传输和异步用例回归；不代表原生 UI 接入通过。"""
import unittest
import struct
from concurrent.futures import Future
from unittest.mock import Mock

from codol_rebuild.application.session.contracts import IdentityRecord
from codol_rebuild.application.room.service import RoomSession
from codol_rebuild.adapters.transport.http import HttpGateway
from codol_rebuild.adapters.game.v1649370.rooms.room import RoomReadyBridge
from codol_rebuild.application.room.contracts import Completion
from test_refactor_equivalence import MemoryProcess


class ReadySessionTests(unittest.TestCase):
    def test_earlier_member_snapshot_is_consumed_before_ready_result(self):
        realtime = Mock()
        session = RoomSession(Mock(), Mock(), 'http://server', realtime)
        self.addCleanup(session._executor.shutdown)
        session._record = IdentityRecord('pl_me', 'rm_test')
        session._members_pending = Future()
        session._members_pending.set_result(Completion('room_members_updated', data={'roomId': 'rm_test'}))
        session._ready_pending = Future()
        session._ready_pending.set_result(Completion('change_ready', data={'roomId': 'rm_test'}))
        self.assertIsNone(session.poll_change_ready())
        self.assertEqual(session.poll_event().operation, 'room_members_updated')
        self.assertEqual(session.poll_change_ready().operation, 'change_ready')
        realtime.poll_event.assert_not_called()

    def test_ready_cancel_deliver_confirmed_snapshot_once(self):
        gateway = Mock()
        session = RoomSession(gateway, Mock(), 'http://server')
        self.addCleanup(session._executor.shutdown)
        session._record = IdentityRecord('pl_me', 'rm_test')
        for ready in (True, False):
            snapshot = {'roomId': 'rm_test', 'members': [
                {'playerId': 'pl_me', 'readyState': ready}]}
            gateway.change_ready.return_value = snapshot
            gateway.member_profile.return_value = None
            self.assertTrue(session.begin_change_ready('rm_test', ready))
            session._ready_pending.result(timeout=5)
            self.assertFalse(session.begin_change_ready('rm_test', ready))
            completion = session.poll_change_ready()
            self.assertEqual(completion.operation, 'change_ready')
            self.assertIs(completion.data, snapshot)
            self.assertIsNone(session.poll_change_ready())
        self.assertEqual(gateway.change_ready.call_count, 2)
        self.assertEqual([call.args for call in gateway.change_ready.call_args_list],
                         [('pl_me', 'rm_test', True), ('pl_me', 'rm_test', False)])

    def test_http_sends_boolean_absolute_state(self):
        gateway = object.__new__(HttpGateway)
        gateway._request = Mock(return_value={'members': []})
        for ready in (True, False):
            gateway.change_ready('pl_me', 'rm_test', ready)
            gateway._request.assert_called_with('PATCH',
                '/api/v1/rooms/rm_test/members/me/ready', {'ready': ready}, 'pl_me')


class ReadyBridgeTests(unittest.TestCase):
    def setUp(self):
        self.process = MemoryProcess()
        self.port = Mock()
        self.room_state = ('rm_test', 0x30000000, 0x31000000)
        self.applying_members = False
        self.bridge = RoomReadyBridge(self.process, self.port, lambda: self.room_state,
                                      lambda: self.applying_members)
        self.traps = self.bridge.install()

    def invoke(self, phase, ready=False):
        frame = self.process.allocate()
        parameters = self.process.allocate()
        arguments = self.process.allocate()
        self.process.write(frame+40, struct.pack('<I', parameters))
        self.process.write(parameters+16, struct.pack('<I', arguments))
        self.process.write(arguments+16, bytes([ready]))
        context = bytearray(716)
        struct.pack_into('<I', context, 180, frame)
        self.bridge.handle(phase, context)
        return frame, struct.unpack_from('<I', context, 184)[0]

    def test_prepare_cancel_and_no_optimistic_write(self):
        for ready in (True, False):
            self.port.reset_mock()
            self.bridge.pending_room = None
            self.invoke('set_ready', ready)
            self.invoke('set_ready', ready)
            self.port.begin_change_ready.assert_called_once_with('rm_test', ready)
        self.assertFalse(any(address >= 0x30000000 for address, value in self.process.writes))

    def test_initialization_and_echo_do_not_submit_commands(self):
        self.applying_members = True
        self.invoke('set_ready', True)
        self.invoke('set_ready', False)
        self.port.begin_change_ready.assert_not_called()

    def test_getter_reads_confirmed_member_and_outside_room_uses_original(self):
        for ready in (False, True, False):
            self.process.write(self.room_state[1]+0xE3F8, bytes([ready]))
            frame, continuation = self.invoke('get_ready')
            self.assertEqual(continuation, self.bridge.continuations['get_ready'][0])
            self.assertEqual(struct.unpack('<I', self.process.read(frame+28, 4))[0], int(ready))
        self.room_state = None
        for phase in ('get_ready', 'set_ready'):
            self.assertEqual(self.invoke(phase)[1], self.bridge.continuations[phase][1])

    def test_success_delivered_once_and_departure_discards_result(self):
        self.invoke('set_ready', True)
        result = Completion('change_ready', data={'roomId': 'rm_test'})
        self.port.poll_change_ready.return_value = result
        self.assertIs(self.bridge.poll(), result)
        self.assertIsNone(self.bridge.poll())
        self.invoke('set_ready', False)
        self.bridge.discard_pending_result()
        self.assertIsNone(self.bridge.poll())

    def test_only_two_gfx_slots_are_replaced(self):
        native_writes = [(address, value) for address, value in self.process.writes
                         if address < 0x10000000]
        self.assertEqual([address for address, value in native_writes], [0xDDD71C, 0xDDD728])
        self.assertTrue(all(len(value) == 4 for address, value in native_writes))
