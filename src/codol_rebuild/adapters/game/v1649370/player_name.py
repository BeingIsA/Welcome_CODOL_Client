"""玩家昵称的固定版本本地桥：原生setter、宿主提交握手及UI资源替换。

仅接管独立local-profile会话的GFx回调；不改共享在线服务或登录状态。
宿主处理Unicode校验与持久化，本模块处理GFx/原生字符串和机器码ABI。
先应用昵称并回读，再由宿主提交；失败调用原生setter恢复旧名后回调失败。
"""
from __future__ import annotations
import struct

HANDLER_SLOT = 0xDCCB44
HANDLER = 0x6988B0
SETTER = 0x771240
COMPLETE = 0x698750
USERNAME = 0x8269854
DW = 0x849B874
ROOM = 0x82E13FC
LANGUAGE = 0x7ACE410
DIRECT_STRING_MODE = 0x331C5C0
ERROR_STRING = 0xDCC254
EMPTY_STRING = 0xDAE091
STATE_SIZE = 136  # apply, committed, new[64], old[64]
NAME_CARD = 50001
REFRESH_INVENTORY = 0x5E9720
REFRESH_PREVIEW = 0x5E72B0


def consume_card(read, write, inventory, next_state, commit):
    """按应用层计算的剩余数量发布消费；归零才清空槽位，提交失败恢复原槽。"""
    from codol_rebuild.adapters.game.v1649370.profile import CAPACITY, STRIDE
    pool, count = int(inventory['pool'], 16), inventory['items']
    if not 0 < count <= CAPACITY or _u32(read, 0x82C4A8C) != count:
        raise ValueError('改名库存数量不一致')
    if read(pool-16, 16) != b'LOCAL-INVENTORY!' or read(pool+CAPACITY*STRIDE, 16) != b'LOCAL-INVENTORY!':
        raise ValueError('改名库存边界损坏')
    slots = [pool+i*STRIDE for i in range(count) if _u32(read, pool+i*STRIDE+0x40) == NAME_CARD]
    if len(slots) != 1 or _u32(read, slots[0]) != 0xD59C78:
        raise ValueError('改名卡库存记录不一致')
    remaining = (next_state.get('quantities', {}).get(str(NAME_CARD), 1)
                 if NAME_CARD in next_state['owned_ids'] else 0)
    address = slots[0]+0x40
    previous = read(address, 12)
    if _u32(read, slots[0]+0x44) != remaining+1:
        raise ValueError('改名卡数量与应用状态不一致')
    after = (struct.pack('<III', NAME_CARD, remaining, struct.unpack_from('<I', previous, 8)[0])
             if remaining else bytes(12))
    try:
        write(address, after)
        if read(address, 12) != after:
            raise ValueError('改名卡消费回读失败')
        return commit()
    except Exception:
        write(address, previous)
        if read(address, 12) != previous:
            raise ValueError('改名卡回滚回读失败')
        raise


def _u32(read, address):
    return struct.unpack('<I', read(address, 4))[0]


def _string(read, address, capacity=64):
    if address < 0x10000:
        raise ValueError('昵称字符串指针无效')
    value = bytearray()
    for i in range(capacity):
        byte = read(address+i, 1)
        if byte == b'\0':
            return bytes(value)
        value.extend(byte)
    raise ValueError('昵称字符串未在上限内终止')


def native_name(read):
    """只读真实dw_username，拒绝类型不符或未初始化的对象。"""
    pointer = _u32(read, USERNAME)
    if pointer < 0x10000 or read(pointer+8, 1) != b'\x07':
        raise ValueError('dw_username尚未就绪或不是字符串')
    if _string(read, _u32(read, pointer)) != b'dw_username':
        raise ValueError('dw_username对象身份不符')
    return _string(read, _u32(read, pointer+12))


def encode_name(read, value):
    """匹配6BBD70/72FB20：直接字符串模式或英语用UTF-8，其他语言用CP936。"""
    language = _u32(read, LANGUAGE)
    if language < 0x10000:
        raise ValueError('昵称显示编码尚未就绪')
    encoding = 'utf-8' if (_u32(read, DIRECT_STRING_MODE) == 1 or
                           _u32(read, language+12) == 0) else 'gbk'
    raw = value.encode(encoding, errors='strict')
    if not raw or len(raw) >= 64 or b'\0' in raw:
        raise ValueError('昵称超出原生字符串容量')
    return raw


