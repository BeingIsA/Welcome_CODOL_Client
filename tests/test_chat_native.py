"""独立 x86 夹具执行普通聊天发布桩；验证调用栈，不替代真实 UI 验收。"""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest
from codol_rebuild.adapters.game.v1649370.chat.chat import build_adapter
from codol_rebuild.adapters.game.v1649370.chat.chat_message import encode_message
from test_chat import server_message
from test_room_list_native import relocate_fixture_calls


class ChatNativeTests(unittest.TestCase):
    def test_lobby_and_room_render_without_speaker_completion(self):
        compiler = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Microsoft.NET/Framework/v4.0.30319/csc.exe'
        if not compiler.exists():
            self.skipTest('需要 Windows .NET Framework x86 编译器')
        project = Path(__file__).resolve().parents[2]
        work = project / 'verification/client/workspaces/test-temp'
        work.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=work, prefix='chat-native-') as directory:
            workspace = Path(directory)
            plan = build_adapter(0x20000000, 0x21000000)
            instructions = bytearray(plan['code'])
            for address in plan['traps']:
                instructions[address - 0x20000000] = 0x90
            relocate_fixture_calls(instructions, 0x20000000)
            (workspace / 'code.bin').write_bytes(instructions)
            executable = workspace / 'fixture.exe'
            build = subprocess.run([str(compiler), '/nologo', '/platform:x86', '/out:' + str(executable),
                                    str(project / 'client/tests/native/broadcast_fixture.cs')], capture_output=True, timeout=30)
            self.assertEqual(build.returncode, 0, build.stdout.decode('mbcs', errors='replace'))
            for channel_id, scope in ((0, 'lobby'), (3, 'room')):
                with self.subTest(scope=scope):
                    (workspace / 'message.bin').write_bytes(encode_message(
                        server_message(scope, '10')['payload'], '12345', 0x21000000, channel_id, -1))
                    result = subprocess.run([str(executable), str(workspace / 'code.bin'), str(plan['poll']),
                                             str(workspace / 'message.bin'), '-1', str(channel_id)],
                                            capture_output=True, timeout=20)
                    self.assertEqual(result.returncode, 0,
                                     (result.stdout + result.stderr).decode('mbcs', errors='replace'))


if __name__ == '__main__':
    unittest.main()
