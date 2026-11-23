"""验证资源缓存复用、篡改拒绝、会话隔离与 Windows 权限继承。"""
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from codol_rebuild.resources.runtime_cache import prepare_library


class RuntimeCacheTests(unittest.TestCase):
    def test_direct_library_no_copy_and_validation(self):
        """首次和重复准备均不复制资源，变化及强制校验不能绕过内容检查。"""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            library = root / 'library'
            (library / 'main').mkdir(parents=True)
            asset = library / 'main/asset'
            asset.write_bytes(b'original')
            (library / 'manifest.json').write_bytes(b'manifest')
            (library / 'summary.json').write_text('{}')
            entries = [{'game_path': 'main/asset', 'size': 8,
                        'sha256': hashlib.sha256(b'original').hexdigest()}]
            args = (root / 'receipts', library, b'manifest', entries, lambda n: n, lambda p: None)
            audit = {}
            with patch('shutil.copyfile', side_effect=AssertionError('unexpected copy')):
                self.assertEqual(prepare_library(*args, audit=audit), library)
                self.assertEqual(audit['copied_bytes'], 0)
                with patch('codol_rebuild.resources.runtime_cache.digest', side_effect=AssertionError('unexpected hash')):
                    self.assertEqual(prepare_library(*args, audit=audit), library)
                self.assertEqual(audit['hashed_bytes'], 0)
            with patch('codol_rebuild.resources.runtime_cache.digest', side_effect=RuntimeError('forced')):
                with self.assertRaisesRegex(RuntimeError, 'forced'):
                    prepare_library(*args, force_verify=True)
            extra = library / 'main/extra'
            extra.write_bytes(b'extra')
            with self.assertRaisesRegex(ValueError, '未登记'):
                prepare_library(*args)
            extra.unlink()
            asset.write_bytes(b'changed!')
            with self.assertRaisesRegex(ValueError, '指纹不符'):
                prepare_library(*args)


    @unittest.skipUnless(os.name == 'nt', 'Windows GUI 启动')
    def test_gui_acl_commands_work_without_standard_input(self):
        """双击启动没有可继承的标准输入，权限命令仍应静默完成并报告真实失败。"""
        import _winapi
        from ctypes import WinError
        from codol_rebuild.platform.resource_acl import apply_runtime_acl
        from codol_rebuild.platform.resource_cache_acl import protect_cache

        original_get_handle = _winapi.GetStdHandle

        def get_handle_without_input(handle_type):
            if handle_type == _winapi.STD_INPUT_HANDLE:
                raise WinError(6)
            return original_get_handle(handle_type)

        with (tempfile.TemporaryDirectory() as folder,
              patch.object(_winapi, 'GetStdHandle', side_effect=get_handle_without_input),
              patch.object(subprocess, 'run', wraps=subprocess.run) as run_command):
            for function, name, error_message in (
                (protect_cache, 'cache', '共享资源保护失败'),
                (apply_runtime_acl, 'runtime', '独立运行目录ACL失败'),
            ):
                with self.subTest(directory=name):
                    directory = Path(folder) / name
                    directory.mkdir()
                    function(directory)
                    with patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess(
                            ['icacls'], 1, b'', b'access denied')):
                        with self.assertRaisesRegex(RuntimeError, error_message + ': access denied'):
                            function(directory)
            for invocation in run_command.call_args_list:
                self.assertEqual(invocation.args[0][0], 'icacls')
                self.assertTrue(invocation.kwargs['creationflags'] & subprocess.CREATE_NO_WINDOW)


    @unittest.skipUnless(os.name == 'nt', 'Windows junction')
    def test_junction_readable_but_cache_not_writable_by_client(self):
        from codol_rebuild.platform.resource_mount import mount_resource
        from codol_rebuild.platform.resource_cache_acl import protect_cache
        from codol_rebuild.platform.win32 import current_sid, low_token, api, A, PTR, check, close
        from ctypes import wintypes as W
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);cache=root/'cache';stage=root/'stage'
            cache.mkdir();stage.mkdir();(cache/'main').mkdir();(cache/'main/asset').write_bytes(b'original')
            (cache/'manifest.json').write_bytes(b'manifest')
            mount=stage/'images'
            try:
                entries = [{'game_path': 'main/asset', 'size': 8,
                            'sha256': hashlib.sha256(b'original').hexdigest()}]
                prepare_library(root/'receipts', cache, b'manifest', entries, lambda n: n, protect_cache)
                subprocess.run(['icacls',str(stage),'/grant:r','*'+current_sid()+':(OI)(CI)F'],check=True,capture_output=True)
                subprocess.run(['icacls',str(stage),'/setintegritylevel','(OI)(CI)L'],check=True,capture_output=True)
                mount_resource(cache/'main',mount,root/'cache',stage)
                self.assertTrue(mount.is_junction())
                token=low_token()
                try:
                    check(api(A,'ImpersonateLoggedOnUser',W.BOOL,PTR)(token))
                    self.assertEqual((mount/'asset').read_bytes(),b'original')
                    with self.assertRaises(PermissionError):(mount/'asset').write_bytes(b'changed!')
                    with self.assertRaises(PermissionError):(mount/'new').write_bytes(b'new')
                    (stage/'config.cfg').write_bytes(b'writable')
                finally:
                    check(api(A,'RevertToSelf',W.BOOL)());close(token)
                with self.assertRaises(ValueError):mount_resource(cache,mount,cache,stage)
            finally:
                if mount.is_junction():os.rmdir(mount)
                subprocess.run(['icacls',str(root),'/grant:r','*'+current_sid()+':(OI)(CI)F','/T'],capture_output=True)
