"""会话与房间边界回归：生命周期顺序、身份保留、公共调度和单向依赖。"""
import ast
from pathlib import Path
import struct
import unittest
from unittest.mock import Mock, patch

from codol_rebuild.application.errors import OnlineError
from codol_rebuild.application.session.contracts import IdentityRecord, LoginCompletion
from codol_rebuild.application.session.service import OnlineSession
from codol_rebuild.application.room.service import RoomSession
from codol_rebuild.application.chat.service import ChatSession
from codol_rebuild.application.realtime.dispatch import EventInbox
from codol_rebuild.adapters.game.v1649370.session import online_session
from codol_rebuild.adapters.game.v1649370.session.session_bridge import GameSessionBridge
from codol_rebuild.adapters.game.v1649370.native_state import LOGIN, IDENTITY
from test_multiplayer_bridge import Memory


class OnlineSessionTests(unittest.TestCase):
    def test_login_preserves_order_and_close_keeps_connection_until_room_cleanup(self):
        calls = []
        gateway, store, rooms, realtime = Mock(), Mock(), Mock(), Mock()
        store.load.side_effect = lambda server: calls.append('load') or IdentityRecord('pl_saved', '10')
        gateway.login.side_effect = lambda *arguments: calls.append('login') or {'playerId': 'pl_saved', 'displayName': '服务端昵称'}
        rooms.bind_identity.side_effect = lambda identity: calls.append('bind')
        store.save.side_effect = lambda *arguments: calls.append('save')
        realtime.start.side_effect = lambda *arguments: calls.append('connect')
        rooms.restore.side_effect = lambda: calls.append('restore')
        rooms.close.side_effect = lambda: calls.append('leave')
        realtime.close.side_effect = lambda: calls.append('disconnect')
        session = OnlineSession(gateway, store, 'http://localhost', 'machine', rooms, realtime)
        try:
            self.assertTrue(session.begin_login('本地昵称'))
            session._pending.result(timeout=2)
            self.assertFalse(session.begin_login('重复昵称'))
            self.assertEqual(session.poll_login().data['displayName'], '服务端昵称')
            self.assertIsNone(session.poll_login())
            gateway.login.assert_called_once_with('machine', '本地昵称', 'pl_saved')
        finally:
            session.close()
        self.assertEqual(calls, ['load', 'login', 'bind', 'save', 'connect', 'restore', 'leave', 'disconnect'])

    def test_failed_persistence_retains_confirmed_identity_for_explicit_retry(self):
        gateway, store, rooms = Mock(), Mock(), Mock()
        gateway.login.return_value = {'playerId': 'pl_confirmed', 'displayName': '玩家'}
        store.load.return_value = None
        store.save.side_effect = [OSError('disk'), None]
        session = OnlineSession(gateway, store, 'http://localhost', 'machine', rooms)
        try:
            session.begin_login('玩家')
            session._pending.result(timeout=2)
            self.assertEqual(session.poll_login().error.code, 'LOCAL_IDENTITY_ERROR')
            rooms.restore.assert_not_called()
            session.begin_login('玩家')
            session._pending.result(timeout=2)
            self.assertIsNone(session.poll_login().error)
            self.assertEqual(gateway.login.call_args.args[-1], 'pl_confirmed')
        finally:
            session.close()

    def test_room_cleanup_failure_still_closes_connection_once(self):
        rooms, realtime = Mock(), Mock()
        rooms.close.side_effect = OnlineError('NETWORK_ERROR', '离房失败')
        session = OnlineSession(Mock(), Mock(), 'http://localhost', 'machine', rooms, realtime)
        with self.assertRaises(OnlineError):
            session.close()
        session.close()
        rooms.close.assert_called_once()
        realtime.close.assert_called_once()

    def test_room_receives_identity_but_does_not_load_it_or_control_connection(self):
        gateway, store, inbox = Mock(), Mock(), EventInbox()
        rooms = RoomSession(gateway, store, 'http://localhost', inbox)
        try:
            rooms.bind_identity(IdentityRecord('pl_self', '10'))
            self.assertIsNone(rooms.confirmed_room_id())
            rooms.restore()
            gateway.leave.assert_called_once_with('pl_self', '10')
            self.assertIsNone(rooms.confirmed_room_id())
            with self.assertRaises(ValueError):
                rooms.begin('login', '玩家')
            store.load.assert_not_called()
            gateway.login.assert_not_called()
        finally:
            rooms.close()

    def test_native_login_success_notifies_once_and_keeps_existing_gate_layout(self):
        process = Memory()
        state = 0x21000000
        process.write(state, bytes(4096))
        port = Mock(begin_login=Mock(return_value=True))
        port.poll_login.side_effect = [LoginCompletion(data={'playerId': 'pl_self', 'displayName': '玩家'}), None]
        connected = Mock()
        bridge = online_session.OnlineSessionBridge(process, port, state, connected, Mock())
        with patch.object(online_session, 'username', return_value='玩家'):
            bridge.poll()
            bridge.poll()
        port.begin_login.assert_called_once_with('玩家')
        connected.assert_called_once_with('pl_self', '玩家')
        self.assertEqual(struct.unpack('<I', process.read(state + LOGIN, 4))[0], 1)
        self.assertNotEqual(process.read(state + IDENTITY, 8), bytes(8))

    def test_public_frame_dispatches_without_any_speaker_adapter(self):
        bridge = GameSessionBridge.__new__(GameSessionBridge)
        process = Mock(main_thread_id=1, context=Mock(return_value=(100, bytearray(256))))
        bridge.process = process
        bridge.match_bridge = None
        bridge.session_traps = {0x2000: 'session_poll'}
        bridge.online_bridge = Mock()
        bridge.realtime_dispatcher = Mock()
        self.assertTrue(bridge.handle_exception(Mock(tid=1), 2147483651, 0x2000, {}))
        bridge.online_bridge.poll.assert_called_once()
        bridge.realtime_dispatcher.poll.assert_called_once()
        self.assertEqual(struct.unpack_from('<I', process.set_context.call_args.args[1], 184)[0], 0x2001)

    def test_chat_checks_application_membership_even_when_native_room_is_still_visible(self):
        inbox, sender = EventInbox(), Mock()
        current_room = Mock(return_value=None)
        chat = ChatSession(sender, inbox, current_room)
        chat.begin('迟到发送', 'room', '10')
        sender.send_message.assert_not_called()
        inbox.receive({'type': 'chat_message', 'payload': {'scope': 'room', 'roomId': '10'}})
        self.assertIsNone(chat.poll_message('10'))
        current_room.return_value = '10'
        self.assertIsNone(chat.poll_message('10'))

    def test_room_and_speaker_modules_do_not_depend_on_session_or_dispatch_implementations(self):
        package = Path(__file__).resolve().parents[1] / 'src/codol_rebuild'
        for relative in ('application/room/service.py', 'adapters/game/v1649370/rooms/room_session_bridge.py',
                         'adapters/game/v1649370/chat/broadcast.py', 'adapters/game/v1649370/session/online_session.py'):
            tree = ast.parse((package / relative).read_text(encoding='utf-8'))
            imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
            self.assertNotIn('codol_rebuild.application.session.service', imports)
            self.assertNotIn('codol_rebuild.application.realtime.dispatch', imports)
            if relative.endswith('online_session.py'):
                self.assertNotIn('codol_rebuild.adapters.game.v1649370.rooms.room_session_bridge', imports)
        tree = ast.parse((package / 'application/room/service.py').read_text(encoding='utf-8'))
        calls = [node.func.attr for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
        self.assertNotIn('login', calls)
        self.assertNotIn('start', calls)
        self.assertNotIn('load', calls)


if __name__ == '__main__':
    unittest.main()
