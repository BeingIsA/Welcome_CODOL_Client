"""1649370 商品购买桥；复用原生报价、条件和任务完成接口。"""
import math
import struct
from codol_rebuild.adapters.game.v1649370.accounts.profile import CAPACITY, STRIDE, currency_snapshot, publish_currency

HANDLER_SLOT = 0xDDD428
HANDLER = 0x7CD430
REFRESH_SLOT = 0xDDD464
REFRESH_HANDLER = 0x7CD630
INFO = 32
MESSAGE = 128


def u32(read, address):
    return struct.unpack('<I', read(address, 4))[0]


def checked_write(read, write, address, value):
    write(address, value)
    if read(address, len(value)) != value:
        raise ValueError('本地购买数据回读失败')


def request(read, frame):
    params = u32(read, frame + 40)
    if params < 0x10000 or u32(read, params + 20) != 2:
        raise ValueError('购买参数数量不符')
    args = u32(read, params + 16)
    if args < 0x10000:
        raise ValueError('购买参数指针无效')
    values = []
    for offset in (0, 32):
        kind = u32(read, args + offset + 12)
        raw = read(args + offset + 16, 8)
        if kind in (3, 4):
            value = struct.unpack('<i' if kind == 3 else '<I', raw[:4])[0]
        elif kind == 5:
            value = struct.unpack('<d', raw)[0]
            if not math.isfinite(value) or value != int(value):
                raise ValueError('购买参数不是整数')
            value = int(value)
        else:
            raise ValueError('购买参数类型不符')
        values.append(value)
    sku, quantity = values
    if not 0 < sku < 65536 or quantity != 1:
        raise ValueError('本地购买每次仅支持一份商品')
    return sku, quantity


def set_failure(read, write, state, message):
    checked_write(read, write, state + 16, bytes(4))
    raw = str(message).encode('utf-8')[:360]
    raw = raw.decode('utf-8', errors='ignore').encode('utf-8') + b'\0'
    checked_write(read, write, state + MESSAGE, raw)


def quote(read, state, catalog):
    if any(u32(read, state + offset) != 1 for offset in (20, 24, 28)):
        raise ValueError('商品已下架或未满足购买条件')
    info = read(state + INFO, 60)
    product, sku, currency, price = (struct.unpack_from('<I', info, o)[0] for o in (0, 16, 20, 24))
    if sku != u32(read, state + 4) or currency not in (1, 2):
        raise ValueError('本地购买仅支持 GP/CP 商品')
    discount, rate, original = struct.unpack_from('<iii', info, 48)
    # 5DF7C1：仅三个字段均为正时，UI 才使用折扣价格。
    if min(discount, rate, original) > 0:
        price = discount
    if not 0 < product < u32(read, 0x7797FFC):
        raise ValueError('商品索引无效')
    index = struct.unpack('<H', read(u32(read, 0x7797FF8) + product * 2, 2))[0]
    if not 0 < index < u32(read, 0x7797FEC):
        raise ValueError('商品记录无效')
    record = read(u32(read, 0x7797FE8) + index * 28, 28)
    if not record[14]:
        raise ValueError('商品内容为空')
    item_pointer = struct.unpack_from('<I', record, 16)[0]
    contents = []
    for index in range(record[14]):
        entry = read(item_pointer + index * 4, 4)
        item_id = struct.unpack_from('<H', entry)[0]
        item = catalog.get(item_id)
        if item is None:
            raise ValueError('商品物品不存在')
        raw = bytes.fromhex(item['raw'])
        stackable = bool(raw[32] & 2)
        # 原生报价 80D27B 使用产品条目的 +2 字节作为堆叠数量。
        contents.append({'item_id': item_id, 'stackable': stackable,
                         'count': entry[2] if stackable else 1})
    return {'item_id': contents[0]['item_id'], 'contents': contents,
            'sku_id': sku, 'currency': 'gp' if currency == 1 else 'cp',
            'price': price, 'equipment': not contents[0]['stackable'], 'available': True,
            'selected_duration': struct.unpack_from('<I', info, 36)[0]}