def read_request(read, frame, ready):
    """解析原GFx参数；所有线程在宿主陷阱暂停，不读取可变后台快照。"""
    dw = _u32(read, DW)
    if _u32(read, ready) != 1 or dw < 0x10000 or _u32(read, dw+12) != 0:
        raise ValueError('本地改名仅支持已就绪的离线档案')
    room = _u32(read, ROOM)
    # 62FDC6是cmp byte ptr [ecx+38710],0，邻接字节不是该标志的一部分。
    if room < 0x10000 or _u32(read, room+0x1c) or read(room+0x38710, 1) != b'\0':
        raise ValueError('请离开房间后改名')
    params = _u32(read, frame+40)  # PUSHFD/PUSHAD后原ESP+4
    if params < 0x10000 or _u32(read, params+20) != 1:
        raise ValueError('改名GFx参数数量不符')
    args = _u32(read, params+16)
    if args < 0x10000:
        raise ValueError('改名GFx参数无效')
    kind = _u32(read, args+12)
    if kind not in (6, 0x46):
        raise ValueError('改名GFx参数不是UTF-8字符串')
    pointer = _u32(read, args+16)
    if kind & 0x40:
        if pointer < 0x10000:
            raise ValueError('改名GFx托管字符串指针无效')
        pointer = _u32(read, pointer)
    return _string(read, pointer).decode('utf-8', errors='strict')


def stage_name(read, write, state, value):
    """发布一次独立请求，保留旧名用于数据库拒绝或setter失败时回滚。"""
    raw, previous = encode_name(read, value), native_name(read)
    payload = struct.pack('<II', 1, 0)+raw.ljust(64, b'\0')+previous.ljust(64, b'\0')
    write(state, payload)
    if read(state, STATE_SIZE) != payload:
        raise ValueError('昵称请求发布回读失败')


def verify_applied(read, state, *, rollback=False):
    """必须匹配原生setter后的真实值，不能仅依赖宿主写入的结果标志。"""
    expected = read(state+(72 if rollback else 8), 64).split(b'\0', 1)[0]
    if native_name(read) != expected:
        raise ValueError('昵称原生回读失败'+('（回滚）' if rollback else ''))


def validate_contract(read):
    """验证GFx槽、setter、任务完成函数及字符串显示分支。"""
    checks = {
        HANDLER_SLOT: struct.pack('<I', HANDLER),
        HANDLER: bytes.fromhex('558bec83e4f881eccc000000'),
        SETTER: bytes.fromhex('558bec81ec14040000'),
        0x771253: bytes.fromhex('807e08078b45087521'),
        COMPLETE: bytes.fromhex('558bec83e4f883ec44'),
        0x698878: bytes.fromhex('51526820050000e8fc790900'),
        0x6BBDE0: bytes.fromhex('a15498260885c0'),
        0x72FB48: bytes.fromhex('8b0d10e4ac07'),
        0x62FDC0: bytes.fromhex('83791c00750c80b91087030000'),
        ERROR_STRING: b'@SF_UI_REDEEM_NAME_CHANGE_UNKNOW_ERROR\0',
        REFRESH_INVENTORY: bytes.fromhex('558bec83ec3c535657'),
        # 原生缓存构建跳过 ID 为 0 的空槽。
        0x5E9741: bytes.fromhex('8b4ff46685c90f84cd000000'),
        REFRESH_PREVIEW: bytes.fromhex('558bec83e4f8b8f4140000'),
    }
    for address, expected in checks.items():
        if read(address, len(expected)) != expected:
            raise ValueError(f'玩家昵称ABI不匹配：{address:#x}')


