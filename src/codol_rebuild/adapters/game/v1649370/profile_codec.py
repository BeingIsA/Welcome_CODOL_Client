"""1649370 账号格式适配：游戏表解析、DDL 编码、快照校验与归档验证。"""
import struct
from codol_rebuild.adapters.game.v1649370.layout import BUFFER_CAPACITY, PUBLIC_IDENTITY, SECURE_IDENTITY
from codol_rebuild.adapters.game.v1649370.profile_unlock import apply_full_unlock, UnlockError
from codol_rebuild.adapters.game.profile_format.fields import Fields

IDENTITIES={'public':PUBLIC_IDENTITY,'secure':SECURE_IDENTITY}

# 业务配装字段到档案 DDL 路径的映射，仅本版本适配层使用。
LOADOUT_DDL_FIELDS = (
    ('primaryBase', lambda row: row['primary']['base']),
    ('primaryAttach.0', lambda row: row['primary']['attachments'][0]),
    ('primaryAttach.1', lambda row: row['primary']['attachments'][1]),
    ('secondaryBase', lambda row: row['secondary']['base']),
    ('secondaryAttach.0', lambda row: row['secondary']['attachments'][0]),
    ('perks.0', lambda row: row['perks'][0]),
    ('perks.1', lambda row: row['perks'][1]),
    ('perks.2', lambda row: row['perks'][2]),
    ('lethal', lambda row: row['lethal']),
    ('tactical', lambda row: row['tactical']),
    ('wildcards.0', lambda row: row['wildcards'][0]),
)

def progression(tables):
    rank=tables['mp/rankTable.csv'];exp=tables['mp/rankExpTable.csv']
    maximum=next(r for r in rank if r[0]=='maxrank')
    prestige=int(next(r for r in rank if r[0]=='maxprestige')[1])
    index=int(maximum[1]);version=int(maximum[3])
    row=next(r for r in exp if r[0]==str(index))
    # Native 50C390 subtracts one from saved rank. 50C250 adds saved
    # experience to this rank's threshold (prestige*3+1).
    return {'rank':index+1,'rank_version':version,'prestige':prestige,
            'experience':int(row[prestige*3+2])-1}



