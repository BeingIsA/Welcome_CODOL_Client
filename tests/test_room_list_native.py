"""在独立32位进程中执行大厅发布桩；需 Windows 自带 .NET Framework C# 编译器。"""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest
import struct
import capstone
from codol_rebuild.adapters.game.v1649370.room_list import build_adapter, encode_rows
from test_room_list import room


def relocate_fixture_calls(code, entry):
    """夹具把游戏低地址调用搬到独立高地址，避免与 CLR/ASLR 映像冲突。"""
    for instruction in capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32).disasm(bytes(code), entry):
        if instruction.mnemonic in ('call', 'jmp') and instruction.op_str.startswith('0x'):
            target = int(instruction.op_str, 16)
            if target < 0x10000000:
                replacement = 0x28000000+(target & 0xffff)
                struct.pack_into('<I', code, instruction.address-entry+1,
                                 (replacement-instruction.address-5) & 0xffffffff)


class NativeRoomListTests(unittest.TestCase):
    def test_join_callbacks_execute_with_native_stack_contract(self):
        from codol_rebuild.adapters.game.v1649370.room import build_join_adapter
        from codol_rebuild.adapters.game.v1649370.server_session_bridge import build_tick
        compiler = Path(os.environ.get('WINDIR', 'C:/Windows'))/'Microsoft.NET/Framework/v4.0.30319/csc.exe'
        if not compiler.exists():
            self.skipTest('需要 Windows .NET Framework x86 C# 编译器')
        work = Path(__file__).resolve().parents[2]/'verification/client/workspaces/test-temp'
        work.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=work, prefix='room-join-native-') as temp:
            folder = Path(temp)
            plan = build_join_adapter(0x20000000, 0x21000000, 0x23000000)
            code = bytearray(plan['code'])
            for address in plan['traps']:
                code[address-0x20000000] = 0x90
            relocate_fixture_calls(code, 0x20000000)
            (folder/'code.bin').write_bytes(code)
            tick = build_tick(0x20002000, 0x21000000, 0x21000034, 0x28006000,
                              {**plan['entries'], 'create_complete': 0x28006000,
                               'leave_complete': 0x28006000})
            tick_code = bytearray(tick['code'])
            for address in tick['traps']:
                tick_code[address-0x20002000] = 0x90
            (folder/'tick.bin').write_bytes(tick_code)
            exe = folder/'fixture.exe'
            build = subprocess.run([str(compiler), '/nologo', '/platform:x86', '/out:'+str(exe),
                                    str(Path(__file__).parent/'native/room_join_fixture.cs')],
                                   capture_output=True, timeout=30)
            self.assertEqual(build.returncode, 0, build.stdout.decode('mbcs', errors='replace'))
            result = subprocess.run([str(exe), str(folder/'code.bin'),
                                     str(plan['entries']['join_complete']), str(plan['entries']['join_failed']),
                                     str(plan['entries']['settings_apply']), str(folder/'tick.bin'),
                                     str((0x7C46EB+5+struct.unpack('<i', plan['patches'][1]['after'][1:])[0]) & 0xffffffff)],
                                    capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, (result.stdout+result.stderr).decode('mbcs', errors='replace'))

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
            relocate_fixture_calls(code, 0x20000000)
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
