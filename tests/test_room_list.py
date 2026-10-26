"""大厅列表回归：显示数据、生命周期、分页取消与固定版本调用边。"""
from codol_rebuild.application.errors import OnlineError
from pathlib import Path
import struct
import unittest
from unittest.mock import Mock
import pefile
from codol_rebuild.adapters.game.v1649370.rooms import room_list as listing
from codol_rebuild.application.room.contracts import Completion


def room(identity='rm_one', mode='td'):
    return dict(roomId=identity, ownerPlayerId='pl_owner', ownerDisplayName='中文房主',
                playerCount=2, maxPlayers=12, hasPassword=True,
                map='mp_crash', mode=mode)


class Memory:
    """严格独立内存，不读写正在运行的客户端。"""
    def __init__(self):
        self.data = {}
        self.main_thread_id = 1
    def write(self, address, data):
        self.data.update({address+i: b for i, b in enumerate(data)})
    def read(self, address, size):
        try:
            return bytes(self.data[address+i] for i in range(size))
        except KeyError as error:
            raise OSError('unmapped') from error
    def word(self, address, value):
        self.write(address, struct.pack('<I', value))


class RoomListTests(unittest.TestCase):
    def test_wire_mapping_filters_after_collection_and_never_copies_password(self):
        base = 0x21000000
        first = room()
        first['password'] = 'must-not-leak'
        data, ids = listing.encode_rows([room('rm_other', 'sd'), first], 'td', base)
        self.assertEqual(ids, ('rm_one',))
        self.assertNotIn(b'must-not-leak', data)
        fields = {}
        for i in range(len(listing.FIELDS)):
            offset = listing.ROWS+i*36
            key, _, _, interface, kind, value = struct.unpack_from('<IIIIII', data, offset)
            text = lambda ptr: data[ptr-base:].split(b'\0')[0].decode('utf8')
            self.assertEqual(interface, 0)
            fields[text(key)] = text(value) if kind == 6 else value
        self.assertEqual(fields['name'], '中文房主')
        self.assertEqual(fields['password'], '*')
        self.assertEqual(fields['currentPlayers'], 2)
        self.assertEqual(fields['currentMaxPlayers'], 12)
        self.assertEqual(fields['enableShoutcast'], 0)
        self.assertEqual(text(struct.unpack_from('<I', data, listing.MODE_KEY)[0]), 'currentMatchMode')
        offset = listing.ROWS + len(listing.FIELDS)*36
        key, _, _, _, kind, value = struct.unpack_from('<IIIIII', data, offset)
        self.assertEqual((text(key), kind, text(value)), ('id', 6, 'td'))
        empty, ids = listing.encode_rows([first], 'sd', base)
        self.assertEqual(struct.unpack_from('<I', empty, listing.COUNT)[0], 0)
        self.assertEqual(ids, ())

    def test_nested_modes_and_password_flags_stay_with_each_room(self):
        """不同模式及密码状态不能在连续行或列表筛选后串用。"""
        base = 0x21000000
        rooms = [room('rm_spy', 'spy'), room('rm_sd', 'sd')]
        rooms[1]['hasPassword'] = False
        data, ids = listing.encode_rows(rooms, '', base)
        text = lambda ptr: data[ptr-base:].split(b'\0')[0].decode('utf8')
        self.assertEqual(ids, ('rm_spy', 'rm_sd'))
        for index, (mode, password) in enumerate((('spy', '*'), ('sd', ''))):
            row_start = listing.ROWS + index*listing.STRIDE
            nested = row_start + len(listing.FIELDS)*36
            self.assertEqual(text(struct.unpack_from('<I', data, nested+20)[0]), mode)
            password_field = row_start + listing.FIELDS.index('password')*36
            self.assertEqual(text(struct.unpack_from('<I', data, password_field+20)[0]), password)

    def test_oversize_and_embedded_null_are_rejected(self):
        with self.assertRaises(ValueError):
            listing.encode_rows([room()] * 1001, '', 0x21000000)
        bad = room(); bad['ownerDisplayName'] = 'a\0b'
        with self.assertRaises(ValueError):
            listing.encode_rows([bad], '', 0x21000000)

    def test_native_contract_and_all_detour_targets(self):
        image = Path(__file__).resolve().parents[1] / 'bin/v1649370/codol1649370-offline.exe'
        pe = pefile.PE(str(image))
        listing.validate_contract(lambda a, n: pe.get_data(a-0x400000, n))
        with self.assertRaises(ValueError):
            listing.validate_contract(lambda a, n: bytes(n))
        plan = listing.build_adapter(0x20000000, 0x21000000)
        for patch in plan['patches']:
            target = patch['va']+5+struct.unpack_from('<i', patch['after'], 1)[0]
            self.assertIn(target, plan['traps'])
            self.assertEqual(plan['code'][target-0x20000000], 0xcc)
        self.assertEqual(plan['patches'][0]['after'][0], 0xe8)
        self.assertLess(len(plan['code']), 4096)

    def make_bridge(self):
        memory, port, events = Memory(), Mock(), []
        port.poll_list.return_value = None
        port.begin_list.return_value = True
        bridge = listing.RoomListBridge(memory, port, events.append, lambda: '玩家', str)
        bridge.state = 0x21000000
        memory.write(bridge.state, bytes(listing.ROWS))
        view, frame, mode = 0x22000000, 0x23000000, 0x24000000
        memory.write(view, bytes(0x190))
        memory.word(view, 0x25000000)
        memory.write(view+4, b'\1')
        memory.word(view+0x24, 0x26000000)
        memory.word(view+0x1c, 0x26000000)
        memory.word(0xc77bb98, 0x27000000)
        memory.word(frame+8, view)
        memory.word(frame+12, 1)
        memory.write(mode, b'td\0')
        ctx = bytearray(204)
        struct.pack_into('<I', ctx, 180, frame)
        struct.pack_into('<I', ctx, 172, mode)
        bridge.handle('room_list_query', ctx, True)
        return bridge, memory, port, events

    def test_hidden_page_drops_late_results_and_releases_channel(self):
        bridge, memory, port, events = self.make_bridge()
        bridge.handle('room_list_hide', bytearray(204), True)
        port.poll_list.return_value = Completion('list', data={'rooms': [room()]})
        bridge.poll()
        port.cancel_list.assert_called_once()
        self.assertFalse(bridge.active)
        self.assertEqual(memory.read(bridge.state, 4), bytes(4))
        self.assertEqual(events, [])

    def test_changed_root_drops_completion_and_error_restores_empty_list(self):
        bridge, memory, port, _ = self.make_bridge()
        memory.word(bridge.view+0x24, 0)
        port.poll_list.return_value = Completion('list', data={'rooms': [room()]})
        bridge.poll()
        self.assertIsNone(bridge.view)
        bridge, memory, port, events = self.make_bridge()
        port.poll_list.return_value = Completion('list', error=OnlineError('NETWORK_ERROR', '断网'))
        bridge.poll()
        self.assertEqual(memory.read(bridge.state, 4), b'\1\0\0\0')
        self.assertEqual(memory.read(bridge.state+listing.COUNT, 4), bytes(4))
        self.assertEqual(events[-1]['error'], '断网')
