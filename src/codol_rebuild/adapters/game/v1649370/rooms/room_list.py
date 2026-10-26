"""1649370 大厅列表适配：原生查询快照、GFx 数据转换和主线程发布。

查询调用边 7423A3 的 ECX 是已解析的模式 ID，外层 EBP+8 是大厅视图。
发布使用原有 cppSetRoomData(Array)，由原生监听器更新列表与选中详情。
HTTP 与分页归应用端口；本模块不拥有玩家身份或服务端房间规则。
"""
import struct

QUERY = 0x7423A3
HIDE = 0x73F850
SELECT = 0x740040
CANCEL = 0x5D2E60
CONTRACT = {
    QUERY: bytes.fromhex('e8182b0800'),
    HIDE: bytes.fromhex('558bec83e4f8'),
    SELECT: bytes.fromhex('558bec83e4f8'),
    CANCEL: bytes.fromhex('8b0dfc132e08'),
    0x72FDD0: bytes.fromhex('558beca198bb770c53'),
    0x72FD70: bytes.fromhex('558beca198bb770c53'),
    0x436750: bytes.fromhex('558bec518bc1'),
    0x436660: bytes.fromhex('568bf18b460c'),
    0x730570: bytes.fromhex('558bec83e4f883ec50'),
}
# 独立发布区：命令、行数、执行状态、GFx 临时对象，以及只读字段描述。
PUBLISH, COUNT, STATUS, ARRAY, OBJECT, ROWS = 0, 4, 8, 32, 64, 128
# MODE 是独立 GFx 临时对象；MODE_KEY 保存父对象属性名，逐行构造并释放。
MODE_KEY, MODE = 12, 96
CAPACITY = 2 * 1024 * 1024
FIELDS = ('id', 'index', 'name', 'password', 'currentPlayers', 'currentMaxPlayers',
          'status', 'isPlayList', 'enableShoutcast', 'map', 'mode')
STRIDE = (len(FIELDS) + 1) * 36


def _u32(read, address):
    return struct.unpack('<I', read(address, 4))[0]


def _write(process, address, data):
    process.write(address, data)
    if process.read(address, len(data)) != data:
        raise ValueError('大厅列表写入回读失败')


def validate_contract(read):
    """只允许经过静态核对的版本与调用约定。"""
    for address, expected in CONTRACT.items():
        if read(address, len(expected)) != expected:
            raise ValueError(f'大厅列表 ABI 不匹配：{address:#x}')


def encode_rows(rooms, mode, base):
    """生成独立的 GFx 字段描述；字符串仅作展示，不泄露密码。

    HTTP 全分页完成后再筛选，因此某页无匹配项不会提前终止。字符串由
    GFx SetMember 复制到虚拟机；跨刷新不保留本通信区字符串指针。
    """
    selected = [r for r in rooms if not mode or r['mode'] == mode]
    if len(selected) > 1000:
        raise ValueError('大厅列表超过1000条容量')
    data = bytearray(ROWS + len(selected) * STRIDE)
    struct.pack_into('<I', data, COUNT, len(selected))
    for offset in (ARRAY, OBJECT, MODE):
        struct.pack_into('<II', data, offset, 0xffffffff, 0xffffffff)
    strings = {}

    def string(value):
        if '\0' in value:
            raise ValueError('大厅文本不能包含空字符')
        if value not in strings:
            strings[value] = base + len(data)
            data.extend(value.encode('utf-8') + b'\0')
        return strings[value]

    struct.pack_into('<I', data, MODE_KEY, string('currentMatchMode'))
    for index, room in enumerate(selected):
        values = (room['roomId'], index, room['ownerDisplayName'],
                  '*' if room['hasPassword'] else '', room['playerCount'],
                  room['maxPlayers'], 0, False, False, room['map'], room['mode'])
        # 最后一项属于嵌套模式对象，不能把带点的属性名当成对象结构。
        descriptors = list(zip(FIELDS, values)) + [('id', room['mode'])]
        for field, (key, value) in enumerate(descriptors):
            offset = ROWS + index * STRIDE + field * 36
            kind = 6 if isinstance(value, str) else 2 if type(value) is bool else 4
            payload = string(value) if kind == 6 else int(value)
            struct.pack_into('<IIIIII', data, offset, string(key),
                             0xffffffff, 0xffffffff, 0, kind, payload)
    if len(data) > CAPACITY:
        raise ValueError('大厅列表发布缓冲区容量不足')
    return bytes(data), tuple(r['roomId'] for r in selected)


