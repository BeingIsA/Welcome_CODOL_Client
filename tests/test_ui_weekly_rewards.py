"""解析组合电影并独立执行大厅每周奖励展示分支，验证范围及资源契约。"""
import sys,struct,zlib,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'research'))
from trace_matchmaking_debugline import Reader,abc_parse
from inspect_loadout_rename import disasm as raw_disasm

def disasm(method,names,strings):
 rows=raw_disasm(method,names,strings)
 # That legacy research decoder evaluates r.p before reading the s24 operand.
 # Resolve branch targets independently from the instruction bytes.
 code=bytes.fromhex(method['code'])
 for row in rows:
  if row['op'].startswith('branch_'):
   at=row['offset'];row['args']=[at+4+int.from_bytes(code[at+1:at+4],'little',signed=True)]
 return rows
from codol_rebuild.adapters.game.v1649370.session import ui_movie
from codol_rebuild.adapters.game.v1649370.frontend import ui_weekly_rewards

def board(movie):
 r=Reader(movie);r.p=8+(5+4*(movie[8]>>3)+7)//8+4
 while r.p<len(movie):
  h=struct.unpack('<H',r.raw(2))[0];k,n=h>>6,h&63
  if n==63:n=struct.unpack('<I',r.raw(4))[0]
  p=r.raw(n)
  if k!=82:continue
  a=Reader(p);a.raw(4)
  if a.cstr()=='codo/component/career/ChallengeChecklistBoard':
   ms,ns,ss=abc_parse(p[a.p:]);m=next(m for m in ms if m.get('trait')=='::setData');return m,disasm(m,ns,ss)
 raise AssertionError('missing board')

class WeeklyRewardTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.raw=(ROOT/'research/client/ui-investigation/codo.gfx').read_bytes()
  cls.movie=ui_movie.patch_movie(cls.raw);cls.method,cls.rows=board(cls.movie)
 def test_movie_contract(self):
  original,_=board(self.raw)
  self.assertEqual(self.method['limits'],[6,15,*original['limits'][2:]])
  self.assertEqual(len(original['code']),len(self.method['code']))
  self.assertLessEqual(len(zlib.compress(self.movie,9)),ui_movie.COMPRESSED_SIZE)
  offsets={r['offset'] for r in self.rows}
  for r in self.rows:
   self.assertFalse(r['op'].startswith('UNKNOWN'),r)
   if r['op'].startswith('branch_'):self.assertIn(r['args'][0],offsets)
 def run_display(self,lobby,name):
  rows=self.rows
  # Original buildRewards stores local7; the following guarded block is the patch.
  start=next(i+1 for i,r in enumerate(rows) if r['op']=='setlocal' and r['args']==[7])
  end=next(i for i in range(start,len(rows)-1) if rows[i]['op']=='getlocal' and rows[i]['args']==[7] and rows[i+1]['op']=='pushnull')
  original=[{'sentinel':'original reward'}]
  loc={0:{'parent':{'parent':{'parent':None,'home':lobby}}},1:{'name':name},7:original}
  klass={'REWARD_TYPE_ITEM':2,'REWARD_TYPE_GP':1,'REWARD_TYPE_XP':0}
  stack=[];pc=start;indices={r['offset']:i for i,r in enumerate(rows)}
  while pc<end:
   r=rows[pc];op=r['op'];a=r['args'];pc+=1
   if op.startswith('getlocal'):stack.append(loc[a[0] if op=='getlocal' else int(op[-1])])
   elif op=='setlocal':loc[a[0]]=stack.pop()
   elif op in ('pushbyte','pushshort','pushstring'):stack.append(a[0])
   elif op=='getproperty':
    key=stack.pop() if a[0]=='<late-multiname>' else a[0].split('::')[-1];stack.append(stack.pop()[key])
   elif op=='setproperty':v=stack.pop();stack.pop()[a[0].split('::')[-1]]=v
   elif op=='findpropstrict':stack.append({'ChallengeRewardObj':klass})
   elif op=='constructprop':stack.pop();stack.append({})
   elif op=='newarray':
    count=a[0];values=stack[-count:] if count else []
    if count:del stack[-count:]
    stack.append(values)
   elif op=='dup':stack.append(stack[-1])
   elif op=='pushfalse':stack.append(False)
   elif op=='pushtrue':stack.append(True)
   elif op=='istypelate':stack.pop();stack.append(bool(stack.pop().get('home')))
   elif op=='multiply':b=stack.pop();stack.append(stack.pop()*b)
   elif op=='callproperty':
    self.assertEqual(a[0],'flash.utils::getDefinitionByName');self.assertEqual(stack.pop(),'codo.views.HomeView');stack.pop();stack.append('HomeView')
   elif op=='pop':stack.pop()
   elif op=='branch_10':pc=indices[a[0]]
   elif op=='branch_11':
    if stack.pop():pc=indices[a[0]]
   elif op=='branch_12':
    if not stack.pop():pc=indices[a[0]]
   elif op=='branch_14':
    b=stack.pop()
    if stack.pop()!=b:pc=indices[a[0]]
   else:self.fail(r)
   self.assertLessEqual(len(stack),self.method['limits'][0])
  self.assertEqual(stack,[]);self.assertEqual(original,[{'sentinel':'original reward'}])
  return loc[7],original
 def test_weekly_lobby_has_requested_rewards(self):
  actual,original=self.run_display(True,'@SF_UI_WEEKLY_CHALLENGES')
  self.assertIsNot(actual,original)
  self.assertEqual(actual,[{'type':2,'entityInfo':15,'entityId':50006},{'type':1,'entityInfo':1500},{'type':0,'entityInfo':15000}])
 def test_other_views_keep_original_rewards(self):
  for lobby,name in [(False,'@SF_UI_WEEKLY_CHALLENGES'),(True,'@SF_UI_DAILY_CHALLENGES'),(False,'other')]:
   with self.subTest(lobby=lobby,name=name):
    actual,original=self.run_display(lobby,name);self.assertIs(actual,original)
 def test_catalog_contains_original_currency_asset(self):
  import json
  capture=Path(__file__).parent/'fixtures/local-profile-capture/capture.json'
  catalog=json.loads(capture.read_text())['catalog']
  self.assertTrue(any(r['id']==50006 and r['name']=='currency_shard' for r in catalog))

if __name__=='__main__':unittest.main()

