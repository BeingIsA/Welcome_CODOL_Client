"""房间成员菜单局部补丁及主线程桥；业务请求通过注入端口提交。

房间内复用原本隐藏的小队踢人项，房间外保留原行为。GetRoomInfo 的
两参数形式仅供本补丁查询标签和权限；原生零参数形式不变。
"""
import hashlib
import struct

GENERATE_AT = 4788556
# 仅保存标签补丁覆盖的 40 字节前缀，不复制原生菜单选择和筛选循环。
GENERATE_PREFIX_BEFORE = bytes.fromhex(
    'f103f043d030208054d7ef01640043ef01650143ef01660245ef01670352ef01680453ef01690554')
CHECK_AT = 4789379
CHECK_BEFORE = bytes.fromhex('f103f0a701d030ef016500a701f0a901d0d146620196762a761206000029d0467400762a761207000029d046750096762a761207000029d046760096762a761207000029d0d14673017648')


def _method(parts, capacity):
    """解析固定指令及相对跳转；保持原方法体长度和跳转落点边界。"""
    code, labels, refs = bytearray(), {}, []
    for part in parts:
        if isinstance(part, tuple):
            opcode, target = part
            code.append(opcode)
            refs.append((len(code), target))
            code.extend(bytes(3))
        elif part.endswith(':'):
            labels[part[:-1]] = len(code)
        else:
            code.extend(bytes.fromhex(part))
    for at, target in refs:
        code[at:at+3] = (labels[target]-at-3).to_bytes(3, 'little', signed=True)
    if len(code) > capacity:
        raise ValueError('成员菜单方法超出原始容量')
    return bytes(code).ljust(capacity, b'\x02')


def replacements():
    """保留三个菜单集合及逐项检查；仅在房内切换小队项的标签和权限。"""
    # 仅用方法开头的调试信息空间设置标签；从偏移 40 起逐字保留原生代码。
    # 原来的菜单选择、UserObj 转换、enableChecker 循环及跳转位置均不改变。
    label = _method([
        'd066242c06 d0467500', (0x12, 'party'),
        '606b240220466d02', (0x10, 'setlabel'),
        'party:', '601c6621', 'setlabel:', '615b'], 34)
    generate = bytes.fromhex('d030') + label + bytes.fromhex('208054d7')
    check = _method([
        'd030 d0467500', (0x12, 'party'),
        '606b2400d1667d466d0226ac48',
        'party:', 'd0d1466201', (0x11, 'no'), 'd0467400', (0x12, 'no'),
        'd0467600', (0x11, 'no'), 'd0d14673017648', 'no:', '2748'], len(CHECK_BEFORE))
    return [(GENERATE_AT, GENERATE_PREFIX_BEFORE, generate),
            (GENERATE_AT-6, b'\x03', b'\x05'),
            (CHECK_AT, CHECK_BEFORE, check), (CHECK_AT-5, b'\x02', b'\x03')]


def _word(read, address):
    return struct.unpack('<I', read(address, 4))[0]


def _string_value(read, value):
    """读取原生 GFx UTF-8 玩家显示 ID，兼容托管字符串。"""
    kind = _word(read, value+12)
    if kind not in (6, 0x46):
        raise ValueError('踢人目标不是字符串')
    pointer = _word(read, value+16)
    if kind & 0x40:
        pointer = _word(read, pointer)
    data = bytearray()
    for index in range(32):
        item = read(pointer+index, 1)
        if item == b'\0':
            return data.decode('ascii')
        data.extend(item)
    raise ValueError('踢人目标显示 ID 过长')