def build_adapter(entry, state):
    """生成有界循环发布桩和原生边界钩子，不改游戏磁盘镜像。"""
    code, labels, refs, traps, patches = bytearray(), {}, [], {}, []

    def emit(value): code.extend(bytes.fromhex(value))
    def word(value): code.extend(struct.pack('<I', value))
    def label(name): labels[name] = len(code)
    def branch(op, name):
        emit(op); refs.append((len(code), name)); word(0)
    def call(address):
        emit('e8'); word((address-entry-len(code)-4) & 0xffffffff)
    def mov_ecx(address): emit('b9'); word(address)
    def push(address): emit('68'); word(address)

    # 查询是 CALL 替换：保留外层 EBP、ECX、实参和调用方清栈。
    for address, phase in ((QUERY, 'query'), (HIDE, 'hide'),
                           (SELECT, 'select'), (CANCEL, 'cancel')):
        start = entry + len(code)
        traps[start] = 'room_list_' + phase
        emit('cc')
        if address == QUERY:
            emit('c3')
        else:
            code.extend(CONTRACT[address])
            emit('e9'); word((address+len(CONTRACT[address])-entry-len(code)-4) & 0xffffffff)
        original = CONTRACT[address]
        patches.append({'va': address, 'before': original,
                        'after': (b'\xe8' if address == QUERY else b'\xe9')
                        + struct.pack('<I', (start-address-5) & 0xffffffff)
                        + b'\x90' * (len(original)-5)})

    label('publish')
    emit('9c60 8bec')
    emit('833d'); word(state+PUBLISH); emit('01'); branch('0f85', 'done')
    emit('c705'); word(state+PUBLISH); word(0)
    emit('c705'); word(state+STATUS); word(1)
    # 原生包装函数自行处理 Movie 锁，返回 AL 表示成功。
    push(state+ARRAY); call(0x72FDD0); emit('83c404 84c0'); branch('0f84', 'cleanup')
    emit('8b1d'); word(state+COUNT)
    emit('be'); word(state+ROWS)
    emit('85db'); branch('0f84', 'dispatch')
    label('row')
    push(state+OBJECT); call(0x72FD70); emit('83c404 84c0'); branch('0f84', 'cleanup')
    emit('bf'); word(len(FIELDS))
    label('field')
    emit('8d4604 50 ff36'); mov_ecx(state+OBJECT); call(0x436750)
    emit('84c0'); branch('0f84', 'cleanup')
    emit('83c624 4f'); branch('0f85', 'field')
    # 创建真正的 currentMatchMode 对象，按 GFx 所有权规则挂到房间行。
    push(state+MODE); call(0x72FD70); emit('83c404 84c0'); branch('0f84', 'cleanup')
    emit('8d4604 50 ff36'); mov_ecx(state+MODE); call(0x436750)
    emit('84c0'); branch('0f84', 'cleanup')
    push(state+MODE); emit('ff35'); word(state+MODE_KEY)
    mov_ecx(state+OBJECT); call(0x436750)
    emit('84c0'); branch('0f84', 'cleanup')
    mov_ecx(state+MODE); call(0x436660)
    emit('83c624')
    # ObjectInterface::PushBack(arrayHandle, &object)，callee 清理两个实参。
    mov_ecx(state+ARRAY+8); emit('8b09 8b11')
    push(state+OBJECT); emit('ff35'); word(state+ARRAY+16); emit('ff523c')
    emit('84c0'); branch('0f84', 'cleanup')
    mov_ecx(state+OBJECT); call(0x436660)
    emit('4b'); branch('0f85', 'row')
    label('dispatch')
    emit('6a01'); push(state+ARRAY); emit('6a00'); push(0xDDCB84)
    emit('bf'); word(15); call(0x730570); emit('83c410')
    emit('c705'); word(state+STATUS); word(2)
    label('cleanup')
    mov_ecx(state+MODE); call(0x436660)
    mov_ecx(state+OBJECT); call(0x436660)
    mov_ecx(state+ARRAY); call(0x436660)
    traps[entry+len(code)] = 'room_list_published'; emit('cc')
    label('done'); emit('8be5 619d c3')
    for offset, target in refs:
        struct.pack_into('<i', code, offset, labels[target]-offset-4)
    return {'code': bytes(code), 'patches': patches, 'traps': traps,
            'publish': entry+labels['publish']}


