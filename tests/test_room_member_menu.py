"""房主菜单、原生 ABI 与踢人业务回归；模拟测试不代替原生 UI 验收。"""
import hashlib
from pathlib import Path
import struct
import sys
import unittest
from unittest.mock import Mock

from codol_rebuild.adapters.game.v1649370 import room_member_menu as menu, ui_movie
from codol_rebuild.application.room.service import RoomSession
from codol_rebuild.application.room.contracts import IdentityRecord, RoomError
from codol_rebuild.adapters.transport.http import HttpGateway
from test_refactor_equivalence import MemoryProcess

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'research'))
from trace_matchmaking_debugline import Reader, abc_parse, disassemble


def methods(movie):
    """从真实电影重新解析修改后的方法、常量池和栈限制。"""
    r = Reader(movie); r.p = 8+(5+4*(movie[8]>>3)+7)//8+4
    while r.p < len(movie):
        head = int.from_bytes(r.raw(2), 'little'); kind, size = head>>6, head&63
        if size == 63: size = int.from_bytes(r.raw(4), 'little')
        payload = r.raw(size)
        if kind != 82: continue
        a = Reader(payload); a.raw(4)
        if a.cstr() == 'codo/controllers/RightClickMenuController':
            return abc_parse(payload[a.p:])
    raise AssertionError('缺少菜单 ABC')


def evaluate(method, names, strings, owner, *args):
    """独立执行本补丁使用的 AVM2 指令，检查分支、操作数栈及选项过滤。"""
    rows = disassemble(method, names, strings)
    indices = {r['offset']: i for i, r in enumerate(rows)}
    locals_ = [owner, *args]+[None]*8; stack = []; pc = 0
    def key(name): return name.split('::')[-1]
    def get(obj, name):
        if name == 'length': return len(obj)
        return obj[name]
    for _ in range(1000):
        row = rows[pc]; pc += 1; op, values = row['instruction'], row['args']
        if op in ('nop', 'label', 'debug', 'debugline', 'debugfile'): continue
        if op.startswith('getlocal'): stack.append(locals_[int(op[-1])] if op[-1].isdigit() else locals_[values[0]])
        elif op.startswith('setlocal'): locals_[int(op[-1]) if op[-1].isdigit() else values[0]] = stack.pop()
        elif op == 'pushscope': stack.pop()
        elif op == 'getlex': stack.append(owner['globals'][key(values[0])])
        elif op == 'findpropstrict': stack.append(owner['globals'])
        elif op == 'getproperty':
            name = stack.pop() if values[0] == '<late-multiname>' else key(values[0])
            stack.append(get(stack.pop(), name))
        elif op == 'setproperty':
            value = stack.pop(); name = stack.pop() if values[0] == '<late-multiname>' else key(values[0])
            stack.pop()[name] = value
        elif op == 'callproperty':
            argv = [stack.pop() for _ in range(values[1])][::-1]
            stack.append(get(stack.pop(), key(values[0]))(*argv))
        elif op == 'constructprop':
            argv = [stack.pop() for _ in range(values[1])][::-1]; stack.append(stack.pop()[key(values[0])](*argv))
        elif op == 'istypelate':
            typ = stack.pop(); stack.append(isinstance(stack.pop(), typ))
        elif op == 'coerce': pass
        elif op == 'kill': locals_[values[0]] = None
        elif op == 'pushnull': stack.append(None)
        elif op == 'pop': stack.pop()
        elif op == 'pushshort': stack.append(values[0])
        elif op == 'convert_i': stack.append(int(stack.pop()))
        elif op == 'increment_i': stack.append(int(stack.pop())+1)
        elif op == 'astypelate':
            typ=stack.pop(); value=stack.pop(); stack.append(value if isinstance(value,typ) else None)
        elif op == 'lookupswitch':
            index=stack.pop(); default,cases=values
            pc=indices[cases[index] if 0 <= index < len(cases) else default]
        elif op in ('ifstrictne', 'iflt'):
            b=stack.pop(); a=stack.pop()
            take=(type(a)!=type(b) or a!=b) if op=='ifstrictne' else a<b
            if take: pc=indices[values[0]]
        elif op == 'pushbyte': stack.append(values[0])
        elif op == 'pushstring': stack.append(values[0])
        elif op == 'pushtrue': stack.append(True)
        elif op == 'pushfalse': stack.append(False)
        elif op == 'dup': stack.append(stack[-1])
        elif op == 'equals': b=stack.pop(); stack.append(stack.pop()==b)
        elif op == 'strictequals': b=stack.pop(); a=stack.pop(); stack.append(type(a)==type(b) and a==b)
        elif op == 'convert_b': stack.append(bool(stack.pop()))
        elif op == 'inclocal_i': locals_[values[0]] += 1
        elif op == 'jump': pc = indices[values[0]]
        elif op in ('iftrue', 'iffalse'):
            v = bool(stack.pop())
            if v == (op == 'iftrue'): pc = indices[values[0]]
        elif op == 'ifge':
            b=stack.pop(); a=stack.pop()
            if a>=b: pc=indices[values[0]]
        elif op == 'returnvalue':
            result=stack.pop()
            if stack: raise AssertionError('方法返回时栈不为空')
            return result
        else: raise AssertionError(op)
        if len(stack)>method['limits'][0]: raise AssertionError('超出 max_stack')
    raise AssertionError('菜单循环未结束')


