"""通过小型构建夹具验证 Windows 程序发行清单及跨权限启动所需的目录继承。"""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


@unittest.skipUnless(os.name == 'nt', 'Windows 发行包和 NTFS 权限')
class HostReleaseTests(unittest.TestCase):
    def test_published_files_and_runtime_receipt_inherit_output_permissions(self):
        from codol_rebuild.platform.win32 import current_sid

        tool_path = Path(__file__).resolve().parents[1] / 'tools/build_host.py'
        module_spec = importlib.util.spec_from_file_location('build_host_for_test', tool_path)
        builder = importlib.util.module_from_spec(module_spec)
        with patch.object(sys, 'path', sys.path.copy()):
            module_spec.loader.exec_module(builder)

        runtime_files = (
            'bin/offline_guard.exe', 'bin/v1649370/codol1649370-offline.exe', 'config/server.ini',
            'manifests/offline-menu-patches.json', 'bin/v1649370/packed-reader/ifs_reader.exe',
            'bin/v1649370/packed-reader/cloud_files.dll',
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            client = root / 'client'
            for relative_path in (*runtime_files, 'resources/v1649370/packed/obsolete.ifs'):
                fixture = client / relative_path
                fixture.parent.mkdir(parents=True, exist_ok=True)
                fixture.write_bytes(b'fixture')
            compiled = root / 'compiled'
            compiled.mkdir()
            (compiled / 'CODOL.exe').write_bytes(b'fixture')
            output_parent = root / 'outputs'
            output_parent.mkdir()
            release = output_parent / 'CODOL-Windows-x64'

            # 在测试目标父目录显式添加本用户的可继承读取项，即使系统 TEMP 本身是私有目录。
            user_sid = current_sid()
            subprocess.run(['icacls', str(output_parent), '/grant', f'*{user_sid}:(OI)(CI)RX'],
                           check=True, capture_output=True)
            with (
                patch.object(builder, 'CLIENT_ROOT', client),
                patch.object(builder, 'compile_host', return_value=compiled),
                patch.object(builder.subprocess, 'run') as launch_check,
                patch.object(sys, 'argv', [str(tool_path), '--output', str(release)]),
                patch('builtins.print'),
            ):
                builder.main()
                launch_check.assert_called_once()

            with self.subTest('程序包清单'):
                self.assertEqual({path.relative_to(release).as_posix() for path in release.rglob('*')
                                  if path.is_file()}, {'CODOL.exe', *runtime_files})
                self.assertFalse((release / 'resources').exists())
            acl_file = root / 'receipt-acl.txt'

            def read_acl(path):
                subprocess.run(['icacls', str(path), '/save', str(acl_file)],
                               check=True, capture_output=True)
                return acl_file.read_text(encoding='utf-16-le')

            with self.subTest('发布目录权限继承'):
                if f';;;{user_sid})' not in read_acl(output_parent):
                    self.skipTest('当前环境未授予夹具所需的目录 ACL，请在普通 Windows 用户环境运行')
                receipt = release / 'workspaces/offline-runs/test/guard-ready.json'
                receipt.parent.mkdir(parents=True)
                receipt.touch()
                # icacls /save 使用 SDDL，ID 表示此文件的权限项继承自父目录。
                self.assertRegex(read_acl(receipt), rf'\(A;[^;]*ID[^;]*;[^)]*;;;{user_sid}\)')
