"""固定版本原生档案：捕获初始化输入，生成档案桥并发布库存与余额。

Build-specific local profile bridge. No networking or official identity service.

Only the debugger host owns process access. This adapter owns addresses/ABI;
profile policy and persistence belong to independent modules.
"""
from __future__ import annotations
import json
import struct
from pathlib import Path
from codol_rebuild.adapters.game.v1649370 import layout

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
SKU_SALE_OFFSET = 4096 + 65536
INITIALIZER_DATA_SIZE = SKU_SALE_OFFSET + 65536


def install_upload_checksum_order(read, write):
    """仅调整上传专用入口：原生整理/解码后计算CRC，再走原压缩分包。

    重进时正文可能仍为编码态；先计算CRC会让随后整理出的明文与头部失配。
    保留EDI参数及整理函数ret4约定，不修改公共函数或原生上传状态分支。
    """
    address = 0x7F6EE2
    original = bytes.fromhex('e889fbffff57e8f30e0000')
    replacement = bytes.fromhex('57e8f80e0000e883fbffff')
    if read(address, len(original)) != original:
        raise ValueError('Native profile upload order mismatch')
    write(address, replacement)
    if read(address, len(replacement)) != replacement:
        raise ValueError('Native profile upload order readback mismatch')


def publish_consumption(read, write, inventory, item_id, next_state, commit):
    """发布已规划的单件消耗，沿既有库存槽约定保存数量，提交失败恢复原槽。"""
    pool_address = int(inventory['pool'], 16)
    for item_index in range(inventory['items']):
        record_address = pool_address + item_index * STRIDE + 0x40
        if struct.unpack('<I', read(record_address, 4))[0] == item_id:
            break
    else:
        raise ValueError('库存中找不到消耗物品')
    remaining_count = (next_state.get('quantities', {}).get(str(item_id), 1)
                       if item_id in next_state['owned_ids'] else 0)
    previous_record = read(record_address, 12)
    next_record = (struct.pack('<II', item_id, remaining_count) + previous_record[8:]
                   if remaining_count else bytes(12))
    try:
        write(record_address, next_record)
        return commit()
    except Exception:
        write(record_address, previous_record)
        raise


def validate_account_native_contract(read):
    """核对商城 filter=1 分派与 GP/CP 类型、步长、金额偏移，版本不符时拒绝启动。"""
    for address, expected in (
        (0x5E679B, '8bc6e85e63220059c3'),
        (0x5E67F4, '9b675e00'),
        (0x5E92D0, 'b878408e0c803801'),
        (0x5E9350, '803802'),
        (0x5E968B, 'c1e2058b927c408e0ce952fcffff'),
        (0x5E9699, 'c1e1058b897c408e0ce9bffcffff'),
        (0x80D7AD, 'e84e36e5ff'),
        (0x80D7E8, '8d5606663356066633156473e50066c1c204'),
        (0x80D2C7, '66c1c0040fb7c08945f0894614'),
        (0x5DF38C, '8b855cffffff'),
    ):
        raw = bytes.fromhex(expected)
        if read(address, len(raw)) != raw:
            raise ValueError(f'Account native contract mismatch at {address:#x}')


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

