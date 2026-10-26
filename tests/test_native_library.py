"""完整资源库回归：覆盖顺序、失败不发布、树外清单、顶层装配和启动参数。"""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_offline_resources as fixtures
from codol_rebuild.resources import native_library
from codol_rebuild.resources.runtime_cache import prepare_library


class NativeLibraryTests(unittest.TestCase):
    def setUp(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.project_root = Path(temporary_directory.name)
        self.version_root = self.project_root / 'client/resources/v1649370'
        self.version_root.mkdir(parents=True)

    def fixture_plan(self, corrupt=False):
        """按原生顺序布置两个同名候选，第二个必须胜出。"""
        sources = []
        for mount_index, content in enumerate((fixtures.DATA, fixtures.DATA + b'patch')):
            source = self.project_root / ('candidate-' + str(mount_index))
            source.write_bytes(fixtures.encoded(content, filename='MixedCase.iwi'))
            sources.append({'source': str(source), 'encoded_size': source.stat().st_size,
                            'decode_filename': 'MixedCase.iwi', 'mount_index': mount_index,
                            'archive': 'candidate-' + str(mount_index) + '.ifs'})
        if corrupt:
            encoded_bytes = bytearray(source.read_bytes())
            encoded_bytes[0] ^= 1
            source.write_bytes(encoded_bytes)
        return {'resources': [{'game_path': 'hires/images/mixedcase.iwi',
                               'size': len(fixtures.DATA) + 5, 'candidates': sources}],
                'resource_count': 1, 'expected_output_bytes': len(fixtures.DATA) + 5,
                'source_receipts': [], 'mount_order': [], 'client_image': 'unused'}

    def test_overrides_and_identical_rebuild(self):
        plan = self.fixture_plan()
        with patch.object(native_library, 'create_plan', return_value=plan), \
             patch.object(native_library, 'RESOURCE_KEY', fixtures.KEY):
            report = native_library.build_library(self.project_root, publish=True, progress=lambda value: None)
            self.assertTrue(report['summary']['completed'])
            self.assertEqual(report['overrides'][0]['selected_mount_index'], 1)
            output = self.version_root / 'game/hires/images/mixedcase.iwi'
            self.assertEqual(output.read_bytes(), fixtures.DATA + b'patch')
            self.assertFalse((self.version_root / 'game/manifest.json').exists())
            repeated = native_library.build_library(self.project_root, publish=True, progress=lambda value: None)
            self.assertEqual(repeated['summary']['already_present'], 1)
            output.write_bytes(b'tampered')
            refused = native_library.build_library(self.project_root, publish=True, progress=lambda value: None)
            self.assertFalse(refused['summary']['completed'])
            self.assertEqual(output.read_bytes(), b'tampered')

    def test_broken_source_never_publishes_completion(self):
        plan = self.fixture_plan(corrupt=True)
        with patch.object(native_library, 'create_plan', return_value=plan), \
             patch.object(native_library, 'RESOURCE_KEY', fixtures.KEY):
            report = native_library.build_library(self.project_root, publish=True, progress=lambda value: None)
        self.assertFalse(report['summary']['completed'])
        self.assertEqual(report['summary']['error'], 1)
        self.assertFalse(native_library.manifest_path(self.version_root / 'game').exists())

    def test_superseded_encoding_is_not_required_by_selected_resource(self):
        """已被完整替换的旧版本保留摘要，不要求新版解码器能解码旧候选。"""
        plan = self.fixture_plan()
        old_candidate = plan['resources'][0]['candidates'][0]
        old_source = Path(old_candidate['source'])
        old_source.write_bytes(b'previous format; no longer mounted at this path')
        old_candidate['encoded_size'] = old_source.stat().st_size
        with patch.object(native_library, 'create_plan', return_value=plan), \
             patch.object(native_library, 'RESOURCE_KEY', fixtures.KEY):
            report = native_library.build_library(self.project_root, publish=True, progress=lambda value: None)
        self.assertTrue(report['summary']['completed'])
        self.assertEqual(report['overrides'][0]['candidates'][0]['encoded_sha256'],
                         hashlib.sha256(old_source.read_bytes()).hexdigest())

    def test_tree_external_manifest_checks_every_directory(self):
        game_root = self.version_root / 'game'
        resource = game_root / 'hires/images/sample.iwi'
        resource.parent.mkdir(parents=True)
        resource.write_bytes(fixtures.DATA)
        manifest_file = native_library.manifest_path(game_root)
        manifest_file.write_bytes(b'external manifest')
        entries = [{'game_path': 'hires/images/sample.iwi', 'size': len(fixtures.DATA),
                    'sha256': hashlib.sha256(fixtures.DATA).hexdigest()}]
        parameters = (self.project_root / 'receipts', game_root, b'external manifest', entries,
                      native_library.canonical_native_resource, lambda directory: None)
        prepare_library(*parameters, external_manifest=manifest_file)
        (game_root / 'unregistered').write_bytes(b'extra')
        with self.assertRaisesRegex(ValueError, '未登记'):
            prepare_library(*parameters, external_manifest=manifest_file)

    def test_receipt_requires_actual_file_coverage_and_contiguous_order(self):
        package_root = self.project_root / 'raw/package'
        resource = package_root / 'main/images/mixedcase.iwi'
        resource.parent.mkdir(parents=True)
        resource.write_bytes(fixtures.encoded())
        (package_root / 'package.lst').write_text('main/images/MixedCase.iwi\n')
        receipt_file = package_root / 'receipt.json'
        receipt = {'archive': 'package.ifs', 'other': [], 'missing': ['main', 'main/images'],
                   'files': [{'path': 'main/images/mixedcase.iwi', 'size': resource.stat().st_size}]}
        receipt_file.write_text(json.dumps(receipt))
        order = [{'index': 0, 'archive': 'package.ifs'}]
        candidates, receipts = native_library.collect_candidates(self.project_root / 'raw', order)
        self.assertEqual(candidates['main/images/mixedcase.iwi'][0]['decode_filename'], 'MixedCase.iwi')
        self.assertEqual(receipts[0]['resource_count'], 1)
        with patch.object(native_library, 'RESOURCE_KEY', fixtures.KEY):
            decoded, provenance = native_library.load_candidate(candidates['main/images/mixedcase.iwi'][0],
                                                                'main/images/mixedcase.iwi', Path('unused'))
        self.assertEqual(decoded, fixtures.DATA)
        receipt['missing'].append('main/models/missing_lod')
        receipt_file.write_text(json.dumps(receipt))
        with self.assertRaisesRegex(ValueError, '缺失项'):
            native_library.collect_candidates(self.project_root / 'raw', order)
        with self.assertRaisesRegex(ValueError, '不连续'):
            native_library.collect_candidates(self.project_root / 'raw', [{'index': 4, 'archive': 'package.ifs'}])

    def test_patch_uses_shared_listing_with_original_case(self):
        """无独立 lst 的 V0.1 分包使用此前同系列列表，不能采用提取后的小写 nonce。"""
        raw_root = self.project_root / 'raw'
        base_package = raw_root / 'lf_map'
        base_package.mkdir(parents=True)
        (base_package / 'lf_map.lst').write_text('main/images/MixedCase.iwi\n')
        (base_package / 'receipt.json').write_text(json.dumps({
            'archive': 'lf_map.ifs', 'other': [], 'missing': [],
            'files': [{'path': 'lf_map.lst', 'size': 26}]}))
        patch_package = raw_root / 'lf_map_V0.1'
        resource = patch_package / 'main/images/mixedcase.iwi'
        resource.parent.mkdir(parents=True)
        resource.write_bytes(fixtures.encoded())
        (patch_package / 'receipt.json').write_text(json.dumps({
            'archive': 'lf_map_V0.1.ifs', 'other': [], 'missing': [],
            'files': [{'path': 'main/images/mixedcase.iwi', 'size': resource.stat().st_size}]}))
        candidates, receipts = native_library.collect_candidates(raw_root, [
            {'index': 0, 'archive': 'lf_map.ifs'}, {'index': 1, 'archive': 'lf_map_V0.1.ifs'}])
        self.assertEqual(candidates['main/images/mixedcase.iwi'][0]['decode_filename'], 'MixedCase.iwi')
        self.assertEqual(receipts[0]['original_name_listing'], receipts[1]['original_name_listing'])

    def test_windows_paths_and_native_scope(self):
        for name in ('../main/images/a.iwi', 'main/images/CON.iwi', 'main/images/a.iwi:stream'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                native_library.canonical_native_resource(name)
        self.assertEqual(native_library.canonical_native_resource('hires/images/Test.iwi'), 'hires/images/test.iwi')
        self.assertIsNone(native_library.canonical_native_resource('main/plugin.dll'))
        self.assertIsNone(native_library.canonical_native_resource('players/config_mp.cfg'))

    def test_bootstrap_only_mounts_top_directories(self):
        from codol_rebuild.bootstrap import resource_setup
        game_root = self.version_root / 'game'
        game_root.mkdir()
        runtime = self.project_root / 'run/runtime'
        runtime.mkdir(parents=True)
        dependency = game_root / 'libcef.dll'
        dependency.write_bytes(b'dependency')
        manifest = {'resources': [{'game_path': 'libcef.dll', 'size': 10,
                                  'sha256': hashlib.sha256(b'dependency').hexdigest()}],
                    'summary': {'validated_output_bytes': 10}}
        with contextlib.ExitStack() as patches:
            patches.enter_context(patch.object(resource_setup, 'ROOT', self.project_root / 'client'))
            patches.enter_context(patch.object(native_library, 'read_manifest', return_value=(b'manifest', manifest)))
            patches.enter_context(patch.object(resource_setup, 'prepare_library'))
            resource_directories, dependencies = resource_setup.prepare_native_library(game_root, runtime.parent, {}, False)
        self.assertEqual([directory.name for directory in resource_directories], list(native_library.MOUNT_DIRECTORIES))
        self.assertTrue(all(directory.parent == game_root for directory in resource_directories))
        self.assertEqual(dependencies, manifest['resources'])
        self.assertFalse((runtime / 'libcef.dll').exists(), '资源校验不再复制程序依赖')

    def test_removed_legacy_arguments_are_rejected(self):
        from codol_rebuild.bootstrap.config import parse_args
        with contextlib.redirect_stderr(io.StringIO()):
            for legacy in ('--overlay', '--resource-library', '--loose-library', '--prewarm-run'):
                with self.subTest(legacy=legacy), self.assertRaises(SystemExit):
                    parse_args(['--name', 'test', legacy, 'old'])
            with self.assertRaises(SystemExit):
                parse_args(['--name', 'test', '--supply-resources'])
        self.assertEqual(parse_args(['--name', 'test']).native_library.name, 'game')
