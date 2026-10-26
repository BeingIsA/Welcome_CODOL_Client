"""重构差分验证：原生安装轨迹与真实档案初始化字节等价。"""
import ast
import ctypes as C
from ctypes import wintypes as W
import copy
import json
from pathlib import Path
import re
import struct
import sys
import tempfile
import textwrap
import types
import unittest
import pefile

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'client/src'))
BASE=ROOT/'client/tests/fixtures/refactor-20261020'
# 2026-10-22 重冻结基线：问题 2/7/8/10 修复后的当前源码快照，守护后续漂移。
# multiplayer_bridge.py 于 2026-10-23 单独重冻结，原因见本目录 README.md。
BASE22=ROOT/'client/tests/fixtures/refactor-20261022'
PKG=ROOT/'client/src/codol_rebuild'
CAPTURE=ROOT/'client/tests/fixtures/local-profile-capture'
IMAGE=ROOT/'client/bin/v1649370/codol1649370-offline.exe'
REPLACEMENTS={'codol_rebuild.accounts.bootstrap_service':'codol_rebuild.application.accounts.service',
 'codol_rebuild.analysis.bootstrap':'codol_rebuild.analysis.pe_recovery',
 'codol_rebuild.profiles.unlock':'codol_rebuild.adapters.game.v1649370.accounts.profile_unlock',
 'codol_rebuild.accounts':'codol_rebuild.application.accounts','codol_rebuild.storage':'codol_rebuild.adapters.persistence',
 'codol_rebuild.profiles':'codol_rebuild.adapters.game.profile_format','codol_rebuild.client.v1649370':'codol_rebuild.adapters.game.v1649370'}
PROFILE_SYMBOL_MODULES = {
 'VIEW_TRACE_POINTS': 'viewer', 'VIEW_RESULT_POINTS': 'viewer',
 'view_state': 'viewer', 'view_result': 'viewer',
 'LOCAL_PLAYLIST_CONTRACT': 'playlist',
 'validate_local_playlist_contract': 'playlist', 'build_local_playlist_adapter': 'playlist',
 'local_optional_service_patches': 'local_ui_services', 'local_welcome_page_patch': 'local_ui_services',
}

def old_source(path):
    """只在加载时适配历史导入路径，冻结夹具的算法与原始文件保持不变。"""
    text=(BASE/path).read_text(encoding='utf-8-sig')
    for a,b in REPLACEMENTS.items():text=text.replace(a,b)
    def moved_import(match):
        groups = {}
        for item in match.group(2).split(','):
            item = item.strip()
            module = PROFILE_SYMBOL_MODULES.get(item.split()[0], 'profile')
            groups.setdefault(module, []).append(item)
        return '\n'.join(f'{match.group(1)}from codol_rebuild.adapters.game.v1649370.{module} import {",".join(names)}'
                         for module, names in groups.items())
    text = re.sub(r'^([ \t]*)from codol_rebuild\.adapters\.game\.v1649370\.profile import ([^\n]+)$',
                  moved_import, text, flags=re.MULTILINE)
    return text

class MemoryProcess:
    """固定镜像只读底图加独立写入记录，不访问运行游戏。"""
    def __init__(self):
        self.pe=pefile.PE(str(IMAGE));self.memory={};self.cursor=0x20000000;self.writes=[];self.protections=[]
        self.main_thread_id=123
    def allocate(self,size=4096):
        address=self.cursor;self.cursor+=(size+4095)&~4095
        self.memory.update({i:0 for i in range(address,address+size)})
        return address
    def read(self,address,size):
        data=self.pe.get_data(address-0x400000,size) if 0x400000<=address<0x10000000 else bytes(size)
        data=data.ljust(size,b'\0')
        return bytes(self.memory.get(address+i,data[i]) for i in range(size))
    def write(self,address,data):
        self.writes.append((address,bytes(data)))
        self.memory.update({address+i:b for i,b in enumerate(data)})
    def executable(self,address,size=4096):self.protections.append((address,size))
    def periodic(self,interval):
        from codol_rebuild.runtime.scheduler import PeriodicTask
        return PeriodicTask(interval)

class Debugger:
    def __init__(self,process):self.process=process;self.breaks={}
    def arm(self,address,label):
        if address not in self.breaks:
            self.breaks[address]=(self.process.read(address,1),label);self.process.write(address,b'\xcc')

