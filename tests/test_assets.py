# SPDX-License-Identifier: AGPL-3.0-or-later
"""验证资源真实边界：压缩校验、内联指针、未解析引用与多余字节。"""
import struct
import unittest
import zlib

from codol_rebuild.resources.assets import AssetError, decode_netconst, decode_rawfile


class AssetTests(unittest.TestCase):
    """使用独立构造的微型资产，避免测试依赖本机商业游戏文件。"""

    def test_rawfile_valid_and_corrupt_stream(self):
        """损坏压缩流必须失败，不能因为名字和长度合理就接受。"""
        payload = b"main() { println(\"test\"); }\n"
        compressed = zlib.compress(payload)
        head = struct.pack("<4I", 0xFFFFFFFF, len(compressed), len(payload), 0xFFFFFFFF)
        fixture = head + b"maps/test.gsc\0" + compressed
        self.assertEqual(decode_rawfile(fixture), ("maps/test.gsc", payload))
        damaged = bytearray(fixture)
        damaged[-1] ^= 1
        with self.assertRaises(AssetError):
            decode_rawfile(bytes(damaged))

    def test_rawfile_rejects_extra_data_and_size_lie(self):
        """校验预期长度并拒绝拼接第二个压缩流或尾随垃圾。"""
        compressed = zlib.compress(b"abc") + b"tail"
        fixture = struct.pack("<4I", 0xFFFFFFFF, len(compressed), 3, 0xFFFFFFFF)
        with self.assertRaises(AssetError):
            decode_rawfile(fixture + b"test.bin\0" + compressed)
        fixture = struct.pack("<4I", 0xFFFFFFFF, 0, 2, 0xFFFFFFFF)
        with self.assertRaises(AssetError):
            decode_rawfile(fixture + b"test.bin\0abc\0")

    def test_pooled_name_is_explicitly_unresolved(self):
        """池化名称不能被误读为本资产内的字符串。"""
        fixture = struct.pack("<4I", 0x30000001, 0, 0, 0xFFFFFFFF) + b"\0"
        with self.assertRaises(AssetError) as caught:
            decode_rawfile(fixture)
        self.assertEqual(caught.exception.reason, "unresolved_reference")

    def test_netconst_inline_values_and_truncated_table(self):
        """常量表逐引用解析，数组越界与剩余字节都必须报错。"""
        fixture = (struct.pack("<5I", 0xFFFFFFFF, 0, 3, 2, 0xFFFFFFFF)
                   + b"ncs_mdl_level\0" + struct.pack("<2I", 0xFFFFFFFF, 0xFFFFFFFF)
                   + b"model_a\0model_b\0")
        result = decode_netconst(fixture)
        self.assertEqual(result["values"], ["model_a", "model_b"])
        for bad in (fixture[:-1], fixture + b"x"):
            with self.assertRaises(AssetError):
                decode_netconst(bad)

    def test_netconst_null_array_with_count_is_invalid(self):
        """空指针配非零计数属于坏数据，不能误标为待解析引用。"""
        fixture = struct.pack("<5I", 0xFFFFFFFF, 0, 3, 1, 0) + b"ncs_test\0"
        with self.assertRaises(AssetError) as caught:
            decode_netconst(fixture)
        self.assertEqual(caught.exception.reason, "invalid_pointer")


if __name__ == "__main__":
    unittest.main()
