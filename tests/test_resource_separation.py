"""资源子系统职责分离回归：建库器公开接口、松散来源独立装载、解码校验一致。"""
import importlib.util
import json
from pathlib import Path
import unittest
import test_offline_resources as fixtures
from test_loose_library import LooseTests
from codol_rebuild.resources import offline_resources as resources
from codol_rebuild.resources import loose_library as loose
from codol_rebuild.resources.image_library import build_library

TOOLS_PATH = Path(__file__).resolve().parents[1] / 'tools' / 'prepare_offline_resources.py'


def load_prepare_tool():
    """从工具目录装载准备工具模块，验证其解码行为与资源层一致。"""
    spec = importlib.util.spec_from_file_location('prepare_offline_resources_for_test', TOOLS_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GuardedSource:
    """仅暴露公开接口的建库来源；任何下划线私有属性访问都视为契约违例。"""

    def __init__(self, inner):
        self.__dict__['_inner'] = inner

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AssertionError('建库器访问了供应器私有属性: ' + name)
        return getattr(self.__dict__['_inner'], name)


class PublicInterfaceOnlyTests(fixtures.ResourceFixture):
    def test_build_library_never_touches_supplier_internals(self):
        # 建库器必须只经 indexed_names/cached_entry/receipt_entries/library_constraints 取数。
        supplier = resources.OfflineResourceSupplier(self.root)
        result = build_library(self.root, source=GuardedSource(supplier))
        self.assertEqual(result['summary']['status'], 'complete')
        self.assertEqual(result['summary']['error'], 0)

    def test_query_snapshots_cannot_change_candidate_loading(self):
        """调用者修改查询得到的元数据，不能改变后续实际读取和校验。"""
        self.write_cache_manifest()
        supplier = resources.OfflineResourceSupplier(self.root)
        logical_name = 'main/images/mixedcase.iwi'
        cached_entry = supplier.cached_entry(logical_name)
        receipt_entries = supplier.receipt_entries(logical_name)
        cached_entry[1]['size'] = 0
        receipt_entries[0][2]['encoded_size'] = 0
        candidates = supplier.load_candidates(logical_name)
        self.assertEqual([resource_bytes for resource_bytes, provenance in candidates],
                         [fixtures.DATA, fixtures.DATA])
        self.assertEqual(supplier.indexed_names(), (logical_name,))
        self.assertIsNone(supplier.cached_entry('main/images/absent.iwi'))
        self.assertEqual(supplier.receipt_entries('main/images/absent.iwi'), ())


class LooseIndependenceTests(LooseTests):
    def test_loose_source_no_longer_loads_iwi_manifest(self):
        # 松散来源改为直接继承只读基类：损坏的 IWI 缓存清单不应影响其构造。
        expected_resources = self.prepare()[1]
        manifest_dir = self.root/'client/resources/v1649370'
        (manifest_dir/'offline-resources-manifest-v3.json').write_text(json.dumps({'source_exe_sha256': 'bad'}), encoding='utf-8')
        with self.assertRaises(ValueError):
            resources.OfflineResourceSupplier(self.root)
        source = loose.LooseResourceSource(self.root)
        self.assertEqual(len(source.indexed_names()), 4)
        self.assertFalse(hasattr(source, 'supply'), '只读非图片来源不应暴露 runtime 补件能力')
        constraints = source.library_constraints()
        constraints['resource_count'] = 0
        constraints['expected_output_bytes'] = 0
        result = build_library(self.root, source=GuardedSource(source))
        self.assertEqual(result['summary']['status'], 'complete')
        self.assertEqual(result['summary']['published'], len(expected_resources))
        for name, expected_content in expected_resources.items():
            self.assertEqual((Path(result['output_root'])/name).read_bytes(), expected_content)


class DecodeConsistencyTests(fixtures.ResourceFixture):
    def test_prepare_tool_rejects_short_iwi_header(self):
        # 准备工具此前仅查 4 字节签名；统一后与资源层一致要求完整 32 字节 IWi09 头。
        tool = load_prepare_tool()
        short = b'IWi\x09' + bytes(8)
        raw = fixtures.encoded(data=short, filename='short.iwi')
        with self.assertRaises(ValueError):
            resources.decode_iwi(raw, 'short.iwi', fixtures.KEY)
        with self.assertRaises(ValueError):
            tool.decode_loose(raw, 'short.iwi', fixtures.KEY)
        # 非 IWI 扩展名的同一外层结构仍正常解码，证明拒绝来自统一 IWI 头校验。
        self.assertEqual(tool.decode_loose(fixtures.encoded(data=b'plain', filename='a.cfg'), 'a.cfg', fixtures.KEY), b'plain')


class LibraryManifestTests(unittest.TestCase):
    def base_manifest(self):
        return {'schema': 1, 'source_exe_sha256': '36997674b18f2896de5e9d755686f6922af10708f355175714058d0a0172b537',
                'source_resource_version': 461, 'old_version_resources_used': False, 'issues': [],
                'library_kind': 'loose_startup_resources',
                'summary': {'status': 'complete', 'completed': True, 'total': 1, 'processed': 1, 'published': 1,
                            'already_present': 0, 'conflict': 0, 'error': 0, 'validated_output_bytes': 4, 'expected_output_bytes': 4},
                'resources': [{'game_path': 'main/models/a', 'size': 4}]}

    def test_valid_manifest_passes_and_incomplete_fails(self):
        from codol_rebuild.resources.library_manifest import validate_library_manifest
        validate_library_manifest(self.base_manifest(), 'loose_startup_resources')
        for mutate in (
            lambda m: m['summary'].__setitem__('conflict', 1),
            lambda m: m['summary'].__setitem__('validated_output_bytes', 5),
            lambda m: m['summary'].__setitem__('completed', False),
            lambda m: m.pop('library_kind'),
        ):
            manifest = self.base_manifest()
            mutate(manifest)
            with self.assertRaises(ValueError):
                validate_library_manifest(manifest, 'loose_startup_resources')


if __name__ == '__main__':
    unittest.main()
