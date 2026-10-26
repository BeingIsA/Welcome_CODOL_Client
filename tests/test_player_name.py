"""本地昵称回归：持久化隔离、GFx输入、编码、回滚及固定资源身份。"""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest

import pefile
from codol_rebuild.application.accounts.service import validate_display_name,load_display_name,change_display_name
from codol_rebuild.application.accounts.contracts import RevisionConflict
from codol_rebuild.adapters.persistence.sqlite_accounts import AccountRepository
from codol_rebuild.adapters.game.v1649370.accounts import player_name as n
from codol_rebuild.adapters.game.v1649370.session import ui_movie

ROOT = Path(__file__).resolve().parents[2]


class NameStorageTests(unittest.TestCase):
    def setUp(self):
        folder = ROOT/'verification/workspaces/test-temp'
        folder.mkdir(parents=True,exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=folder)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'accounts.sqlite3'
        self.repo = AccountRepository(self.path)
        self.identity = self.repo.identity()

    def test_restart_chinese_and_profile_isolation(self):
        identities={'public':{},'secure':{}}
        self.repo.save('local',0,{'public':b'public','secure':b'secure'},identities)
        before=self.repo.load('local',identities)
        self.assertEqual(load_display_name(self.repo),{'value':None,'revision':0})
        change_display_name(self.repo,0,'本地玩家')
        other=AccountRepository(self.path)
        self.assertEqual(load_display_name(other),{'value':'本地玩家','revision':1})
        self.assertEqual(other.load('local',identities),before)
        change_display_name(other,1,'Player_2')
        self.assertEqual(load_display_name(self.repo)['value'],'Player_2')
        self.assertEqual(other.identity()['uid'],self.identity['uid'])

    def test_stale_session_cannot_overwrite_name(self):
        other=AccountRepository(self.path)
        snapshot=load_display_name(other)
        change_display_name(self.repo,0,'First')
        with self.assertRaises(RevisionConflict):
            change_display_name(other,snapshot['revision'],'Stale')
        self.assertEqual(load_display_name(other)['value'],'First')

    def test_sql_failure_does_not_change_previous_name(self):
        change_display_name(self.repo,0,'First')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TRIGGER fail_name BEFORE UPDATE ON display_names BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            change_display_name(self.repo,1,'Second')
        self.assertEqual(load_display_name(self.repo),{'value':'First','revision':1})

    def test_legacy_schema_migrates_without_touching_profiles(self):
        legacy=Path(self.temp.name)/'legacy.sqlite3'
        with closing(sqlite3.connect(legacy)) as db, db:
            db.execute('CREATE TABLE accounts (name TEXT PRIMARY KEY,uid INTEGER UNIQUE NOT NULL,revision INTEGER NOT NULL,updated REAL NOT NULL)')
            db.execute("INSERT INTO accounts VALUES ('local',123,7,0)")
        old=AccountRepository(legacy)
        self.assertEqual(load_display_name(old)['value'],None)
        change_display_name(old,0,'Migrated')
        self.assertEqual(old.identity(),{'name':'local','uid':123,'revision':7})

    def test_validation_matches_original_input_limits(self):
        for value in ('Ab_1','中文','中'*8,'a'*16,'A中文1'):
            self.assertEqual(validate_display_name(value),value)
        for value in ('','abc','中','a'*17,'中'*9,'a bcd','abc\n','abc\0','abc^','abc😀',None):
            with self.subTest(value=value),self.assertRaises(ValueError):
                change_display_name(self.repo,0,value)
        self.assertEqual(load_display_name(self.repo)['revision'],0)


