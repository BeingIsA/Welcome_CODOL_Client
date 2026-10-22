"""在独立32位进程中执行大厅发布桩；需 Windows 自带 .NET Framework C# 编译器。"""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest
from codol_rebuild.adapters.game.v1649370.room_list import build_adapter, encode_rows
from test_room_list import room


class NativeRoomListTests(unittest.TestCase):
    def test_publisher_executes_with_gfx_abi_and_releases_every_value(self):
        compiler = Path(os.environ.get('WINDIR', 'C:/Windows'))/'Microsoft.NET/Framework/v4.0.30319/csc.exe'
        if not compiler.exists():
            self.skipTest('需要 Windows .NET Framework x86 C# 编译器；不代表实机验收')
        root = Path(__file__).resolve().parents[2]
        work = root/'verification/client/workspaces/test-temp'
        work.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=work, prefix='room-list-native-') as temp:
            folder = Path(temp)
            codefile, rowsfile, emptyfile = [folder/name for name in ('code.bin','rows.bin','empty.bin')]
            plan = build_adapter(0x20000000, 0x21000000)
            code = bytearray(plan['code'])
            # 独立进程没有调试宿主；仅将完成通知陷阱替换 NOP，查询钩子不执行。
            for address, phase in plan['traps'].items():
                if phase == 'room_list_published':
                    code[address-0x20000000] = 0x90
            codefile.write_bytes(code)
            rowsfile.write_bytes(encode_rows([room('rm_a'),room('rm_b')], 'td',0x21000000)[0])
            emptyfile.write_bytes(encode_rows([], 'td',0x21000000)[0])
            exe = folder/'fixture.exe'
            build = subprocess.run([str(compiler),'/nologo','/platform:x86','/out:'+str(exe),
                                    str(Path(__file__).parent/'native/room_list_fixture.cs')],
                                   capture_output=True, timeout=30)
            self.assertEqual(build.returncode,0,build.stdout.decode('mbcs',errors='replace'))
            result = subprocess.run([str(exe),str(codefile),str(plan['publish']),str(rowsfile),str(emptyfile)],
                                    capture_output=True,timeout=20)
            self.assertEqual(result.returncode,0,result.stdout.decode('mbcs',errors='replace'))
