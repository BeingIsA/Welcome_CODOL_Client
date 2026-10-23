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
# 房间设置提交方法本体（房间管理器 vtable+0x7C，构造 0x62C000 写 vtable
# 0xDC7274 得出）。所有配置变更——建房、规则设置页应用、单项改地图/模式
# 及管理器内部路径——最终都经此处把新配置拷入管理器 +0x105F0，拦入口
# 一处即可覆盖全部调用方（0x6300BD/0x6300C6 的两次 637AB0 拷贝之前）。
SETTINGS_SUBMIT = 0x6300A0


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
    """读取原生已深拷贝配置：maxPlayer、password 与完整对局设置。

    配置基址为 room+0x105F0 的 0x70 结构；来源见 637AB0 拷贝构造与
    5CF530 UI 填装（字段映射见 research/room-config-native-findings.md）。
    """
    maximum = read(room+0x105FE, 1)[0]
    if not 1 <= maximum <= 255:
        raise ValueError('原生房间配置未就绪')
    password = native_string(read, u32(read, room+0x10608))
    settings = read_settings(read, room+0x105F0)
    return maximum, (password or None), settings


# 标量字段：传输键 →（配置偏移, struct 格式）。与 0x5CF530 填装偏移一致。
SETTING_SCALARS = {
    'weaponRestrictPreset': (0x0C, '<B'),
    'difficulty': (0x14, '<B'),
    'modeTimeLimit': (0x30, '<f'),
    'modeScoreLimit': (0x34, '<H'),
    'modeRoundLimit': (0x36, '<B'),
    'modeWinLimit': (0x37, '<B'),
    'modeRoundSwitch': (0x38, '<B'),
    'modeNumLives': (0x39, '<B'),
    'modePlayerRespawnDelay': (0x3C, '<f'),
    'modeWaveRespawnDelay': (0x40, '<f'),
    'modeBombTimer': (0x44, '<f'),
    'modePlantTime': (0x48, '<f'),
    'modeDefuseTime': (0x4C, '<f'),
    'playerMaxHealth': (0x50, '<H'),
    'playerHealthRegenTime': (0x52, '<H'),
    'playerHealthRegenRate': (0x54, '<f'),
    'teamFFType': (0x58, '<B'),
    'gameSpectateType': (0x59, '<B'),
    'gameAllowKillCam': (0x5A, '<B'),
    'gameEnableHardcore': (0x5B, '<B'),
    'gameForceUAV': (0x5C, '<B'),
    'gameOnlyHeadshots': (0x5D, '<B'),
    'restrictWpnsAtStart': (0x5E, '<B'),
    'combatHighHostileActionEnd': (0x5F, '<B'),
    'botsAddAll': (0x60, '<H'),
    'clashMode': (0x62, '<B'),
    'clashFOV': (0x63, '<B'),
    'gameEnableBuddyMode': (0x64, '<B'),
    'isEndless': (0x65, '<B'),
    'gameForbidSpawnBulletWeapon': (0x66, '<B'),
    'gameForbidSpawnGrenade': (0x67, '<B'),
    'classRestriction': (0x68, '<B'),
    'weaponRestrictMask': (0x6C, '<I'),
}
# 字符串字段：传输键 → 指针偏移（指向单字节串，空指针视为空串）。
SETTING_STRINGS = {'mode': 0x00, 'map': 0x08}


def native_string(read, pointer):
    """读取原生单字节字符串；UTF-8 失败回退 GBK，上限 256 字节。"""
    if pointer < 0x10000:
        return ''
    raw = bytearray()
    for index in range(257):
        value = read(pointer+index, 1)
        if value == b'\0':
            for encoding in ('utf-8', 'gbk'):
                try:
                    return raw.decode(encoding)
                except UnicodeDecodeError:
                    continue
            return raw.decode('utf-8', errors='replace')
        raw.extend(value)
    raise ValueError('原生字符串超出上限')


