"""编译运行异步改名桥的 32 位固定地址夹具；不等同真实 UI 验收。"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from codol_rebuild.adapters.game.v1649370 import player_name


class NativeRenameTests(unittest.TestCase):
    def test_deferred_completion_stack_registers_and_rollback(self):
        compiler = Path('D:/Games/CODOL/GameServer/.toolchain/msys64/mingw32/bin/g++.exe')
        if not compiler.exists():
            self.skipTest('需要本机 MinGW32 编译器与 Windows x86 执行环境')
        project = Path(__file__).resolve().parents[2]
        workspace_root = project/'verification/client/workspaces/test-temp'
        workspace_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=workspace_root) as directory:
            workspace = Path(directory)
            addresses = {name: getattr(player_name, name)+0x40000000 for name in (
                'HANDLER', 'SETTER', 'COMPLETE', 'USERNAME', 'DW', 'ERROR_STRING',
                'EMPTY_STRING', 'REFRESH_INVENTORY', 'REFRESH_PREVIEW')}
            with patch.multiple(player_name, **addresses):
                plan = player_name.build_adapter(0x20000000, 0x21000000, 0x22000000,
                                                  0x23000000, deferred=True)
            header = 'static const unsigned char helper_code[] = {'+','.join(map(str, plan['code']))+'};\n'
            header += ''.join(f'#define TRAP_{phase.upper()} {address:#x}u\n'
                              for address, phase in plan['traps'].items())
            header += f'#define POLL_ENTRY {plan["poll"]:#x}u\n'
            header += ''.join(f'#define N_{name} {address:#x}u\n' for name, address in addresses.items())
            (workspace/'server_rename_data.h').write_text(header, encoding='ascii')
            environment = dict(os.environ)
            environment['PATH'] = str(compiler.parent)+os.pathsep+environment.get('PATH', '')
            executable = workspace/'server_rename_fixture.exe'
            compiled = subprocess.run([str(compiler), '-std=c++17', '-O2', '-Wall', '-Wextra', '-static',
                '-Wl,--image-base,0x10000000,--disable-dynamicbase', '-I', str(workspace),
                str(Path(__file__).parent/'native/server_rename_fixture.cpp'), '-o', str(executable)],
                env=environment, capture_output=True, text=True, timeout=30)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            for attempt_index in range(5):
                result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=20)
                if result.returncode != 2 or not result.stdout.startswith('allocation failed:'):
                    break
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            self.assertEqual(json.loads(result.stdout), {'passed': True, 'cases': 28})
