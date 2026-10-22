"""固定客户端版本的原生适配；本模块负责 profile。

Build-specific local profile bridge. No networking or official identity service.

Only the debugger host owns process access. This adapter owns addresses/ABI;
profile policy and persistence belong to independent modules.
"""
from __future__ import annotations
import json
import struct
from pathlib import Path

TABLES = ('mp/rankExpTable.csv', 'mp/rankTable.csv', 'mp/uiNotifications.csv',
          'mp/coStartingLoadoutSetting.csv', 'mp/unlockFlags.csv',
          'mp/coWeaponPrimaryRankTable.csv')
DEFINITIONS = ('mp/playerdata.def', 'mp/playersecuredata.def')
CONTAINER = 0xE54BC48
PUBLIC = CONTAINER + 0x10
SECURE = CONTAINER + 0x11150
INVENTORY = 0x88BA0D8
CAPACITY = 24000
STRIDE = 184


def local_optional_service_patches():
    """Local provider has no WarGod credit service; its void request is a no-op.

    AS_FetchWarGodCreditRatingScore calls 63F9A0, which passes a null
    DemonWare service to B8A420 offline. Keep the original RET 4 callback ABI,
    return no invented score, and do not modify any shared network routine.
    """
    return [(0x6AD010, bytes.fromhex('e88b29f9ff'), b'\x90' * 5)]


LOCAL_PLAYLIST_CONTRACT = {
    0x6A9EA8: bytes.fromhex('a1b013330c'),  # UI WarGod setting: load playlist ID.
    0x5D0AFE: bytes.fromhex('e8dd1a1900'),  # AS_PlayListValid -> native predicate.
}


def validate_local_playlist_contract(read):
    """只适配已核对的设置读取和 UI 有效性查询调用边。"""
    for address, expected in LOCAL_PLAYLIST_CONTRACT.items():
        if read(address, len(expected)) != expected:
            raise ValueError(f'本地玩法数据 ABI 不匹配：{address:#x}')


def build_local_playlist_adapter(entry, ready):
    """无赛季的本地 UI 使用无效玩法 ID，避免默认0匹配空赛季。

    原版匹配条比较房间玩法与 WarGod 配置玩法，再读取赛季名称。无赛季且
    配置仍为0时仅调整 UI getter 的返回值，不写原生配置或构造赛季对象。
    AS 会缓存该设置；唯一把它传回原生的 AS_PlayListValid 同时拒绝本地-1，
    即使随后玩法表就绪，也不会让它进入没有下标检查的原生查询。
    """
    if any(type(a) is not int or not 0x10000 <= a <= 0xffffe000 for a in (entry, ready)):
        raise ValueError('本地玩法适配地址必须为有效32位地址')
    code, labels, refs = bytearray(), {}, []

    def emit(value):
        code.extend(bytes.fromhex(value))

    def word(value):
        code.extend(struct.pack('<I', value))

    def label(name):
        labels[name] = len(code)

    def branch(op, name):
        emit(op); refs.append((len(code), name)); word(0)

    def jump(address):
        emit('e9'); word((address-entry-len(code)-4) & 0xffffffff)

    def offline(fallback):
        emit('833d'); word(ready); emit('01'); branch('0f85', fallback)
        emit('a1'); word(0x849B874); emit('85c0'); branch('0f84', fallback)
        emit('83780c00'); branch('0f85', fallback)

    label('setting')
    emit('9c60')
    # 重放原始读取并先保存返回值；所有回退均恢复这个原生 ID。
    code.extend(LOCAL_PLAYLIST_CONTRACT[0x6A9EA8])
    emit('8944241c')
    offline('setting_done')
    emit('833d'); word(0xC3313A0); emit('00'); branch('0f85', 'setting_done')
    emit('837c241c00'); branch('0f85', 'setting_done')
    emit('c744241cffffffff')
    label('setting_done'); emit('619d'); jump(0x6A9EAD)

    label('valid')
    emit('9c60')
    offline('valid_original')
    # ECX 是玩法 ID，原查询无栈参数。只拦截本地供应的无效哨兵。
    emit('83f9ff'); branch('0f85', 'valid_original')
    emit('c744241c00000000 619d c3')
    label('valid_original'); emit('619d'); jump(0x7625E0)

    for position, name in refs:
        struct.pack_into('<i', code, position, labels[name]-position-4)
    patches = []
    for address, name, opcode in ((0x6A9EA8, 'setting', 0xe9), (0x5D0AFE, 'valid', 0xe8)):
        after = bytes([opcode])+struct.pack('<I', (entry+labels[name]-address-5) & 0xffffffff)
        patches.append({'va':address, 'before':LOCAL_PLAYLIST_CONTRACT[address], 'after':after})
    return {'code':bytes(code), 'patches':patches,
            'entries':{name:entry+labels[name] for name in ('setting', 'valid')}}