def build_adapter(entry, state, ready, initializer, *, deferred=False):
    """GFx独立回调和启动初始化包装；陷阱只交接数据，不调用在线改名。"""
    for address in (entry, state, ready, initializer):
        if type(address) is not int or not 0x10000 <= address <= 0xffffe000:
            raise ValueError('昵称适配地址必须为有效32位地址')
    code, labels, refs, traps = bytearray(), {}, [], {}
    def emit(value): code.extend(bytes.fromhex(value))
    def word(value): code.extend(struct.pack('<I', value))
    def label(name): labels[name] = len(code)
    def branch(op, name):
        emit(op); refs.append((len(code), name)); word(0)
    def native(op, target):
        emit(op); word((target-entry-len(code)-4) & 0xffffffff)
    def trap(name):
        emit('8be5'); traps[entry+len(code)] = name; emit('cc')
    def setter(offset):
        emit('83e4f0 83ec0c 8b35'); word(USERNAME)
        emit('68'); word(state+offset); native('e8', SETTER)
        emit('8be5')
    label('request')
    emit('9c60 8bec 833d'); word(ready); emit('01'); branch('0f85', 'original')
    emit('a1'); word(DW); emit('85c0'); branch('0f84', 'original')
    emit('83780c00'); branch('0f85', 'original')
    for offset in (0, 4):
        emit('c705'); word(state+offset); word(0)
    trap('request')
    if deferred:
        # HTTP 等待期间直接返回；后续前端帧消费结果，不能暂停游戏等网络。
        emit('8be5 619d c20400')
        label('poll')
        emit('9c60 8bec')
        trap('poll')
        emit('833d'); word(state+136); emit('00'); branch('0f84', 'poll_done')
        emit('833d'); word(state+136); emit('02'); branch('0f84', 'failure')
    emit('833d'); word(state); emit('01'); branch('0f85', 'failure')
    setter(8); trap('applied')
    emit('833d'); word(state+4); emit('01'); branch('0f84', 'success')
    setter(72); trap('rolled_back')
    label('failure')
    emit('83e4f0 83ec04 6a00 68'); word(state+144 if deferred else ERROR_STRING); emit('6a00')
    branch('e9', 'complete')
    label('success')
    emit('83e4f0'); native('e8', REFRESH_INVENTORY); native('e8', REFRESH_PREVIEW)
    emit('83e4f0 83ec04 6a00 68'); word(EMPTY_STRING); emit('6a01')
    label('complete')
    if deferred:
        emit('833d'); word(state+140); emit('00'); branch('0f84', 'poll_completed')
    native('e8', COMPLETE)
    if deferred:
        label('poll_completed')
        for offset in (136, 140):
            emit('c705'); word(state+offset); word(0)
        label('poll_done')
        emit('8be5 619d c3')
    else:
        emit('8be5 619d c20400')
    label('original'); emit('8be5 619d'); native('e9', HANDLER)
    label('initialize')
    emit('9c60 8bec 83e4f0'); native('e8', initializer)
    emit('833d'); word(state); emit('01'); branch('0f85', 'initialized')
    setter(8); trap('restored')
    label('initialized'); emit('8be5 619d c3')
    for position, name in refs:
        struct.pack_into('<i', code, position, labels[name]-position-4)
    return {'code': bytes(code), 'traps': traps, 'state': state, 'ready': ready,
            'initializer': entry+labels['initialize'], 'patch_va': HANDLER_SLOT,
            'before': struct.pack('<I', HANDLER), 'after': struct.pack('<I', entry),
            **({'poll': entry+labels['poll']} if deferred else {})}


class ServerNameBridge:
    """联机改名的主线程适配：应用确认后沿原生 setter 和任务完成路径发布。"""

    def __init__(self, process, profiles, session, adapter, storage_errors, report_error):
        self.process, self.profiles, self.session = process, profiles, session
        self.adapter, self.storage_errors = adapter, storage_errors
        self.report_error = report_error
        self.completion = None
        self.waiting_ui = False

    def _write(self, offset, value):
        """只在已验证的主线程暂停点写入通信区并回读。"""
        address = self.adapter['state'] + offset
        self.process.write(address, value)
        if self.process.read(address, len(value)) != value:
            raise ValueError('联机改名通信区回读失败')

    def _failure(self, error, notify):
        """复用原生失败任务；后台恢复失败使用已有通知能力。"""
        message = str(error).encode('utf-8')[:511].decode('utf-8', errors='ignore').encode('utf-8')
        self._write(144, message.ljust(512, b'\0'))
        self._write(136, struct.pack('<II', 2, int(notify)))
        if not notify:
            self.report_error(str(error))

    def handle(self, phase, frame, row):
        """处理请求、逐帧轮询、原生应用及回滚，不同步执行网络调用。"""
        state = self.adapter['state']
        if phase == 'request':
            try:
                value = read_request(self.process.read, frame, self.adapter['ready'])
                # 继续使用原有客户端规则及原生编码容量检查；服务端不复验格式。
                self.profiles.validate_name(value)
                encode_name(self.process.read, value)
                self.profiles.check_name_card()
                self.session.begin(value)
                self.waiting_ui = True
            except self.storage_errors as error:
                self._failure(error, True)
                row['server_rename_error'] = str(error)
        elif phase == 'poll':
            completion = self.session.poll()
            if completion is None:
                return
            self.completion = completion
            notify = completion.notify and self.waiting_ui
            self.waiting_ui = False
            if completion.error is not None:
                self._failure(completion.error, notify)
                row['server_rename_error'] = str(completion.error)
                return
            try:
                stage_name(self.process.read, self.process.write, state, completion.display_name)
                self._write(136, struct.pack('<II', 1, int(notify)))
                # setter 或本地提交失败时的默认反馈。
                self._write(144, '改名未能完成。'.encode('utf-8').ljust(512, b'\0'))
            except self.storage_errors as error:
                self._failure(error, notify)
                row['server_rename_error'] = str(error)
        elif phase == 'applied':
            try:
                verify_applied(self.process.read, state)
                if not self.completion.notify:
                    self.profiles.synchronize_name(self.completion.display_name)
                else:
                    self.profiles.rename(self.completion.display_name)
                self._write(4, struct.pack('<I', 1))
                self.session.complete()
                row['server_rename_saved'] = self.profiles.name_snapshot
            except self.storage_errors as error:
                self._write(4, bytes(4))
                message = str(error).encode('utf-8')[:511].decode('utf-8', errors='ignore').encode('utf-8')
                self._write(144, message.ljust(512, b'\0'))
                if not _u32(self.process.read, state+140):
                    self.report_error(str(error))
                row['server_rename_error'] = str(error)
        elif phase == 'rolled_back':
            verify_applied(self.process.read, state, rollback=True)
            row['server_rename_rolled_back'] = True
        else:
            raise ValueError('未知联机改名阶段')