class AccountCodec:
    """固定档案格式实现；不拥有账号数据库或原生进程。"""
    identities = IDENTITIES

    def __init__(self, schemas, tables, catalog):
        self.schemas, self.tables, self.catalog = schemas, tables, catalog

    def initialization_data(self, new_account=True):
        """解析为业务结构：配装按武器/附件/技能分组，物品提供业务分类与商店、
        预留标志；DDL 字段路径与原生分类数字只存在于本版本适配层。"""
        byname={r['name'].lower():r['id'] for r in self.catalog if r['id']}
        def item(value):
            if not value: return 0
            if value.lower() not in byname: raise ValueError('Missing loadout item: '+value)
            return byname[value.lower()]
        loadouts=[]
        for row in (self.tables['mp/coStartingLoadoutSetting.csv'][:3] if new_account else []):
            loadout={'primary': {'base': item(row[1]), 'attachments': [item(row[2]), item(row[3])]},
                     'secondary': {'base': item(row[4]), 'attachments': [item(row[5])]},
                     'perks': [item(row[6]), item(row[7]), item(row[8])],
                     'wildcards': [item(row[13])],
                     'lethal': item(row[9]), 'tactical': item(row[11]),
                     'lethal_double_ammo': row[10]=='1', 'tactical_double_ammo': row[12]=='1'}
            loadouts.append(loadout)
        return {'progression':progression(self.tables), 'default_character': 545,
                'starting_loadouts':loadouts,
                'items': [{'id': row['id'],
                           'category': 'character' if row['type_bytes'][1] == 2 else 'equipment',
                           'slot_group': (row['type_bytes'][1], row['type_bytes'][2]),
                           'marketplace_only': row.get('gp_cp_sale'),
                           'reserved': row['id'] in (0, 15400, 20000)} for row in self.catalog]}

    def initialize(self, saved, policy):
        public_fields=Fields(self.schemas['public']); secure_fields=Fields(self.schemas['secure'])
        ids={r['id'] for r in self.catalog}
        blobs={}
        for kind,identity in IDENTITIES.items():
            base=saved['profiles'].get(kind,bytes(BUFFER_CAPACITY))
            if len(base)!=BUFFER_CAPACITY:raise ValueError('Invalid stored profile capacity')
            blobs[kind]=bytearray(base)
            struct.pack_into('<II',blobs[kind],0,identity['version'],identity['checksum'])
        if not saved['profiles']:
            # The original onboarding preview includes character 545. Validate
            # against this build's catalog before using it as the initial selection.
            if policy['character'] not in ids:raise ValueError('Default character missing from catalog')
            public_fields.write(blobs['public'],'characterClasses.0.slots.0',policy['character'])
            public_fields.write(blobs['public'],'currentCharacterIndex',0)
            public_fields.write(blobs['public'],'onboardCharacterID',policy['character'])
            for row in self.tables['mp/uiNotifications.csv']:
                if row[0].startswith(('onboard_','un_')):
                    public_fields.write(blobs['public'],'uiNotificationFlags.'+row[1],True)
            for slot, row in enumerate(policy['loadouts']):
                prefix=f'weaponClasses.{slot}.'
                public_fields.write(blobs['public'],prefix+'characterIndex',0)
                public_fields.write(blobs['public'],prefix+'name',row['name'])
                for key,read_value in LOADOUT_DDL_FIELDS:
                    public_fields.write(blobs['public'],prefix+key,read_value(row))
                public_fields.write(blobs['public'],prefix+'lethalDoubleAmmo',row['lethal_double_ammo'])
                public_fields.write(blobs['public'],prefix+'tacticalDoubleAmmo',row['tactical_double_ammo'])
        config={**policy['progression'], 'fill_unlock_flags':policy['fill_unlock_flags']}
        blobs['secure'],report=apply_full_unlock(bytes(blobs['secure']),self.schemas['secure'],config)
        blobs['secure']=bytearray(blobs['secure'])
        secure_fields.write(blobs['secure'],'prestige',config['prestige'])
        # 4A6D60 reads signed-int16 unlockFlagsNew entries as progress thresholds,
        # not bits. Grant all declared thresholds in the private full-unlock mode.
        for row in self.tables['mp/unlockFlags.csv']:
            if len(row)>2 and row[2]=='1':
                secure_fields.write(blobs['secure'],'unlockFlagsNew.'+row[1],policy['unlock_threshold'])
        for path,value in [('userIDLow',saved['uid']&0xffffffff),('userIDHigh',saved['uid']>>32)]:
            secure_fields.write(blobs['secure'],path, value if value<2**31 else value-2**32)
        blobs=self.migrate({k:bytes(v) for k,v in blobs.items()})
        return blobs, report

    def migrate(self, profiles):
        """补齐本版本配装格式标记；只修复漏写的零值，不重建配装或授权。"""
        self.validate_snapshot(profiles)
        fields = Fields(self.schemas['public'])
        if fields.read(profiles['public'], 'loadoutVersion') != 0:
            return dict(profiles)
        public = bytearray(profiles['public'])
        fields.write(public, 'loadoutVersion', 3)
        return {**profiles, 'public': bytes(public)}

    def validate_snapshot(self, blobs):
        for kind,identity in self.identities.items():
            if len(blobs[kind]) != BUFFER_CAPACITY or blobs[kind][:8] != struct.pack('<II',identity['version'],identity['checksum']):
                raise ValueError('Runtime profile schema changed; refusing save')
        fields=Fields(self.schemas['public'])
        index=fields.read(blobs['public'],'currentCharacterIndex')
        character=fields.read(blobs['public'],f'characterClasses.{index}.slots.0')
        if character<=0: raise ValueError('Runtime selected character missing; refusing save')
        return character


class SecureArchiveFormat:
    """保留历史归档的容量验证，不增加新的内容限制。"""
    identity = SECURE_IDENTITY
    def validate(self, blob):
        if len(blob) != BUFFER_CAPACITY:
            raise UnlockError(f'存档长度 {len(blob)} 不是缓冲容量 {BUFFER_CAPACITY}')
