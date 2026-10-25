"""执行真实电影中的背包改名事件链，验证净化标志含义及原生写入/警告互斥。"""
from pathlib import Path
import struct
import sys
import unittest
from codol_rebuild.adapters.game.v1649370 import ui_movie

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'research'))
from trace_matchmaking_debugline import Reader,abc_parse


def execute(method,names,strings,scope,this,event):
    """按AVM2栈与分支规则解释所需指令，原生函数使用可观测替身。"""
    r=Reader(bytes.fromhex(method['code']));stack=[]
    locals_=[this,event]+[None]*(method['limits'][1]-2)
    def key():return names[r.u30()].rsplit('::',1)[-1]
    for _ in range(1000):
        at=r.p;op=r.byte()
        if op in (0x02,0x09):pass
        elif op in (0xf0,0xf1):r.u30()
        elif op==0xef:r.byte();r.u30();r.byte();r.u30()
        elif 0xd0<=op<=0xd3:stack.append(locals_[op-0xd0])
        elif 0xd4<=op<=0xd7:locals_[op-0xd4]=stack.pop()
        elif op==0x62:stack.append(locals_[r.u30()])
        elif op==0x63:locals_[r.u30()]=stack.pop()
        elif op==0x08:locals_[r.u30()]=None
        elif op in (0x30,0x29):stack.pop()
        elif op==0x5d:key();stack.append(scope)
        elif op==0x66:
            name=key();stack.append(stack.pop().get(name))
        elif op==0x61:
            name=key();value=stack.pop();stack.pop()[name]=value
        elif op==0x2c:stack.append(strings[r.u30()])
        elif op==0x24:stack.append(r.byte())
        elif op==0x25:stack.append(r.u30())
        elif op==0x20:stack.append(None)
        elif op in (0x26,0x27):stack.append(op==0x26)
        elif op==0x2a:stack.append(stack[-1])
        elif op==0x76:stack.append(bool(stack.pop()))
        elif op in (0x73,0x74):stack.append(int(stack.pop()))
        elif op==0x85:pass
        elif op==0xab:
            right=stack.pop();stack.append(stack.pop()==right)
        elif op==0x55:
            count=r.u30();pairs=stack[-count*2:];del stack[-count*2:]
            stack.append(dict(zip(pairs[::2],pairs[1::2])))
        elif op==0x46:
            name,count=key(),r.u30();args=stack[-count:] if count else []
            if count:del stack[-count:]
            receiver=stack.pop();stack.append(receiver[name](*args))
        elif op in (0x10,0x11,0x12,0x13,0x14,0x19,0x1a):
            delta=int.from_bytes(r.raw(3),'little',signed=True)
            if op==0x10:take=True
            elif op in (0x11,0x12):take=bool(stack.pop())==(op==0x11)
            else:
                right,left=stack.pop(),stack.pop();equal=left==right
                take=equal if op in (0x13,0x19) else not equal
            if take:r.p+=delta
        elif op==0x1b:
            default=int.from_bytes(r.raw(3),'little',signed=True);count=r.u30()
            cases=[int.from_bytes(r.raw(3),'little',signed=True) for _ in range(count+1)]
            index=stack.pop();r.p=at+(cases[index] if 0<=index<len(cases) else default)
        elif op==0x47:
            assert not stack,stack
            return
        else:raise AssertionError((hex(op),at,method['name']))
        assert len(stack)<=method['limits'][0],('stack overflow',method['name'])
    raise AssertionError('方法未终止')


class LoadoutRenameFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        movie=ui_movie.patch_movie((ROOT/'research/client/ui-investigation/codo.gfx').read_bytes())
        r=Reader(movie);r.p=8+(5+4*(movie[8]>>3)+7)//8+4;cls.methods={}
        wanted=('onSetLoadoutNameHandle','onSanitizedFeedbackHandle','onSanitizedHandle','sanitize')
        while r.p<len(movie):
            header=struct.unpack('<H',r.raw(2))[0];kind,size=header>>6,header&63
            if size==63:size=struct.unpack('<I',r.raw(4))[0]
            payload=r.raw(size)
            if kind!=82:continue
            a=Reader(payload);a.raw(4);tag=a.cstr()
            if tag not in ('codo/controllers/WeaponLoadOutController','codo/controllers/SanitizeController',
                           'codo/views/FsgChangeLoadoutNamePopupView'):continue
            methods,names,strings=abc_parse(payload[a.p:])
            for m in methods:
                name=m.get('trait','').rsplit('::',1)[-1]
                if name in wanted:cls.methods[name]=(m,names,strings)
        assert len(cls.methods)==4

    def setUp(self):
        self.writes=[];self.events=[];self.warnings=[]
        self.scope={'SanitizedType':{'LOADOUT_NAME':7,'ROOM_NAME':1,'NONE':0},
            'BCCommonEvent':{'EVT_SANITIZE_STRING':'sanitize','EVT_ON_STRING_SANITIZED':'complete'},
            'BCEvent':{'EVT_EXIT_RENAME_LOADOUT':'close','EVT_REFRESH_UI_AFTER_INVENTORY_CHANGE':'refresh'},
            'CFunctionContainer':{'WL_SetLoadoutName':lambda *a:self.writes.append(a)},
            'Dict':{'RENAME_LOADOUT_SANITIZED':'blocked'},'MsgBtnCount':{'ONE':1},
            'myPopup':{'enableButtons':lambda:None},'showMsg':lambda *a:self.warnings.append(a)}
        self.controller={'_currentChangedNameLoadoutIndex':0,'broadcast':lambda *a:self.events.append(a)}

    def run_method(self,name,this,event):
        execute(*self.methods[name],self.scope,this,event)

    def feedback(self,payload):
        self.run_method('onSanitizedFeedbackHandle',self.controller,{'myPara':payload})
        self.run_method('onSanitizedHandle',{}, {'myPara':payload})

    def test_normal_name_reaches_native_write_and_refresh(self):
        for index,name in ((0,'Alpha'),(4,'中文背包'),(7,'Loadout_2')):
            self.run_method('onSetLoadoutNameHandle',self.controller,{'myPara':{'index':index,'name':name}})
            event,payload=self.events.pop()
            self.assertEqual(event,'sanitize');self.assertIs(payload['sanitized'],False)
            self.run_method('sanitize',self.controller,payload)
            complete,returned=self.events.pop();self.assertEqual(complete,'complete')
            self.assertIs(returned,payload)
            self.feedback(returned)
            self.assertEqual(self.writes[-1],(index,name))
            self.assertEqual(self.events[-2:],[('close',),('refresh',)])
        self.assertEqual(len(self.writes),3);self.assertEqual(self.warnings,[])

    def test_filtered_name_warns_without_writing_or_closing(self):
        self.feedback({'type':7,'content':'Alpha','sanitized':True})
        self.assertEqual(self.writes,[]);self.assertEqual(self.events,[])
        self.assertEqual(len(self.warnings),1);self.assertEqual(self.warnings[0][0],'blocked')

    def test_other_sanitize_types_do_not_rename_loadout(self):
        for value in (False,True):self.feedback({'type':1,'content':'Room','sanitized':value})
        self.assertEqual((self.writes,self.events,self.warnings),([],[],[]))


if __name__=='__main__':unittest.main()
