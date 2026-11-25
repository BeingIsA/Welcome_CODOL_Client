"""Windows Cloud Files 小样本回归；依赖项目随包交付的原生视图 DLL。"""
import ctypes
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codol_rebuild.platform.cloud_files import CloudFilesView, check_status


class CloudFilesViewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / 'packed-view'
        client_root = Path(__file__).resolve().parents[1]
        self.adapter = client_root / 'bin/v1649370/packed-reader/cloud_files.dll'
        self.members = {'main/a.ff': b'first contents', 'hires/b.iwi': b'other contents'}
        self.index_sha256 = hashlib.sha256(b'test resource index').hexdigest()
        self.requests = []
        self.unregistration = ctypes.WinDLL(str(self.adapter)).UnregisterGameRoot
        self.unregistration.argtypes = [ctypes.c_wchar_p]
        self.addCleanup(self.unregister_root)

    def unregister_root(self):
        if self.root.exists():
            check_status('撤销测试同步根注册', self.unregistration(str(self.root)))

    def make_view(self):
        def read_member(identity, process_id, offset, length, destination):
            name = identity.decode('utf-8')
            contents = self.members[name]
            if offset != 0 or length != len(contents):
                return 0
            self.requests.append(name)
            ctypes.memmove(destination, contents, length)
            return 1

        return CloudFilesView(self.root, self.adapter, {name: len(data) for name, data in self.members.items()},
                              read_member, lambda *values: None, index_sha256=self.index_sha256)

    def test_reconnect_uses_retained_content_and_fetches_only_cold_member(self):
        first = self.make_view()
        try:
            first.open()
            self.assertFalse(first.reused)
            self.assertEqual((self.root / 'main/a.ff').read_bytes(), self.members['main/a.ff'])
        finally:
            first.close()
        self.assertEqual(self.requests, ['main/a.ff'])
        second = self.make_view()
        try:
            second.open()
            self.assertTrue(second.reused)
            self.assertEqual((self.root / 'main/a.ff').read_bytes(), self.members['main/a.ff'])
            self.assertEqual((self.root / 'hires/b.iwi').read_bytes(), self.members['hires/b.iwi'])
            self.assertEqual(self.requests, ['main/a.ff', 'hires/b.iwi'])
        finally:
            second.close()

    def test_failed_population_is_rebuilt_before_reuse(self):
        first = self.make_view()
        create_members = first.adapter.AddGameMembers
        created_batches = []

        def fail_second_batch(directory, members, count):
            if created_batches:
                raise OSError('测试中断第二批占位文件创建')
            created_batches.append(directory)
            return create_members(directory, members, count)

        try:
            with patch.object(first.adapter, 'AddGameMembers', side_effect=fail_second_batch):
                with self.assertRaisesRegex(OSError, '中断'):
                    first.open()
        finally:
            first.close()
        recovered = self.make_view()
        try:
            recovered.open()
            self.assertFalse(recovered.reused)
            for name, contents in self.members.items():
                self.assertEqual((self.root / name).read_bytes(), contents)
        finally:
            recovered.close()

    def test_changed_owner_refuses_to_remove_existing_data(self):
        first = self.make_view()
        try:
            first.open()
        finally:
            first.close()
        sentinel = self.root / 'unknown.txt'
        sentinel.write_bytes(b'keep')
        owner = json.loads(first.marker.read_bytes())
        owner['directory'] += 1
        first.marker.write_text(json.dumps(owner), encoding='utf-8')
        second = self.make_view()
        try:
            with self.assertRaises(ValueError):
                second.open()
        finally:
            second.close()
        self.assertEqual(sentinel.read_bytes(), b'keep')

    def test_removed_registration_builds_new_placeholders(self):
        first = self.make_view()
        try:
            first.open()
            self.assertEqual((self.root / 'main/a.ff').read_bytes(), self.members['main/a.ff'])
        finally:
            first.close()
        self.unregister_root()
        second = self.make_view()
        try:
            second.open()
            self.assertFalse(second.reused)
            self.assertEqual((self.root / 'main/a.ff').read_bytes(), self.members['main/a.ff'])
            self.assertEqual(self.requests, ['main/a.ff', 'main/a.ff'])
        finally:
            second.close()

    def test_legacy_external_owner_migrates_and_rebuilds_incomplete_tree(self):
        # 旧版在目录外保存 NTFS 卷和目录标识，未记录整棵树建成。
        self.root.mkdir()
        old_directory = self.root / 'main'
        old_directory.mkdir()
        (old_directory / 'a.ff').write_bytes(b'partial')
        attributes = self.root.stat()
        old_owner = {'provider': 'CODOL packed resources', 'volume': attributes.st_dev,
                     'directory': attributes.st_ino}
        old_marker = self.root.with_name(self.root.name + '.owner.json')
        old_marker.write_text(json.dumps(old_owner), encoding='utf-8')
        migrated = self.make_view()
        try:
            migrated.open()
            self.assertFalse(migrated.reused)
            self.assertFalse(old_marker.exists())
            self.assertEqual(migrated.marker.parent, self.root)
            for name, contents in self.members.items():
                self.assertEqual((self.root / name).read_bytes(), contents)
        finally:
            migrated.close()
        reopened = self.make_view()
        try:
            reopened.open()
            self.assertTrue(reopened.reused)
        finally:
            reopened.close()

    def test_initial_publish_failure_leaves_final_location_available_for_retry(self):
        first = self.make_view()
        rename_directory = Path.rename

        def fail_initial_publication(source, destination):
            if destination == self.root:
                self.assertTrue((source / first.marker.name).is_file())
                raise OSError('测试首次发布目录失败')
            return rename_directory(source, destination)

        try:
            with patch.object(Path, 'rename', fail_initial_publication):
                with self.assertRaisesRegex(OSError, '首次发布'):
                    first.open()
        finally:
            first.close()
        self.assertFalse(self.root.exists())
        retry = self.make_view()
        try:
            retry.open()
            self.assertFalse(retry.reused)
            for name, contents in self.members.items():
                self.assertEqual((self.root / name).read_bytes(), contents)
        finally:
            retry.close()