class RoomMemberMenu:
    """只在暂停的原生主线程读取参数；通过显式回调取得房间状态。"""
    def __init__(self, process, port, state, record):
        self.process, self.port, self.state, self.record = process, port, state, record
        self.busy = False
        self.plans = {}

    def install(self):
        """校验两个原生分发槽；普通房间查询及房间外小队操作走原处理器。"""
        self.buffer = self.process.allocate(4096)
        self.process.write(self.buffer, bytes(4096))
        self.process.write(self.buffer+32, '踢出房间'.encode('utf-8')+b'\0')
        traps = {}
        for name, slot, handler, expected in (
            ('menu_query', 0xDDD7AC, 0x7CFF50, '558bec83e4f8'),
            ('menu_kick', 0xDCD2A0, 0x6A4370, '558bec8b4508')):
            if (self.process.read(slot, 4) != struct.pack('<I', handler)
                    or self.process.read(handler, 6) != bytes.fromhex(expected)):
                raise ValueError('房间菜单原生 ABI 不匹配')
            entry = self.process.allocate(4096)
            code = bytearray.fromhex('9c60 8bec cc 619d c20400 619d e9')
            code.extend(struct.pack('<I', (handler-entry-len(code)-4) & 0xffffffff))
            self.process.write(entry, bytes(code))
            self.process.executable(entry, 4096)
            self.process.write(slot, struct.pack('<I', entry))
            self.plans[name] = (entry+5, entry+10)
            traps[entry+4] = name
        if self.process.read(0x730920, 6) != bytes.fromhex('558bec83e4f8'):
            raise ValueError('原生通知 ABI 不匹配')
        publisher = self.process.allocate(4096)
        # 通知消费在前端帧执行，调用原生 cppShowNotification 包装函数。
        code = bytearray.fromhex('9c60 8bec 833d')
        code.extend(struct.pack('<I', self.buffer)); code.extend(bytes.fromhex('00 7420 c705'))
        code.extend(struct.pack('<II', self.buffer, 0))
        code.extend(bytes.fromhex('83e4f0 b9')); code.extend(struct.pack('<I', self.buffer+128))
        code.append(0xe8); code.extend(struct.pack('<I', (0x730920-publisher-len(code)-4)&0xffffffff))
        code.extend(bytes.fromhex('8be5 619d c3'))
        # 短跳转指向统一恢复路径。
        code[12] = len(code)-5-13
        self.process.write(publisher, bytes(code)); self.process.executable(publisher, 4096)
        return traps, publisher

    def _target(self, display_id):
        """将原生显示 ID 转换为服务端玩家 ID。"""
        room, _, _, members = self.state()
        for player in members:
            identity = str(int.from_bytes(hashlib.sha256(player.encode()).digest()[:8], 'little'))
            if identity == display_id:
                return room, player
        return None

    def message(self, text):
        """排队一次原生通知，不在网络工作线程调用 UI。"""
        raw = text.encode('utf-8')[:1000].decode('utf-8', errors='ignore').encode('utf-8')+b'\0'
        self.process.write(self.buffer+128, raw)
        self.process.write(self.buffer, struct.pack('<I', 1))

    def handle(self, phase, ctx):
        """查询只读；点击仅提交一次异步命令，普通原生调用保留。"""
        read = self.process.read
        frame = struct.unpack_from('<I', ctx, 180)[0]  # 保存的 EBP 指向 PUSHAD 后栈帧。
        params = _word(read, frame+40)
        count = _word(read, params+20)
        handled, fallback = self.plans[phase]
        target = fallback
        room = self.state()[0]
        try:
            if phase == 'menu_query' and count == 2:
                args, result = _word(read, params+16), _word(read, params)
                if _word(read, result+12) not in (0, 1):
                    raise ValueError('菜单查询返回值已托管')
                if _word(read, args+12) not in (3, 4):
                    raise ValueError('菜单查询类型不正确')
                mode = _word(read, args+16)
                if mode == 2:
                    self.process.write(result+12, struct.pack('<II', 6, self.buffer+32))
                elif mode == 0:
                    selected = self._target(_string_value(read, args+32))
                    _, owner, me, _ = self.state()
                    allowed = bool(room and owner and selected and selected[1] != me)
                    self.process.write(result+12, struct.pack('<II', 2, int(allowed)))
                else:
                    raise ValueError('未知成员菜单查询')
                target = handled
            elif phase == 'menu_kick' and room:
                target = handled
                if count != 1:
                    raise ValueError('踢人参数数量不符')
                if self.busy:
                    struct.pack_into('<I', ctx, 184, handled)
                    return
                selected = self._target(_string_value(read, _word(read, params+16)))
                if selected is None:
                    raise ValueError('无法解析踢人目标的玩家 ID')
                self.busy = self.port.begin_kick(*selected)
                if not self.busy:
                    self.message('当前有请求正在处理，请稍后重试')
        except (ValueError, OSError) as error:
            self.message(str(error))
            target = handled
        struct.pack_into('<I', ctx, 184, target)

    def poll(self):
        """由会话桥在主线程消费结果，成功快照交回既有成员发布路径。"""
        if not self.busy:
            return None
        result = self.port.poll_kick()
        if result is None:
            return None
        self.busy = False
        if result.operation == 'kick_cancelled':
            return None
        self.record({'room_kick': result.error is None})
        if result.error:
            self.message(str(result.error))
            return None
        return result.data
