"""1649370 商品购买桥，复用原生购买条件和商城任务完成接口。"""
import math
import struct
from codol_rebuild.application.errors import OnlineError

HANDLER_SLOT = 0xDDD428
HANDLER = 0x7CD430
REFRESH_SLOT = 0xDDD464
REFRESH_HANDLER = 0x7CD630
INFO = 32
MESSAGE = 128
# 位于请求缓冲（前 512 字节）之外，0 空闲、1 等待服务器、2 等待原生完成通知。
ONLINE_TASK_STATE = 512


def u32(read, address):
    return struct.unpack('<I', read(address, 4))[0]


def checked_write(read, write, address, value):
    write(address, value)
    if read(address, len(value)) != value:
        raise ValueError('原生购买数据回读失败')


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
        raise ValueError('每次购买仅支持一份商品')
    return sku, quantity


def set_failure(read, write, state, message):
    checked_write(read, write, state + 16, bytes(4))
    raw = str(message).encode('utf-8')[:360]
    raw = raw.decode('utf-8', errors='ignore').encode('utf-8') + b'\0'
    checked_write(read, write, state + MESSAGE, raw)


def handle_phase(process, phase, state, frame, profiles, inventory_session, storage_errors):
    """主线程只提交一次 SKU，按确认的库存快照完成购买，后续刷新只读。"""
    try:
        if phase == 'request':
            payload = bytearray(512)
            struct.pack_into('<IIII', payload, 96, 0xffffffff, 0xffffffff, 0, 1)
            checked_write(process.read, process.write, state, bytes(payload))
            if inventory_session is None:
                raise ValueError('离线模式无法购买商品')
            sku_id, quantity = request(process.read, frame)
            checked_write(process.read, process.write, state, struct.pack('<III', 1, sku_id, quantity))
        elif phase == 'eligible':
            if any(u32(process.read, state + offset) != 1 for offset in (20, 24, 28)):
                raise ValueError('商品已下架或未满足购买条件')
            inventory_session.begin_purchase(u32(process.read, state + 4))
            checked_write(process.read, process.write, state + ONLINE_TASK_STATE, struct.pack('<I', 1))
        elif phase == 'poll':
            completion = inventory_session.poll('purchase')
            if completion is not None:
                profiles.apply_inventory(completion['inventory'])
                checked_write(process.read, process.write, state + 16, struct.pack('<I', 1))
                checked_write(process.read, process.write, state + ONLINE_TASK_STATE, struct.pack('<I', 2))
        elif phase in ('refreshed', 'inventory_updated'):
            return {'purchase_inventory_refreshed': profiles.inventory_count}
    except (*storage_errors, OnlineError) as error:
        set_failure(process.read, process.write, state, error)
        if inventory_session is not None:
            checked_write(process.read, process.write, state + ONLINE_TASK_STATE, struct.pack('<I', 2))
        return {'purchase_error': str(error)}
    return {}


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
            raise ValueError(f'原生购买 ABI 不匹配：{address:#x}')


def build_adapter(entry, state, ready, *, deferred=False):
    """保留原生购买条件和双任务通知，线上等待结果期间返回前端帧。"""
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
    if deferred:
        # 同一类型的原生任务只能挂起一次，重复 GFx 调用不能覆盖其请求缓冲。
        emit('833d'); word(state + ONLINE_TASK_STATE); emit('00'); branch('0f85', 'request_return')
    trap('request')
    emit('833d'); word(state); emit('01'); branch('0f85', 'request_return' if deferred else 'complete')
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
    trap('eligible')
    if deferred:
        labels['request_return'] = len(code)
        emit('8be5 619d c20400')
        labels['poll'] = len(code)
        emit('9c60 8bec 833d'); word(state + ONLINE_TASK_STATE); emit('00'); branch('0f84', 'poll_done')
        emit('833d'); word(state + ONLINE_TASK_STATE); emit('01'); branch('0f85', 'publish_result')
        trap('poll')
        emit('833d'); word(state + ONLINE_TASK_STATE); emit('01'); branch('0f84', 'poll_done')
        labels['publish_result'] = len(code)
    emit('833d'); word(state+16); emit('01'); branch('0f85', 'complete')
    emit('83e4f0'); call(0x5E9720)
    call(0x5E9250)  # 向 MY_ACCOUNT 发布服务端已确认的余额，触发余额组件重绑。
    call(0x5E72B0)  # 原生预览列表立即重新生成并通知当前商品界面。
    trap('refreshed')
    labels['complete'] = len(code)
    # Undefined GFx Value is an explicit, initialized 32-byte object at state+96.
    emit('83e4f0 68'); word(state+96)
    emit('68'); word(state+MESSAGE)
    emit('ff35'); word(state+16)
    emit('6865040000'); call(0x730280)
    if deferred:
        emit('c705'); word(state + ONLINE_TASK_STATE); word(0)
        labels['poll_done'] = len(code)
        emit('8be5 619d c3')
    else:
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
            'refresh_after': struct.pack('<I', refresh_entry),
            'poll': entry + labels['poll'] if deferred else None}


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