MARKET_OFFSET = 0x4CCBCC
METHOD_LENGTH = 178
# 方法内偏移 84..94：findpropstrict/getproperty CFunctionContainer + callproperty IsTCLSLogOn + convert_b。
TCLS_CHECK_OFFSET = MARKET_OFFSET + 84
TCLS_CHECK_BEFORE = bytes.fromhex('5dbd0166bd0146f70100' '76')
TCLS_CHECK_AFTER = bytes.fromhex('26' + '02' * 10)

COMPLETE_OFFSET = 0x4CCC87
COMPLETE_BEFORE = bytes.fromhex(
    'f108f09804d030ef01e701009804f09a04d046c4010029f09b04d166fc015dfd0166fd0166fe0114650000'
    'f09d045dbd0166bd0146ff010029f09e04d04680020029f0a004d05da40166a4016681025da60166a60166a701'
    '2400d06682025da40166a40166830246a3010529f0a1045d9d01669d012cca02469e010129f0a204d05d3e663e'
    '664f46aa01012910390000f0a604d166c101762a761209000029d166c10166840276121e0000f0a8045ef001'
    '2668f001f0a9045d85025dd00166d0016686024685020129f0ac0447')
# 成功后返回原页面，广播昵称及库存变化，让详情页重取改名卡数量和购买状态。
# 使用现有 BCEvent 与 BaseController.broadcast；失败分支保持原样。
_SUCCESS = bytes.fromhex('d04680020029 d02cca0246aa010129 '
                        'd0603e664946aa010129 '
                        'd02cce02240146a301022947')
COMPLETE_AFTER = COMPLETE_BEFORE[:43]+_SUCCESS.ljust(101, b'\x02')+COMPLETE_BEFORE[144:]
MESSAGE_OFFSET = 0x4C721D
MESSAGE_BEFORE = (b'\x39Refresh Inventory after redeem change name card complete.'
                  b'\x14operationInterrupted\x0eclosePopupPage\x0bCHANGE_NAME'
                  b'\x44codo.controllers:MarketController/private:onRedeemChangeNameComplete')
# 复用日志文字（字符串330）与仅用于诊断的方法显示名（334），不改变字符串索引、
# trait名称或方法标识。中间三个运行时属性名保持原样；字符串区总长度不变。
_REFRESH_EVENT = b'evt_on_career_info_updated'
_SUCCESS_MESSAGE = '改名成功。'.encode('utf-8')
_MESSAGE_CAPACITY = 57 + 68 - len(_REFRESH_EVENT)
MESSAGE_AFTER = (bytes([len(_REFRESH_EVENT)]) + _REFRESH_EVENT
                 + b'\x14operationInterrupted\x0eclosePopupPage\x0bCHANGE_NAME'
                 + bytes([_MESSAGE_CAPACITY]) + _SUCCESS_MESSAGE.ljust(_MESSAGE_CAPACITY, b' '))

PLAYER_OFFSET = 0x114EF7
PLAYER_BEFORE = bytes.fromhex('f104f019d030f01b5d0166012014080000f01d5d0246020029f0225d01660166035d0466044605006106f0245d01660148')
# 保留首次构造和WarGod刷新；每次getObj同步昵称，复用原ABC的DataManager和DataID。
# 用getlex减少字节与栈占用；max_stack=2、locals=1及scope深度均保持不变。
PLAYER_AFTER = bytes.fromhex('d030 60012014050000 5d024f0200 '
                             '6001660360044605006106 '
                             '6023460f0060246625462601 60012b6127 600148').ljust(len(PLAYER_BEFORE), b'\x02')


def replacements() -> list:
    """仅供local-profile主电影安装：本地入口、成功提示及昵称缓存同步。"""
    if len(TCLS_CHECK_BEFORE) != len(TCLS_CHECK_AFTER):
        raise ValueError('TCLS 检查替换长度不一致')
    return [(TCLS_CHECK_OFFSET, TCLS_CHECK_BEFORE, TCLS_CHECK_AFTER),
            (COMPLETE_OFFSET, COMPLETE_BEFORE, COMPLETE_AFTER),
            (MESSAGE_OFFSET, MESSAGE_BEFORE, MESSAGE_AFTER),
            (PLAYER_OFFSET, PLAYER_BEFORE, PLAYER_AFTER)]