class RefactorEquivalenceTests(unittest.TestCase):
    def test_native_generators_unchanged(self):
        """原生生成器漂移守护：当前代码须与 2026-10-22 冻结快照逐模块 AST 等价。

        快照冻结于问题 2/7/8/10 修复后；有意修改这些模块时应重新冻结并说明原因。
        旧 refactor-20261020 基线专为一次性迁移差分设计，其模块拆分特殊分支
        不适用于同构快照，已被本比较取代。
        """
        files=list((BASE22/'client/v1649370').glob('*.py'))
        self.assertGreater(len(files),10)
        for old in files:
            if old.name=='image.py':continue
            baseline=ast.parse(old.read_text(encoding='utf-8'))
            current_path = next((PKG/'adapters/game/v1649370').rglob(old.name), PKG/'adapters/game/v1649370'/old.name)
            current=ast.parse(current_path.read_text(encoding='utf-8'))
            self.assertEqual(ast.dump(baseline),ast.dump(current),old.name)
        # 格式模块迁入的版本身份保持显式断言。
        from codol_rebuild.adapters.game.v1649370 import layout
        self.assertEqual(layout.BUFFER_CAPACITY,0x7ffc)
        self.assertEqual(layout.PUBLIC_IDENTITY,{'version':233,'checksum':0x4a6cc345,'size':15767})
        self.assertEqual(layout.SECURE_IDENTITY,{'version':19,'checksum':0xa34378b4,'size':15124})
        self.assertEqual(layout.KNOWN_DEFINITIONS,{
            'playerdata':bytes.fromhex('e900000045c36c4a'),
            'playersecuredata':bytes.fromhex('13000000b47843a3')})

    def test_full_local_install_trace_equivalent(self):
        """安装轨迹黄金基线：问题 2 修复后当前桥的字节级轨迹，替代 10-20 冻结单体比较。

        旧单体基线不含改名卡消费回写钩子（额外的代码页、指针修补与跳转），
        与当前行为的有意差异已固化为本夹具；后续改动偏离轨迹时应先更新此基线并说明原因。
        """
        from codol_rebuild.adapters.game.v1649370.session.session_bridge import GameSessionBridge
        def enc(value):
            if isinstance(value,bytes):return {'hex':value.hex()}
            if isinstance(value,(tuple,list)):return [enc(item) for item in value]
            if isinstance(value,dict):return {str(key):enc(item) for key,item in value.items()}
            return value
        for local,golden_name in [(False,'install-trace-online.json'),(True,'install-trace-local.json')]:
            with self.subTest(local_profile=local), tempfile.TemporaryDirectory() as tmp:
                args=types.SimpleNamespace(local_profile=local)
                after=MemoryProcess();adebug=Debugger(after)
                bridge=GameSessionBridge(args,Path(tmp),IMAGE,after.pe,{},lambda row:None,lambda name:None,after,None,(ValueError,))
                bridge.bind_debugger(adebug);bridge.install_lobby_adapters()
                actual=enc({'writes':after.writes,'protections':after.protections,'breaks':adebug.breaks,
                            'profile_traps':bridge.profile_traps,'preview_traps':bridge.preview_traps,
                            'name_adapter':bridge.name_adapter})
                expected=json.loads((BASE22/golden_name).read_text(encoding='utf-8'))
                self.assertEqual(expected,actual,f'local_profile={local}')

    def test_real_profile_bootstrap_equivalent(self):
        from codol_rebuild.application.accounts.service import bootstrap
        from codol_rebuild.adapters.game.v1649370.accounts.profile_codec import AccountCodec
        captured=json.loads((CAPTURE/'capture.json').read_text(encoding='utf-8'))
        schemas={kind:json.loads((CAPTURE/label/'playerdata-schema.json').read_text(encoding='utf-8'))
                 for kind,label in [('public','playerdata'),('secure','playersecuredata')]}
        ns={};exec(old_source('accounts/bootstrap_service.py'),ns)
        class Store:
            def __init__(self):self.state={'uid':0x8765432198765432,'profiles':{},'revision':0};self.calls=[]
            def identity(self,name):return self.state
            def load(self,name,identities):return copy.deepcopy(self.state)
            def save(self,name,revision,profiles,identities):
                self.calls.append((name,revision,profiles,identities))
                assert revision==self.state['revision']
                self.state={**self.state,'profiles':profiles,'revision':revision+1}
                return revision+1
        before=Store();after=Store();codec=AccountCodec(schemas,captured['tables'],captured['catalog'])
        # 旧档保留全解锁行为；新档授权策略另有验证。
        ns['bootstrap'](before,schemas,captured['tables'],captured['catalog'])
        after.state=copy.deepcopy(before.state)
        before.calls.clear()
        for cycle in range(2):
            old=ns['bootstrap'](before,schemas,captured['tables'],captured['catalog'])
            new=bootstrap(after,codec)
            self.assertEqual(old,new,f'cycle {cycle}')
            self.assertEqual(before.calls,after.calls)

    def test_account_failed_save_keeps_revision(self):
        from codol_rebuild.application.accounts.service import AccountSession
        from codol_rebuild.application.accounts.contracts import RevisionConflict
        class Codec:
            identities={}
            def validate_snapshot(self,blobs):return 545
        class Store:
            def save(self,*args):raise RevisionConflict('stale')
        service=AccountSession(Store(),Codec())
        service.account={'profiles':{'public':b'old','secure':b'old'},'revision':8}
        previous=copy.deepcopy(service.account)
        with self.assertRaises(RevisionConflict):service.save({'public':b'new','secure':b'new'})
        self.assertEqual(previous,service.account)

if __name__=='__main__':unittest.main()

