"""独立 x86 进程执行准备适配，复用原生 GFx 返回值尾部；不替代游戏 UI 验收。"""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest
from unittest.mock import Mock

from codol_rebuild.adapters.game.v1649370.rooms.room import RoomReadyBridge, build_ready_notifications
from test_refactor_equivalence import MemoryProcess
from test_room_list_native import relocate_fixture_calls


class ReadyNativeTests(unittest.TestCase):
    def test_gfx_return_and_notification_stack(self):
        compiler = Path(os.environ.get('WINDIR', 'C:/Windows'))/'Microsoft.NET/Framework/v4.0.30319/csc.exe'
        if not compiler.exists():
            self.skipTest('需要 Windows .NET Framework x86 编译器')
        project = Path(__file__).resolve().parents[2]
        workspace_root = project/'verification/client/workspaces/ready-tests'
        workspace_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='native-', dir=workspace_root) as directory:
            workspace = Path(directory)
            process = MemoryProcess()
            adapter = RoomReadyBridge(process, Mock(), lambda: None, lambda: False)
            traps = adapter.install()
            for phase, entry in (('set_ready', 0x20000000), ('get_ready', 0x20001000)):
                instruction_bytes = bytearray(next(value for address, value in process.writes if address == entry))
                for address, trap_phase in traps.items():
                    if trap_phase == phase:
                        instruction_bytes[address-entry] = 0x90
                relocate_fixture_calls(instruction_bytes, entry)
                (workspace/(phase+'.bin')).write_bytes(instruction_bytes)
            notification_bytes = bytearray(build_ready_notifications(0x20002000, 0x21000000))
            relocate_fixture_calls(notification_bytes, 0x20002000)
            (workspace/'notify.bin').write_bytes(notification_bytes)
            (workspace/'return.bin').write_bytes(process.read(0x7CF895, 0x38))
            executable = workspace/'fixture.exe'
            compiled = subprocess.run([str(compiler), '/nologo', '/platform:x86', '/out:'+str(executable),
                str(project/'client/tests/native/room_ready_fixture.cs')], capture_output=True, timeout=30)
            self.assertEqual(compiled.returncode, 0, compiled.stdout.decode('mbcs', errors='replace'))
            executed = subprocess.run([str(executable), str(workspace/'set_ready.bin'),
                str(workspace/'get_ready.bin'), str(workspace/'notify.bin'), str(workspace/'return.bin')],
                capture_output=True, timeout=15)
            self.assertEqual(executed.returncode, 0,
                             (executed.stdout+executed.stderr).decode('mbcs', errors='replace'))