def publish_empty_local_store(read, write, inventory_count):
    """Complete local empty offer lists before the first UI activation.

    5CCA30 (IsInitialDataFetchPerformed) requires bits 1|2|4. Inventory
    completion 4A9260 supplies bit 1; 4A7E20 supplies 6 for the offer lists.
    These are fetch-completion bits, not login/authentication readiness.
    Only a fresh offline session with empty offer arrays is supported here.
    """
    def u32(address):
        return struct.unpack('<I', read(address, 4))[0]
    if inventory_count < 0 or u32(0x82C4A8C) != inventory_count or read(0x82C4A86, 1) != b'\x01':
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
    payload = bytearray(INITIALIZER_DATA_SIZE)
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
    # 按原生普通商城筛选器判定售卖资格；使用原生商品时间、SKU 和隐藏名单。
    # 每个 uint16 物品 ID 占一个字节，结果不依赖随后发放的本地库存。
    code.extend(bytes.fromhex('8b1d40587907 8b153c587907 85db'))
    empty_jump = len(code); code.extend(b'\x0f\x84\0\0\0\0')
    code.extend(bytes.fromhex('81fb00000100'))
    oversized_jump = len(code); code.extend(b'\x0f\x83\0\0\0\0')
    market_loop = len(code)
    code.extend(bytes.fromhex('60 0fb732 6633f2 6633357c73e500 66c1c604 81e6ffff0000 bf01000000'))
    call(0x5E66F0)
    code.extend(b'\x88\x86' + struct.pack('<I', data + 4096))
    code.extend(bytes.fromhex('61 83c228 4b 0f85'))
    code.extend(struct.pack('<i', market_loop - len(code) - 4))
    # 原生 SKU 判定保留价格、售卖标记、服务器时间与动态 SKU 的语义。
    # 单独按 SKU 保存结果，不能把其他币种的有效期用于 GP/CP 购买项。
    code.extend(bytes.fromhex('bb01000000'))
    sku_loop = len(code)
    code.extend(bytes.fromhex('60 8bc3'))
    call(0x80D770)
    code.extend(b'\x88\x83' + struct.pack('<I', data + SKU_SALE_OFFSET))
    code.extend(bytes.fromhex('61 43 81fb00000100 0f85'))
    code.extend(struct.pack('<i', sku_loop - len(code) - 4))
    code.extend(b'\xc7\x05' + struct.pack('<II', data + 80, 1))
    for position in (empty_jump, oversized_jump):
        struct.pack_into('<i', code, position + 2, len(code) - position - 6)
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
    # 原生加载完成约定：缓存和库存就绪后，将公共明文档案转为运行时编码态。
    # 7F7CD0 以 EAX 接收公共档案对象；沿用原生转换，不直接改编码标志。
    code.extend(b'\xb8' + struct.pack('<I', PUBLIC))
    call(0x7F7CD0)
    after=entry+len(code);code.append(0xcc)
    code.extend(bytes.fromhex('8d65e0 61 8be5 5d c3'))
    return {'code': bytes(code), 'data': bytes(payload), 'trap': trap,'after':after}