def local_welcome_page_patch():
    """Suppress automatic welcome/MOTD opening for the local provider.

    IsWelcomePageShowed (5CC4F0, vtable DC8238) normally loads BL from
    84AD728. Report true without changing that global or the manual MOTD
    entry. Keep the GFx Boolean assignment, managed-value cleanup and RET 4.
    Only the host's --local-profile branch installs this runtime patch.
    """
    return (0x5CC4F7, bytes.fromhex('8a1d28d74a08'), bytes.fromhex('b30190909090'))


def build_stats_ready_adapter(entry, data):
    """Route only the UI stats predicate to the validated local provider.

    67EB00 still checks ui_mp and owns GFx result construction/cleanup.
    The global online predicate 730B00 and all other callers remain original.
    Host publishes data+72 only after profile/cache/inventory validation.
    """
    code = bytearray(b'\x33\xc0')
    branches = []
    for address in (data + 72, 0x82C4A86):
        code.extend(b'\x80\x3d' + struct.pack('<I', address) + b'\x01\x75\x00')
        branches.append(len(code)-1)
    code.append(0x40)  # inc eax: true only after both checks
    finish = len(code)
    code.append(0xc3)
    for offset in branches:
        code[offset] = finish-offset-1
    site = 0x67EB05
    return {'code':bytes(code), 'patch_va':site,
            'before':b'\xe8'+struct.pack('<i',0x730B00-site-5),
            'after':b'\xe8'+struct.pack('<I',(entry-site-5)&0xffffffff)}

# One-shot entry observations, not replacements for the native callbacks.
VIEW_TRACE_POINTS = {
    0x7D38E0: 'view:RequestWeaponLoadout',
    0x7CD0A0: 'view:GetActiveCharacterLoadout',
    0x7CD3C0: 'view:GetCharacters',
    0x7CC610: 'view:UIViewerSetMode',
    0x7CC670: 'view:UIViewerSet3DSceneVisibility',
    0x617700: 'view:UIViewerFrame',
    0x5CC9E0: 'view:IsWeaponDataReady',
}

# Interior points use EBP's original Params*, never an entry ESP layout.
VIEW_RESULT_POINTS = {
    0x6B9DE4: 'view-result:CharacterObjectCreated',
    0x6B9E2D: 'view-result:CharacterLoadoutReturned',
    0x7DB8F8: 'view-result:WeaponObjectCreated',
    0x7DB933: 'view-result:WeaponLoadoutReturned',
}


def view_result(read, frame):
    """Observe a GFx return value without dereferencing its managed payload."""
    params = struct.unpack('<I', read(frame + 8, 4))[0]
    result = struct.unpack('<I', read(params, 4))[0]
    raw = read(result, 32)
    kind = struct.unpack_from('<I', raw, 12)[0]
    return {'type': kind, 'base_type': kind & 0x8f,
            'interface': hex(struct.unpack_from('<I', raw, 8)[0]),
            'payload': hex(struct.unpack_from('<I', raw, 16)[0])}


def view_state(read):
    """Read-only fixed-build state; no inferred readiness flags are written."""
    values={name:struct.unpack('<I',read(address,4))[0] for name,address in {
        'frontend_allocated':0x82E1334, 'client_interface':0x33E7938,
        'render_gate':0xC7AF69C, 'viewer_world':0x77E7D1C,
        'viewer_model_count':0xC7341D8+0x478FC,
        'viewer_mode':0xC7341E0, 'viewer_requested_mode':0xC7341F0,
        'viewer_requested_item':0xC7341F4, 'viewer_requested_extra':0xC7341F8,
        'selected_character_index':0xE567C0, 'inventory_state':0x82E1314,
        'inventory_count':0x82C4A8C,
        'weapon_definition_count':0x827F738,
        'initial_fetch_bits':0x82C4AA4, 'store_state':0x82C4A88,
    }.items()}
    for name,address in {'viewer_initialized':0xC7341D8,
                         'viewer_draw_enabled':0xC7341DB,
                         'viewer_scene_ready':0xC734204,
                         'viewer_request_pending':0xC7341EC}.items():
        values[name]=read(address,1)[0]
    return values


