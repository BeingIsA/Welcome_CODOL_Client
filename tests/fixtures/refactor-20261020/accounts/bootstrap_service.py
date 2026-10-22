"""账号业务规则与档案初始化；本模块负责 bootstrap_service。

Private-server bootstrap policy over versioned DDL data and a repository.

This layer has no Win32/address/transport dependency. The client adapter supplies
validated game tables. A future account service can use the same repository,
while match simulation remains a separate authority.
"""
from __future__ import annotations
import struct
import re
from codol_rebuild.accounts.contracts import AccountStore
from codol_rebuild.profiles.unlock import BUFFER_CAPACITY, PUBLIC_IDENTITY, SECURE_IDENTITY, apply_full_unlock
from codol_rebuild.profiles.fields import Fields

IDENTITIES={'public':PUBLIC_IDENTITY,'secure':SECURE_IDENTITY}


def validate_display_name(value: str) -> str:
    """遵循原版输入框：字母、数字、下划线、汉字，折算英文长度4～16。"""
    if not isinstance(value, str) or not re.fullmatch(r'[_0-9a-zA-Z一-龥]+', value):
        raise ValueError('昵称仅支持汉字、英文字母、数字和下划线')
    if not 4 <= sum(2 if ord(ch) > 255 else 1 for ch in value) <= 16:
        raise ValueError('昵称长度须为4～16个英文字符，汉字计2个')
    return value


def load_display_name(repository: AccountStore, name='local') -> dict:
    """读取并复核保存的昵称；空记录保留客户端原有默认名。"""
    saved = repository.display_name(name)
    if saved['value'] is not None:
        validate_display_name(saved['value'])
    return saved


def change_display_name(repository: AccountStore, revision: int, value: str, name='local') -> dict:
    """本地改名免费，独立提交昵称，不修改库存、身份或等级档案。"""
    value = validate_display_name(value)
    revision = repository.save_display_name(name, revision, value)
    return {'value': value, 'revision': revision}


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


def bootstrap(repository: AccountStore, schemas, tables, catalog, name='local'):
    repository.identity(name)
    saved=repository.load(name,IDENTITIES)
    public_fields=Fields(schemas['public']); secure_fields=Fields(schemas['secure'])
    byname={r['name'].lower():r['id'] for r in catalog if r['id']}
    ids={r['id'] for r in catalog}
    def item(value):
        if not value:return 0
        if value.lower() not in byname:raise ValueError('Missing loadout item: '+value)
        return byname[value.lower()]
    blobs={}
    for kind,identity in IDENTITIES.items():
        base=saved['profiles'].get(kind,bytes(BUFFER_CAPACITY))
        if len(base)!=BUFFER_CAPACITY:raise ValueError('Invalid stored profile capacity')
        blobs[kind]=bytearray(base)
        struct.pack_into('<II',blobs[kind],0,identity['version'],identity['checksum'])
    if not saved['profiles']:
        # The original onboarding preview includes character 545. Validate
        # against this build's catalog before using it as the initial selection.
        if 545 not in ids:raise ValueError('Default character missing from catalog')
        public_fields.write(blobs['public'],'characterClasses.0.slots.0',545)
        public_fields.write(blobs['public'],'currentCharacterIndex',0)
        public_fields.write(blobs['public'],'onboardCharacterID',545)
        for row in tables['mp/uiNotifications.csv']:
            if row[0].startswith(('onboard_','un_')):
                public_fields.write(blobs['public'],'uiNotificationFlags.'+row[1],True)
        for slot in range(5):
            row=tables['mp/coStartingLoadoutSetting.csv'][slot%3]
            prefix=f'weaponClasses.{slot}.'
            public_fields.write(blobs['public'],prefix+'characterIndex',0)
            public_fields.write(blobs['public'],prefix+'name',f'Loadout {slot+1}')
            for key,col in [('primaryBase',1),('primaryAttach.0',2),('primaryAttach.1',3),
                            ('secondaryBase',4),('secondaryAttach.0',5),('perks.0',6),
                            ('perks.1',7),('perks.2',8),('lethal',9),('tactical',11),('wildcards.0',13)]:
                public_fields.write(blobs['public'],prefix+key,item(row[col]))
            public_fields.write(blobs['public'],prefix+'lethalDoubleAmmo',row[10]=='1')
            public_fields.write(blobs['public'],prefix+'tacticalDoubleAmmo',row[12]=='1')
    config=progression(tables)
    blobs['secure'],report=apply_full_unlock(bytes(blobs['secure']),schemas['secure'],config)
    blobs['secure']=bytearray(blobs['secure'])
    secure_fields.write(blobs['secure'],'prestige',config['prestige'])
    # 4A6D60 reads signed-int16 unlockFlagsNew entries as progress thresholds,
    # not bits. Grant all declared thresholds in the private full-unlock mode.
    for row in tables['mp/unlockFlags.csv']:
        if len(row)>2 and row[2]=='1':
            secure_fields.write(blobs['secure'],'unlockFlagsNew.'+row[1],32767)
    for path,value in [('userIDLow',saved['uid']&0xffffffff),('userIDHigh',saved['uid']>>32)]:
        secure_fields.write(blobs['secure'],path, value if value<2**31 else value-2**32)
    blobs={k:bytes(v) for k,v in blobs.items()}
    revision=saved['revision']
    if blobs!=saved['profiles']:
        revision=repository.save(name,revision,blobs,IDENTITIES)
    return {'profiles':blobs,'revision':revision,'uid':saved['uid'],
            'progression':config,'unlock_report':report,'new_account':not bool(saved['profiles'])}
