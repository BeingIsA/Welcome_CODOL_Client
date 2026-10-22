"""1649370 联机桥：原生异步任务、主线程完成投递和服务端身份显示映射。

HTTP 不在暂停点等待；帧陷阱只提交或消费已完成结果。字符串 playerId 仅在
业务端使用，原生64位显示标识由其稳定摘要映射，不用作服务端认证。
"""
import hashlib
import struct
from codol_rebuild.application.multiplayer.contracts import MultiplayerError, MultiplayerPort, PERMISSION_ERROR_CODES
from codol_rebuild.adapters.game.v1649370.player_name import native_name, LANGUAGE, DIRECT_STRING_MODE

ROOM_POINTER = 0x82E13FC
POLL, ACTION, ROOM, SESSION, LEAVE_ARG, LOGIN, IDENTITY = 0, 4, 8, 12, 16, 20, 24
UNDEFINED, MESSAGE = 64, 128


def display_message(error) -> str:
    """权限类失败统一显示为缺少文件，不向用户暴露封禁等权限细节。"""
    if isinstance(error, MultiplayerError) and error.code in PERMISSION_ERROR_CODES:
        return '缺少文件'
    return str(error)


def u32(read, address):
    """按固定 ABI 读取一个小端整数。"""
    return struct.unpack('<I', read(address, 4))[0]


def checked_write(process, address, value):
    """仅由调试暂停点写入并回读通信区。"""
    process.write(address, value)
    if process.read(address, len(value)) != value:
        raise ValueError('联机桥状态回读失败')


def room_options(read, room):
    """读取原生已深拷贝配置；maxPlayer 在 +0xE，password 在 +0x18。

来源：5CF6FC/5CF702 的容量写入、5CF5E6 的密码写入、637AB0 配置拷贝。
"""
    maximum = read(room+0x105FE, 1)[0]
    pointer = u32(read, room+0x10608)
    if not 1 <= maximum <= 255 or pointer < 0x10000:
        raise ValueError('原生房间配置未就绪')
    raw = bytearray()
    for index in range(257):
        value = read(pointer+index, 1)
        if value == b'\0':
            return maximum, raw.decode('utf-8') or None
        raw.extend(value)
    raise ValueError('房间密码超出原生字符串上限')


def username(read):
    """沿用昵称显示编码，转为业务接口使用的 Unicode。"""
    language = u32(read, LANGUAGE)
    if language < 0x10000:
        raise ValueError('昵称语言信息未就绪')
    encoding = 'utf-8' if u32(read, DIRECT_STRING_MODE) == 1 or u32(read, language+12) == 0 else 'gbk'
    return native_name(read).decode(encoding)


def validate_contract(read):
    """新增调用边的固定字节验证；不依赖旧版本地址。"""
    for address, expected in {
        0x7CB7A0: bytes.fromhex('558bec83e4f883ec24'),
        0x730280: bytes.fromhex('558bec83ec4ca198bb770c'),
        0x5CF5E6: bytes.fromhex('8b4d085083c118'),
        0x5CF6FC: bytes.fromhex('8a45f08b4d0888410e88410f'),
    }.items():
        if read(address, len(expected)) != expected:
            raise ValueError(f'联机桥原生 ABI 不匹配：{address:#x}')


