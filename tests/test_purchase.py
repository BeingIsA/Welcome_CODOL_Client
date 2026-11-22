"""购买入口、服务端确认和原生双任务回显回归。"""
import struct
import unittest
from pathlib import Path
from unittest.mock import Mock

import pefile
import capstone
from codol_rebuild.application.errors import OnlineError
from codol_rebuild.adapters.game.v1649370.accounts import purchase

ROOT = Path(__file__).resolve().parents[2]
STATE = {"version": 1, "owned_ids": [545], "gp": 1000, "cp": 100}


class Codec:
    identities = {'public': {'test': 1}, 'secure': {'test': 2}}
    def validate_snapshot(self, blobs):
        if blobs != {'public': b'public', 'secure': b'secure'}:
            raise ValueError('Invalid test snapshot')


class Memory:
    def __init__(self):
        self.bytes = {}
    def read(self, address, count):
        return bytes(self.bytes.get(address+i, 0) for i in range(count))
    def write(self, address, blob):
        self.bytes.update({address+i: value for i, value in enumerate(blob)})
    def word(self, address, value):
        self.write(address, struct.pack('<I', value))


class PurchaseTests(unittest.TestCase):
    def test_gfx_request_rejects_fraction_and_bulk(self):
        mem = Memory()
        mem.word(0x100028, 0x200000)
        mem.word(0x200014, 2)
        mem.word(0x200010, 0x300000)
        mem.word(0x30000c, 3); mem.word(0x300010, 123)
        mem.word(0x30002c, 5); mem.write(0x300030, struct.pack('<d', 1.0))
        self.assertEqual(purchase.request(mem.read, 0x100000), (123, 1))
        for number in (1.5, 2, float('nan')):
            mem.write(0x300030, struct.pack('<d', number))
            with self.assertRaises(ValueError):
                purchase.request(mem.read, 0x100000)

    def test_quantity_state_rejects_corruption(self):
        from codol_rebuild.application.accounts.policy import validate_initialization
        for amounts in ([], {'545':0}, {'545':True}, {'545':2147483648},
                        {'999':2}, {'0545':2}, {545:2}):
            with self.assertRaises(ValueError):
                validate_initialization({**STATE, 'quantities':amounts})

    def test_purchase_waits_for_server_before_publishing_and_completing(self):
        memory = Memory()
        process = Mock(read=memory.read, write=memory.write)
        session = Mock()
        profiles = Mock()
        state = 0x13000000
        for offset in (20, 24, 28):
            memory.word(state + offset, 1)
        memory.word(state + 4, 91)
        inventory = {'revision': 3, 'items': [], 'cp': 5, 'gp': 10}
        session.poll.side_effect = [None, {'inventory': inventory}]
        for phase in ('eligible', 'poll'):
            purchase.handle_phase(process, phase, state, 0, profiles, session, (ValueError,))
        session.begin_purchase.assert_called_once_with(91)
        profiles.apply_inventory.assert_not_called()
        self.assertEqual(purchase.u32(memory.read, state + 16), 0)
        purchase.handle_phase(process, 'poll', state, 0, profiles, session, (ValueError,))
        profiles.apply_inventory.assert_called_once_with(inventory)
        self.assertEqual(purchase.u32(memory.read, state + 16), 1)
        purchase.handle_phase(process, 'inventory_updated', state, 0, profiles, session, (ValueError,))
        session.begin_purchase.assert_called_once_with(91)

    def test_offline_and_rejected_requests_end_in_failure(self):
        memory = Memory()
        process = Mock(read=memory.read, write=memory.write)
        profiles = Mock()
        state = 0x13000000
        result = purchase.handle_phase(process, 'request', state, 0, profiles, None, (ValueError,))
        self.assertIn('离线', result['purchase_error'])
        session = Mock()
        session.poll.side_effect = OnlineError('REJECTED', '余额不足')
        result = purchase.handle_phase(process, 'poll', state, 0, profiles, session, (ValueError,))
        self.assertEqual(result['purchase_error'], '余额不足')
        self.assertEqual(purchase.u32(memory.read, state + purchase.ONLINE_TASK_STATE), 2)
        self.assertEqual(purchase.u32(memory.read, state + 16), 0)
        profiles.apply_inventory.assert_not_called()

    def test_success_popup_calls_existing_parent_method(self):
        import sys
        from codol_rebuild.adapters.game.v1649370.session import ui_movie
        sys.path.insert(0, str(ROOT/'research'))
        from trace_matchmaking_debugline import Reader, abc_parse, disassemble
        movie=ui_movie.patch_movie((ROOT/'research/client/ui-investigation/codo.gfx').read_bytes())
        reader=Reader(movie)
        reader.p=8+(5+4*(movie[8]>>3)+7)//8+4
        chinese_message = False
        while reader.p<len(movie):
            header=struct.unpack('<H',reader.raw(2))[0]
            kind,size=header>>6,header&63
            if size==63:size=struct.unpack('<I',reader.raw(4))[0]
            data=reader.raw(size)
            if kind!=82:continue
            abc=Reader(data);abc.raw(4)
            tag = abc.cstr()
            if tag == 'codo/controllers/MarketBaseController':
                methods,names,strings=abc_parse(data[abc.p:])
                method=next(m for m in methods if m.get('trait','').endswith('::showPurchaseSuccess'))
                instructions=disassemble(method,names,strings)
                literals=[r['args'][0] for r in instructions if r['instruction']=='pushstring']
                self.assertEqual(literals, ['购买成功'])
                self.assertFalse(any('PURCHASE_SKUS_SUCCESS' in str(r['args']) for r in instructions))
                chinese_message = True
            if tag!='codo/controllers/MarketController':continue
            methods,names,strings=abc_parse(data[abc.p:])
            method=next(m for m in methods if m.get('trait','').endswith('::showPurchaseSuccess'))
            instructions=disassemble(method,names,strings)
            calls=[r for r in instructions if r['instruction']=='callsupervoid']
            self.assertEqual(len(calls),1)
            self.assertTrue(calls[0]['args'][0].endswith('::showPurchaseSuccess'))
            self.assertEqual(calls[0]['args'][1],0)
            self.assertTrue(chinese_message)
            return
        self.fail('Missing purchase controller')

    def test_native_contract_and_adapter_control_flow(self):
        pe = pefile.PE(str(ROOT/'client/bin/v1649370/codol1649370-offline.exe'))
        purchase.validate_contract(lambda a,n: pe.get_data(a-pe.OPTIONAL_HEADER.ImageBase,n))
        entry = 0x10000000
        plan = purchase.build_adapter(entry, entry+4096, entry+8192, deferred=True)
        md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
        instructions = list(md.disasm(plan['code'], entry))
        self.assertEqual(sum(i.size for i in instructions), len(plan['code']))
        starts = {i.address for i in instructions}
        for i in instructions:
            if i.mnemonic.startswith('j') and i.op_str.startswith('0x'):
                self.assertIn(int(i.op_str,16), starts | {purchase.HANDLER, purchase.REFRESH_HANDLER})
        self.assertEqual(set(plan['traps'].values()), {'request', 'eligible', 'poll', 'refreshed', 'inventory_updated'})
        for address in plan['traps']:
            self.assertEqual(plan['code'][address-entry], 0xcc)


if __name__ == '__main__':
    unittest.main()
