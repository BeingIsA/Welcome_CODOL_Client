"""改名扣卡事务、原生回滚和再次购买的独立回归。

多张改名卡无法经普通 UI 取得（持卡时购买入口隐藏），多卡用例直接构造
合法堆叠状态验证消费只减一。
"""
from contextlib import closing
import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path
from codol_rebuild.application.accounts.service import AccountSession
from codol_rebuild.application.accounts.contracts import RevisionConflict
from codol_rebuild.adapters.persistence.sqlite_accounts import AccountRepository
from codol_rebuild.adapters.game.v1649370.accounts import player_name, purchase
from test_purchase import Memory, Codec

class RenameCardFixture(unittest.TestCase):
    """改名卡回归公共夹具：cards 决定持卡数量，应用状态与原生槽保持一致。"""
    cards = 1

    def setUp(self):
        folder=Path(__file__).resolve().parents[2]/'verification/client/workspaces'
        folder.mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=folder)
        self.addCleanup(self.temp.cleanup)
        self.repo=AccountRepository(Path(self.temp.name)/'account.db')
        self.repo.identity()
        self.state={'version':1,'owned_ids':[545,50001],'quantities':{'50001':self.cards},'gp':1000,'cp':1000}
        self.blobs={'public':b'public','secure':b'secure'}
        self.repo.save('local',0,self.blobs,Codec.identities,initialization=self.state)
        self.session=AccountSession(self.repo,Codec())
        self.session.account=self.repo.load('local',Codec.identities)
        self.session.name_snapshot=self.repo.display_name('local')
        self.mem=Memory();self.pool=0x100000
        self.mem.write(self.pool-16,b'LOCAL-INVENTORY!')
        self.mem.write(self.pool+purchase.CAPACITY*purchase.STRIDE,b'LOCAL-INVENTORY!')
        for index,item in enumerate((545,50001)):
            slot=self.pool+index*purchase.STRIDE
            values=((0x44,self.cards if item==50001 else 1),(0x48,7 if item==50001 else 0))
            for offset,value in ((0,0xD59C78),(0x40,item),*values,(0x90,0xffffffff),(0x98,0xffffffff)):
                self.mem.word(slot+offset,value)
        self.mem.word(0x82C4A8C,2)
        purchase.publish_currency(self.mem.read,self.mem.write,self.state)
        self.inventory={'pool':hex(self.pool),'items':2,'weapon_experience':99}

    def rename(self):
        return player_name.consume_card(self.mem.read,self.mem.write,self.inventory,
                                       self.session.consume_state(50001),
                                       lambda:self.session.rename('新昵称',50001))

