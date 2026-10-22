"""类型限定非IWI库：安全路径、严格外层、小资源及容器边界。"""
import json
from pathlib import Path
import struct
import unittest
from unittest.mock import patch
import test_offline_resources as fixtures
from codol_rebuild.resources import offline_resources as resources
from codol_rebuild.resources import loose_library as loose

MODEL=struct.pack('<8I',1,3922314311,0,88,0,0,0,0)+b'model-data'
MP3=bytes.fromhex('fffb9000')+bytes(64)
WAV_BODY=b'WAVE'+b'fmt '+struct.pack('<I',16)+struct.pack('<HHIIHH',1,1,8000,16000,2,16)+b'data'+struct.pack('<I',2)+bytes(2)
WAV=b'RIFF'+struct.pack('<I',len(WAV_BODY))+WAV_BODY

class LooseTests(unittest.TestCase):
    setUp=fixtures.ResourceFixture.setUp

    def prepare(self):
        values={'main/models/model_lod010':MODEL,'main/sound/sample.mp3':MP3,'main/sound/sample.wav':WAV,'main/filesyscheck.cfg':b'set fs_test 1\n'}
        entries={};rows=[]
        for name,data in values.items():
            path=self.folder/name;path.parent.mkdir(parents=True,exist_ok=True)
            decode_name='fileSysCheck.cfg' if name.endswith('filesyscheck.cfg') else path.name
            path.write_bytes(fixtures.encoded(data=data,filename=decode_name))
            entries[name]={'path':name,'decode_filename':decode_name,'encoded_size':path.stat().st_size,'encoded_sha256':resources.file_digest(path)}
            rows.append({'game_path':name,'archive':self.archive,'encoded_sha256':entries[name]['encoded_sha256'],'decoded_sha256':resources.digest(data),'decoded_size':len(data)})
        receipt={'archive':self.receipt['archive'],'entries':entries};p=self.folder/'extraction-inventory.json';p.write_text(json.dumps(receipt),encoding='utf-8');resources.RAW_INVENTORIES[self.archive]=resources.file_digest(p)
        proof=self.root/loose.AUDIT_PATH;proof.parent.mkdir(parents=True);proof.write_text(json.dumps({'resources':rows,'issues':[],'overrides':[],'unique_resource_count':4,'total_selected_decoded_bytes':sum(map(len,values.values()))}),encoding='utf-8')
        priority=self.root/loose.PRIORITY_PATH;priority.write_text('verified fixture native order',encoding='utf-8')
        self.patches.enter_context(patch.object(loose,'AUDIT_SHA',resources.file_digest(proof)));self.patches.enter_context(patch.object(loose,'PRIORITY_SHA',resources.file_digest(priority)))
        return loose.LooseResourceSource(self.root),values

    def test_canonical_only_reviewed_data_types(self):
        self.assertEqual(loose.canonical_loose_resource(r'.\MAIN\models\weapon_lod010'),'main/models/weapon_lod010')
        self.assertEqual(loose.canonical_loose_resource('main/fileSysCheck.cfg'),'main/filesyscheck.cfg')
        for path in ['main/models/test.exe','main/models/test.dll','main/sound/test.cmd','main/autoexec.cfg','main/images/test.iwi']:
            self.assertIsNone(loose.canonical_loose_resource(path))
        for path in ['../main/models/test','main/models/CON','main/sound/a.wav:run']:
            with self.assertRaises(ValueError):loose.canonical_loose_resource(path)

    def test_build_all_data_types_and_resume(self):
        source,values=self.prepare();result=loose.build_library(self.root,source=source)
        self.assertEqual(result['library_kind'],'loose_startup_resources');self.assertEqual(result['summary']['published'],4);self.assertEqual(result['issues'],[])
        output=Path(result['output_root']);self.assertEqual(output.name,'offline-loose-library')
        for name,data in values.items():self.assertEqual((output/name).read_bytes(),data)
        self.assertEqual(loose.build_library(self.root,source=source)['summary']['already_present'],4)

    def test_type_headers_and_container_bounds(self):
        for data,kind in [(MODEL,'model'),(MP3,'mp3'),(WAV,'wav'),(b'x','config')]:loose.inspect_format(data,kind)
        for data,kind in [(b'MZ'+bytes(40),'model'),(MODEL[:16],'model'),(b'ID3\x03\0\0\x7f\x7f\x7f\x7f','mp3'),(WAV[:-1],'wav'),(b'bad\0cfg','config')]:
            with self.assertRaises(ValueError):loose.inspect_format(data,kind)

    def test_shared_decoder_allows_short_data_but_iwi_does_not(self):
        raw=fixtures.encoded(data=b'x',filename='short.cfg')
        self.assertEqual(resources.decode_loose(raw,'short.cfg',fixtures.KEY),b'x')
        with self.assertRaises(ValueError):resources.decode_iwi(raw,'short.cfg',fixtures.KEY)

    def test_fixed_library_target_cannot_be_redirected(self):
        source,_=self.prepare();source.library_directory='other';
        with self.assertRaises(ValueError):loose.build_library(self.root,source=source)

    def test_manifest_scope_mismatch_is_rejected(self):
        source,_=self.prepare();source.expected_bytes+=1
        with self.assertRaises(ValueError):loose.build_library(self.root,source=source)

if __name__=='__main__':unittest.main()