def publish_empty_local_store(read, write, inventory_count):
    """Complete local empty offer lists before the first UI activation.

    5CCA30 (IsInitialDataFetchPerformed) requires bits 1|2|4. Inventory
    completion 4A9260 supplies bit 1; 4A7E20 supplies 6 for the offer lists.
    These are fetch-completion bits, not login/authentication readiness.
    Only a fresh offline session with empty offer arrays is supported here.
    """
    def u32(address):
        return struct.unpack('<I', read(address, 4))[0]
    if not inventory_count or u32(0x82C4A8C) != inventory_count or read(0x82C4A86, 1) != b'\x01':
        raise ValueError('Local inventory must be published before store completion')
    counts = {hex(a): u32(a) for a in (0x82C4A90, 0x82C4A94, 0x82C4AA0)}
    if any(counts.values()):
        raise ValueError('Refusing to replace nonempty native offer lists')
    before = u32(0x82C4AA4)
    if not before & 1 or u32(0x82C4A88) not in (0, 2, 3):
        raise ValueError('Native fetch state is not an idle local snapshot')
    # 4A7C80's empty-list branch uses state 3; 4A7E20 completes lists
    # with state 3 and OR 6. Preserve unrelated completion bits.
    values = {0x82C4AEC: 3, 0x82C4A88: 3, 0x82C4AA4: before | 6}
    for address, value in values.items():
        write(address, struct.pack('<I', value))
        if u32(address) != value:
            raise ValueError('Local store completion readback mismatch')
    return {'provider': 'local_empty_store', 'offer_counts': counts,
            'fetch_bits_before': before, 'fetch_bits_after': before | 6,
            'initial_data_fetch_performed': True, 'purchases_supported': False}


def inventory_relocations(pool):
    """Only reviewed inventory-buffer operands; adjacent objects stay put.

    Many unrelated 0x4E20 constants are item IDs (currency), not capacities.
    Do not globally replace them. The original static pool keeps its original
    CRT construction/destruction; this session-owned pool is constructed below.
    """
    rows=[]
    for address,offset in [(0x4A7788,0),(0x4A77C2,0),(0x4A77D1,0x40),
                           (0x4A7822,0),(0x4A8FF8,0),(0x4A901B,0),
                           (0x4A90F9,0),(0x4A911B,0),(0x4A9242,0),(0x5E973D,0x4C)]:
        rows.append((address,struct.pack('<I',INVENTORY+offset),struct.pack('<I',pool+offset)))
    for address in (0x4A779F,0x4A900F,0x4A910F):
        rows.append((address,struct.pack('<I',INVENTORY+20000*STRIDE),struct.pack('<I',pool+CAPACITY*STRIDE)))
    for address in (0x4A777A,0x4A7783,0x4A9016,0x4A9116):
        rows.append((address,struct.pack('<I',20000),struct.pack('<I',CAPACITY)))
    return rows


def build_initializer(entry: int, data: int, pool: int) -> dict:
    code = bytearray(bytes.fromhex('55 8bec 60 83e4f0'))
    payload = bytearray(4096)
    cursor = 256
    def call(target):
        code.extend(b'\xe8'+struct.pack('<I', (target-entry-len(code)-5)&0xffffffff))
    for i, name in enumerate(TABLES + DEFINITIONS):
        raw = name.encode()+b'\0'; payload[cursor:cursor+len(raw)] = raw
        code.extend(b'\x6a\x01\x68'+struct.pack('<I', data+cursor))
        code.extend(b'\x6a'+bytes([0x25 if i < len(TABLES) else 0x27]))
        call(0x49CF30)
        code.extend(b'\x83\xc4\x0c\xa3'+struct.pack('<I',data+i*4))
        cursor += len(raw)
    code.extend(b'\xbe'+struct.pack('<I',pool)+b'\xbf'+struct.pack('<I',CAPACITY))
    loop=len(code)
    code.extend(b'\x6a\x01\x8b\xce')
    call(0xB8CE80)  # thiscall constructor, ret 4
    code.extend(b'\x81\xc6'+struct.pack('<I',STRIDE)+b'\x4f\x0f\x85')
    code.extend(struct.pack('<i',loop-len(code)-4))
    trap = entry + len(code); code.append(0xcc)
    call(0x50C1A0)
    code.extend(b'\xff\x35'+struct.pack('<I',data+64)+b'\x6a\x01')
    call(0x4A9260)
    code.extend(b'\x83\xc4\x08')
    # AS_GetItemList uses 6B71B0(0), a separate selected-character cache.
    # A profile/DDL cache alone leaves this zero: character-specific list
    # builders then return without constructing their GFx array.
    code.extend(b'\x6a\x01')
    call(0x6B71E0)
    code.extend(b'\x83\xc4\x04\x33\xc0')
    call(0x6B71B0)
    code.extend(b'\xa3'+struct.pack('<I',data+68))
    after=entry+len(code);code.append(0xcc)
    code.extend(bytes.fromhex('8d65e0 61 8be5 5d c3'))
    return {'code': bytes(code), 'data': bytes(payload), 'trap': trap,'after':after}


