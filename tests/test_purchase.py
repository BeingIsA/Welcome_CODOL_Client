"""本地购买的事务、失败回滚、期限策略及原生契约验证。"""
import copy
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
import pefile
import capstone

from codol_rebuild.application.accounts.purchase import permanent_purchase
from codol_rebuild.application.accounts.service import AccountSession
from codol_rebuild.adapters.persistence.sqlite_accounts import AccountRepository
from codol_rebuild.adapters.game.v1649370.accounts import purchase

ROOT = Path(__file__).resolve().parents[2]
STATE = {'version': 1, 'owned_ids': [545], 'gp': 1000, 'cp': 100}
OFFER = {'item_id': 123, 'currency': 'gp', 'price': 50, 'equipment': True, 'available': True}


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
    def test_bundle_accumulates_and_deduplicates_atomically(self):
        bundle = {**OFFER, 'contents': [
            {'item_id': 545, 'stackable': False, 'count': 1},
            {'item_id': 123, 'stackable': False, 'count': 1},
            {'item_id': 456, 'stackable': True, 'count': 5},
            {'item_id': 456, 'stackable': True, 'count': 2},
        ]}
        first = permanent_purchase(STATE, bundle)
        second = permanent_purchase(first, bundle)
        self.assertEqual(second['owned_ids'], [123, 456, 545])
        self.assertEqual(second['quantities'], {'456': 14})
        self.assertEqual(second['gp'], 900)
        self.assertNotIn('quantities', STATE)
        old = {**STATE, 'owned_ids': [456, 545]}
        self.assertEqual(permanent_purchase(old, bundle)['quantities']['456'], 8)
        for content in ({'item_id': 20000, 'stackable': True, 'count': 1},
                        {'item_id': 456, 'stackable': True, 'count': 0}):
            with self.assertRaises(ValueError):
                permanent_purchase(first, {**bundle, 'contents': [*bundle['contents'], content]})
        with self.assertRaises(ValueError):
            permanent_purchase({**first, 'quantities': {'456': 2147483647}}, bundle)
        with self.assertRaises(ValueError):
            permanent_purchase(first, {**bundle, 'contents': bundle['contents'][:2]})

    def test_bundle_persistence_and_native_rollback(self):
        bundle = {**OFFER, 'contents': [
            {'item_id': 545, 'stackable': True, 'count': 4},
            {'item_id': 123, 'stackable': False, 'count': 1}]}
        with tempfile.TemporaryDirectory() as folder:
            session, blobs = self.session(Path(folder)/'account.db')
            session.commit_purchase(blobs, bundle)
            session.commit_purchase(blobs, bundle)
            saved = session.repository.load('local', session.codec.identities)['initialization']
            self.assertEqual(saved['quantities'], {'545': 9})
            mem, pool = Memory(), 0x100000
            mem.write(pool-16, b'LOCAL-INVENTORY!')
            mem.write(pool+purchase.CAPACITY*purchase.STRIDE, b'LOCAL-INVENTORY!')
            for slot in (pool, pool+purchase.STRIDE):
                mem.word(slot, 0xD59C78)
                mem.word(slot+0x90, 0xffffffff)
                mem.word(slot+0x98, 0xffffffff)
            mem.write(pool+0x40, struct.pack('<III', 545, 1, 17))
            mem.word(0x82C4A8C, 1)
            purchase.publish_currency(mem.read, mem.write, STATE)
            inventory = {'pool': hex(pool), 'items': 1, 'weapon_experience': 99}
            items = [{'id': 545, 'type_bytes': [0, 8]}, {'id': 123, 'type_bytes': [0, 0]}]
            before = mem.read(pool, purchase.STRIDE*2)
            def reject(): raise ValueError('database failed')
            with self.assertRaises(ValueError):
                purchase.publish_transaction(mem.read, mem.write, inventory, items, saved, reject)
            self.assertEqual(mem.read(pool, purchase.STRIDE*2), before)
            self.assertEqual(purchase.currency_snapshot(mem.read), {'gp': 1000, 'cp': 100})
            purchase.publish_transaction(mem.read, mem.write, inventory, items, saved, lambda: True)
            self.assertEqual(purchase.u32(mem.read, pool+0x44), 9)
            self.assertEqual(purchase.u32(mem.read, pool+0x48), 17)
            self.assertEqual(inventory['items'], 2)

    def test_every_duration_grants_permanent_without_mutation(self):
        for duration in (0, 1, 7, 30, 65535, 4294967295):
            before = copy.deepcopy(STATE)
            after = permanent_purchase(before, {**OFFER, 'selected_duration': duration})
            self.assertEqual(after, {**STATE, 'gp': 950, 'owned_ids': [123, 545]})
            self.assertEqual(before, STATE)

    def test_rejections(self):
        for offer, quantity in [
            ({**OFFER, 'item_id': 545}, 1), ({**OFFER, 'price': 1001}, 1),
            ({**OFFER, 'price': -5}, 1), ({**OFFER, 'currency': 'honor'}, 1),
            ({**OFFER, 'equipment': False}, 1), ({**OFFER, 'available': False}, 1),
            (OFFER, 2), (OFFER, True), ({**OFFER, 'price': True}, 1),
        ]:
            with self.assertRaises(ValueError):
                permanent_purchase(STATE, offer, quantity)

    def session(self, path):
        repo, codec = AccountRepository(path), Codec()
        repo.identity()
        blobs = {'public': b'public', 'secure': b'secure'}
        repo.save('local', 0, blobs, codec.identities, initialization=STATE)
        session = AccountSession(repo, codec)
        session.account = repo.load('local', codec.identities)
        return session, blobs

    def test_sqlite_restart_duplicate_and_conflict(self):
        with tempfile.TemporaryDirectory() as folder:
            session, blobs = self.session(Path(folder)/'account.db')
            stale = AccountSession(session.repository, session.codec)
            stale.account = copy.deepcopy(session.account)
            session.commit_purchase(blobs, {**OFFER, 'currency': 'cp'})
            restored = session.repository.load('local', session.codec.identities)
            self.assertEqual(restored['initialization']['cp'], 50)
            self.assertIn(123, restored['initialization']['owned_ids'])
            with self.assertRaises(ValueError):
                session.commit_purchase(blobs, OFFER)
            with self.assertRaises(ValueError):
                stale.commit_purchase(blobs, OFFER)
            self.assertEqual(session.repository.load('local', session.codec.identities), restored)

    def test_sqlite_failure_rolls_back_currency_and_ownership(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'account.db'
            session, blobs = self.session(path)
            before = copy.deepcopy(session.account)
            with sqlite3.connect(path) as db:
                db.execute("CREATE TRIGGER reject_purchase BEFORE UPDATE ON account_initialization BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
            db.close()
            with self.assertRaises(sqlite3.Error):
                session.commit_purchase(blobs, OFFER)
            self.assertEqual(session.account, before)
            self.assertEqual(session.repository.load('local', session.codec.identities), before)

    def test_native_publish_rollback_and_success(self):
        mem, pool = Memory(), 0x100000
        mem.write(pool-16, b'LOCAL-INVENTORY!')
        mem.write(pool+purchase.CAPACITY*purchase.STRIDE, b'LOCAL-INVENTORY!')
        mem.word(pool+0x40, 545)
        slot = pool+purchase.STRIDE
        mem.word(slot, 0xD59C78)
        mem.word(slot+0x90, 0xffffffff)
        mem.word(slot+0x98, 0xffffffff)
        mem.word(0x82C4A8C, 1)
        purchase.publish_currency(mem.read, mem.write, STATE)
        inventory = {'pool': hex(pool), 'items': 1, 'weapon_experience': 99}
        item = {'id': 123, 'type_bytes': [0, 0]}
        before = mem.read(slot, purchase.STRIDE)
        def reject():
            raise ValueError('database failed')
        with self.assertRaises(ValueError):
            purchase.publish_transaction(mem.read, mem.write, inventory, item,
                                         permanent_purchase(STATE, OFFER), reject)
        self.assertEqual(mem.read(slot, purchase.STRIDE), before)
        self.assertEqual(purchase.currency_snapshot(mem.read), {'gp': 1000, 'cp': 100})
        self.assertEqual(inventory['items'], 1)
        purchase.publish_transaction(mem.read, mem.write, inventory, item,
                                     permanent_purchase(STATE, OFFER), lambda: True)
        self.assertEqual(inventory['items'], 2)
        self.assertEqual(purchase.currency_snapshot(mem.read)['gp'], 950)
        self.assertEqual(mem.read(slot+0x90, 4), b'\xff'*4)

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

    def test_quote_uses_native_discount_and_classifies_contents(self):
        mem, state = Memory(), 0x100000
        mem.word(state+4, 55)
        for offset in (20,24,28): mem.word(state+offset,1)
        for offset,value in ((0,9),(16,55),(20,2),(24,100),(36,7),(48,60),(52,60),(56,100)):
            mem.word(state+purchase.INFO+offset,value)
        mem.word(0x7797FFC,10);mem.word(0x7797FF8,0x200000)
        mem.write(0x200012,struct.pack('<H',1))
        mem.word(0x7797FEC,2);mem.word(0x7797FE8,0x300000)
        mem.write(0x300000+28+14,b'\x01');mem.word(0x300000+28+16,0x400000)
        mem.write(0x400000,struct.pack('<HH',123,1))
        item={'id':123,'type_bytes':[0,0], 'raw':bytes(40).hex()}
        offer=purchase.quote(mem.read,state,{123:item})
        self.assertEqual((offer['currency'],offer['price'],offer['selected_duration']),('cp',60,7))
        mem.word(state+purchase.INFO+52,0)
        self.assertEqual(purchase.quote(mem.read,state,{123:item})['price'],100)
        for offset in (20,24,28):
            mem.word(state+offset,0)
            with self.assertRaises(ValueError): purchase.quote(mem.read,state,{123:item})
            mem.word(state+offset,1)
        mem.write(0x300000+28+14,b'\x02')
        with self.assertRaises(ValueError): purchase.quote(mem.read,state,{123:item})
        mem.write(0x300000+28+14,b'\x01')
        self.assertTrue(purchase.quote(mem.read,state,{123:{**item,'type_bytes':[0,8,3]}})['equipment'])
        raw=bytearray(40);raw[32]=2
        self.assertFalse(purchase.quote(mem.read,state,{123:{**item,'raw':raw.hex()}})['equipment'])
        mem.write(0x300000+28+14,b'\x02')
        mem.write(0x400004,struct.pack('<HBB',456,7,0))
        offer=purchase.quote(mem.read,state,{123:item,456:{**item,'id':456,'raw':raw.hex()}})
        self.assertEqual(offer['contents'], [
            {'item_id':123,'count':1,'stackable':False},
            {'item_id':456,'count':7,'stackable':True}])

    def test_quantity_state_rejects_corruption(self):
        from codol_rebuild.application.accounts.policy import validate_initialization
        for amounts in ([], {'545':0}, {'545':True}, {'545':2147483648},
                        {'999':2}, {'0545':2}, {545:2}):
            with self.assertRaises(ValueError):
                validate_initialization({**STATE, 'quantities':amounts})

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
        plan = purchase.build_adapter(entry, entry+4096, entry+8192)
        md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
        instructions = list(md.disasm(plan['code'], entry))
        self.assertEqual(sum(i.size for i in instructions), len(plan['code']))
        starts = {i.address for i in instructions}
        for i in instructions:
            if i.mnemonic.startswith('j') and i.op_str.startswith('0x'):
                self.assertIn(int(i.op_str,16), starts | {purchase.HANDLER, purchase.REFRESH_HANDLER})
        self.assertEqual(set(plan['traps'].values()), {'request','quoted','refreshed','inventory_updated'})
        for address in plan['traps']:
            self.assertEqual(plan['code'][address-entry], 0xcc)


if __name__ == '__main__':
    unittest.main()