def publish_transaction(read, write, inventory, items, next_state, commit):
    """预检全部物品后暂存原生库存；任一提交失败恢复整笔交易。"""
    if isinstance(items, dict):
        items = [items]
    pool = int(inventory['pool'], 16)
    count = inventory['items']
    if not 0 < count <= CAPACITY or u32(read, 0x82C4A8C) != count:
        raise ValueError('本地库存数量不一致')
    if read(pool - 16, 16) != b'LOCAL-INVENTORY!' or read(pool + CAPACITY * STRIDE, 16) != b'LOCAL-INVENTORY!':
        raise ValueError('本地库存边界损坏')
    slots = {u32(read, pool + i * STRIDE + 0x40): pool + i * STRIDE for i in range(count)}
    empty = [pool + i * STRIDE for i in range(count)
             if not any(read(pool + i * STRIDE + 0x40, 12))]
    changes = []
    new_count = count
    for item in {item['id']: item for item in items}.values():
        address = slots.get(item['id'])
        if address is None:
            if empty:
                address = empty.pop()
            else:
                if new_count >= CAPACITY:
                    raise ValueError('本地库存已满')
                address = pool + new_count * STRIDE
                new_count += 1
            if any(read(address + 0x40, 12)):
                raise ValueError('新库存槽不是空的原生对象')
            if read(address + 0x90, 4) != b'\xff' * 4 or read(address + 0x98, 4) != b'\xff' * 4:
                raise ValueError('库存槽默认永久期限不符')
        if u32(read, address) != 0xD59C78:
            raise ValueError('库存槽原生对象类型不符')
        before = read(address + 0x40, 12)
        xp = (struct.unpack_from('<I', before, 8)[0] if item['id'] in slots
              else inventory['weapon_experience'] if item['type_bytes'][1] == 0 else 0)
        amount = next_state.get('quantities', {}).get(str(item['id']), 1)
        changes.append((address + 0x40, before, struct.pack('<III', item['id'], amount, xp)))
    balances = currency_snapshot(read)
    try:
        for address, before, after in changes:
            checked_write(read, write, address, after)
        checked_write(read, write, 0x82C4A8C, struct.pack('<I', new_count))
        publish_currency(read, write, next_state)
        committed = commit()
    except Exception:
        for address, before, after in reversed(changes):
            checked_write(read, write, address, before)
        checked_write(read, write, 0x82C4A8C, struct.pack('<I', count))
        publish_currency(read, write, balances)
        raise
    inventory['items'] = inventory['native_count'] = new_count
    return committed


def validate_contract(read):
    checks = {
        HANDLER_SLOT: struct.pack('<I', HANDLER),
        REFRESH_SLOT: struct.pack('<I', REFRESH_HANDLER),
        REFRESH_HANDLER: bytes.fromhex('e86bbacdffb801000000'),
        HANDLER: bytes.fromhex('558bec8b45088b40108b4810518b4830'),
        0x80D180: bytes.fromhex('558bec83ec105357'),
        0x80D770: bytes.fromhex('5156576685c0'),
        0x730280: bytes.fromhex('558bec83ec4ca198bb770c'),
        0x730404: bytes.fromhex('8b45148b480c80e18f80f901'),
        0x5E9720: bytes.fromhex('558bec83ec3c535657'),
        0x5E9250: bytes.fromhex('558bec83e4f883ec44'),
        0x5E9656: bytes.fromhex('6803040000e8206c1400'),
        0x5E72B0: bytes.fromhex('558bec83e4f8b8f4140000'),
        0x4A6D60: bytes.fromhex('558bec83ec10538b5d0856578bf8'),
    }
    for address, expected in checks.items():
        if read(address, len(expected)) != expected:
            raise ValueError(f'本地购买 ABI 不匹配：{address:#x}')


