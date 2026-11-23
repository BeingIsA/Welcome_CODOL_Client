"""发布清单回归：旧展开库和工作材料不随独立原包客户端分发。"""
import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


def load_packager():
    tool_path = Path(__file__).resolve().parents[1] / 'tools/package_resources.py'
    module_spec = importlib.util.spec_from_file_location('packed_package_tool', tool_path)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


class PackedPackageTests(unittest.TestCase):
    def test_collects_indexed_files_from_standalone_directory(self):
        packager = load_packager()
        with tempfile.TemporaryDirectory() as temporary:
            client_root = Path(temporary).resolve()
            packed_root = client_root / 'resources/v1649370/packed'
            indexed_name = 'IIPS/IIPSDownload/example.ifs'
            expected = {
                packed_root / packager.INDEX_NAME, packed_root / indexed_name,
                client_root / 'bin/v1649370/packed-reader/ifs_reader.exe',
                client_root / 'bin/v1649370/packed-reader/cloud_files.dll',
                client_root / 'manifests/offline-menu-patches.json',
            }
            extras = {client_root / 'resources/迷彩表.csv', client_root / 'resources/item_catalog.json',
                      client_root / 'resources/v1649370/offline-menu-patches.json',
                      client_root / 'resources/v1649370/game/main/unpacked.iwi',
                      packed_root / 'IIPS/unused.exe', client_root / 'workspaces/session-log.txt'}
            for file_path in expected | extras:
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_bytes(b'fixture')

            index = SimpleNamespace(physical_files={indexed_name: {}})
            with patch.object(packager, 'read_packed_index', return_value=index) as read_index:
                self.assertEqual(set(packager.collect_resources(client_root)), expected)
                read_index.assert_called_once_with(packed_root, verify_resources=True)
                (client_root / 'bin/v1649370/packed-reader/ifs_reader.exe').unlink()
                with self.assertRaises(FileNotFoundError):
                    packager.collect_resources(client_root)
