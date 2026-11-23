"""验证启动器的版本判断、离线检查和本地安装事务。"""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from codol_rebuild import updater


class UpdateTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.program = self.root / 'CODOL.exe'
        self.program.write_bytes(b'old')
        self.config = self.root / 'config/server.ini'
        self.config.parent.mkdir()
        self.config.write_bytes(b'custom')
        archive = io.BytesIO()
        with ZipFile(archive, 'w') as package:
            package.writestr('CODOL.exe', b'new')
            package.writestr('config/server.ini', b'default')
            package.writestr('resources/example', b'unwanted')
            package.writestr('new.dll', b'data')
        self.data = archive.getvalue()
        self.release = dict(version='v0.1.10', size=len(self.data),
                            sha256=hashlib.sha256(self.data).hexdigest(),
                            path='releases/v0.1.10/CODOL-Windows-x64.zip')

    def install(self):
        with patch.object(updater, 'urlopen', return_value=io.BytesIO(self.data)):
            updater.install_release(self.root, 'https://example.test', self.release)

    def test_check(self):
        with patch.object(updater, 'urlopen', return_value=io.BytesIO(json.dumps(self.release).encode())):
            self.assertEqual(updater.check_release('https://example.test'), self.release)
        self.assertGreater(updater.version_number('v0.1.10'), updater.version_number('v0.1.9'))
        with patch.object(updater, 'urlopen', side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):
                updater.check_release('https://example.test')

    def test_install(self):
        self.install()
        self.assertEqual(self.program.read_bytes(), b'new')
        self.assertEqual(self.config.read_bytes(), b'custom')
        self.assertFalse((self.root / 'resources').exists())
        self.assertEqual(json.loads((self.root / 'installed-version.json').read_text())['version'], 'v0.1.10')

    def test_corrupt(self):
        self.data += b'corrupt'
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(self.program.read_bytes(), b'old')

    def test_rollback(self):
        replace = updater.os.replace
        def fail_marker(source, target):
            if target.name == 'installed-version.json':
                raise PermissionError()
            replace(source, target)
        with patch.object(updater.os, 'replace', side_effect=fail_marker), self.assertRaises(PermissionError):
            self.install()
        self.assertEqual(self.program.read_bytes(), b'old')
        self.assertFalse((self.root / 'new.dll').exists())
