"""运行目录回归：系统独占锁、进程持锁、会话联接与程序零复制复用。"""
from pathlib import Path
import contextlib
import hashlib
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from codol_rebuild.platform.runtime_directory import acquire_runtime_directory, SESSION_DIRECTORIES
from codol_rebuild.bootstrap import resource_setup
from codol_rebuild.resources import native_library


class RuntimeDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name).resolve()
        self.root = self.project / 'client'
        self.sessions = self.root / 'workspaces/offline-runs'
        self.programs = self.root / 'workspaces/runtime-programs'
        self.sessions.mkdir(parents=True)

    def acquire(self, revision='a' * 64):
        lease = acquire_runtime_directory(self.programs, revision, self.sessions)
        self.addCleanup(lease.close)
        return lease

    def test_concurrent_leases_separate_and_release_reuses_slot(self):
        first = self.acquire()
        second = self.acquire()
        self.assertNotEqual(first.path, second.path)
        first.close()
        reused = self.acquire()
        self.assertEqual(first.path, reused.path)
        self.assertNotEqual(reused.path, self.acquire('b' * 64).path)

    def test_child_retains_lock_after_host_releases_it(self):
        first = self.acquire()
        child = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.readline()'],
                                 stdin=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            first.hold_for_process(child._handle)
            first.close()
            concurrent = self.acquire()
            self.assertNotEqual(first.path, concurrent.path)
            child.communicate(b'finish\n', timeout=10)
            self.assertEqual(first.path, self.acquire().path)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()

    def session(self, name):
        directory = self.sessions / name / 'runtime'
        for child_name in SESSION_DIRECTORIES:
            (directory / child_name).mkdir(parents=True)
        return directory

    def test_rebind_preserves_old_session_data(self):
        lease = self.acquire()
        lease.path.mkdir()
        previous = self.session('previous')
        current = self.session('current')
        original_settings = previous / 'players/config_mp.cfg'
        original_settings.write_bytes(b'previous settings')
        lease.bind_session(previous)
        lease.bind_session(current)
        self.assertEqual(original_settings.read_bytes(), b'previous settings')
        self.assertEqual((lease.path / 'players').resolve(), current / 'players')
        with self.assertRaisesRegex(ValueError, '越界'):
            lease.bind_session(self.project)

    def test_second_prepare_copies_nothing_and_keeps_original_image(self):
        library = self.root / 'resources/v1649370/game'
        for name in native_library.MOUNT_DIRECTORIES:
            (library / name).mkdir(parents=True)
        (library / 'libcef.dll').write_bytes(b'dependency')
        image = self.root / 'bin/v1649370/codol1649370-offline.exe'
        image.parent.mkdir(parents=True)
        image.write_bytes(b'original')
        patches = self.root / 'patches.json'
        patches.write_text('[]')
        manifest = {'resources': [{'game_path': 'libcef.dll', 'size': 10,
                                  'sha256': hashlib.sha256(b'dependency').hexdigest()}],
                    'summary': {'validated_output_bytes': 10}}
        arguments = SimpleNamespace(native_library=library, verify_resources=False,
                                    patches=patches, argument=[])

        def prepare_image(executable, patch_file, log):
            if patch_file is not None:
                self.assertEqual(executable.read_bytes(), b'original')
                executable.write_bytes(b'patched')
            log['patches'] = []
            return 'image metadata'

        def prepare(name):
            run = self.sessions / name
            stage = run / 'runtime'
            stage.mkdir(parents=True)
            log = {}
            prepared = resource_setup.prepare_reusable_runtime(
                arguments, run, stage, log, lambda phase: None, image)
            self.addCleanup(prepared['runtime_lease'].close)
            return prepared, log

        with contextlib.ExitStack() as mocks:
            mocks.enter_context(patch.object(resource_setup, 'ROOT', self.root))
            mocks.enter_context(patch.object(resource_setup, 'accounts_dir', return_value=self.project / 'accounts'))
            mocks.enter_context(patch.object(native_library, 'read_manifest', return_value=(b'manifest', manifest)))
            mocks.enter_context(patch.object(resource_setup, 'prepare_library'))
            mocks.enter_context(patch.object(resource_setup, 'protect_cache'))
            mocks.enter_context(patch.object(resource_setup, 'apply_runtime_acl'))
            mocks.enter_context(patch.object(resource_setup, 'prepare_runtime_image', side_effect=prepare_image))
            first, first_log = prepare('first')
            executable_identity = first['exe'].stat().st_ino
            first['runtime_lease'].close()
            with patch.object(resource_setup.shutil, 'copyfile', side_effect=AssertionError('unexpected copy')):
                second, second_log = prepare('second')
            self.assertEqual(first['exe'], second['exe'])
            self.assertEqual(second['exe'].stat().st_ino, executable_identity)
            self.assertEqual(image.read_bytes(), b'original')
            self.assertFalse(first_log['runtime_program']['reused'])
            self.assertTrue(second_log['runtime_program']['reused'])
            self.assertEqual(second_log['runtime_program']['copied_bytes'], 0)
            self.assertFalse(second_log['runtime_program']['patches_applied_this_run'])
            second['runtime_lease'].close()
            second['exe'].write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, '发生变化'):
                prepare('tampered')
            # 失败路径必须释放槽位，而不是泄漏占用导致后续悄悄生成新副本。
            revision = second_log['runtime_program']['revision']
            self.assertEqual(self.acquire(revision).path, second['exe'].parent)


if __name__ == '__main__':
    unittest.main()