def build_adapter(entry, state, ready):
    """GFx 回调→请求陷阱→原生报价→事务陷阱→刷新库存→原生任务完成。"""
    code, labels, refs, traps = bytearray(), {}, [], {}
    def emit(value): code.extend(bytes.fromhex(value))
    def word(value): code.extend(struct.pack('<I', value))
    def branch(op, name):
        emit(op); refs.append((len(code), name)); word(0)
    def call(target):
        emit('e8'); word((target-entry-len(code)-4) & 0xffffffff)
    def trap(name):
        emit('8be5'); traps[entry+len(code)] = name; emit('cc')
    emit('9c60 8bec 833d'); word(ready); emit('01'); branch('0f85', 'original')
    emit('a1'); word(0x849B874); emit('85c0'); branch('0f84', 'original')
    emit('83780c00'); branch('0f85', 'original')
    trap('request')
    emit('833d'); word(state); emit('01'); branch('0f85', 'complete')
    emit('83e4f0 a1'); word(state+4); call(0x80D770)
    emit('0fb6c0 a3'); word(state+20)
    emit('a1'); word(state+4); emit('be'); word(state+INFO); call(0x80D180)
    emit('0fb6c0 a3'); word(state+24)
    emit('85c0'); branch('0f84', 'quoted')
    # Same unlock requirement check as 5DEF86; ignoreEntitlement skips it.
    emit('803d'); word(state+INFO+41); emit('00'); branch('0f85', 'eligible')
    emit('ff35'); word(state+INFO+8); emit('a1'); word(state+INFO+4)
    call(0x4A6D60); emit('83c404 0fb6c0 a3'); word(state+28)
    branch('e9', 'quoted')
    labels['eligible'] = len(code)
    emit('c705'); word(state+28); word(1)
    labels['quoted'] = len(code)
    trap('quoted')
    emit('833d'); word(state+16); emit('01'); branch('0f85', 'complete')
    emit('83e4f0'); call(0x5E9720)
    call(0x5E9250)  # 向 MY_ACCOUNT 发布已提交的本地余额，触发余额组件重绑。
    call(0x5E72B0)  # 原生预览列表立即重新生成并通知当前商品界面。
    trap('refreshed')
    labels['complete'] = len(code)
    # Undefined GFx Value is an explicit, initialized 32-byte object at state+96.
    emit('83e4f0 68'); word(state+96)
    emit('68'); word(state+MESSAGE)
    emit('ff35'); word(state+16)
    emit('6865040000'); call(0x730280)
    emit('8be5 619d c20400')
    labels['original'] = len(code)
    emit('8be5 619d e9'); word((HANDLER-entry-len(code)-4) & 0xffffffff)
    refresh_entry = entry + len(code)
    emit('9c60 8bec 833d'); word(ready); emit('01'); branch('0f85', 'refresh_original')
    emit('a1'); word(0x849B874); emit('85c0'); branch('0f84', 'refresh_original')
    emit('83780c00'); branch('0f85', 'refresh_original')
    # UI 的购买成功回调会再请求一次库存刷新，不能落回在线拉取流程。
    emit('83e4f0'); call(0x5E9720)
    emit('68'); word(state+96)
    emit('6891e0da00 6a01 6866040000'); call(0x730280)
    trap('inventory_updated')
    emit('8be5 619d c20400')
    labels['refresh_original'] = len(code)
    emit('8be5 619d e9'); word((REFRESH_HANDLER-entry-len(code)-4) & 0xffffffff)
    for pos, name in refs:
        struct.pack_into('<i', code, pos, labels[name]-pos-4)
    return {'code': bytes(code), 'state': state, 'traps': traps,
            'patch_va': HANDLER_SLOT, 'after': struct.pack('<I', entry),
            'refresh_after': struct.pack('<I', refresh_entry)}


def replacements():
    """恢复商城购买成功提示；复用父类原生消息框，不创建新窗口。"""
    before = bytes.fromhex('f108f0c90dd030f0cc0d47')
    # protected showPurchaseSuccess 的继承解析由 AVM2 callsupervoid 执行。
    after = bytes.fromhex('d030d04eba040047').ljust(len(before), b'\x02')
    # 复用 ABC 字符串 4（构造函数的调试名称，非类名/trait 名）保存中文。
    # 将腾出的字节留在相邻 debugfile 字符串末尾，保持整个常量池等长，
    # 不移动后续方法、不改变字符串索引。debugfile 长度 138 使用两字节 U30。
    source = b'D:\\projects\\AutoBuilder.rel2\\iw4\\game\\ui\\default\\src;codo\\controllers;MarketBaseController.as'
    debug_name = b'codo.controllers:MarketBaseController/MarketBaseController'
    text = '购买成功'.encode('utf-8')
    strings_before = bytes([len(source)]) + source + bytes([len(debug_name)]) + debug_name
    strings_after = b'\x8a\x01' + source + b' ' * 45 + bytes([len(text)]) + text
    return [(5045078, before, after),
            (4995927, strings_before, strings_after),
            # showMsg 的首参数直接 pushstring 4，不再读取缺失的 Dict 词条。
            (5001502, bytes.fromhex('5d3a663a6640'), bytes.fromhex('2c0402020202'))]
