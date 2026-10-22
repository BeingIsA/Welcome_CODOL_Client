"""用微型文件验证版本误配、路径边界及独立复制，不依赖游戏资源。"""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from codol_rebuild.core.baseline import BaselineError, load_manifest, stage_inputs, verify_inputs


class BaselineTests(unittest.TestCase):
    """覆盖会污染原件或产生错误版本结论的关键失败路径。"""

    def setUp(self):
        """建立与真实游戏无关的最小输入夹具。"""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.client = self.root / "client"
        self.client.mkdir()
        self.payload = b"not executable: test input"
        (self.client / "sample.bin").write_bytes(self.payload)
        self.manifest = {"schema_version": 1, "build": "1649370", "files": [{
            "path": "sample.bin", "bytes": len(self.payload),
            "sha256": hashlib.sha256(self.payload).hexdigest()}]}

    def test_mismatch_prevents_output_creation(self):
        """输入被改动后，拒绝建立看似成功的实验目录。"""
        (self.client / "sample.bin").write_bytes(b"wrong version")
        output = self.root / "stage"
        self.assertFalse(verify_inputs(self.client, self.manifest)["ready"])
        with self.assertRaises(BaselineError):
            stage_inputs(self.client, output, self.manifest)
        self.assertFalse(output.exists())

    def test_copy_is_independent_and_existing_run_is_preserved(self):
        """副本修改不得影响原件，重复实验不得覆盖现有结果。"""
        output = self.root / "stage"
        result = stage_inputs(self.client, output, self.manifest)
        self.assertFalse(result["launch_ready"])
        self.assertFalse((output / "sample.bin").samefile(self.client / "sample.bin"))
        (output / "sample.bin").write_bytes(b"changed copy")
        self.assertEqual((self.client / "sample.bin").read_bytes(), self.payload)
        with self.assertRaises(BaselineError):
            stage_inputs(self.client, output, self.manifest)
        self.assertEqual((output / "sample.bin").read_bytes(), b"changed copy")

    def test_refuse_output_inside_original(self):
        """阻止误把原客户端子目录当作实验写入位置。"""
        with self.assertRaises(BaselineError):
            stage_inputs(self.client, self.client / "probe", self.manifest)

    def test_missing_file_is_reported(self):
        """缺文件必须报告 missing，不能由其他文件通过而掩盖。"""
        (self.client / "sample.bin").unlink()
        result = verify_inputs(self.client, self.manifest)
        self.assertFalse(result["ready"])
        self.assertEqual(result["files"][0]["status"], "missing")

    def test_manifest_rejects_traversal_and_duplicate_paths(self):
        """清单边界保证版本校验不会读取客户端以外的路径。"""
        path = self.root / "manifest.json"
        for name in ["../outside.bin", "C:/outside.bin", "/outside.bin", "folder\\file",
                     "foo:bar", "NUL.txt", "CON", "foo. ", "."]:
            self.manifest["files"][0]["path"] = name
            path.write_text(json.dumps(self.manifest), encoding="utf-8")
            with self.assertRaises(BaselineError):
                load_manifest(path)
        self.manifest["files"][0]["path"] = "sample.bin"
        self.manifest["files"].append(dict(self.manifest["files"][0]))
        path.write_text(json.dumps(self.manifest), encoding="utf-8")
        with self.assertRaises(BaselineError):
            load_manifest(path)

    def test_malformed_manifest_has_explicit_diagnostic(self):
        """错误 JSON 结构应得到预期诊断，不能泄漏为未处理的属性异常。"""
        path = self.root / "malformed.json"
        for value in [[], {"schema_version": 1, "build": "1649370", "files": [None]}]:
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(BaselineError):
                load_manifest(path)


if __name__ == "__main__":
    unittest.main()
