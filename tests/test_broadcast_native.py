"""独立 x86 进程执行广播桩；需要 Windows .NET 编译器，不等同真实 UI 验收。"""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest
from codol_rebuild.adapters.game.v1649370.broadcast import build_adapter, encode_message
from test_broadcast import server_message
from test_room_list_native import relocate_fixture_calls


class BroadcastNativeTests(unittest.TestCase):
    def test_completion_and_push_execute_independently_with_native_stack_contract(self):
        compiler = Path(os.environ.get('WINDIR', 'C:/Windows'))/'Microsoft.NET/Framework/v4.0.30319/csc.exe'
        if not compiler.exists():
            self.skipTest('需要 Windows .NET Framework x86 编译器')
        project = Path(__file__).resolve().parents[2]
        work = project/'verification/client/workspaces/test-temp'
        work.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=work, prefix='broadcast-native-') as directory:
            workspace = Path(directory)
            plan = build_adapter(0x20000000, 0x21000000)
            instructions = bytearray(plan['code'])
            for address in plan['traps']:
                instructions[address - 0x20000000] = 0x90
            relocate_fixture_calls(instructions, 0x20000000)
            (workspace/'code.bin').write_bytes(instructions)
            (workspace/'message.bin').write_bytes(encode_message(server_message()['payload'], '12345', 0x21000000))
            executable = workspace/'fixture.exe'
            build = subprocess.run([str(compiler), '/nologo', '/platform:x86', '/out:' + str(executable),
                                    str(project/'client/tests/native/broadcast_fixture.cs')], capture_output=True, timeout=30)
            self.assertEqual(build.returncode, 0, build.stdout.decode('mbcs', errors='replace'))
            for native_type, speaker_type in enumerate(('large', 'small')):
                with self.subTest(speaker_type=speaker_type):
                    (workspace/'message.bin').write_bytes(encode_message(
                        server_message(speaker_type)['payload'], '12345', 0x21000000))
                    result = subprocess.run([str(executable), str(workspace/'code.bin'), str(plan['poll']),
                                             str(workspace/'message.bin'), str(native_type)],
                                            capture_output=True, timeout=20)
                    self.assertEqual(result.returncode, 0,
                                     (result.stdout + result.stderr).decode('mbcs', errors='replace'))



if __name__ == '__main__':
    unittest.main()