def capture(read, data: int, folder: Path, pool: int = INVENTORY) -> dict:
    from codol_rebuild.profiles.schema import capture_schema
    folder.mkdir(parents=True, exist_ok=True)
    pointers = struct.unpack('<8I',read(data,32))
    pages={}
    def string(address):
        if not address: return ''
        raw=bytearray()
        for i in range(4096):
            at=address+i;page=at&~4095
            if page not in pages:pages[page]=read(page,4096)
            c=pages[page][at-page:at-page+1]
            if c==b'\0': return raw.decode('utf-8',errors='replace')
            raw.extend(c)
        raise ValueError('Unterminated asset string')
    tables={}
    for name,pointer in zip(TABLES,pointers):
        actual,cols,rows,cells=struct.unpack('<4I',read(pointer,16))
        if string(actual).lower()!=name.lower() or not 0<cols<=128 or not 0<rows<=30000:
            raise ValueError(f'Unexpected table: {name}')
        raw=read(cells,cols*rows*8); cache={0:''}
        values=[]
        for i in range(cols*rows):
            p=struct.unpack_from('<I',raw,i*8)[0]
            if p not in cache:cache[p]=string(p)
            values.append(cache[p])
        tables[name]=[values[i*cols:(i+1)*cols] for i in range(rows)]
    schemas={}
    for name,pointer in zip(DEFINITIONS,pointers[len(TABLES):]):
        definition=struct.unpack('<I',read(pointer+8,4))[0]
        label=Path(name).stem
        schemas[label]=capture_schema(read,definition,folder/label)
    count=struct.unpack('<I',read(0x7795840,4))[0]
    records=struct.unpack('<I',read(0x779583C,4))[0]
    if not 0<count<65536:raise ValueError('Invalid catalog size')
    salt=struct.unpack('<H',read(0xE5737C,2))[0]
    raw=read(records,count*40);catalog=[]
    for i in range(count):
        row=raw[i*40:(i+1)*40];n=struct.unpack_from('<H',row)[0] ^ ((records+i*40)&65535) ^ salt
        item_id=((n<<4)|(n>>12))&65535
        catalog.append({'id':item_id,'name':string(struct.unpack_from('<I',row,8)[0]),
                        'type_bytes':list(row[4:8]),'raw':row.hex(),'words':list(struct.unpack('<10I',row))})
    result={'tables':tables,'catalog':catalog,'schemas':schemas,
            'inventory_first':read(pool,STRIDE).hex(),
            'public_record':read(PUBLIC,144).hex(),'secure_record':read(SECURE,144).hex()}
    (folder/'capture.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result


def populate_inventory(read, write, pool, catalog, weapon_xp):
    items=[r for r in catalog if r['id'] not in (0,15400,20000)]
    if not 0<len(items)<=CAPACITY or len({r['id'] for r in items})!=len(items):
        raise ValueError('Invalid local entitlement count or duplicate item ID')
    blob=bytearray(read(pool,CAPACITY*STRIDE))
    for i in range(CAPACITY):
        if struct.unpack_from('<I',blob,i*STRIDE)[0]!=0xD59C78 or any(blob[i*STRIDE+0x40:i*STRIDE+0x4C]):
            raise ValueError('Inventory pool is not an empty native-constructed pool')
    for i,item in enumerate(items):
        struct.pack_into('<III',blob,i*STRIDE+0x40,item['id'],1,weapon_xp if item['type_bytes'][1]==0 else 0)
    write(pool,bytes(blob))
    if read(pool,len(blob))!=blob:raise ValueError('Inventory readback mismatch')
    return {'items':len(items),'capacity':CAPACITY,'pool':hex(pool),'weapon_experience':weapon_xp}