class RenameCardTests(RenameCardFixture):
    def test_success_restart_and_repurchase_reuses_slot(self):
        self.rename()
        restored=self.repo.load('local',Codec.identities)
        self.assertEqual(self.repo.display_name('local')['value'],'新昵称')
        self.assertEqual(restored['initialization']['owned_ids'],[545])
        self.assertEqual(restored['initialization']['quantities'],{})
        self.assertEqual(restored['profiles'],self.blobs)
        self.assertEqual(self.mem.read(self.pool+purchase.STRIDE+0x40,12),bytes(12))
        offer={'item_id':50001,'contents':[{'item_id':50001,'stackable':True,'count':1}],
               'available':True,'currency':'cp','price':50}
        next_state=self.session.purchase_state(offer)
        purchase.publish_transaction(self.mem.read,self.mem.write,self.inventory,
            {'id':50001,'type_bytes':[0,8,0,0]},next_state,
            lambda:self.session.commit_purchase(offer))
        self.assertEqual(self.inventory['items'],2)
        self.assertEqual(purchase.u32(self.mem.read,self.pool+purchase.STRIDE+0x40),50001)
        self.assertEqual(self.session.account['initialization']['quantities']['50001'],1)

    def test_database_failure_restores_card_and_both_snapshots(self):
        before=copy.deepcopy(self.session.account)
        with closing(sqlite3.connect(self.repo.path)) as db, db:
            db.execute("CREATE TRIGGER fail_card BEFORE UPDATE ON account_initialization BEGIN SELECT RAISE(ABORT,'injected failure'); END")
        with self.assertRaises(sqlite3.Error):self.rename()
        self.assertEqual(self.repo.load('local',Codec.identities),before)
        self.assertEqual(self.session.account,before)
        self.assertEqual(self.session.name_snapshot,{'value':None,'revision':0})
        self.assertEqual(self.repo.display_name('local'),self.session.name_snapshot)
        self.assertEqual(purchase.u32(self.mem.read,self.pool+purchase.STRIDE+0x40),50001)

    def test_stale_account_rolls_back_nickname_and_card(self):
        self.repo.save('local',1,self.blobs,Codec.identities,initialization=self.state)
        with self.assertRaises(RevisionConflict):self.rename()
        self.assertEqual(self.repo.display_name('local')['value'],None)
        self.assertEqual(purchase.u32(self.mem.read,self.pool+purchase.STRIDE+0x40),50001)

    def test_stale_nickname_does_not_consume(self):
        self.repo.save_display_name('local',0,'Other')
        with self.assertRaises(RevisionConflict):self.rename()
        self.assertEqual(self.repo.display_name('local')['value'],'Other')
        self.assertEqual(self.repo.load('local',Codec.identities)['initialization'],self.state)
        self.assertEqual(purchase.u32(self.mem.read,self.pool+purchase.STRIDE+0x40),50001)

    def test_native_count_mismatch_rejected_before_write(self):
        slot=self.pool+purchase.STRIDE
        self.mem.word(slot+0x44,2)
        with self.assertRaises(ValueError):self.rename()
        self.assertEqual(purchase.u32(self.mem.read,slot+0x44),2)
        self.assertEqual(self.repo.load('local',Codec.identities)['initialization'],self.state)
        self.assertEqual(self.repo.display_name('local')['value'],None)

class StackedRenameCardTests(RenameCardFixture):
    """多张改名卡：一次改名只消耗一张，授权与原生槽都保留剩余数量。"""
    cards = 3

    def test_rename_decrements_stack_in_state_and_native_slot(self):
        self.rename()
        restored=self.repo.load('local',Codec.identities)
        self.assertEqual(restored['initialization']['owned_ids'],[545,50001])
        self.assertEqual(restored['initialization']['quantities'],{'50001':2})
        self.assertEqual(self.repo.display_name('local')['value'],'新昵称')
        slot=self.pool+purchase.STRIDE
        self.assertEqual(purchase.u32(self.mem.read,slot+0x40),50001)
        self.assertEqual(purchase.u32(self.mem.read,slot+0x44),2)
        self.assertEqual(purchase.u32(self.mem.read,slot+0x48),7)
        self.assertEqual(self.session.account['initialization']['quantities'],{'50001':2})

    def test_database_failure_restores_whole_stack(self):
        before=copy.deepcopy(self.session.account)
        with closing(sqlite3.connect(self.repo.path)) as db, db:
            db.execute("CREATE TRIGGER fail_card BEFORE UPDATE ON account_initialization BEGIN SELECT RAISE(ABORT,'injected failure'); END")
        with self.assertRaises(sqlite3.Error):self.rename()
        self.assertEqual(self.repo.load('local',Codec.identities),before)
        self.assertEqual(self.session.account,before)
        slot=self.pool+purchase.STRIDE
        self.assertEqual(purchase.u32(self.mem.read,slot+0x40),50001)
        self.assertEqual(purchase.u32(self.mem.read,slot+0x44),3)

    def test_last_card_of_stack_releases_authorization(self):
        for expected in (2,1,0):
            self.rename()
            state=self.repo.load('local',Codec.identities)['initialization']
            slot=self.pool+purchase.STRIDE
            if expected:
                self.assertEqual(state['quantities'],{'50001':expected})
                self.assertEqual(purchase.u32(self.mem.read,slot+0x44),expected)
            else:
                self.assertEqual(state['quantities'],{})
                self.assertEqual(state['owned_ids'],[545])
                self.assertEqual(self.mem.read(slot+0x40,12),bytes(12))

if __name__=='__main__':unittest.main()
