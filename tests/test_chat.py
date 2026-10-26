"""普通聊天回归：单次发送、频道隔离、自身成员核对及原生补丁边界。"""
from dataclasses import replace
from pathlib import Path
import struct
import unittest
from unittest.mock import Mock, patch
import capstone
import pefile

from codol_rebuild.application.chat.service import ChatSession
from codol_rebuild.application.realtime.dispatch import EventInbox, RealtimeDispatcher
from codol_rebuild.application.session.contracts import IdentityRecord
from codol_rebuild.application.room.service import RoomSession
from codol_rebuild.adapters.game.v1649370.chat import chat, chat_message
from codol_rebuild.adapters.game.v1649370.rooms.room_session_bridge import RoomSessionBridge
from codol_rebuild.adapters.game.v1649370.session.session_tick import build_tick


def server_message(scope='lobby', room_id=None):
    payload = {'messageId': 'chat_one', 'scope': scope, 'senderId': 'pl_other',
               'senderName': '中文玩家', 'text': '一起玩吗？',
               'sentAt': '2026-10-26T00:00:00+00:00'}
    if room_id is not None:
        payload['roomId'] = room_id
    return {'type': 'chat_message', 'payload': payload}


class ChatTests(unittest.TestCase):
    def test_send_once_uses_scope_and_unique_request_id_without_local_echo(self):
        sender, inbox = Mock(), EventInbox()
        session = ChatSession(sender, inbox, lambda: '10')
        session.begin('大厅', 'lobby', '10')
        session.begin('房间', 'room', '10')
        frames = [call.args[0] for call in sender.send_message.call_args_list]
        self.assertEqual([frame['type'] for frame in frames], ['chat_send'] * 2)
        self.assertNotEqual(frames[0]['requestId'], frames[1]['requestId'])
        self.assertEqual(frames[0]['payload'], {'scope': 'lobby', 'text': '大厅'})
        self.assertEqual(frames[1]['payload'], {'scope': 'room', 'text': '房间', 'roomId': '10'})
        self.assertIsNone(session.poll_message('10'))
        session.begin('房外', 'room', None)
        self.assertEqual(sender.send_message.call_count, 2)

    def test_lobby_and_current_room_display_old_room_is_consumed_without_replay(self):
        inbox = EventInbox()
        session = ChatSession(Mock(), inbox, lambda: '10')
        for scope, message_room, current_room, accepted in (
                ('lobby', None, None, True), ('lobby', None, '20', True),
                ('room', '10', '10', True), ('room', '10', '20', False),
                ('room', '10', None, False), ('room', None, None, False)):
            with self.subTest(scope=scope, message_room=message_room, current_room=current_room):
                event = server_message(scope, message_room)
                inbox.receive(event)
                self.assertEqual(session.poll_message(current_room), event['payload'] if accepted else None)
                self.assertIsNone(session.poll_message(message_room))

    def test_send_result_is_discarded_and_only_push_is_displayed(self):
        incoming = [
            {'type': 'chat_result', 'payload': {'error': {'code': 'NOT_ROOM_MEMBER'}}},
            server_message()]
        connection = Mock(poll_event=Mock(side_effect=incoming))
        inbox = EventInbox()
        dispatcher = RealtimeDispatcher(connection)
        dispatcher.register(('chat_message',), inbox)
        session = ChatSession(connection, inbox, lambda: '10')
        dispatcher.poll()
        self.assertIsNone(session.poll_message(None))
        dispatcher.poll()
        self.assertEqual(session.poll_message(None), incoming[1]['payload'])
        self.assertIsNone(session.poll_message(None))

    def test_room_identity_during_login_join_leave_or_departure_is_not_confirmed(self):
        session = RoomSession(Mock(), Mock(), 'http://localhost')
        try:
            identity = IdentityRecord('pl_self', '10')
            session._record = identity
            self.assertEqual(session.confirmed_room_id(), '10')
            for field in ('uncertain_create', 'uncertain_join'):
                session._record = replace(identity, **{field: True})
                self.assertIsNone(session.confirmed_room_id())
            session._record = identity
            for field, value in (('_pending', object()), ('_departure_pending', object()), ('_closed', True)):
                original = getattr(session, field)
                setattr(session, field, value)
                self.assertIsNone(session.confirmed_room_id())
                setattr(session, field, original)
        finally:
            session._record = None
            session.close()

    def test_native_room_query_requires_matching_snapshot_and_self_membership(self):
        port = Mock(confirmed_room_id=Mock(return_value='10'))
        bridge = RoomSessionBridge(Mock(), port, Mock())
        bridge.identity_ready = True
        bridge.player_id = 'pl_self'
        bridge.current_room_id = '10'
        bridge.join_snapshot = ({'roomId': '10'}, [{'playerId': 'pl_self'}], [], [], [])
        self.assertEqual(bridge.displayed_room_id(), '10')
        for field, value in (('pending', ('leave', 1, 2)), ('join_cleanup', True),
                             ('applying_members', True),
                             ('identity_ready', False), ('current_room_id', '20')):
            original = getattr(bridge, field)
            setattr(bridge, field, value)
            self.assertIsNone(bridge.displayed_room_id(), field)
            setattr(bridge, field, original)
        bridge.join_snapshot[1].clear()
        self.assertIsNone(bridge.displayed_room_id())
        bridge.join_snapshot[1].append({'playerId': 'pl_other'})
        self.assertIsNone(bridge.displayed_room_id())
        bridge.join_snapshot[1].append({'playerId': 'pl_self'})
        self.assertEqual(bridge.displayed_room_id(), '10')
        port.confirmed_room_id.assert_not_called()

    def test_native_fields_select_actual_channel_and_disable_speaker_decoration(self):
        for scope, channel in (('lobby', 0), ('room', 3)):
            state = 0x21000000
            data = chat_message.encode_message(server_message(scope, '10')['payload'], '12345', state, channel, -1)
            fields = {}
            def text(pointer):
                return data[pointer - state:].split(b'\0')[0].decode('utf-8')
            for field_index in range(6):
                name, unused_first, unused_second, interface, kind, value = struct.unpack_from(
                    '<IIIIII', data, chat_message.FIELDS + field_index * 36)
                fields[text(name)] = text(value) if kind == 6 else value
            self.assertEqual(fields['channelID'], channel)
            self.assertEqual(fields['loudSpeakerType'], 0xFFFFFFFF)
            self.assertEqual(fields['message'], '一起玩吗？')

    def test_dispatch_parameters_reuse_extracted_gfx_values(self):
        memory = bytearray(4096)
        text_bytes = '聊天&lt;正文&gt;'.encode('utf-8') + b'\0'
        memory[0x500:0x500 + len(text_bytes)] = text_bytes
        read = lambda address, size: bytes(memory[address:address + size])
        for channel, scope in ((0, 'lobby'), (3, 'room'), (5, None), (9, None)):
            struct.pack_into('<II', memory, 0x110, channel, 0x500)
            self.assertEqual(chat.read_request(read, 0x100), ('聊天&lt;正文&gt;', scope) if scope else None)

    def test_send_preserves_native_tail_and_unhandled_channel_fallback(self):
        for identity, request in ((None, ('正文', 'lobby')), ('pl_self', None),
                                  ('pl_self', ('正文', 'lobby')), ('pl_self', ('正文', 'room'))):
            context = bytearray(256)
            struct.pack_into('<I', context, 196, 0x1000)
            process = Mock(main_thread_id=1, context=Mock(return_value=(100, context)))
            port = Mock()
            bridge = chat.ChatBridge(process, port, lambda: '10')
            bridge.player_id = identity
            bridge.traps = {0x2000: 'send'}
            with patch.object(chat, 'read_request', return_value=request):
                bridge.handle(Mock(tid=1), 0x2000)
            if identity is not None and request is not None:
                port.begin.assert_called_once_with(*request, '10')
                self.assertEqual(struct.unpack_from('<I', context, 184)[0], chat.SEND_TAIL)
            else:
                port.begin.assert_not_called()
                self.assertEqual(struct.unpack_from('<I', context, 184)[0], 0x2001)

    def test_old_room_is_dropped_before_any_native_memory_access(self):
        inbox = EventInbox()
        inbox.receive(server_message('room', '10'))
        process = Mock(main_thread_id=1, context=Mock(return_value=(100, bytearray(256))))
        bridge = chat.ChatBridge(process, ChatSession(Mock(), inbox, lambda: '10'), lambda: '20')
        bridge.player_id = 'pl_self'
        bridge.traps = {0x2000: 'poll'}
        bridge.handle(Mock(tid=1), 0x2000)
        process.read.assert_not_called()
        process.write.assert_not_called()

    def test_patch_exactly_covers_dispatch_and_common_renderer_abi(self):
        image = pefile.PE(str(Path(__file__).resolve().parents[1] / 'bin/v1649370/codol1649370-offline.exe'))
        for address, expected in chat.CONTRACT.items():
            self.assertEqual(image.get_data(address - image.OPTIONAL_HEADER.ImageBase, len(expected)), expected)
        entry = 0x20000000
        plan = chat.build_adapter(entry, 0x21000000)
        self.assertEqual(len(plan['patch']), 7)
        self.assertEqual(plan['code'][1:8], chat.CONTRACT[chat.SEND_DISPATCH])
        decoded = list(capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32).disasm(plan['code'], entry))
        self.assertEqual(sum(instruction.size for instruction in decoded), len(plan['code']))
        self.assertEqual(decoded[3].op_str, hex(chat.SEND_DISPATCH + 7))
        calls = [int(instruction.op_str, 16) for instruction in decoded if instruction.mnemonic == 'call']
        self.assertEqual(calls, [0x72FD70, 0x436750, 0x730570, 0x436660])

    def test_chat_poll_runs_after_room_events_and_native_completion(self):
        entry, chat_poll = 0x20000000, 0x22000000
        plan = build_tick(entry, 0x21000000, 0x23000000, 0x24000000,
                          {'create_complete': 0x25000000, 'leave_complete': 0x25000100,
                           'settings_apply': 0x25000200}, chat_poll=chat_poll)
        decoded = list(capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32).disasm(plan['code'], entry))
        call_address = next(instruction.address for instruction in decoded
                            if instruction.mnemonic == 'call' and instruction.op_str == hex(chat_poll))
        self.assertGreater(call_address, max(plan['traps']))


if __name__ == '__main__':
    unittest.main()
