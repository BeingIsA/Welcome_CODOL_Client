"""独立 x86 进程执行生成代码及原生槽位函数，通知接收端使用桩。"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import pefile
from codol_rebuild.adapters.game.v1649370.rooms.member_profile import build_member_publish
from test_room_list_native import relocate_fixture_calls


class TeamNativeTests(unittest.TestCase):
    def test_twelve_participants_and_six_spectators(self):
        compiler = Path(os.environ.get('WINDIR', 'C:/Windows'))/'Microsoft.NET/Framework/v4.0.30319/csc.exe'
        if not compiler.exists():
            self.skipTest('需要 Windows .NET Framework x86 编译器')
        project = Path(__file__).resolve().parents[2]
        workspace_root = project/'verification/client/workspaces'
        workspace_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='team-native-', dir=workspace_root) as directory:
            workspace = Path(directory)
            image = pefile.PE(str(project/'client/bin/v1649370/codol1649370-offline.exe'))
            # 原函数内部仅相对跳转，可整体搬迁；夹具仅重定位外部调用地址。
            (workspace/'allocator.bin').write_bytes(image.get_data(0x629ff0-0x400000, 0x69))
            publisher = bytearray(build_member_publish(0x20000000, 0x21000000))
            relocate_fixture_calls(publisher, 0x20000000)
            (workspace/'publisher.bin').write_bytes(publisher)
            executable = workspace/'fixture.exe'
            compiled = subprocess.run([str(compiler), '/nologo', '/platform:x86', '/out:'+str(executable),
                str(project/'client/tests/native/room_team_fixture.cs')], capture_output=True, timeout=30)
            self.assertEqual(compiled.returncode, 0, compiled.stdout.decode('mbcs', errors='replace'))
            executed = subprocess.run([str(executable), str(workspace/'publisher.bin'), str(workspace/'allocator.bin')],
                capture_output=True, timeout=15)
            self.assertEqual(executed.returncode, 0, (executed.stdout+executed.stderr).decode('mbcs', errors='replace'))
