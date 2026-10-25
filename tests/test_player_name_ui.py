"""从完整补丁电影重解析ABC，独立执行昵称相关指令路径并检查栈上限。"""
from pathlib import Path
import struct
import sys
import unittest

from codol_rebuild.adapters.game.v1649370 import ui_movie

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'research'))
from trace_matchmaking_debugline import Reader,abc_parse,disassemble


def methods(movie):
    reader=Reader(movie)
    reader.p=8+(5+4*(movie[8]>>3)+7)//8+4
    result={}
    while reader.p<len(movie):
        header=struct.unpack('<H',reader.raw(2))[0]
        kind,size=header>>6,header&63
        if size==63:size=struct.unpack('<I',reader.raw(4))[0]
        payload=reader.raw(size)
        if kind!=82:continue
        item=Reader(payload);item.raw(4);tag=item.cstr()
        if tag not in ('codo/controllers/MarketController','codo/objects/PlayerObj','codo/component/playercard/PlayerCardShared'):continue
        parsed,names,strings=abc_parse(payload[item.p:])
        for m in parsed:
            if m.get('trait','').rsplit('::',1)[-1] in ('getObj','onRedeemChangeNameComplete','onCareerInfoUpdate'):
                name = m['name'] if 'onRedeemChangeNameComplete' not in m.get('trait','') else 'codo.controllers:MarketController/private:onRedeemChangeNameComplete'
                result[name]=(m,disassemble(m,names,strings))
    return result


def execute(method,rows,scope,locals_):
    """小型指令解释器：属性接收者、分支和调用参数按AVM2栈语义处理。"""
    stack=[];position=0;peak=0
    offsets={row['offset']:i for i,row in enumerate(rows)}
    def key(name):return name.rsplit('::',1)[-1]
    for _ in range(500):
        row=rows[position];position+=1;op,args=row['instruction'],row['args']
        if op in ('nop','debugfile','debugline','debug'):pass
        elif op=='getlocal0':stack.append(locals_[0])
        elif op=='getlocal1':stack.append(locals_[1])
        elif op=='pushscope':stack.pop()
        elif op=='findpropstrict':stack.append(scope)
        elif op=='getlex':stack.append(scope[key(args[0])])
        elif op=='getproperty':stack.append(stack.pop().get(key(args[0])))
        elif op in ('setproperty','initproperty'):
            value=stack.pop();receiver=stack.pop();receiver[key(args[0])]=value
        elif op in ('pushstring','pushbyte'):stack.append(args[0])
        elif op=='pushnull':stack.append(None)
        elif op=='pop':stack.pop()
        elif op=='dup':stack.append(stack[-1])
        elif op=='convert_b':stack.append(bool(stack.pop()))
        elif op=='swap':stack[-1],stack[-2]=stack[-2],stack[-1]
        elif op in ('callproperty','callpropvoid'):
            name,count=args;values=stack[-count:] if count else []
            if count:del stack[-count:]
            receiver=stack.pop();value=receiver[key(name)](*values)
            if op=='callproperty':stack.append(value)
        elif op=='ifne':
            right,left=stack.pop(),stack.pop()
            if left!=right:position=offsets[args[0]]
        elif op=='iffalse':
            if not stack.pop():position=offsets[args[0]]
        elif op=='jump':position=offsets[args[0]]
        elif op in ('returnvalue','returnvoid'):
            value=stack.pop() if op=='returnvalue' else None
            assert not stack,stack
            return value,peak
        else:raise AssertionError(op)
        peak=max(peak,len(stack))
        assert peak<=method['limits'][0],(peak,method['limits'])
    raise AssertionError('方法没有终止')


class PlayerNameUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        original=(ROOT/'research/client/ui-investigation/codo.gfx').read_bytes()
        cls.before=methods(original)
        cls.after=methods(ui_movie.patch_movie(original))

    def test_abc_boundaries_and_declared_limits_preserved(self):
        self.assertEqual(len(self.after),3)
        for key,(m,rows) in self.after.items():
            previous=self.before[key][0]
            self.assertEqual(m['limits'],previous['limits'])
            self.assertEqual(len(m['code']),len(previous['code']))
            offsets={r['offset'] for r in rows}
            for row in rows:
                if row['instruction'].startswith('if') or row['instruction']=='jump':
                    self.assertIn(row['args'][0],offsets)

    def test_cached_and_new_player_receive_new_name_without_losing_career(self):
        method,rows=self.after['codo.objects:PlayerObj/getObj']
        for cached in (False,True):
            name=['First'];created=[];war={};career={}
            scope={'_playerObj':{'name':'Old','careerData':career} if cached else None,
                   'CFunctionContainer':{'AS_GetWarGodPlayerData':lambda:war},
                   'DataID':{'PLAYER_USER_NAME':'player_user_name'},
                   'DataManager':{'getInstance':lambda:{'getDP':lambda key:name[0]}}}
            def make():
                created.append(True);scope['_playerObj']={'name':'Old','careerData':career}
            scope['makePlayerObj']=make
            for value in ('First','中文玩家','Player_2'):
                name[0]=value
                result,peak=execute(method,rows,scope,[scope])
                self.assertIs(result,scope['_playerObj'])
                self.assertEqual(result['name'],value)
                self.assertIs(result['careerData'],career)
                self.assertIs(career['wargodPlayerInfo'],war)
                self.assertLessEqual(peak,2)
            self.assertEqual(len(created),0 if cached else 1)

    def test_success_has_local_message_and_never_restarts_or_saves_stats(self):
        method,rows=self.after['codo.controllers:MarketController/private:onRedeemChangeNameComplete']
        calls=[]
        controller={'hideMsg':lambda:calls.append(('hide',)),
                    'prevPage':lambda:calls.append(('prev',)),
                    'showMsg':lambda *a:calls.append(('show',*a)),
                    'broadcast':lambda *a:calls.append(('broadcast',*a))}
        scope={'ResultObj':{'RESULT_SUCCESS':1},
               'BCEvent':{'EVT_REFRESH_UI_AFTER_INVENTORY_CHANGE':'evt_refresh_ui_after_inventory_change'}}
        execute(method,rows,scope,[controller,{'result':1}])
        self.assertEqual(calls[:2],[('hide',),('prev',)])
        self.assertEqual(calls[2],('broadcast','evt_on_career_info_updated'))
        self.assertEqual(calls[3],('broadcast','evt_refresh_ui_after_inventory_change'))
        self.assertEqual(calls[4][1].strip(),'改名成功。')
        self.assertEqual(calls[4][2],1)
        self.assertEqual(len(calls),5)

    def test_success_refreshes_existing_card_through_real_listener_and_getter(self):
        complete = self.after['codo.controllers:MarketController/private:onRedeemChangeNameComplete']
        listener = self.after['codo.component.playercard:PlayerCardShared/private:onCareerInfoUpdate']
        getter = self.after['codo.objects:PlayerObj/getObj']
        native_name = ['Old']
        player = {'name':'Old','careerData':{}}
        scope = {'_playerObj':player,
                 'CFunctionContainer':{'AS_GetWarGodPlayerData':lambda:{}},
                 'DataID':{'PLAYER_USER_NAME':'player_user_name'},
                 'DataManager':{'getInstance':lambda:{'getDP':lambda key:native_name[0]}}}
        rendered = []
        class Card(dict):
            def __setitem__(self, key, value):
                super().__setitem__(key,value)
                if key == 'data': rendered.append(value['name'])
        card = Card()
        inventory_refreshes = []
        def broadcast(event):
            if event == 'evt_refresh_ui_after_inventory_change':
                inventory_refreshes.append(event)
                return
            self.assertEqual(event,'evt_on_career_info_updated')
            execute(*listener,{'PlayerObj':{'getObj':lambda:execute(*getter,scope,[scope])[0]}},[card,None])
        controller = {'hideMsg':lambda:None,'prevPage':lambda:None,
                      'showMsg':lambda *a:None,'broadcast':broadcast}
        for name in ('First','中文玩家','Player_2'):
            native_name[0] = name
            execute(*complete,{'ResultObj':{'RESULT_SUCCESS':1},
                              'BCEvent':{'EVT_REFRESH_UI_AFTER_INVENTORY_CHANGE':'evt_refresh_ui_after_inventory_change'}},
                    [controller,{'result':1}])
            self.assertEqual(rendered[-1],name)
        execute(*complete,{'ResultObj':{'RESULT_SUCCESS':1}},[controller,{'result':0,'data':None}])
        self.assertEqual(rendered,['First','中文玩家','Player_2'])
        self.assertEqual(len(inventory_refreshes),3)

    def test_failed_result_cannot_run_success_branch(self):
        method,rows=self.after['codo.controllers:MarketController/private:onRedeemChangeNameComplete']
        calls=[];controller={'hideMsg':lambda:calls.append('hide')}
        execute(method,rows,{'ResultObj':{'RESULT_SUCCESS':1}},[controller,{'result':0,'data':None}])
        self.assertEqual(calls,['hide'])


if __name__=='__main__':unittest.main()
