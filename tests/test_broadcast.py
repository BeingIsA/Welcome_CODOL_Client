"""大喇叭回归：发送方只由推送显示，分派隔离与固定版本 ABI。"""
from collections import deque
from pathlib import Path
import struct
import threading
import unittest
from unittest.mock import Mock
import pefile
import capstone
from codol_rebuild.application.broadcast.service import BroadcastSession
from codol_rebuild.application.realtime.dispatch import EventInbox, RealtimeDispatcher, RoutedConnection
from codol_rebuild.adapters.transport.http import HttpGateway
from codol_rebuild.adapters.game.v1649370 import broadcast


def server_message():
    """服务端原始广播信封，发送者也是接收者。"""
    return {'type': 'broadcast', 'payload': {
        'messageId': 'broadcast_one', 'senderId': 'pl_sender', 'senderName': '中文玩家',
        'speakerType': 'large', 'text': '一起玩吗？', 'sentAt': '2026-10-26T00:00:00+00:00'}}


class BroadcastTests(unittest.TestCase):
    def test_availability_requires_identity_and_current_connection(self):
        connection_available = Mock(return_value=False)
        session = BroadcastSession(Mock(), EventInbox(), connection_available)
        try:
            self.assertFalse(session.available())
            session.connected('pl_sender', '玩家')
            self.assertFalse(session.available())
            connection_available.return_value = True
            self.assertTrue(session.available())
            connection_available.return_value = False
            self.assertFalse(session.available())
        finally:
            session.close()

    def test_login_updates_native_availability_before_next_poll(self):
        memory = bytearray(64)
        process = Mock()
        process.write.side_effect = lambda address, data: memory.__setitem__(
            slice(address - 0x1000, address - 0x1000 + len(data)), data)
        process.read.side_effect = lambda address, size: bytes(
            memory[address - 0x1000:address - 0x1000 + size])
        port = Mock(available=Mock(return_value=True))
        bridge = broadcast.BroadcastBridge(process, port, Mock(), Mock())
        bridge.state = 0x1000
        bridge.connected('pl_sender', '玩家')
        port.connected.assert_called_once_with('pl_sender', '玩家')
        self.assertEqual(struct.unpack_from('<I', memory, broadcast.BROADCAST_AVAILABLE)[0], 1)
        process.write.assert_called_once_with(0x1000 + broadcast.BROADCAST_AVAILABLE,
                                             struct.pack('<I', 1))

    def test_http_uses_existing_endpoint_and_authenticated_identity(self):
        gateway = HttpGateway('http://127.0.0.1:8000')
        gateway._request = Mock(return_value={'messageId': 'broadcast_one'})
        self.assertEqual(gateway.send_broadcast('pl_sender', '一起玩吗？'), {'messageId': 'broadcast_one'})
        gateway._request.assert_called_once_with('POST', '/api/v1/broadcasts',
            {'speakerType': 'large', 'text': '一起玩吗？'}, 'pl_sender')

    def test_push_before_http_completion_displays_once_and_does_not_complete_send(self):
        inbox = EventInbox()
        entered, release = threading.Event(), threading.Event()

        def send(player_id, text):
            entered.set()
            if not release.wait(2):
                raise RuntimeError('测试请求未释放')
            return {'messageId': 'broadcast_one'}

        gateway = Mock(send_broadcast=Mock(side_effect=send))
        session = BroadcastSession(gateway, inbox, lambda: True)
        session.connected('pl_sender', '中文玩家')
        try:
            session.begin('一起玩吗？')
            self.assertTrue(entered.wait(1))
            self.assertIsNone(session.poll_sent())
            inbox.receive(server_message())
            self.assertEqual(session.poll_message(), server_message()['payload'])
            self.assertIsNone(session.poll_message())
            self.assertIsNone(session.poll_sent())
        finally:
            release.set()
            session.close()
        self.assertEqual(session.poll_sent(), {'messageId': 'broadcast_one'})
        self.assertIsNone(session.poll_sent())
        self.assertIsNone(session.poll_message())
        gateway.send_broadcast.assert_called_once_with('pl_sender', '一起玩吗？')

    def test_http_completion_never_creates_local_chat_message(self):
        inbox = EventInbox()
        gateway = Mock(send_broadcast=Mock(return_value={'messageId': 'broadcast_one'}))
        session = BroadcastSession(gateway, inbox, lambda: True)
        session.connected('pl_sender', '中文玩家')
        session.begin('一起玩吗？')
        session.close()
        self.assertEqual(session.poll_sent(), {'messageId': 'broadcast_one'})
        self.assertIsNone(session.poll_message())
        inbox.receive(server_message())
        self.assertEqual(session.poll_message()['senderId'], 'pl_sender')
        self.assertIsNone(session.poll_message())

    def test_room_wait_does_not_consume_broadcast_or_block_dispatch(self):
        first = {'type': 'room_members_updated', 'payload': {'roomId': '1'}}
        last = {'type': 'room_settings_updated', 'payload': {'roomId': '1'}}
        incoming = deque((first, server_message(), last))
        transport = Mock(poll_event=lambda: incoming.popleft() if incoming else None)
        rooms, broadcasts = EventInbox(), EventInbox()
        dispatcher = RealtimeDispatcher(transport)
        dispatcher.register(('room_members_updated', 'room_settings_updated'), rooms)
        dispatcher.register(('broadcast',), broadcasts)
        routed = RoutedConnection(transport, rooms)
        # 房间没有取消息，模拟等待请求完成；广播仍被独立分派。
        for dispatch_index in range(3):
            dispatcher.poll()
        self.assertEqual(broadcasts.poll_event(), server_message())
        self.assertEqual(routed.poll_event(), first)
        self.assertEqual(routed.poll_event(), last)
        self.assertIsNone(routed.poll_event())
        routed.start('pl_sender', 'machine')
        routed.close()
        transport.start.assert_called_once_with('pl_sender', 'machine')
        transport.close.assert_called_once_with()

    def test_native_object_fields_match_existing_chat_contract(self):
        base = 0x21000000
        data = broadcast.encode_message(server_message()['payload'], '12345', base)
        fields = {}

        def read_string(pointer):
            return data[pointer-base:].split(b'\0')[0].decode('utf-8')

        for field_index in range(len(broadcast.MESSAGE_FIELDS)):
            key, unused_first, unused_second, interface, kind, value = struct.unpack_from(
                '<IIIIII', data, broadcast.FIELDS + field_index * 36)
            self.assertEqual(interface, 0)
            fields[read_string(key)] = read_string(value) if kind == 6 else value
        self.assertEqual(fields['channelID'], 0)
        self.assertEqual(fields['loudSpeakerType'], 0)
        self.assertEqual(fields['senderID'], '12345')
        self.assertEqual(fields['senderName'], '中文玩家')
        self.assertEqual(fields['message'], '一起玩吗？')
        self.assertNotIn('selfSend', fields)  # 由原生接收逻辑依据真实 ID 判断。
        self.assertNotIn('messageId', fields)

    def test_send_parameters_support_managed_string_and_leave_small_speaker_alone(self):
        memory = bytearray(4096)
        struct.pack_into('<I', memory, 0x104, 0x200)
        struct.pack_into('<I', memory, 0x210, 0x300)
        struct.pack_into('<I', memory, 0x32C, 0x40)
        struct.pack_into('<I', memory, 0x330, 0x400)
        struct.pack_into('<I', memory, 0x400, 0x500)
        text = '中文发送'.encode('utf-8') + b'\0'
        memory[0x500:0x500+len(text)] = text
        read = lambda address, size: bytes(memory[address:address+size])
        self.assertEqual(broadcast.read_request(read, 0x100), '中文发送')
        struct.pack_into('<I', memory, 0x310, 1)
        self.assertIsNone(broadcast.read_request(read, 0x100))
        struct.pack_into('<II', memory, 0x32C, 6, 0x500)
        struct.pack_into('<I', memory, 0x310, 0)
        self.assertEqual(broadcast.read_request(read, 0x100), '中文发送')

    def test_fixed_image_contract_and_patch_boundaries(self):
        executable = Path(__file__).resolve().parents[1]/'bin/v1649370/codol1649370-offline.exe'
        image = pefile.PE(str(executable))
        for address, expected in broadcast.CONTRACT.items():
            self.assertEqual(image.get_data(address - image.OPTIONAL_HEADER.ImageBase, len(expected)),
                             expected, hex(address))
        entry = 0x20000000
        plan = broadcast.build_adapter(entry, 0x21000000)
        self.assertEqual(len(plan['patch']), 6)
        self.assertEqual(plan['code'][1:7], broadcast.CONTRACT[broadcast.SEND_HANDLER])
        decoded = list(capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32).disasm(plan['code'], entry))
        self.assertEqual(sum(instruction.size for instruction in decoded), len(plan['code']))
        targets = [int(instruction.op_str, 16) for instruction in decoded
                   if instruction.mnemonic == 'call']
        self.assertIn(0x7C4AB0, targets)
        self.assertIn(0x730570, targets)
        self.assertNotIn(0x4AB2C0, targets)  # 不再提交原生旧在线请求。


if __name__ == '__main__':
    unittest.main()