def build_tick(entry, state, ready, preview_entry, room_entries):
    """包装既有前端帧；网络等待时立即返回，仅就绪结果进入原生回调。"""
    code, labels, refs, traps = bytearray(), {}, [], {}
    def emit(value): code.extend(bytes.fromhex(value))
    def word(value): code.extend(struct.pack('<I', value))
    def label(name): labels[name] = len(code)
    def branch(op, name):
        emit(op); refs.append((len(code), name)); word(0)
    def call(address):
        emit('e8'); word((address-entry-len(code)-4) & 0xffffffff)
    def trap(name):
        emit('8be5'); traps[entry+len(code)] = name; emit('cc')
    emit('8d6424f4'); call(preview_entry); emit('8d64240c 9c60 8bec')
    emit('833d'); word(ready); emit('01'); branch('0f85', 'done')
    emit('833d'); word(state+POLL); emit('00'); branch('0f84', 'done')
    # 按前端帧节流，避免每帧跨进程暂停。开始请求时重置计数。
    emit('ff05'); word(state+36)
    emit('833d'); word(state+36); emit('06'); branch('0f82', 'done')
    emit('c705'); word(state+36); word(0)
    trap('poll')
    emit('a1'); word(state+ACTION)
    for value, target in ((1, 'created'), (2, 'create_failed'), (3, 'left'), (4, 'leave_failed')):
        emit('83f8'+f'{value:02x}'); branch('0f84', target)
    branch('e9', 'done')
    label('created')
    emit('8b1d'); word(state+ROOM); emit('8b3d'); word(state+SESSION)
    emit('83e4f0'); call(room_entries['create_complete'])
    branch('e9', 'completed')
    label('create_failed')
    emit('83e4f0 83ec08 6a00 ff35'); word(state+SESSION); call(0x633FE0)
    branch('e9', 'completed')
    label('left')
    emit('83e4f0 83ec0c ff35'); word(state+LEAVE_ARG); call(room_entries['leave_complete'])
    emit('83c404 83ec0c 6a00 b9'); word(0xE69CB0); call(0x7CB7A0)
    branch('e9', 'completed')
    label('leave_failed')
    # 仅完成离房任务的失败响应，不能调用会清空界面的成功通知。
    emit('83e4f0 68'); word(state+UNDEFINED)
    emit('68'); word(state+MESSAGE); emit('6a00 6a64'); call(0x730280)
    label('completed'); trap('completed')
    label('done'); emit('8be5 619d c3')
    for offset, target in refs:
        struct.pack_into('<i', code, offset, labels[target]-offset-4)
    return {'code': bytes(code), 'traps': traps}


