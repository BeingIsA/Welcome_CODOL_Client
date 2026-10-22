"""回归验证：检查 local_profile_service 的行为与边界，不作为客户端实测结果。"""
import json
from pathlib import Path
import tempfile
import unittest
from codol_rebuild.adapters.persistence.sqlite_accounts import AccountRepository, RevisionConflict
from codol_rebuild.adapters.game.profile_format.fields import Fields
from codol_rebuild.adapters.game.v1649370.profile_codec import progression

ROOT=Path(__file__).resolve().parents[2]


class FieldsTests(unittest.TestCase):
    def setUp(self):
        self.schema=json.loads((Path(__file__).parent/'fixtures/local-profile-capture/playerdata/playerdata-schema.json').read_text(encoding='utf-8'))
        self.fields=Fields(self.schema)

    def test_nested_fields_preserve_neighbours(self):
        b=bytearray(32764)
        self.fields.write(b,'characterClasses.0.slots.0',545)
        self.fields.write(b,'characterClasses.1.slots.0',546)
        self.fields.write(b,'weaponClasses.4.name','Local 5')
        self.assertEqual(self.fields.read(b,'characterClasses.0.slots.0'),545)
        self.assertEqual(self.fields.read(b,'characterClasses.1.slots.0'),546)
        self.assertEqual(self.fields.read(b,'weaponClasses.4.name'),'Local 5')
        self.assertEqual(self.fields.read(b,'weaponClasses.3.name'),'')

    def test_array_bounds_and_bit_write(self):
        b=bytearray(32764)
        with self.assertRaises(ValueError):self.fields.write(b,'characterClasses.12.slots.0',1)
        with self.assertRaises(ValueError):self.fields.write(b,'characterClasses.-1.slots.0',1)
        self.fields.write(b,'uiNotificationFlags.87',True)
        self.assertTrue(self.fields.read(b,'uiNotificationFlags.87'))
        self.assertFalse(self.fields.read(b,'uiNotificationFlags.86'))


class RepositoryTests(unittest.TestCase):
    def test_atomic_pair_reload_and_stale_save(self):
        temp_root=ROOT/'verification/workspaces/test-temp'
        temp_root.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temp_root) as tmp:
            path=Path(tmp)/'accounts.sqlite3'
            repo=AccountRepository(path);account=repo.identity()
            identities={'public':{'version':1},'secure':{'version':2}}
            self.assertEqual(repo.load('local',identities)['profiles'],{})
            self.assertEqual(repo.save('local',0,{'public':b'a','secure':b'b'},identities),1)
            reopened=AccountRepository(path)
            self.assertEqual(reopened.identity()['uid'],account['uid'])
            with self.assertRaises(RevisionConflict):
                repo.save('local',0,{'public':b'bad','secure':b'bad'},identities)
            self.assertEqual(reopened.load('local',identities)['profiles'],{'public':b'a','secure':b'b'})
            with self.assertRaises(ValueError):repo.save('local',1,{'public':b'only'},identities)
            with self.assertRaises(ValueError):repo.load('local',{'public':{'version':99},'secure':{'version':2}})


class ProgressionTests(unittest.TestCase):
    def test_rank_is_one_based_experience_is_within_rank(self):
        tables={'mp/rankTable.csv':[['maxrank','69','version','4'],['maxprestige','4']],
                'mp/rankExpTable.csv':[['69','3582550','150000','3732550','4478187','187500','4665687',
                                      '5373825','225000','5598825','6269462','262500','6531962','11056700','267000','11323700']]}
        self.assertEqual(progression(tables),{'rank':70,'rank_version':4,'prestige':4,'experience':266999})
