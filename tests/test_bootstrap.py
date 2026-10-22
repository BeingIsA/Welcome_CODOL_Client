"""验证压缩边界和错误输入；不启动客户端，不依赖原游戏作为单元夹具。"""
import struct
import json
import unittest
import zlib
import tempfile
from pathlib import Path
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import Mock

from codol_rebuild.analysis.bootstrap import decode_lz4_block, recover_lz4_section, scan_ap32, import_hashes, main


class Lz4Tests(unittest.TestCase):
    """覆盖字面量、重叠回溯、长长度和包装边界，而不是照抄实现。"""

    def test_literals_and_long_literals(self):
        """短字面量和扩展长度都能精确还原。"""
        self.assertEqual(decode_lz4_block(b"\x50hello", 5), b"hello")
        self.assertEqual(decode_lz4_block(b"\xf0\xff\x0a" + b"A" * 280, 280), b"A" * 280)

    def test_overlapping_match(self):
        """距离 1、长度 7 的匹配必须逐字节重复，而非只复制一个字节。"""
        self.assertEqual(decode_lz4_block(b"\x13A\x01\x00\x50hello", 13), b"A" * 8 + b"hello")

    def test_bad_blocks_fail(self):
        """截断、零距离、回溯越界、解压大小不符及扩展溢出均拒绝。"""
        for packed, size in [(b"\xf0", 20), (b"\x50abc", 5), (b"\x10A\x00\x00", 5),
                             (b"\x10A\x02\x00", 5), (b"\x50hello", 6),
                             (b"\x10A\x01", 5), (b"\xf0\xff", 16), (b"\x50hello", 4)]:
            with self.subTest(packed=packed), self.assertRaises(ValueError):
                decode_lz4_block(packed, size)

    def test_multiblock_unknown_trailer_is_not_checksum(self):
        """恢复连续两块并保留未解释尾字段；不声称 CRC 已通过。"""
        packed = b"\x50hello"
        block = struct.pack("<II", 5, len(packed)) + packed + b"ABCD"
        raw, report = recover_lz4_section(block * 2 + bytes(8), 10)
        self.assertEqual(raw, b"hellohello")
        self.assertEqual(report["block_count"], 2)
        self.assertFalse(report["trailer_integrity_verified"])
        self.assertEqual(report["zero_padding_bytes"], 8)

    def test_frame_nonzero_padding_and_truncation_fail(self):
        """合法数据后不能夹带未解释尾部，包装长度也必须完整。"""
        frame = struct.pack("<II", 5, 6) + b"\x50hello" + b"ABCD"
        for data, expected in [(frame + b"x", 5), (frame[:-1], 5), (frame, 4), (frame, 6)]:
            with self.subTest(data=data), self.assertRaises(ValueError):
                recover_lz4_section(data, expected)


class Ap32Tests(unittest.TestCase):
    """AP32 的字符串误报与校验失败不可变成恢复成功。"""

    def test_false_positive_never_calls_decoder(self):
        """WINHTTP/API 等字符串中的 AP32.dll 不能作为压缩流。"""
        decoder = Mock()
        rows, result = scan_ap32(b"AP32.dll" + bytes(30), decoder)
        self.assertEqual(rows[0]["rejected_reason"], "header_size_or_bounds")
        self.assertEqual(result, {})
        decoder.assert_not_called()

    def test_packed_crc_failure_never_calls_decoder(self):
        """压缩 CRC 不符时不能进入解压阶段。"""
        decoder = Mock()
        data = b"AP32" + struct.pack("<5I", 24, 1, 0, 1, 0) + b"X"
        rows, result = scan_ap32(data, decoder)
        self.assertEqual(rows[0]["rejected_reason"], "packed_crc_mismatch")
        self.assertEqual(result, {})
        decoder.assert_not_called()

    def test_strict_decoder_and_independent_output_check(self):
        """外部 decoder 返回结果仍需独立核对长度与 CRC。"""
        data = b"AP32" + struct.pack("<5I", 24, 1, zlib.crc32(b"X"), 2, zlib.crc32(b"YZ")) + b"X"
        decoder = Mock(return_value=b"YZ")
        rows, result = scan_ap32(data, decoder)
        self.assertTrue(rows[0]["verified"])
        self.assertEqual(result, {0: b"YZ"})
        decoder.assert_called_once_with(data, strict=True)
        decoder.return_value = b"AB"
        rows, result = scan_ap32(data, decoder)
        self.assertFalse(rows[0]["verified"])
        self.assertEqual(result, {})

    def test_truncated_header(self):
        """不足 24 字节的 AP32 头保留明确拒绝原因。"""
        rows, _ = scan_ap32(b"AP32")
        self.assertEqual(rows[0]["rejected_reason"], "truncated_header")

    def test_known_hash_pair(self):
        """用旧版可追溯导入算法的固定示例约束哈希方向和常量。"""
        self.assertEqual(import_hashes("A"), (65, 84696414))


class OutputBoundaryTests(unittest.TestCase):
    """错误路径不能向原客户端或已有实验写入 failure.json。"""

    def test_manifest_missing_required_input_returns_controlled_error(self):
        """EXE/Base 任一条目缺失时先报清单错误，不能读取输入或创建输出。"""
        names = ["codoMP_client_shipRetail.exe", "codoMP_client_shipRetailBase.dll"]
        for missing in names:
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                manifest = root / "manifest.json"
                manifest.write_text(json.dumps({
                    "schema_version": 1, "build": "1649370",
                    "files": [{"path": name, "bytes": 1, "sha256": "0" * 64}
                              for name in names if name != missing]}), encoding="utf-8")
                output = root / "output"
                captured = StringIO()
                with redirect_stdout(captured):
                    result = main(["--client", str(root / "missing-client"),
                                   "--output-dir", str(output), "--manifest", str(manifest)])
                self.assertEqual(result, 1)
                report = json.loads(captured.getvalue())
                self.assertEqual(report["status"], "bootstrap_failed")
                self.assertIn("版本清单缺少必需文件", report["error"])
                self.assertIn(missing, report["error"])
                self.assertFalse(output.exists())

    def test_source_directory_cannot_be_output(self):
        """用户误把输出设为原目录时，命令失败且目录内容不变。"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "original.bin"
            marker.write_bytes(b"unchanged")
            with redirect_stdout(StringIO()):
                result = main(["--client", str(root), "--output-dir", str(root)])
            self.assertEqual(result, 1)
            self.assertEqual([item.name for item in root.iterdir()], ["original.bin"])
            self.assertEqual(marker.read_bytes(), b"unchanged")

    def test_existing_output_not_polluted_by_failure_record(self):
        """已有输出拒绝覆盖时不能追加错误文件改变旧实验。"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            client, output = root / "client", root / "output"
            client.mkdir()
            output.mkdir()
            with redirect_stdout(StringIO()):
                result = main(["--client", str(client), "--output-dir", str(output)])
            self.assertEqual(result, 1)
            self.assertEqual(list(output.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
