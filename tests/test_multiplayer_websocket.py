"""实时连接回归：真实握手、掉线重连、登录门控和退出清理。"""
import threading
import json
import time
import unittest
from unittest.mock import Mock
from dataclasses import replace

from websockets.sync.server import serve
from websockets.exceptions import ConnectionClosed

from codol_rebuild.adapters.transport.websocket import WebSocketConnection
from codol_rebuild.application.room.contracts import RoomError, RealtimeStatus
from codol_rebuild.application.room.service import RoomSession


def wait_for(predicate):
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('等待实时连接状态超时')


class RealtimeTests(unittest.TestCase):
    def test_application_dispatch_separates_receiving_from_settings_handling(self):
        from codol_rebuild.application.room.contracts import IdentityRecord
        realtime = Mock()
        session = RoomSession(Mock(), Mock(), 'http://localhost', 'fp', realtime)
        payload = {'roomId': 'rm_test', 'settings': {'mode': 'td'}}
        try:
            session._record = IdentityRecord('pl_test', room_id='rm_test')
            result = session._handle_room_settings_updated(payload)
            self.assertEqual(result.data, payload)
            realtime.poll_event.assert_not_called()
            session._handle_room_settings_updated = Mock(return_value=result)
            realtime.poll_event.side_effect = [
                {'type': 'room_settings_updated', 'payload': payload},
                {'type': 'unhandled', 'payload': {}}, None]
            self.assertIs(session.poll_event(), result)
            self.assertIsNone(session.poll_event())
            self.assertIsNone(session.poll_event())
            session._handle_room_settings_updated.assert_called_once_with(payload)
        finally:
            session._record = None
            session.close()

    def test_idle_connection_delivers_settings_in_received_order(self):
        from codol_rebuild.application.room.contracts import IdentityRecord
        messages = [{'type': 'room_settings_updated', 'payload': {
            'roomId': 'rm_test', 'revision': revision, 'settings': {'mode': mode}}}
            for revision, mode in ((7, 'td'), (6, 'dm'))]
        def handler(connection):
            for message in messages:
                connection.send(json.dumps(message))
            try:
                for message in connection:
                    pass
            except ConnectionClosed:
                pass
        with serve(handler, '127.0.0.1', 0) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            client = WebSocketConnection('http://127.0.0.1:' + str(server.socket.getsockname()[1]))
            session = RoomSession(Mock(), Mock(), 'http://localhost', 'fp', client)
            session._record = IdentityRecord('pl_test', room_id='rm_test')
            try:
                client.start('pl_test', 'fp')
                received = []
                def done():
                    event = session.poll_event()
                    if event is not None:
                        received.append(event.data)
                    return len(received) == 2
                wait_for(done)
                self.assertEqual(received, [m['payload'] for m in messages])
                session._gateway.room_detail.assert_not_called()
                session._gateway.update_settings.assert_not_called()
                session._record = IdentityRecord('pl_test')
                client._events.put(messages[0])
                self.assertIsNone(session.poll_event())
            finally:
                session._record = None
                session.close()
                server.shutdown()
                thread.join(3)

    def test_close_interrupts_reconnect_wait(self):
        from websockets.exceptions import ConnectionClosedOK
        client = WebSocketConnection('http://localhost')
        connection = Mock()
        connection.recv.side_effect = ConnectionClosedOK(None, None)
        client._connect = Mock(return_value=connection)
        client.start('pl_test', 'fp +&中文')
        wait_for(lambda: connection.close.called)
        client.close()
        self.assertEqual(client._connect.call_count, 1)
        self.assertFalse(client._thread.is_alive())

    def test_first_connection_failure_does_not_start_thread(self):
        client = WebSocketConnection('http://localhost')
        client._connect = Mock(side_effect=OSError('offline'))
        with self.assertRaises(RoomError):
            client.start('pl_test', 'fp +&中文')
        self.assertIsNone(client._thread)
        client.close()

    def test_connect_reconnect_and_close(self):
        connections = []
        paths = []
        def handler(connection):
            connections.append(connection)
            paths.append(connection.request.path)
            try:
                for message in connection:
                    pass
            except ConnectionClosed:
                pass
        with serve(handler, '127.0.0.1', 0) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            client = WebSocketConnection('http://127.0.0.1:' + str(server.socket.getsockname()[1]))
            try:
                client.start('pl_test', 'fp +&中文')
                wait_for(lambda: len(connections) == 1)
                client.start('pl_test', 'fp +&中文')
                connections[0].close(code=1012)
                wait_for(lambda: len(connections) == 2)
                self.assertEqual(paths, ['/ws?playerId=pl_test&machine_code=fp+%2B%26%E4%B8%AD%E6%96%87'] * 2)
            finally:
                client.close()
            wait_for(lambda: connections[-1].close_code is not None)
            self.assertFalse(client._thread.is_alive())
            server.shutdown()
            thread.join(3)

    def test_identity_rejection_invalidates_and_allows_restart(self):
        connections = []
        def handler(connection):
            connections.append(connection)
            if len(connections) == 1:
                connection.close(code=4401)
                return
            try:
                for message in connection:
                    pass
            except ConnectionClosed:
                pass
        with serve(handler, '127.0.0.1', 0) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            client = WebSocketConnection('http://127.0.0.1:' + str(server.socket.getsockname()[1]))
            try:
                client.start('pl_test', 'fp +&中文')
                wait_for(lambda: len(connections) == 1)
                wait_for(lambda: client.status() == RealtimeStatus.INVALIDATED)
                self.assertEqual(client.status(), RealtimeStatus.INVALIDATED)
                self.assertIsNone(client._thread)
                # 失效后再次 start 应真正重建连接，而不是因线程残留静默返回。
                client.start('pl_test', 'fp +&中文')
                wait_for(lambda: len(connections) == 2)
                self.assertEqual(client.status(), RealtimeStatus.CONNECTED)
            finally:
                client.close()
            server.shutdown()
            thread.join(3)

    def test_disconnect_without_identity_rejection_reconnects(self):
        connections = []
        def handler(connection):
            connections.append(connection)
            try:
                for message in connection:
                    pass
            except ConnectionClosed:
                pass
        with serve(handler, '127.0.0.1', 0) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            client = WebSocketConnection('http://127.0.0.1:' + str(server.socket.getsockname()[1]))
            try:
                client.start('pl_test', 'fp +&中文')
                wait_for(lambda: len(connections) == 1)
                connections[0].close(code=1012)
                wait_for(lambda: len(connections) == 2)
                self.assertEqual(client.status(), RealtimeStatus.CONNECTED)
            finally:
                client.close()
            server.shutdown()
            thread.join(3)

    def test_failed_handshake_prevents_login_and_retry_preserves_identity(self):
        gateway, store, realtime = Mock(), Mock(), Mock()
        store.load.return_value = None
        gateway.login.return_value = {'playerId': 'pl_test'}
        realtime.start.side_effect = [RoomError('WS_CONNECT_FAILED', '失败'), None]
        session = RoomSession(gateway, store, 'http://localhost', 'fp', realtime)
        def login():
            session.begin('login', '测试')
            result = []
            def done():
                item = session.poll()
                if item is not None:
                    result.append(item)
                return bool(result)
            wait_for(done)
            return result[0]
        try:
            self.assertEqual(login().error.code, 'WS_CONNECT_FAILED')
            self.assertIsNone(login().error)
            self.assertEqual(gateway.login.call_args.args[-1], 'pl_test')
            gateway.leave.side_effect = RoomError('NETWORK_ERROR', '失败')
            session._record = replace(session._record, room_id='rm_test')
            with self.assertRaises(RoomError):
                session.close()
            realtime.close.assert_called_once()
        finally:
            session.close()


if __name__ == '__main__':
    unittest.main()
