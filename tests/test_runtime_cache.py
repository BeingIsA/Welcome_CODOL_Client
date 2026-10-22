"""验证资源缓存复用、篡改拒绝、会话隔离与 Windows 权限继承。"""
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from codol_rebuild.resources.runtime_cache import prepare_cache, prepare_library, link_resource


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
            with patch('codol_rebuild.resources.runtime_cache.shutil.copyfile', side_effect=AssertionError('unexpected copy')):
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

    def test_reuse_and_conflict(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            library = root / 'library'
            library.mkdir()
            (library / 'asset').write_bytes(b'original')
            entries = [{'game_path': 'asset', 'size': 8,
                        'sha256': hashlib.sha256(b'original').hexdigest()}]
            args = (root / 'cache', library, b'manifest', entries, lambda n: n, lambda p: None)
            cache = prepare_cache(*args)
            with patch('codol_rebuild.resources.runtime_cache.shutil.copyfile', side_effect=AssertionError('unexpected copy')):
                self.assertEqual(prepare_cache(*args), cache)
                for run in ('run1', 'run2'):
                    link_resource(cache / 'asset', root / run / 'asset')
                    self.assertTrue(os.path.samefile(cache / 'asset', root / run / 'asset'))
            self.assertFalse(os.path.samefile(library / 'asset', cache / 'asset'))
            # 建链接会改变文件元数据；重新验收一次后，热启动不得读取资源内容。
            prepare_cache(*args)
            audit = {}
            with patch('codol_rebuild.resources.runtime_cache.digest', side_effect=AssertionError('unexpected hash')):
                self.assertEqual(prepare_cache(*args, audit=audit), cache)
            self.assertEqual(audit['hashed_bytes'], 0)
            with patch('codol_rebuild.resources.runtime_cache.digest', side_effect=RuntimeError('full check requested')):
                with self.assertRaisesRegex(RuntimeError, 'full check requested'):
                    prepare_cache(*args, force_verify=True)
            (root / 'conflict').write_bytes(b'different')
            with self.assertRaises(ValueError):
                link_resource(cache / 'asset', root / 'conflict')
            (cache / 'asset').write_bytes(b'tampered')
            with self.assertRaises(ValueError):
                prepare_cache(*args)

    def test_source_change_invalidates_receipt(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);library=root/'library';library.mkdir()
            (library/'asset').write_bytes(b'original')
            entries=[{'game_path':'asset','size':8,'sha256':hashlib.sha256(b'original').hexdigest()}]
            args=(root/'cache',library,b'manifest',entries,lambda n:n,lambda p:None)
            prepare_cache(*args)
            (library/'asset').write_bytes(b'changed!')
            with self.assertRaises(ValueError):prepare_cache(*args)

    def test_unlisted_cache_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);library=root/'library';library.mkdir()
            (library/'asset').write_bytes(b'original')
            entries=[{'game_path':'asset','size':8,'sha256':hashlib.sha256(b'original').hexdigest()}]
            args=(root/'cache',library,b'manifest',entries,lambda n:n,lambda p:None)
            cache=prepare_cache(*args)
            (cache/'unlisted').write_bytes(b'unknown')
            with self.assertRaises(ValueError):prepare_cache(*args)

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

    def test_reject_escape(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                prepare_cache(Path(folder)/'cache', Path(folder), b'x',
                              [{'game_path':'../escape'}], lambda n: None, lambda p: None)

    @unittest.skipUnless(os.name == 'nt', 'Windows ACL')
    def test_shared_acl_survives_low_stage_inheritance(self):
        from codol_rebuild.platform.resource_cache_acl import protect_cache
        from codol_rebuild.platform.win32 import current_sid, low_token, api, A, PTR, check, close
        from ctypes import wintypes as W
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cache = root/'cache'
            cache.mkdir()
            asset = cache/'asset'
            asset.write_bytes(b'original')
            try:
                protect_cache(cache)
                stage = root/'runtime'
                stage.mkdir()
                link_resource(asset, stage/'asset')
                for options in [('/grant:r', '*'+current_sid()+':(OI)(CI)F'),
                                ('/setintegritylevel', '(OI)(CI)L')]:
                    subprocess.run(['icacls',str(stage),*options],check=True,capture_output=True)
                token = low_token()
                try:
                    check(api(A, 'ImpersonateLoggedOnUser', W.BOOL, PTR)(token))
                    with self.assertRaises(PermissionError):
                        (stage/'asset').write_bytes(b'changed!')
                    self.assertEqual((stage/'asset').read_bytes(), b'original')
                finally:
                    check(api(A, 'RevertToSelf', W.BOOL)())
                    close(token)
                self.assertEqual(asset.read_bytes(), b'original')
            finally:
                subprocess.run(['icacls',str(root),'/grant:r','*'+current_sid()+':(OI)(CI)F','/T'],
                               capture_output=True)
