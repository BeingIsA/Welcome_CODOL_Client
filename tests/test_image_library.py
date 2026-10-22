"""图片库批量模式：尺寸计划、逐字节复跑、真实冲突和失败报告。"""
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from codol_rebuild.resources import image_library as library
from codol_rebuild.resources import offline_resources as resources
import test_offline_resources as fixtures
DATA, NAME, encoded = fixtures.DATA, fixtures.NAME, fixtures.encoded


class LibraryTests(unittest.TestCase):
    setUp = fixtures.ResourceFixture.setUp

    def test_override_proof_outside_fixed_resources_is_rejected(self):
        source = self.make_source()
        proof = self.root / 'outside-proof.json'
        proof.write_text('matching hash does not authorize this location', encoding='utf-8')
        for path in ('outside-proof.json', 'client/resources/v1649370/../../../outside-proof.json'):
            rule = {'evidence': {'path': path, 'sha256': resources.file_digest(proof)}}
            with self.subTest(path=path), patch.dict(resources.RESOLUTIONS, {'main/images/mixedcase.iwi': rule}):
                with self.assertRaisesRegex(ValueError, '路径越界'):
                    source.select_candidate('main/images/mixedcase.iwi', [(DATA, {})])

    def make_source(self):
        return resources.OfflineResourceSupplier(self.root)

    def test_plan_readonly_source_and_publish_resume(self):
        source=self.make_source();self.assertIsNone(source.runtime)
        self.assertEqual(source.supply('images/mixedcase.iwi')['status'],'error')
        plan=library.build_library(self.root,source=source,plan_only=True)
        self.assertEqual(plan['expected_output_bytes'],len(DATA))
        output=Path(plan['output_root']);self.assertFalse((output/'main').exists())
        orphan=output/'main/images/mixedcase.iwi.building';orphan.parent.mkdir(parents=True);orphan.write_bytes(b'old interrupted write')
        first=library.build_library(self.root,source=source)
        self.assertEqual(first['summary']['published'],1)
        original_load=source.load_candidates
        def checked_load(name):
            pending=json.loads((output/'manifest.json').read_text(encoding='utf-8'))
            self.assertFalse(pending['summary']['completed'])
            return original_load(name)
        with patch.object(source,'load_candidates',side_effect=checked_load):
            second=library.build_library(self.root,source=source)
        self.assertEqual(second['summary']['already_present'],1)
        self.assertEqual(second['summary']['validated_output_bytes'],len(DATA))
        self.assertEqual(second['issues'],[])
        self.assertFalse((self.runtime.parent/'resource-supply.jsonl').exists())
        self.assertEqual((output/'main/images/mixedcase.iwi').read_bytes(),DATA)

    def test_encoded_tamper_is_explicit_error_without_output(self):
        source=self.make_source();self.raw.write_bytes(b'X'+self.raw.read_bytes()[1:])
        result=library.build_library(self.root,source=source)
        self.assertTrue(result['summary']['completed']);self.assertEqual(result['summary']['error'],1)
        self.assertEqual(result['resources'],[]);self.assertIn('摘要',result['issues'][0]['reason'])

    def test_existing_different_file_is_preserved(self):
        source=self.make_source();root=self.root/'client/resources/v1649370/offline-image-library';target=root/'main/images/mixedcase.iwi'
        target.parent.mkdir(parents=True);target.write_bytes(b'preserve')
        result=library.build_library(self.root,source=source)
        self.assertEqual(result['summary']['error'],1);self.assertEqual(target.read_bytes(),b'preserve')

    def test_known_conflict_never_loads_or_publishes(self):
        evidence=self.root/'client/resources/v1649370/common-mp-fragment-conflicts.json';evidence.parent.mkdir(parents=True)
        evidence.write_text(json.dumps({'resources':{'main/images/mixedcase.iwi':{'decoded_content_identical':False,'candidates':[{'decoded_sha256':'a'},{'decoded_sha256':'b'}]}}}),encoding='utf-8')
        source=self.make_source()
        with patch.object(source,'load_candidates',side_effect=AssertionError('不应选择冲突候选')):
            result=library.build_library(self.root,source=source)
        self.assertEqual(result['summary']['conflict'],1);self.assertEqual(result['resources'],[])

    def test_two_actual_decoded_candidates_conflict(self):
        source=self.make_source();archive='other.ifs';original=self.source_archive(archive)
        folder=self.workspace/'offline-resource-raw/other';raw=folder/'main/images'/NAME;raw.parent.mkdir(parents=True)
        raw.write_bytes(encoded(data=DATA[:-1]+b'Z'))
        entry={'path':f'main/images/{NAME}','decode_filename':NAME,'encoded_size':raw.stat().st_size,'encoded_sha256':resources.file_digest(raw)}
        source._receipts['main/images/mixedcase.iwi'].append((archive,folder,entry,'fixture'))
        with patch.dict(resources.ARCHIVES,{archive:resources.file_digest(original)}):
            result=library.build_library(self.root,source=source)
        self.assertEqual(result['summary']['conflict'],1);self.assertEqual(result['resources'],[])
        self.assertEqual(len(result['issues'][0]['candidates']),2)

    def test_proven_single_override_is_shared_and_fails_closed(self):
        source=self.make_source();source.runtime=self.runtime
        archive='patch.ifs';original=self.source_archive(archive)
        folder=self.workspace/'offline-resource-raw/patch';raw=folder/'main/images'/NAME;raw.parent.mkdir(parents=True)
        newer=DATA[:-1]+b'Z';raw.write_bytes(encoded(data=newer))
        entry={'path':f'main/images/{NAME}','decode_filename':NAME,'encoded_size':raw.stat().st_size,'encoded_sha256':resources.file_digest(raw)}
        source._receipts['main/images/mixedcase.iwi'].append((archive,folder,entry,'fixture'))
        proof=self.root/'client/resources/v1649370/fixture-priority.json';proof.parent.mkdir(parents=True);proof.write_text('fixture priority',encoding='utf-8')
        rule={'selected_archive':archive,'evidence':{'path':'client/resources/v1649370/fixture-priority.json','sha256':resources.file_digest(proof)},'candidates':[
            {'archive':self.archive,'encoded_sha256':self.entry['encoded_sha256'],'decoded_sha256':resources.digest(DATA),'decoded_size':len(DATA)},
            {'archive':archive,'encoded_sha256':entry['encoded_sha256'],'decoded_sha256':resources.digest(newer),'decoded_size':len(newer)}]}
        with patch.dict(resources.ARCHIVES,{archive:resources.file_digest(original)}):
            rejected=source.supply('images/mixedcase.iwi')
            self.assertEqual(rejected['status'],'error');self.assertIn('冲突',rejected['reason'])
            with patch.dict(resources.RESOLUTIONS,{'main/images/mixedcase.iwi':rule}):
                supplied=source.supply('images/mixedcase.iwi');self.assertEqual(supplied['status'],'supplied')
                self.assertEqual(supplied['provenance']['archive'],archive)
                prior=self.root/'client/resources/v1649370/offline-image-library/main/images/mixedcase.iwi'
                prior.parent.mkdir(parents=True);prior.write_bytes(DATA)
                result=library.build_library(self.root,source=source)
                self.assertEqual(result['summary']['replaced_known_obsolete'],1)
                self.assertEqual(Path(result['replacements'][0]['old_copy']).read_bytes(),DATA)
                self.assertEqual(prior.read_bytes(),newer)
                self.assertEqual(result['issues'],[]);self.assertEqual(result['resources'][0]['sha256'],resources.digest(newer))
                self.assertEqual(len(result['resolutions']),1)
                proof.write_text('modified evidence',encoding='utf-8')
                failed=source.supply('images/mixedcase.iwi');self.assertEqual(failed['status'],'error');self.assertIn('证据摘要',failed['reason'])
                self.assertEqual((self.runtime/'main/images/mixedcase.iwi').read_bytes(),newer)

    def source_archive(self,name):
        # 生产代码从项目内固定源包目录读取，夹具同步镜像该布局。
        original=self.root/'client/workspaces/offline-resource-inputs'/name;original.parent.mkdir(parents=True,exist_ok=True);original.write_bytes(b'another new archive');return original

    def test_interrupt_is_reported_and_propagated(self):
        source=self.make_source()
        with patch.object(source,'load_candidates',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):library.build_library(self.root,source=source)
        saved=json.loads((self.root/'client/resources/v1649370/offline-image-library/manifest.json').read_text(encoding='utf-8'))
        self.assertFalse(saved['summary']['completed']);self.assertEqual(saved['summary']['status'],'interrupted')

    def test_project_mismatch_fails_before_output(self):
        with self.assertRaises(ValueError):library.plan_library(self.root/'outside',self.make_source())


if __name__=='__main__':unittest.main()