class NameBridgeTests(unittest.TestCase):
    def setUp(self):
        self.bytes={}
        self.state,self.ready,self.frame=0x21000000,0x22000000,0x24000000
        self.dvar,self.room,self.lang,self.params,self.args=0x25000000,0x26000000,0x27000000,0x28000000,0x29000000
        for a,s in ((self.state,136),(self.frame,44),(self.params,24),(self.args,32),
                    (self.dvar,80),(self.lang,32)):
            self.write(a,bytes(s))
        self.u32(n.USERNAME,self.dvar);self.u32(self.dvar,0x30000000)
        self.write(0x30000000,b'dw_username\0');self.write(self.dvar+8,b'\x07')
        self.u32(self.dvar+12,0x31000000);self.write(0x31000000,b'OldName\0')
        self.u32(n.LANGUAGE,self.lang);self.u32(self.lang+12,0);self.u32(n.DIRECT_STRING_MODE,0)
        self.u32(self.ready,1);self.u32(n.DW,0x32000000);self.u32(0x3200000c,0)
        self.u32(n.ROOM,self.room);self.u32(self.room+0x1c,0);self.u32(self.room+0x38710,0)
        self.u32(self.frame+40,self.params);self.u32(self.params+16,self.args);self.u32(self.params+20,1)
        self.u32(self.args+12,6);self.u32(self.args+16,0x33000000)
        self.write(0x33000000,'本地玩家'.encode()+b'\0')

    def write(self,a,data):
        self.bytes.update((a+i,b) for i,b in enumerate(data))
    def u32(self,a,v):self.write(a,struct.pack('<I',v))
    def read(self,a,s):
        try:return bytes(self.bytes[a+i] for i in range(s))
        except KeyError as error:raise OSError('fixture unreadable') from error

    def test_gfx_managed_and_plain_utf8(self):
        self.assertEqual(n.read_request(self.read,self.frame,self.ready),'本地玩家')
        self.u32(self.args+12,0x46);self.u32(self.args+16,0x34000000);self.u32(0x34000000,0x33000000)
        self.assertEqual(n.read_request(self.read,self.frame,self.ready),'本地玩家')

    def test_request_refuses_room_online_not_ready_and_bad_types(self):
        for address,value in ((self.room+0x1c,1),(self.room+0x38710,1),(self.ready,0),
                              (0x3200000c,1),(self.args+12,7),(self.params+20,2)):
            old=self.read(address,4);self.u32(address,value)
            with self.subTest(address=address),self.assertRaises(ValueError):
                n.read_request(self.read,self.frame,self.ready)
            self.write(address,old)

    def test_room_pending_flag_ignores_adjacent_state_and_padding(self):
        # 原生62FDC6只比较room+38710的一个字节；后面是其他状态与填充。
        for surrounding in (b'\0\x01\0\0',b'\0\0\x01\0',b'\0\0\0\xcc'):
            self.write(self.room+0x38710,surrounding)
            with self.subTest(surrounding=surrounding.hex()):
                self.assertEqual(n.read_request(self.read,self.frame,self.ready),'本地玩家')

    def test_encoding_and_rollback_readback(self):
        self.u32(self.lang+12,8)
        n.stage_name(self.read,self.write,self.state,'本地玩家')
        self.assertEqual(self.read(self.state+8,64).split(b'\0')[0],'本地玩家'.encode('gbk'))
        with self.assertRaises(ValueError):n.verify_applied(self.read,self.state)
        self.u32(self.dvar+12,self.state+8)
        n.verify_applied(self.read,self.state)
        self.u32(self.dvar+12,self.state+72)
        n.verify_applied(self.read,self.state,rollback=True)
        self.assertEqual(n.native_name(self.read),b'OldName')
        self.u32(n.DIRECT_STRING_MODE,1)
        self.assertEqual(n.encode_name(self.read,'本地玩家'),'本地玩家'.encode())

    def test_invalid_utf8_and_unterminated_input(self):
        for value in (b'\xff\0',b'x'*64):
            self.write(0x33000000,value)
            with self.assertRaises(ValueError):n.read_request(self.read,self.frame,self.ready)

    def test_real_image_contract_and_changed_slot_rejection(self):
        p=pefile.PE(str(ROOT/'client/bin/v1649370/codol1649370-offline.exe'))
        read=lambda a,s:p.get_data(a-0x400000,s)
        n.validate_contract(read)
        with self.assertRaises(ValueError):
            n.validate_contract(lambda a,s:bytes(s) if a==n.HANDLER_SLOT else read(a,s))


if __name__=='__main__':unittest.main()