def read_settings(read, config):
    """把 0x70 配置结构读成对局设置字典（camelCase 传输键）。"""
    if config < 0x10000:
        raise ValueError('原生房间配置未就绪')
    settings = {}
    for key, offset in SETTING_STRINGS.items():
        settings[key] = native_string(read, u32(read, config+offset))
    for key, (offset, form) in SETTING_SCALARS.items():
        settings[key] = struct.unpack_from(form, read(config+offset, struct.calcsize(form)))[0]
        if form == '<f':
            settings[key] = round(settings[key], 6)
    return settings


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
        SETTINGS_SUBMIT: bytes.fromhex('558bec83ec10'),
    }.items():
        if read(address, len(expected)) != expected:
            raise ValueError(f'联机桥原生 ABI 不匹配：{address:#x}')


def build_settings_detours(entry):
    """生成设置提交方法（0x6300A0）入口的重定向桩与补丁计划。

    被替换的 6 字节是原函数序幕（push ebp; mov ebp,esp; sub esp,0x10）。
    入口必须用 E9 JMP 重定向而不是 CALL：JMP 不压返回地址，桩入口的
    栈与原方法入口完全一致（[esp]=调用者返回地址、[esp+4]=新配置指针），
    否则整个方法在 esp 偏移 4 的栈上运行，ret 4 会吞掉调用者返回地址
    （2026-10-23 实机崩溃教训）。桩先以 int3 陷阱上报，再执行原序幕
    并跳回 0x6300A6 继续原方法，原生行为保持不变。
    """
    code, traps, patches = bytearray(), {}, []
    stub = 0
    traps[entry+stub] = 'settings_submit'
    code.append(0xCC)                       # 陷阱：读取 [esp+4] 的配置指针
    code.extend(bytes.fromhex('558bec83ec10'))  # 原序幕 6 字节
    code.extend(b'\xe9')                    # jmp 0x6300A6（相对跳回）
    code.extend(struct.pack('<i', (SETTINGS_SUBMIT+6)-(entry+len(code))-4))
    before = bytes.fromhex('558bec83ec10')
    after = b'\xe9' + struct.pack('<I', (entry+stub-SETTINGS_SUBMIT-5) & 0xffffffff)
    after += b'\x90'
    patches.append({'va': SETTINGS_SUBMIT, 'before': before, 'after': after})
    return {'code': bytes(code), 'patches': patches, 'traps': traps}


