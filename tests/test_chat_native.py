"""独立 x86 夹具执行普通聊天发布桩；验证调用栈，不替代真实 UI 验收。"""
from pathlib import Path
import os
import subprocess
import struct
import tempfile
import unittest
import capstone
import pefile
from codol_rebuild.adapters.game.v1649370.chat.chat import build_adapter, SEND_CLEANUP
from codol_rebuild.adapters.game.v1649370.chat.chat_message import encode_message
from test_chat import server_message
from test_room_list_native import relocate_fixture_calls


class ChatNativeTests(unittest.TestCase):
    def test_original_send_cleanup_releases_gfx_and_restores_caller_stack(self):
        compiler = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Microsoft.NET/Framework/v4.0.30319/csc.exe'
        if not compiler.exists():
            self.skipTest('需要 Windows .NET Framework x86 编译器')
        project = Path(__file__).resolve().parents[2]
        image = pefile.PE(str(project / 'client/bin/v1649370/codol1649370-offline.exe'))
        cleanup_bytes = bytearray(image.get_data(SEND_CLEANUP - image.OPTIONAL_HEADER.ImageBase, 32))
        cleanup_instructions = list(capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32).disasm(
            cleanup_bytes, SEND_CLEANUP))
        call_targets = {0x436660: 0x20002000, 0xC3C20F: 0x28001300}
        self.assertEqual([int(instruction.op_str, 16) for instruction in cleanup_instructions
                          if instruction.mnemonic == 'call'], list(call_targets))
        self.assertEqual((cleanup_instructions[-1].mnemonic, cleanup_instructions[-1].op_str), ('ret', '4'))
        for instruction in cleanup_instructions:
            if instruction.mnemonic == 'call':
                byte_offset = instruction.address - SEND_CLEANUP
                struct.pack_into('<i', cleanup_bytes, byte_offset + 1,
                                 call_targets[int(instruction.op_str, 16)] - (0x20001000 + byte_offset + 5))
        release_bytes = image.get_data(0x436660 - image.OPTIONAL_HEADER.ImageBase, 47)
        workspace_root = project / 'verification/client/workspaces/test-temp'
        workspace_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=workspace_root, prefix='chat-cleanup-') as directory:
            workspace = Path(directory)
            (workspace / 'cleanup.bin').write_bytes(cleanup_bytes)
            (workspace / 'release.bin').write_bytes(release_bytes)
            executable = workspace / 'fixture.exe'
            build = subprocess.run([str(compiler), '/nologo', '/platform:x86', '/out:' + str(executable),
                                    str(project / 'client/tests/native/chat_cleanup_fixture.cs')],
                                   capture_output=True, timeout=30)
            self.assertEqual(build.returncode, 0, build.stdout.decode('mbcs', errors='replace'))
            result = subprocess.run([str(executable), str(workspace / 'cleanup.bin'), str(workspace / 'release.bin')],
                                    capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0,
                             (result.stdout + result.stderr).decode('mbcs', errors='replace'))

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