class RoomListBridge:
    """只在游戏主线程处理视图生命期；通过应用端口获取不可变结果快照。"""
    def __init__(self, process, port, record, username, error_text):
        self.process, self.port, self.record = process, port, record
        self.username, self.error_text = username, error_text
        self.state = None
        self.traps = {}
        self.view = None
        self.mode = ''
        self.waiting = False
        self.ids = ()

    def install(self):
        """分配固定容量缓冲区；所有调用边验证完成后才安装重定向。"""
        validate_contract(self.process.read)
        self.state = self.process.allocate(CAPACITY)
        entry = self.process.allocate(4096)
        plan = build_adapter(entry, self.state)
        if len(plan['code']) > 4096:
            raise ValueError('大厅发布桩超出代码页')
        _write(self.process, self.state, bytes(ROWS))
        _write(self.process, entry, plan['code'])
        self.process.executable(entry, 4096)
        for patch in plan['patches']:
            _write(self.process, patch['va'], patch['after'])
        self.traps = plan['traps']
        return plan['publish']

    @property
    def active(self):
        """视图有查询周期时保持主线程轮询，以便消费取消的后台结果。"""
        return self.waiting or self.view is not None

    def cancel(self):
        """视图隐藏、切换或建房时撤销发布，不碰既有建房与离房任务。"""
        self.port.cancel_list()
        self.view = None
        self.ids = ()
        if self.state is not None:
            _write(self.process, self.state+PUBLISH, bytes(4))

    def handle(self, phase, ctx, logged_in):
        """解析已经核对的寄存器/栈参数；不等待网络。"""
        read = self.process.read
        if phase == 'room_list_query':
            frame = struct.unpack_from('<I', ctx, 180)[0]
            view = _u32(read, frame+8)
            pointer = struct.unpack_from('<I', ctx, 172)[0]
            raw = bytearray()
            for i in range(257):
                char = read(pointer+i, 1)
                if char == b'\0':
                    break
                raw.extend(char)
            else:
                raise ValueError('房间筛选模式过长')
            mode = raw.decode('ascii')
            # 已收齐全部 HTTP 分页；原生滚动到底部的非强制请求无需重查。
            if self.view == view and self.mode == mode and not _u32(read, frame+12):
                return
            self.view, self.mode = view, mode
            # 记录 Movie 与对象句柄，重建页面后拒绝向旧周期投递。
            self.identity = (_u32(read, 0xC77BB98), _u32(read, view))
            _write(self.process, self.state+PUBLISH, bytes(4))
            if logged_in:
                self.waiting = self.port.begin_list(self.username())
            else:
                self._publish([], '服务端登录尚未成功，请稍后刷新')
        elif phase in ('room_list_hide', 'room_list_cancel', 'room_list_select'):
            self.cancel()
        elif phase == 'room_list_published':
            ok = _u32(read, self.state+STATUS) == 2
            self.record({'room_list_published': ok, 'count': len(self.ids)})

    def _valid_view(self):
        if self.view is None:
            return False
        read, view = self.process.read, self.view
        try:
            return (self.identity == (_u32(read, 0xC77BB98), _u32(read, view))
                    and read(view+4, 1) != b'\0'
                    and _u32(read, view+0x24) == _u32(read, view+0x1C)
                    and _u32(read, view+0x174) == 0)
        except OSError:
            return False

    def poll(self):
        """完整分页成功才替换列表；取消/切页后的完成结果只释放通道。"""
        if self.view is not None and not self._valid_view():
            self.cancel()
        result = self.port.poll_list()
        if result is None:
            return
        self.waiting = False
        if result.operation == 'list_cancelled':
            return
        if self.view is None:
            return
        self._publish(result.data['rooms'] if result.error is None else [],
                      self.error_text(result.error) if result.error else None)

    def _publish(self, rooms, error=None):
        data, ids = encode_rows(rooms, self.mode, self.state)
        _write(self.process, self.state, data)
        self.ids = ids
        _write(self.process, self.state+PUBLISH, struct.pack('<I', 1))
        self.record({'room_list': 'ready', 'count': len(ids),
                     **({'error': error} if error else {})})