def build_tick(entry, state, ready, preview_entry, room_entries, list_publish=None):
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
    if list_publish is not None:
        emit('83e4f0'); call(list_publish); emit('8be5')
    emit('a1'); word(state+ACTION)
    actions = [(1, 'created'), (2, 'create_failed'), (3, 'left'), (4, 'leave_failed')]
    if 'join_complete' in room_entries:
        actions += [(5, 'joined'), (6, 'join_failed')]
    for value, target in actions:
        emit('83f8'+f'{value:02x}'); branch('0f84', target)
    branch('e9', 'done')
    if 'join_complete' in room_entries:
        label('joined')
        emit('83e4f0'); call(room_entries['join_complete'])
        branch('e9', 'completed')
        label('join_failed')
        emit('83e4f0'); call(room_entries['join_failed'])
        branch('e9', 'completed')
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
        # 已提交但尚未被 _poll 消费的设置补交；主流程收尾时据此保持轮询，
        # 否则设置结果会被搁浅、通道永久占用。
        self._settings_outstanding = False
        # 失效后丢弃迟到完成结果的窗口，重新鉴权成功后关闭。
        self.discard_results = False
        self.room_list = None
        self.join_snapshot = None
        self.join_scratch = None
        self.ownership = None
        self.join_cleanup = False
        self.join_cancelled = False
        self.player_id = None
        self.is_owner = True

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
        from codol_rebuild.adapters.game.v1649370.room_list import RoomListBridge
        self.room_list = RoomListBridge(self.process, self.port, self.record,
                                       lambda: username(self.process.read), display_message)
        publisher = self.room_list.install()
        from codol_rebuild.adapters.game.v1649370.room import build_join_adapter, validate_join_contract
        validate_join_contract(self.process.read)
        self.ownership = room_plan['ownership']
        self.join_scratch = self.process.allocate(4096)
        join_entry = self.process.allocate(4096)
        join_plan = build_join_adapter(join_entry, self.state, self.join_scratch)
        checked_write(self.process, join_entry, join_plan['code'])
        self.process.executable(join_entry, 4096)
        for patch in join_plan['patches']:
            checked_write(self.process, patch['va'], patch['after'])
        room_plan = {**room_plan, 'entries': {**room_plan['entries'], **join_plan['entries']}}
        plan = build_tick(entry, self.state, ready, preview_entry, room_plan['entries'], publisher)
        if len(plan['code']) > 4096:
            raise ValueError('联机帧桥超出代码页')
        checked_write(self.process, entry, plan['code'])
        self.process.executable(entry, 4096)
        self.traps = {**room_plan['traps'], **plan['traps'], **self.room_list.traps, **join_plan['traps']}
        return entry

    def install_settings_detours(self):
        """安装房内改设置两处提交调用的重定向桩；原生行为不变。"""
        validate_contract(self.process.read)
        entry = self.process.allocate(4096)
        plan = build_settings_detours(entry)
        if len(plan['code']) > 4096:
            raise ValueError('设置同步桩超出代码页')
        checked_write(self.process, entry, plan['code'])
        self.process.executable(entry, 4096)
        for patch in plan['patches']:
            if self.process.read(patch['va'], len(patch['before'])) != patch['before']:
                raise ValueError(f'设置调用边字节与预期不符：{patch["va"]:#x}')
            checked_write(self.process, patch['va'], patch['after'])
        self.traps = {**self.traps, **plan['traps']}
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
        self._word(ACTION, 6 if operation == 'join' else 2 if operation == 'create' else 4)
        self.record({'multiplayer': operation, 'success': False, 'error': message})

    def handle(self, event, address):
        """仅处理已登记主线程陷阱；不等待后台 future。"""
        if event.tid != self.process.main_thread_id:
            raise ValueError('联机原生回调必须在主线程执行')
        thread, ctx = self.process.context(event.tid)
        try:
            phase = self.traps[address]
            frame = struct.unpack_from('<I', ctx, 196)[0]
            if phase == 'join_input':
                self._request_join(frame)
                # 被替换 CALL 的返回地址及 strtoull 的三个实参均由此清除。
                struct.pack_into('<I', ctx, 196, frame+16)
                struct.pack_into('<I', ctx, 184, 0x5D3232)
                self.process.set_context(thread, ctx)
                return
            elif phase == 'join_allocated':
                room = u32(self.process.read, self.state+ROOM)
                session = u32(self.process.read, self.state+SESSION)
                checked_write(self.process, self.ownership, struct.pack('<II', room, session))
                self.pending = ('join', room, session)
                self._initialize_join_room()
            elif phase == 'join_snapshot':
                self._publish_join_snapshot()
            elif phase == 'join_allocation_failed':
                self._failure('join', '原生房间会话不可用，请重试')
                self.join_cleanup = self.port.begin('leave', username(self.process.read))
            elif phase.startswith('room_list_'):
                if phase == 'room_list_cancel' and self.pending and self.pending[0] == 'join':
                    self.join_cancelled = True
                self.room_list.handle(phase, ctx, self.login_done)
                if self.room_list.active:
                    self._word(POLL, 1)
                    self._word(36, 0)
            elif phase in ('create', 'leave'):
                self._request(phase, frame)
            elif phase == 'settings_submit':
                # JMP 重定向后桩入口栈与原方法一致：[esp]=调用者返回、[esp+4]=配置。
                self._submit_settings(u32(self.process.read, frame+4))
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
                    if action == 5 and not self._valid_room(room, session):
                        raise ValueError('服务端入房成功，但原生房间未完成')
                    self.record({'multiplayer_native_completed': operation, 'success': action in (1, 3, 5)})
                self.pending = None
                self._word(ACTION, 0)
                # 重新鉴权或设置补交仍在等待时保持轮询，两者都由 _poll 清零；
                # 否则设置结果会被主流程收尾搁浅，通道被永久占用。
                self._word(POLL, 1 if (self.login_started and not self.login_done)
                           or self._settings_outstanding or self._listing() or self.join_cleanup else 0)
            struct.pack_into('<I', ctx, 184, address+1)
            self.process.set_context(thread, ctx)
        finally:
            self.process.close_thread(thread)

    def _request_join(self, frame):
        """在原生数字转换前复制字符串参数；不保留 GFx 临时指针。"""
        if self.pending is not None or self.join_cleanup:
            return
        read = self.process.read
        room = u32(read, ROOM_POINTER)
        if room < 0x10000 or u32(read, room+0x1C):
            return
        self.pending = ('join', room, 0)
        self.join_cancelled = False
        self._word(ROOM, room)
        self._word(SESSION, 0)
        self._word(POLL, 1)
        self._word(36, 0)
        try:
            if not self.login_done:
                raise ValueError('服务端登录尚未成功')
            room_id = native_string(read, u32(read, frame+4))
            password = native_string(read, u32(read, frame+28)) or None
            if self.room_list is not None:
                self.room_list.cancel()
            if not self.port.begin('join', username(read), room_id=room_id, password=password):
                raise ValueError('已有服务端请求正在处理，请稍后重试')
        except (ValueError, OSError) as error:
            self._failure('join', str(error))

    def _prepare_join_snapshot(self, data):
        """先校验整个快照与原生容量，再允许主线程创建会话。"""
        import math
        from codol_rebuild.adapters.game.v1649370.room import MEMBER_CAPACITY
        read = self.process.read
        room = self.pending[1]
        if u32(read, ROOM_POINTER) != room or u32(read, room+0x1C):
            raise ValueError('等待入房期间房间对象已改变')
        members = data['members']
        if not 1 <= len(members) <= MEMBER_CAPACITY:
            raise ValueError(f'当前原生客户端最多支持 {MEMBER_CAPACITY} 个成员')
        if not any(m['playerId'] == self.player_id for m in members):
            raise ValueError('入房快照缺少本机玩家')
        # 本机槽排第一，以复用原生档案与头像身份映射；房主由独立 owner ID 决定。
        members = sorted(members, key=lambda m: m['playerId'] != self.player_id)
        ids = [hashlib.sha256(m['playerId'].encode()).digest()[:8] for m in members]
        if bytes(8) in ids or len(set(ids)) != len(ids):
            raise ValueError('成员显示身份冲突')
        if data['ownerPlayerId'] not in [m['playerId'] for m in members]:
            raise ValueError('入房快照缺少房主')
        maximum = data['maxPlayers']
        if type(maximum) is not int or not len(members) <= maximum <= 255:
            raise ValueError('入房人数无效')
        settings = data['settings']
        scalars = []
        for key, (offset, form) in SETTING_SCALARS.items():
            value = settings[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError('房间设置数值无效：'+key)
            scalars.append((offset, struct.pack(form, value)))
        strings = bytearray(4096)
        for offset, text in ((0, settings['mode']), (256, settings['map']),
                             (512, data.get('ownerDisplayName', '房间'))):
            raw = text.encode('utf-8')
            if len(raw) > 255 or b'\0' in raw:
                raise ValueError('房间展示字符串无效')
            strings[offset:offset+len(raw)] = raw
        language = u32(read, LANGUAGE)
        encoding = 'utf-8' if u32(read, DIRECT_STRING_MODE) == 1 or u32(read, language+12) == 0 else 'gbk'
        names = []
        for member in members:
            raw = member['displayName'].encode(encoding)
            if b'\0' in raw or len(raw) > 63:
                raise ValueError('成员昵称超出原生显示容量')
            names.append(raw.ljust(64, b'\0'))
        self.join_snapshot = (data, members, ids, names, scalars)
        checked_write(self.process, self.join_scratch, bytes(strings))

    def _initialize_join_room(self):
        """会话分配后填入标量设置；字符串随后由主线程原生函数赋值。"""
        data, members, ids, names, scalars = self.join_snapshot
        room = self.pending[1]
        for offset, raw in scalars:
            checked_write(self.process, room+0x105F0+offset, raw)
        checked_write(self.process, room+0x105FE, bytes([data['maxPlayers']])*2)

    def _publish_join_snapshot(self):
        """只写原生已构造的值类型成员槽，保留本机角色数据与原生对象指针。"""
        data, members, ids, names, scalars = self.join_snapshot
        room, session = self.pending[1:]
        if not self._valid_room(room, session):
            raise ValueError('入房发布对象已失效')
        owner_index = next(i for i, m in enumerate(members) if m['playerId'] == data['ownerPlayerId'])
        self.is_owner = data['ownerPlayerId'] == self.player_id
        # 原生 session 的成员摘要为 0x60 字节；房间 UI 的成员记录为 0x1D0 字节。
        checked_write(self.process, session+0x10, bytes(0x30+len(members)*0x60))
        checked_write(self.process, session+0x18, struct.pack('<I', len(members)))
        checked_write(self.process, session+0x20, ids[owner_index])
        checked_write(self.process, room+0x338, ids[owner_index])
        for index, (identity, name) in enumerate(zip(ids, names)):
            checked_write(self.process, room+0xE368+index*0x1D0, identity+name)
            checked_write(self.process, session+0x40+index*0x60, identity+name)
        checked_write(self.process, room+0x105D8,
                      struct.pack('<IIII', len(members), len(members), 0, data['maxPlayers']-len(members)))
        checked_write(self.process, room+0x278, bytes([int(self.is_owner), 1, 0]))
        checked_write(self.process, room+0x27C, bytes(4))
        checked_write(self.process, room+0x281, bytes(7))
        checked_write(self.process, room+0x38711, b'\x01')
        self.record({'multiplayer': 'join', 'success': True, 'roomId': data['roomId'],
                     'members': len(members)})

    def _request(self, operation, frame):
        if self.pending is not None:
            return  # 既有原生等待任务保持不变，重复点击不提交新请求。
        if self.room_list is not None:
            self.room_list.cancel()
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
            if operation == 'create':
                maximum, password, settings = room_options(read, room)
                submitted = self.port.begin(operation, username(read),
                                            max_players=maximum, password=password,
                                            settings=settings)
            else:
                submitted = self.port.begin(operation, username(read))
            if not submitted:
                raise ValueError('已有服务端请求正在处理，请稍后重试')
        except (OSError, ValueError) as error:
            self._failure(operation, str(error))

    def _submit_settings(self, config):
        """逐次提交设置，不按内容去重；原生提交继续执行，失败只记录。"""
        try:
            if not self.is_owner:
                return
            settings = read_settings(self.process.read, config)
            if self.pending is not None:
                # 建房/离房期间保留原有生命周期保护，不另提交房内设置请求。
                return
            if not (self.login_done or self.login_started):
                self.record({'multiplayer_settings': 'skipped', 'reason': 'login not ready'})
                return
            if not self.port.begin('settings', username(self.process.read), settings=settings):
                self.record({'multiplayer_settings': 'skipped', 'reason': 'busy'})
                return
            # 主流程收尾后帧桩已把 POLL 置 0，而设置结果只能由 _poll 消费；
            # 不重新武装轮询，这个 future 会永久占住设置通道，此后每次改动
            # 都按 busy 丢弃。计数清零与 _request 一致，使补交尽快被取走。
            self._settings_outstanding = True
            self._word(POLL, 1)
            self._word(36, 0)
        except (OSError, ValueError) as error:
            self.record({'multiplayer_settings': 'error', 'error': str(error)})

    def _drop_pending(self):
        """丢弃仍在等待的原生请求结果，进入失效后丢弃窗口。"""
        self.pending = None
        self.discard_results = True

    def _poll(self):
        if self.room_list is not None:
            self.room_list.poll()
            if (self.login_done and self.pending is None and not self._settings_outstanding
                    and not self._listing() and not self.join_cleanup):
                self._word(POLL, 0)
        if not self.login_started:
            self.login_started = self.port.begin('login', username(self.process.read))
        if self.pending and self.pending[2] and not self._valid_room(*self.pending[1:]):
            # 不向回收后的对象投递迟到回调；宿主关闭时通过业务端口清理服务端房间。
            raise ValueError('等待服务端响应期间原生房间已改变')
        result = self.port.poll()
        if result is None:
            return
        if result.operation == 'join' and self.pending:
            room = self.pending[1]
            if (self.join_cancelled or u32(self.process.read, ROOM_POINTER) != room
                    or (not self.pending[2] and u32(self.process.read, room+0x1C))):
                self.pending = None
                self._word(ACTION, 0)
                self.join_cleanup = self.port.begin('leave', username(self.process.read))
                self._word(POLL, int(self.join_cleanup or self._listing()))
                self.record({'multiplayer_join_cancelled': True})
                return
        if result.operation == 'login':
            if result.error:
                if self.pending is not None:
                    self._drop_pending()
                self._failure('login', display_message(result.error))
                # 启动登录失败只投递错误，不在后续帧自动重试。
                self.login_started = True
                return
            identity = hashlib.sha256(result.data['playerId'].encode('utf-8')).digest()[:8]
            if identity == bytes(8):
                identity = b'\x01' + bytes(7)
            checked_write(self.process, self.state+IDENTITY, identity)
            self.login_done = True
            self.player_id = result.data['playerId']
            self.discard_results = False
            self._word(LOGIN, 1)
            if self.pending is None:
                self._word(POLL, int(self._listing()))
            self.record({'multiplayer': 'login', 'success': True})
        elif result.operation == 'settings':
            # 原生界面已本地生效，这里只确认服务端落库结果；失败不回滚。
            self.record({'multiplayer_settings': 'done', 'success': result.error is None,
                         **({'error': str(result.error)} if result.error else {})})
            # 设置通道已腾空，无主流程在途时停止轮询，与登录分支同样处理。
            self._settings_outstanding = False
            if self.pending is None:
                self._word(POLL, int(self._listing()))
        elif result.operation == 'leave' and self.join_cleanup:
            self.join_cleanup = False
            self.record({'multiplayer_join_cleanup': result.error is None})
            self._word(POLL, int(self._listing()))
        elif self.discard_results:
            # 失效窗口内迟到的创建/退出结果只记录，不再投递原生回调。
            self.record({'multiplayer_discarded': result.operation, 'success': result.error is None})
        elif self.pending is None or result.operation != self.pending[0]:
            raise ValueError('联机完成结果没有匹配的原生请求')
        elif result.error:
            self._failure(result.operation, display_message(result.error))
            if result.operation == 'join':
                self.join_cleanup = self.port.begin('leave', username(self.process.read))
        elif result.operation == 'join':
            try:
                self._prepare_join_snapshot(result.data)
                self._word(ACTION, 5)
            except (ValueError, OSError, KeyError, TypeError, struct.error) as error:
                self._failure('join', str(error))
                self.join_cleanup = self.port.begin('leave', username(self.process.read))
        else:
            self.is_owner = result.operation == 'create'
            self._word(ACTION, 1 if result.operation == 'create' else 3)
            self.record({'multiplayer': result.operation, 'success': True,
                         **({'roomId': result.data['roomId']} if result.operation == 'create' else {})})

    def _listing(self):
        """大厅查询和取消结果未消费完毕时维持主线程调度。"""
        return self.room_list is not None and self.room_list.active
