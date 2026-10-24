"""玩家资料格式与原生调用计划回归；静态测试不替代双客户端实机验收。"""
import base64
from pathlib import Path
import struct
import unittest
from concurrent.futures import Future
from unittest.mock import Mock

import capstone
import pefile

from codol_rebuild.adapters.game.v1649370 import member_profile as profile, layout


def sample_profile():
    """产生两个合法 DDL 头与可区分的展示快照。"""
    values = {'public': struct.pack('<II', 233, 0x4a6cc345)+bytes(32756),
              'secure': struct.pack('<II', 19, 0xa34378b4)+bytes(32756),
              'tf141': bytes([2])*160, 'scar': bytes([1])*160}
    return {'contentVersion': profile.VERSION,
            **{key: base64.b64encode(value).decode() for key, value in values.items()}}


class MemberProfileTests(unittest.TestCase):
    def test_query_uses_private_container_and_original_notification(self):
        from test_refactor_equivalence import MemoryProcess
        process = MemoryProcess()
        identity = b'abcdefgh'
        snapshot = profile.decode_snapshot(sample_profile())
        cache = {identity: snapshot}
        query = profile.ProfileQueryBridge(process, lambda: cache)
        traps = query.install()
        self.assertEqual(set(traps.values()), {'profile_query', 'profile_initialized'})
        frame = process.allocate(4096)
        process.write(frame+0x18, identity)
        ctx = bytearray(716)
        struct.pack_into('<I', ctx, 196, frame)
        query.handle('profile_query', ctx)
        self.assertEqual(struct.unpack_from('<I', ctx, 184)[0], query.plan['initialize'])
        # 此处只模拟原生初始化已经完成，不能替代执行原生构造器的实机验证。
        query.handle('profile_initialized', ctx)
        self.assertEqual(process.read(query.container+0xA0, 32764), snapshot['public'])
        self.assertEqual(process.read(query.container+0x111E0, 32764), snapshot['secure'])
        self.assertEqual(struct.unpack_from('<I', ctx, 156)[0], query.container)
        self.assertEqual(struct.unpack_from('<I', ctx, 184)[0], 0x68D5CC)
        cache.clear()
        query.handle('profile_query', ctx)
        self.assertEqual(struct.unpack_from('<I', ctx, 184)[0], query.plan['fallback'])

    def test_query_stub_branches_and_constructor_stack_contract(self):
        decoder = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
        for entry in (0x20000000, 0xf0000000):
            plan = profile.build_query(entry, 0x21000000, 0x22000000)
            rows = list(decoder.disasm(plan['code'], entry))
            self.assertEqual(sum(row.size for row in rows), len(plan['code']))
            calls = [int(row.op_str, 16) for row in rows if row.mnemonic == 'call']
            self.assertEqual(calls, [0x7F8E10])
            jumps = [int(row.op_str, 16) for row in rows if row.mnemonic == 'jmp']
            self.assertEqual(jumps, [profile.QUERY_SITE+5, 0x68D5CC])
        pe = pefile.PE(str(Path(__file__).resolve().parents[1]/'bin/v1649370/codol1649370-offline.exe'))
        self.assertEqual(pe.get_data(0x7F8F3D-0x400000, 3), bytes.fromhex('c20800'))

    def test_background_member_refresh_loads_each_available_profile(self):
        from codol_rebuild.application.room.contracts import IdentityRecord
        from codol_rebuild.application.room.service import RoomSession
        gateway = Mock()
        gateway.room_detail.return_value = {'roomId': 'rm_test', 'members': [
            {'playerId': 'pl_a'},
            {'playerId': 'pl_b'}]}
        gateway.member_profile.side_effect = [sample_profile(), sample_profile()]
        session = RoomSession(gateway, Mock(), 'http://localhost', 'fingerprint', Mock())
        try:
            result = session._refresh_members(IdentityRecord('pl_a', 'rm_test'))
            self.assertIsNone(result.error)
            self.assertEqual(result.operation, 'room_members_updated')
            self.assertEqual(gateway.member_profile.call_args_list,
                [unittest.mock.call('pl_a', 'rm_test', 'pl_a'),
                 unittest.mock.call('pl_a', 'rm_test', 'pl_b')])
            self.assertTrue(all(m['profile'] == sample_profile() for m in result.data['members']))
        finally:
            session.close()

    def test_old_membership_refresh_is_discarded(self):
        from codol_rebuild.application.room.contracts import Completion, IdentityRecord
        from codol_rebuild.application.room.service import RoomSession
        session = RoomSession(Mock(), Mock(), 'http://localhost', 'fingerprint', Mock())
        try:
            session._record = IdentityRecord('pl_a', 'rm_test')
            session._members_pending = Future()
            session._members_pending.set_result(Completion('room_members_updated', {'roomId': 'rm_test'}))
            session._members_generation = 1
            session._room_generation = 2
            self.assertIsNone(session.poll_event())
        finally:
            session.close()

    def test_all_profile_bytes_survive_round_trip(self):
        data = sample_profile()
        decoded = profile.decode_snapshot(data)
        self.assertEqual(decoded['tf141'], bytes([2])*160)
        self.assertEqual(decoded['scar'], bytes([1])*160)
        self.assertEqual(len(decoded['public']), layout.BUFFER_CAPACITY)
        self.assertEqual(len(decoded['secure']), layout.BUFFER_CAPACITY)

    def test_rejects_wrong_version_size_and_schema(self):
        for update in ({'contentVersion': 'other'}, {'public': 'AAAA'},
                       {'secure': base64.b64encode(bytes(32764)).decode()},
                       {'tf141': '!'*216}):
            with self.subTest(update=list(update)):
                with self.assertRaises(ValueError):
                    profile.decode_snapshot({**sample_profile(), **update})

    def test_native_capture_contract_matches_fixed_image(self):
        pe = pefile.PE(str(Path(__file__).resolve().parents[1]/'bin/v1649370/codol1649370-offline.exe'))
        for address, expected in profile.CAPTURE_CONTRACT.items():
            self.assertEqual(pe.get_data(address-0x400000, len(expected)), expected)

    def test_generated_calls_and_branches(self):
        decoder = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
        for entry in (0x20000000, 0xf0000000):
            for code, external in ((profile.build_capture(entry, 0x21000000), {0x4f73b0, 0x7c5d60}),
                                   (profile.build_member_publish(entry, 0x21000000), {0x6305f0, 0x7cbab0})):
                rows = list(decoder.disasm(code, entry))
                self.assertEqual(sum(row.size for row in rows), len(code))
                starts = {row.address for row in rows}
                for row in rows:
                    if row.mnemonic.startswith('j') or row.mnemonic == 'call':
                        self.assertIn(int(row.op_str, 16), starts | external)
                self.assertEqual(code[:2], bytes.fromhex('9c60'))
                self.assertEqual(code[-3:], bytes.fromhex('619dc3'))
