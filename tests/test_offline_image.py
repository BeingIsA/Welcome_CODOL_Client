"""独立试运行镜像的边界测试；没有客户端执行或 DLL 加载。"""
import tempfile
import unittest
import struct
from pathlib import Path
from types import SimpleNamespace

from codol_rebuild.adapters.game.v1649370.image import demote_elevation_manifest, inspect_tls, inspect_imports, prepare_image, build_offline_image


def fake_tls_pe(index_writable=True, callback=0):
    """构造小型 TLS 模型，测试虚拟尾部 index 与回调执行属性。"""
    section = SimpleNamespace(VirtualAddress=0x1000, Misc_VirtualSize=0x1000,
                              SizeOfRawData=0x100, Characteristics=0xC0000040 if index_writable else 0x40000040)
    tls = SimpleNamespace(StartAddressOfRawData=0x401000, EndAddressOfRawData=0x401030,
                          AddressOfIndex=0x401200, AddressOfCallBacks=0x401040)
    pe = SimpleNamespace(sections=[section], OPTIONAL_HEADER=SimpleNamespace(ImageBase=0x400000),
                         DIRECTORY_ENTRY_TLS=SimpleNamespace(struct=tls),
                         get_data=lambda rva, size: struct.pack("<I", callback) if rva == 0x1040 else bytes(size))
    return pe


class OfflineImageTests(unittest.TestCase):
    """不让缺项或危险输入被误报成静态加载检查通过。"""

    def test_manifest_demotion_preserves_size_and_xml(self):
        """降为当前用户执行时，资源尺寸及 XML 可解析性保持不变。"""
        source = b'<assembly><requestedExecutionLevel level="requireAdministrator" uiAccess="false"/></assembly>'
        modified, count = demote_elevation_manifest(source)
        self.assertEqual(count, 1)
        self.assertEqual(len(source), len(modified))
        self.assertIn(b'level="asInvoker"', modified)
        self.assertNotIn(b"requireAdministrator", modified)

    def test_non_elevating_manifest_is_unchanged(self):
        """无需提权的资源原样保留，不注入额外权限结构。"""
        source = b'<assembly manifestVersion="1.0"/>'
        self.assertEqual(demote_elevation_manifest(source), (source, 0))

    def test_tls_index_may_be_in_writable_virtual_tail(self):
        """TLS index 只需由加载器分配的可写虚拟区，不要求磁盘 raw 数据。"""
        result = inspect_tls(fake_tls_pe())
        self.assertEqual(result["template_bytes"], 48)
        self.assertEqual(result["callbacks"], [])
        self.assertTrue(result["index_writable"])

    def test_tls_rejects_read_only_index(self):
        """加载器要写入 index，目标只读时必须拒绝。"""
        with self.assertRaisesRegex(ValueError, "不可写"):
            inspect_tls(fake_tls_pe(index_writable=False))

    def test_tls_rejects_non_executable_callback(self):
        """非零回调必须指向可执行节，不能盲目保留数据指针。"""
        with self.assertRaisesRegex(ValueError, "不可执行"):
            inspect_tls(fake_tls_pe(callback=0x401000))

    def test_forbidden_static_dependency_fails(self):
        """原保护模块出现在静态导入时立即拒绝生成候选。"""
        pe = SimpleNamespace(DIRECTORY_ENTRY_IMPORT=[SimpleNamespace(dll=b"TenRpcs.dll", imports=[])])
        with self.assertRaisesRegex(ValueError, "保护组件"):
            inspect_imports(pe)

    def test_unrecognized_recovery_fingerprint_fails(self):
        """未知恢复镜像不得应用固定入口/结构规则。"""
        with self.assertRaisesRegex(ValueError, "经过验证"):
            prepare_image(b"MZ-untrusted-input")

    def test_original_directory_cannot_be_output(self):
        """原客户端目录作为输出时必须在读写任何镜像前拒绝。"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(ValueError):
                build_offline_image(root / "absent.bin", root, root)
            self.assertEqual(list(root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()


class RuntimeImageLocationTests(unittest.TestCase):
    """固定基底可进入复制阶段，但不扩大到任意 bin 文件。"""

    def test_source_location_boundary(self):
        from unittest.mock import patch
        from codol_rebuild.bootstrap import resource_setup
        from codol_rebuild.core.paths import REBUILD_ROOT

        class CopyReached(Exception):
            pass

        cases = (
            (REBUILD_ROOT / 'bin/v1649370/codol1649370-offline.exe', True),
            (REBUILD_ROOT / 'workspaces/candidate.exe', True),
            (REBUILD_ROOT / 'bin/other.exe', False),
            (REBUILD_ROOT.parent / 'outside.exe', False),
        )
        for image, allowed in cases:
            with self.subTest(image=image):
                args = SimpleNamespace(image=image, client=REBUILD_ROOT.parent / 'Original Client')
                with patch.object(resource_setup.shutil, 'copyfile', side_effect=CopyReached) as copy:
                    with self.assertRaises(CopyReached if allowed else ValueError):
                        resource_setup.prepare(args, REBUILD_ROOT / 'workspaces/offline-runs/test',
                                               REBUILD_ROOT / 'workspaces/offline-runs/test/runtime',
                                               {}, lambda *args: None)
                    self.assertEqual(copy.call_count, int(allowed))