class MenuTests(unittest.TestCase):
    def test_original_menu_items_and_party_behavior_survive(self):
        raw=(ROOT/'client/workspaces/ui-investigation/codo.gfx').read_bytes()
        ms,names,strings=methods(ui_movie.patch_movie(raw))
        generate=next(m for m in ms if 'generateRightClickMenu' in m['name'])
        check=next(m for m in ms if 'canKickPartyMember' in m['name'])
        for in_room, host, target, party, expected in (
            (True,True,'other',False,True), (True,False,'other',False,False),
            (True,True,'self',False,False), (False,True,'other',True,True),
            (False,False,'other',True,False), (True,True,'absent',False,False)):
            owner={'isInRoom':lambda:in_room, 'isMe':lambda u:u['id']=='self',
                   'isPartyHost':lambda:host, 'isRankedMatch':lambda:False,
                   'isInMyParty':lambda u:party}
            owner['globals']={'RightClickMenuType':{'FULL':'full','DETAIL_ONLY':'detail','BLACKLIST':'blacklist'},
                'UserObj':dict, 'DictParty':{'KICK_OUT_PARTY':'踢出小队'},
                'CFunctionContainer':{'GetRoomInfo':lambda mode,id:'踢出房间' if mode==2 else host and id=='other'}}
            option={'enableChecker':lambda u:evaluate(check,names,strings,owner,u)}
            keep={'label':'详细信息','enableChecker':lambda u:True}
            hidden={'label':'删除好友','enable':True,'enableChecker':lambda u:False}
            owner.update(_optKickPartyMember=option,_rightClickMenuItemsFull=[keep,option,hidden],
                         _rightClickMenuItemsDetailOnly=[keep],_rightClickMenuItemsBlacklist=[])
            result=evaluate(generate,names,strings,owner,'full',{'id':target})
            self.assertIs(result[0],keep);self.assertTrue(keep['enable'])
            self.assertFalse(hidden['enable'])
            self.assertEqual(option['enable'],expected)
            self.assertEqual(option['label'],'踢出房间' if in_room else '踢出小队')

    def test_native_menu_selection_and_filter_bytes_are_unchanged(self):
        raw=(ROOT/'client/workspaces/ui-investigation/codo.gfx').read_bytes()
        patched_movie=ui_movie.patch_movie(raw)
        original_methods,_,_=methods(raw)
        patched_methods,_,_=methods(patched_movie)
        original=bytes.fromhex(next(m for m in original_methods if 'generateRightClickMenu' in m['name'])['code'])
        patched=bytes.fromhex(next(m for m in patched_methods if 'generateRightClickMenu' in m['name'])['code'])
        prefix_rules=[r for r in menu.replacements() if r[0]==menu.GENERATE_AT]
        self.assertEqual(len(prefix_rules),1)
        _,before,after=prefix_rules[0]
        self.assertEqual(len(before),40)
        self.assertEqual(len(after),40)
        self.assertEqual(before,original[:40])
        self.assertEqual(patched[:40],after)
        self.assertEqual(len(patched),len(original))
        self.assertEqual(patched[40:],original[40:])
        self.assertEqual(patched[36:40],bytes.fromhex('208054d7'))
        # 所有补丁范围均不得覆盖原生方法后续的菜单选择和筛选指令。
        for offset,before,after in menu.replacements():
            self.assertTrue(offset+len(before)<=menu.GENERATE_AT+40
                            or offset>=menu.GENERATE_AT+len(original))

    def test_native_query_and_click_map_identity_once_and_preserve_fallback(self):
        process=MemoryProcess();port=Mock();state=['rm_a',True,'pl_me',['pl_me','pl_other']]
        bridge=menu.RoomMemberMenu(process,port,lambda:tuple(state),Mock(),Mock());traps=bridge.install()
        frame=process.allocate();params=process.allocate();args=process.allocate();result=process.allocate();text=process.allocate()
        identity=str(int.from_bytes(hashlib.sha256(b'pl_other').digest()[:8],'little'))
        process.write(text,identity.encode()+b'\0')
        process.write(frame+40,struct.pack('<I',params));process.write(params,struct.pack('<I',result))
        process.write(params+16,struct.pack('<II',args,2));process.write(result+12,struct.pack('<I',1))
        process.write(args+12,struct.pack('<II',3,0));process.write(args+44,struct.pack('<II',6,text))
        ctx=bytearray(716);struct.pack_into('<I',ctx,180,frame)
        bridge.handle('menu_query',ctx)
        self.assertEqual(process.read(result+12,8),struct.pack('<II',2,1))
        process.write(params+20,struct.pack('<I',0));bridge.handle('menu_query',ctx)
        self.assertEqual(struct.unpack_from('<I',ctx,184)[0],bridge.plans['menu_query'][1])
        process.write(params+20,struct.pack('<I',1));process.write(args+12,struct.pack('<II',6,text))
        port.begin_kick.return_value=True
        state[1]=False  # 点击路径不重复执行菜单权限判断，由服务端裁决。
        bridge.handle('menu_kick',ctx);bridge.handle('menu_kick',ctx)
        port.begin_kick.assert_called_once_with('rm_a','pl_other')
        state[0]=None;bridge.handle('menu_kick',ctx)
        self.assertEqual(struct.unpack_from('<I',ctx,184)[0],bridge.plans['menu_kick'][1])

    def test_http_contract(self):
        gateway=HttpGateway('http://127.0.0.1:8080');gateway._request=Mock(return_value={})
        gateway.kick('pl_me','rm_a','pl_other')
        gateway._request.assert_called_once_with('POST','/api/v1/rooms/rm_a/kick',{'targetPlayerId':'pl_other'},'pl_me')

    def test_service_delegates_permissions_without_readback_and_delivers_results(self):
        gateway=Mock();session=RoomSession(gateway,Mock(),'http://localhost','test')
        session._record=IdentityRecord('pl_me','rm_a');session._is_owner=True
        data={'roomId':'rm_a','members':[{'playerId':'pl_me'}]}
        gateway.kick.side_effect=RoomError('NETWORK','回包丢失',uncertain=True)
        gateway.room_detail.return_value=data;gateway.member_profile.return_value={}
        try:
            self.assertTrue(session.begin_kick('rm_a','pl_other'))
            self.assertFalse(session.begin_kick('rm_a','pl_other'))
            session._kick_pending.result(timeout=3)
            self.assertEqual(session.poll_kick().error.code, 'NETWORK')
            gateway.kick.assert_called_once();gateway.room_detail.assert_not_called()
            gateway.kick.side_effect=RoomError('FORBIDDEN','服务端拒绝')
            self.assertIsNotNone(session._execute_kick('rm_a','pl_me').error)
            session._is_owner=False
            self.assertIsNotNone(session._execute_kick('rm_a','pl_other').error)
            self.assertEqual(gateway.kick.call_count, 3)
            gateway.kick.assert_called_with('pl_me','rm_a','pl_other')
            gateway.kick.side_effect=None;gateway.kick.return_value=data
            self.assertIsNone(session._execute_kick('rm_a','pl_other').error)
            self.assertTrue(session.begin_kick('rm_a','pl_other'));session._kick_pending.result(timeout=3)
            session._record=IdentityRecord('pl_me')
            self.assertEqual(session.poll_kick().operation,'kick')
        finally:
            session._record=None;session.close()