def capture(read, data: int, folder: Path, pool: int = INVENTORY) -> dict:
    from codol_rebuild.adapters.game.profile_format.schema import capture_schema
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
        schemas[label]=capture_schema(read,definition,folder/label,max_profile_size=layout.BUFFER_CAPACITY)
    count=struct.unpack('<I',read(0x7795840,4))[0]
    records=struct.unpack('<I',read(0x779583C,4))[0]
    if not 0<count<65536:raise ValueError('Invalid catalog size')
    if read(data + 80, 4) != struct.pack('<I', 1):
        raise ValueError('Native marketplace classification did not complete')
    salt=struct.unpack('<H',read(0xE5737C,2))[0]
    raw=read(records,count*40);catalog=[]
    market_flags = read(data + 4096, 65536)
    for i in range(count):
        row=raw[i*40:(i+1)*40];n=struct.unpack_from('<H',row)[0] ^ ((records+i*40)&65535) ^ salt
        item_id=((n<<4)|(n>>12))&65535
        catalog.append({'id':item_id,'name':string(struct.unpack_from('<I',row,8)[0]),
                        'market_sale': bool(market_flags[item_id]),
                        'type_bytes':list(row[4:8]),'raw':row.hex(),'words':list(struct.unpack('<10I',row))})
    result={'tables':tables,'catalog':catalog,'schemas':schemas,
            'inventory_first':read(pool,STRIDE).hex(),
            'public_record':read(PUBLIC,144).hex(),'secure_record':read(SECURE,144).hex()}
    (folder/'capture.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result


def populate_inventory(read, write, pool, catalog, weapon_xp, owned_ids=None, quantities=None):
    items=[r for r in catalog if r['id'] not in (0,15400,20000)]
    if owned_ids is not None:
        requested = set(owned_ids)
        if len(requested) != len(owned_ids) or not requested <= {r['id'] for r in items}:
            raise ValueError('Saved entitlement absent from current catalog')
        items = [r for r in items if r['id'] in requested]
    if not 0<=len(items)<=CAPACITY or len({r['id'] for r in items})!=len(items):
        raise ValueError('Invalid local entitlement count or duplicate item ID')
    blob=bytearray(read(pool,CAPACITY*STRIDE))
    for i in range(CAPACITY):
        if struct.unpack_from('<I',blob,i*STRIDE)[0]!=0xD59C78 or any(blob[i*STRIDE+0x40:i*STRIDE+0x4C]):
            raise ValueError('Inventory pool is not an empty native-constructed pool')
    for i,item in enumerate(items):
        struct.pack_into('<III',blob,i*STRIDE+0x40,item['id'],(quantities or {}).get(str(item['id']), 1),weapon_xp if item['type_bytes'][1]==0 else 0)
    write(pool,bytes(blob))
    if read(pool,len(blob))!=blob:raise ValueError('Inventory readback mismatch')
    return {'items':len(items),'capacity':CAPACITY,'pool':hex(pool),'weapon_experience':weapon_xp}


def currency_snapshot(read):
    """原生账户 UI：类型1为 codPoint（GP），类型2为 moneyPoint（CP）。"""
    raw = read(0xC8E4078, 11 * 32)
    result = {}
    for offset in range(0, len(raw), 32):
        kind = raw[offset]
        if kind in (1, 2):
            key = 'gp' if kind == 1 else 'cp'
            if key in result:
                raise ValueError('Duplicate native currency record')
            result[key] = struct.unpack_from('<i', raw, offset + 4)[0]
    if set(result) != {'gp', 'cp'} or any(v < 0 for v in result.values()):
        raise ValueError('Invalid native currency records')
    return result


def publish_currency(read, write, balances):
    """只写已有原生余额槽的类型与金额，保留槽中构造好的对象指针。"""
    raw = read(0xC8E4078, 11 * 32)
    slots = {}
    free = []
    for offset in range(0, len(raw), 32):
        kind = raw[offset]
        if kind in (1, 2):
            if kind in slots:
                raise ValueError('Duplicate native currency record')
            slots[kind] = offset
        elif kind == 0:
            free.append(offset)
    writes = []
    for kind, key in ((1, 'gp'), (2, 'cp')):
        value = balances[key]
        if type(value) is not int or not 0 <= value <= 2147483647:
            raise ValueError('Invalid initial currency balance')
        if kind not in slots:
            if not free:
                raise ValueError('Native currency slots exhausted')
            slots[kind] = free.pop(0)
        address = 0xC8E4078 + slots[kind]
        writes.extend(((address, bytes([kind])), (address + 4, struct.pack('<i', value))))
    for address, value in writes:
        write(address, value)
        if read(address, len(value)) != value:
            raise ValueError('Native currency readback mismatch')
    return currency_snapshot(read)


def install_maximum_weapon_experience(process):
    """仅覆盖物品查询输出的武器经验；保留拥有、数量、期限和原生等级表。"""
    query_address = 0x5E3760
    original_bytes = bytes.fromhex('55 8b ec 8b 0d d4 13 91 0c')
    if process.read(query_address, len(original_bytes)) != original_bytes:
        raise ValueError('Native item query entry mismatch')
    adapter_address = process.allocate(4096)
    trampoline_address = adapter_address + 512
    trampoline = original_bytes + b'\xe9' + struct.pack('<i', query_address + len(original_bytes) - trampoline_address - len(original_bytes) - 5)
    # 原入口以 EAX 接收物品 ID、EDI 接收结果指针，无栈参数。
    code = bytearray(b'\x50')
    def call_native(target_address):
        code.extend(b'\xe8' + struct.pack('<i', target_address - adapter_address - len(code) - 5))
    call_native(trampoline_address)
    code.extend(bytes.fromhex('9c 60 83 3d 10 13 2e 08 00'))
    branches = []
    def skip_if(opcode):
        code.extend(opcode)
        branches.append(len(code))
        code.extend(bytes(4))
    skip_if(b'\x0f\x84')
    code.extend(bytes.fromhex('8b 44 24 24'))
    call_native(0x660C90)
    code.extend(bytes.fromhex('85 c0'))
    skip_if(b'\x0f\x84')
    code.extend(bytes.fromhex('80 78 05 00'))
    skip_if(b'\x0f\x85')
    # 与原生武器等级读取一致：分类字段 +6 小于6使用主武器表。
    code.extend(bytes.fromhex('0f b6 40 06 83 f8 06 0f 9c c3 0f b6 db 53'))
    call_native(0x4740D0)
    code.extend(bytes.fromhex('83 c4 04 53 50'))
    call_native(0x474130)
    code.extend(bytes.fromhex('83 c4 08 89 47 1c'))
    finish_offset = len(code)
    code.extend(bytes.fromhex('61 9d 8d 64 24 04 c3'))
    for branch_offset in branches:
        struct.pack_into('<i', code, branch_offset, finish_offset - branch_offset - 4)
    process.write(adapter_address, bytes(code))
    process.write(trampoline_address, trampoline)
    if process.read(adapter_address, len(code)) != bytes(code) or process.read(trampoline_address, len(trampoline)) != trampoline:
        raise ValueError('Weapon experience adapter readback mismatch')
    process.executable(adapter_address, 4096)
    # 只替换入口五字节；跳板执行被覆盖指令的完整九字节后返回。
    patch = b'\xe9' + struct.pack('<i', adapter_address - query_address - 5)
    process.write(query_address, patch)
    if process.read(query_address, len(patch)) != patch:
        raise ValueError('Weapon experience entry readback mismatch')
    return {'entry': hex(adapter_address), 'query': hex(query_address), 'field': 'weapon_experience'}