class MultiplayerBridge:
    """版本适配只拥有原生请求快照，通过联机端口触发业务。"""
    def __init__(self, process, port: MultiplayerPort, record):
        self.process, self.port, self.record = process, port, record
        self.state = None
        self.traps = {}
        self.pending = None
        self.login_started = False
        self.login_done = False
        # 失效后丢弃迟到完成结果的窗口，重新鉴权成功后关闭。
        self.discard_results = False

    def allocate(self):
        """在已暂停的初始化边分配独立通信页。"""
        validate_contract(self.process.read)
        self.state = self.process.allocate(4096)
        data = bytearray(4096)
        struct.pack_into('<I', data, POLL, 1)
        struct.pack_into('<IIII', data, UNDEFINED, 0xffffffff, 0xffffffff, 0, 1)
        checked_write(self.process, self.state, bytes(data))

    def install_tick(self, ready, preview_entry, room_plan):
        """安装主线程完成调度；返回替换原预览 CALL 的新入口。"""
        entry = self.process.allocate(4096)
        plan = build_tick(entry, self.state, ready, preview_entry, room_plan['entries'])
        if len(plan['code']) > 4096:
            raise ValueError('联机帧桥超出代码页')
        checked_write(self.process, entry, plan['code'])
        self.process.executable(entry, 4096)
        self.traps = {**room_plan['traps'], **plan['traps']}
        return entry

    def _word(self, offset, value):
        checked_write(self.process, self.state+offset, struct.pack('<I', value))

    def _valid_room(self, room, session):
        read = self.process.read
        return (room >= 0x10000 and session >= 0x10000
                and u32(read, ROOM_POINTER) == room and u32(read, room+0x1C) == session
                and u32(read, session+0x25C8) == room and read(session, 1) != b'\0')

    def _failure(self, operation, message):
        # 游戏 GFx 接口接收 UTF-8 文本；密码、机器码不进入错误日志。
        raw = message.encode('utf-8')[:510].decode('utf-8', errors='ignore').encode('utf-8') + b'\0'
        checked_write(self.process, self.state+MESSAGE, raw)
        self._word(ACTION, 2 if operation == 'create' else 4)
        self.record({'multiplayer': operation, 'success': False, 'error': message})

    def handle(self, event, address):
        """仅处理已登记主线程陷阱；不等待后台 future。"""
        if event.tid != self.process.main_thread_id:
            raise ValueError('联机原生回调必须在主线程执行')
        thread, ctx = self.process.context(event.tid)
        try:
            phase = self.traps[address]
            frame = struct.unpack_from('<I', ctx, 196)[0]
            if phase in ('create', 'leave'):
                self._request(phase, frame)
            elif phase == 'poll':
                self._poll()
            else:
                if self.pending:
                    operation, room, session = self.pending
                    action = u32(self.process.read, self.state+ACTION)
                    if action == 1 and not self._valid_room(room, session):
                        raise ValueError('服务端建房成功，但原生创建未完成')
                    if action == 3 and u32(self.process.read, room+0x1C) != 0:
                        raise ValueError('服务端离房成功，但原生房间未清理')
                    self.record({'multiplayer_native_completed': operation, 'success': action in (1, 3)})
                self.pending = None
                self._word(ACTION, 0)
                # 重新鉴权还在后台进行时保持轮询，登录完成后由 _poll 清零。
                self._word(POLL, 1 if self.login_started and not self.login_done else 0)
            struct.pack_into('<I', ctx, 184, address+1)
            self.process.set_context(thread, ctx)
        finally:
            self.process.close_thread(thread)

    def _request(self, operation, frame):
        if self.pending is not None:
            return  # 既有原生等待任务保持不变，重复点击不提交新请求。
        read = self.process.read
        room = u32(read, frame+16) if operation == 'create' else u32(read, ROOM_POINTER)
        session = u32(read, frame) if operation == 'create' else u32(read, room+0x1C)
        if not self._valid_room(room, session):
            raise ValueError('原生房间请求的双向引用失效')
        self.pending = (operation, room, session)
        self._word(ROOM, room)
        self._word(SESSION, session)
        self._word(POLL, 1)
        self._word(36, 0)
        if operation == 'leave':
            self._word(LEAVE_ARG, u32(read, frame+40))
        try:
            if not self.login_done and not self.login_started:
                raise ValueError('服务端登录尚未成功')
            maximum, password = room_options(read, room) if operation == 'create' else (4, None)
            if not self.port.begin(operation, username(read), max_players=maximum, password=password):
                raise ValueError('已有服务端请求正在处理，请稍后重试')
        except (OSError, ValueError) as error:
            self._failure(operation, str(error))

    def _drop_pending(self):
        """丢弃仍在等待的原生请求结果，进入失效后丢弃窗口。"""
        self.pending = None
        self.discard_results = True

    def _invalidate_login(self):
        """实时身份被服务端拒绝：清除原生登录标志，弹出统一提示并重新鉴权。"""
        self.login_done = False
        self.login_started = False
        self._drop_pending()
        self._word(LOGIN, 0)
        self._failure('login', '缺少文件')

    def _poll(self):
        if self.login_done and self.port.invalidated():
            self._invalidate_login()
        if not self.login_started:
            self.login_started = self.port.begin('login', username(self.process.read))
        if self.pending and not self._valid_room(*self.pending[1:]):
            # 不向回收后的对象投递迟到回调；宿主关闭时通过业务端口清理服务端房间。
            raise ValueError('等待服务端响应期间原生房间已改变')
        result = self.port.poll()
        if result is None:
            return
        if result.operation == 'login':
            if result.error:
                if self.pending is not None:
                    self._drop_pending()
                self._failure('login', display_message(result.error))
                # 不自动反复重登；用户再次操作时随请求重新鉴权。
                self.login_started = False
                return
            identity = hashlib.sha256(result.data['playerId'].encode('utf-8')).digest()[:8]
            if identity == bytes(8):
                identity = b'\x01' + bytes(7)
            checked_write(self.process, self.state+IDENTITY, identity)
            self.login_done = True
            self.discard_results = False
            self._word(LOGIN, 1)
            if self.pending is None:
                self._word(POLL, 0)
            self.record({'multiplayer': 'login', 'success': True})
        elif self.discard_results:
            # 失效窗口内迟到的创建/退出结果只记录，不再投递原生回调。
            self.record({'multiplayer_discarded': result.operation, 'success': result.error is None})
        elif self.pending is None or result.operation != self.pending[0]:
            raise ValueError('联机完成结果没有匹配的原生请求')
        elif result.error:
            self._failure(result.operation, display_message(result.error))
        else:
            self._word(ACTION, 1 if result.operation == 'create' else 3)
            self.record({'multiplayer': result.operation, 'success': True,
                         **({'roomId': result.data['roomId']} if result.operation == 'create' else {})})
