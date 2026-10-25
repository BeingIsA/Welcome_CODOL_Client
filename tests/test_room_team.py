"""换队命令、静默失败及迟到快照回归；不替代游戏 UI 验收。"""
import struct
import unittest
from unittest.mock import Mock

from codol_rebuild.adapters.game.v1649370.room import RoomTeamBridge
from codol_rebuild.application.room.contracts import Completion, IdentityRecord, RoomError
from codol_rebuild.application.room.service import RoomSession
from codol_rebuild.adapters.transport.http import HttpGateway
from test_refactor_equivalence import MemoryProcess


class TeamBridgeTests(unittest.TestCase):
    def setUp(self):
        self.process = MemoryProcess()
        self.port = Mock()
        self.state = ('rm_test', 0x30000000, 0x31000000)
        self.bridge = RoomTeamBridge(self.process, self.port, lambda: self.state)
        self.bridge.install()

    def command(self, value):
        frame = self.process.allocate()
        parameters = self.process.allocate()
        arguments = self.process.allocate()
        self.process.write(frame+40, struct.pack('<I', parameters))
        self.process.write(parameters+16, struct.pack('<II', arguments, 1))
        self.process.write(arguments+12, struct.pack('<II', 3, value))
        context = bytearray(716)
        struct.pack_into('<I', context, 180, frame)
        self.bridge.handle(context)
        return struct.unpack_from('<I', context, 184)[0]

    def test_mapping_no_optimistic_native_write_and_no_duplicate(self):
        for value, target in enumerate(('tf141', 'scar', 'spectator')):
            self.port.reset_mock()
            self.bridge.pending_room = None
            self.assertEqual(self.command(value), self.bridge.handled)
            self.command(value)
            self.port.begin_change_team.assert_called_once_with('rm_test', target)
        self.assertFalse(any(address >= 0x30000000 for address, data in self.process.writes))

    def test_failure_is_consumed_silently_and_next_request_allowed(self):
        self.command(2)
        self.port.poll_change_team.return_value = Completion('change_team', error=RoomError('ROOM_TEAM_FULL', '满员'))
        before = list(self.process.writes)
        self.assertIsNone(self.bridge.poll())
        self.assertEqual(self.process.writes, before)
        self.assertIsNone(self.bridge.pending_room)
        self.command(0)
        self.assertEqual(self.port.begin_change_team.call_count, 2)

    def test_success_and_stale_snapshot(self):
        self.command(2)
        snapshot = {'roomId': 'rm_test', 'members': []}
        self.port.poll_change_team.return_value = Completion('change_team', data=snapshot)
        self.assertIs(self.bridge.poll(), snapshot)
        self.command(0)
        self.state = None
        self.assertIsNone(self.bridge.poll())
        self.assertEqual(self.command(0), self.bridge.fallback)

    def test_reused_room_address_does_not_accept_departed_request(self):
        self.command(2)
        self.bridge.discard_pending_result()
        self.port.poll_change_team.return_value = Completion('change_team', data={'roomId': 'rm_test'})
        self.assertIsNone(self.bridge.poll())
        self.assertIsNone(self.bridge.pending_room)


class TeamSessionTests(unittest.TestCase):
    def test_member_snapshot_consumption_keeps_request_order(self):
        from concurrent.futures import Future
        realtime = Mock()
        session = RoomSession(Mock(), Mock(), 'http://server', 'fingerprint', realtime)
        self.addCleanup(session._executor.shutdown)
        session._members_pending = Future()
        self.assertFalse(session.begin_change_team('rm_test', 'spectator'))
        session._members_pending = None
        session._kick_pending = Future()
        self.assertFalse(session.begin_change_team('rm_test', 'spectator'))
        session._kick_pending = None
        session._team_pending = Future()
        self.assertIsNone(session.poll_event())
        realtime.poll_event.assert_not_called()
        self.assertFalse(session.begin_kick('rm_test', 'pl_other'))

    def test_failure_once_and_channel_released(self):
        gateway = Mock()
        gateway.change_team.side_effect = RoomError('ROOM_TEAM_FULL', '满员')
        session = RoomSession(gateway, Mock(), 'http://server', 'fingerprint')
        self.addCleanup(session._executor.shutdown)
        session._record = IdentityRecord('pl_me', 'rm_test')
        self.assertTrue(session.begin_change_team('rm_test', 'spectator'))
        session._team_pending.result(timeout=5)
        self.assertFalse(session.begin_change_team('rm_test', 'spectator'))
        result = session.poll_change_team()
        self.assertEqual(result.error.code, 'ROOM_TEAM_FULL')
        gateway.change_team.assert_called_once_with('pl_me', 'rm_test', 'spectator')
        self.assertIsNone(session.poll_change_team())
        self.assertTrue(session.begin_change_team('rm_test', 'tf141'))

    def test_http_contract(self):
        gateway = object.__new__(HttpGateway)
        gateway._request = Mock(return_value={'members': []})
        self.assertEqual(gateway.change_team('pl_me', 'rm_test', 'scar'), {'members': []})
        gateway._request.assert_called_once_with('PATCH', '/api/v1/rooms/rm_test/members/me/team', {'team': 'scar'}, 'pl_me')
