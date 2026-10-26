"""验证退出页计时器清理可重复执行，且原版弹窗关闭导航完整保留。"""
from pathlib import Path
import struct,sys,unittest,zlib
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'research'))
from trace_matchmaking_debugline import Reader,abc_parse,disassemble
from codol_rebuild.adapters.game.v1649370.session import ui_movie

def parse(movie,tag):
 r=Reader(movie);r.p=8+(5+4*(movie[8]>>3)+7)//8+4
 while r.p<len(movie):
  h=struct.unpack('<H',r.raw(2))[0];k,n=h>>6,h&63
  if n==63:n=struct.unpack('<I',r.raw(4))[0]
  p=r.raw(n)
  if k!=82:continue
  a=Reader(p);a.raw(4)
  if a.cstr()==tag:return abc_parse(p[a.p:])
 raise AssertionError(tag)

class QuitReturnTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.original=(ROOT/'research/client/ui-investigation/codo.gfx').read_bytes()
  cls.patched=ui_movie.patch_movie(cls.original)
  tag='codo/views/quit_parts/QuitGameSummaryContentNextLoginReward'
  before,_,_=parse(cls.original,tag);after,names,strings=parse(cls.patched,tag)
  cls.old=next(m for m in before if m.get('trait')=='::deactive')
  cls.new=next(m for m in after if m.get('trait')=='::deactive')
  cls.rows=disassemble(cls.new,names,strings)
 def run_cleanup(self,timer):
  obj={'mTimerCount':timer,'updateTimeToNextLogin':'callback'};stack=[];calls=[];pc=0
  targets={r['offset']:i for i,r in enumerate(self.rows)}
  while pc<len(self.rows):
   r=self.rows[pc];pc+=1;op,args=r['instruction'],r['args']
   if op=='getlocal0':stack.append(obj)
   elif op=='pushscope':stack.pop()
   elif op=='callsuper':self.assertIs(stack.pop(),obj);calls.append('super.deactive');stack.append(None)
   elif op=='findpropstrict':stack.append({'TimerEvent':{'TIMER':'timer'}})
   elif op=='getproperty':stack.append(stack.pop()[args[0].split('::')[-1]])
   elif op=='iffalse':
    if stack.pop() is None:pc=targets[args[0]]
   elif op=='callproperty':
    argc=args[1];argv=stack[-argc:] if argc else []
    if argc:del stack[-argc:]
    receiver=stack.pop();self.assertIs(receiver,timer);self.assertIsNotNone(receiver)
    calls.append((args[0].split('::')[-1],argv));stack.append(None)
   elif op=='pushnull':stack.append(None)
   elif op=='initproperty':v=stack.pop();stack.pop()[args[0].split('::')[-1]]=v
   elif op=='pop':stack.pop()
   elif op=='returnvoid':self.assertEqual(stack,[]);return calls,obj['mTimerCount']
   elif op!='nop':self.fail(r)
   self.assertLessEqual(len(stack),self.new['limits'][0])
  self.fail('missing return')
 def test_missing_timer_closes_without_dereference(self):
  self.assertEqual(self.run_cleanup(None),(['super.deactive'],None))
 def test_existing_timer_released_and_repeat_close_safe(self):
  calls,timer=self.run_cleanup(object())
  self.assertEqual(calls,['super.deactive',('reset',[]),('removeEventListener',['timer','callback'])]);self.assertIsNone(timer)
  self.assertEqual(self.run_cleanup(timer),(['super.deactive'],None))
 def test_native_close_route_restored_byte_for_byte(self):
  for tag in ('codo/controllers/SummaryController','codo/controllers/PageNavigationController','codo/base/PageManagerBase','codo/manager/PageManagerMain'):
   self.assertEqual(parse(self.original,tag),parse(self.patched,tag),tag)
 def test_limits_branches_and_capacity(self):
  self.assertEqual(self.old['limits'],self.new['limits'])
  self.assertEqual(len(self.old['code']),len(self.new['code']))
  offsets={r['offset'] for r in self.rows}
  for r in self.rows:
   if r['instruction']=='iffalse':self.assertIn(r['args'][0],offsets)
  self.assertLessEqual(len(zlib.compress(self.patched,9)),ui_movie.COMPRESSED_SIZE)
if __name__=='__main__':unittest.main()
