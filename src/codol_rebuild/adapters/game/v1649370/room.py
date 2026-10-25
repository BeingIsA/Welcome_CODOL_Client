"""本地房间服务边界：保留原生参数解析、成员构造、任务通知和 UI。

仅接管本地档案就绪且 DW=0 的创建请求。状态区显式记录房间及会话所有权；
不改变在线服务状态。原生离房清除会话后，所有权检查自然失效。
未来服务端应替换创建完成供应方，而不是修改 GFx 页面跳转。
"""
from __future__ import annotations

import struct

CREATE = 0x62CCEA
VALID = 0x62FDE0
IDENTITY = 0x7CADFA
LEAVE = 0x7CF9CC
MEMBER_INIT = 0x62B4F3
MEMBER_IDENTITY = 0x7C72F3
PLAYER_ID = 0x67EC4D
MEMBER_REFRESH = 0x630635
AVATAR_IDENTITY = 0x7C64AF
# 仅本地房间命名空间的单人标识；不是平台账号或服务端玩家 ID。
LOCAL_MEMBER_ID = 1


class RoomTeamBridge:
    """接管联机房间的 UI 换队命令；原生展示刷新不经过此入口。"""
    SLOT = 0xDDD788
    HANDLER = 0x7CFB80

    def __init__(self, process, port, room_state):
        self.process = process
        self.port = port
        self.room_state = room_state
        self.pending_room = None

    def install(self):
        """仅替换 ChangeTeam 虚表槽，房间外跳回原处理器。"""
        if (self.process.read(self.SLOT, 4) != struct.pack('<I', self.HANDLER)
                or self.process.read(self.HANDLER, 6) != bytes.fromhex('558bec8b4508')):
            raise ValueError('换队原生 ABI 不匹配')
        entry = self.process.allocate(4096)
        instruction_bytes = bytearray.fromhex('9c60 8bec cc 619d c20400 619d e9')
        instruction_bytes.extend(struct.pack('<I',
            (self.HANDLER-entry-len(instruction_bytes)-4) & 0xffffffff))
        self.process.write(entry, bytes(instruction_bytes))
        self.process.executable(entry, 4096)
        self.process.write(self.SLOT, struct.pack('<I', entry))
        self.handled = entry+5
        self.fallback = entry+10
        return {entry+4: 'change_team'}

    def handle(self, context):
        """在暂停的主线程提取 UI 目标，不写成员、不发送原生命令。"""
        room_state = self.room_state()
        continuation = self.fallback
        if room_state is not None:
            continuation = self.handled
            if self.pending_room is None:
                try:
                    def read_word(address):
                        return struct.unpack('<I', self.process.read(address, 4))[0]
                    frame = struct.unpack_from('<I', context, 180)[0]
                    parameters = read_word(frame+40)
                    arguments = read_word(parameters+16)
                    if read_word(parameters+20) == 1 and read_word(arguments+12) in (3, 4):
                        target_team = {0: 'tf141', 1: 'scar', 2: 'spectator'}.get(read_word(arguments+16))
                        if target_team is not None and self.port.begin_change_team(room_state[0], target_team):
                            self.pending_room = room_state
                except (ValueError, OSError, struct.error):
                    pass  # 无效请求静默结束，不落回原生上行路径。
        struct.pack_into('<I', context, 184, continuation)

    def poll(self):
        """失败只释放请求；成功快照仅交给仍有效的原生房间。"""
        if self.pending_room is None:
            return None
        result = self.port.poll_change_team()
        if result is None:
            return None
        requested_room, self.pending_room = self.pending_room, None
        if result.error is not None or requested_room != self.room_state():
            return None
        return result.data

    def discard_pending_result(self):
        """离房后仍消费旧请求，但即使原生地址复用也不应用旧结果。"""
        if self.pending_room is not None:
            self.pending_room = ()


CONTRACT = {
    CREATE: bytes.fromhex('e841b11b00'),
    VALID: bytes.fromhex('8b411c5685c0'),
    IDENTITY: bytes.fromhex('e801680300'),
    LEAVE: bytes.fromhex('e84fece6ff'),
    MEMBER_INIT: bytes.fromhex('e818faffff'),
    MEMBER_IDENTITY: bytes.fromhex('e808a30300'),
    PLAYER_ID: bytes.fromhex('505268bc6cdb00'),
    MEMBER_REFRESH: bytes.fromhex('5f5ec20400'),
    AVATAR_IDENTITY: bytes.fromhex('0f84cc000000'),
    0x7CF9F1: bytes.fromhex('5e8be55dc20400'),
    0x62F640: bytes.fromhex('568bf1'),
    0x633FE0: bytes.fromhex('558bec'),
    0x62B3D0: bytes.fromhex('558bec83e4f8'),
}


def validate_room_contract(read):
    """固定版本调用边不一致时拒绝安装。"""
    for address, expected in CONTRACT.items():
        if read(address, len(expected)) != expected:
            raise ValueError(f'本地房间 ABI 不匹配：{address:#x}')


def build_room_adapter(entry: int, state: int, ready: int, *, deferred=False, identity_address=None,
                       profile_capture=None) -> dict:
    """生成代码与补丁计划；state 为独立的12字节所有权/发布重入记录。"""
    for address in (entry, state, ready):
        if type(address) is not int or not 0x10000 <= address <= 0xffffe000:
            raise ValueError('房间适配地址必须为有效32位地址')
    code, labels, refs, traps = bytearray(), {}, [], {}

    def emit(value):
        code.extend(bytes.fromhex(value))

    def word(value):
        code.extend(struct.pack('<I', value))

    def label(name):
        labels[name] = len(code)

    def branch(op, name):
        emit(op); refs.append((len(code), name)); word(0)

    def native(op, address):
        emit(op); word((address-entry-len(code)-4) & 0xffffffff)

    def offline(fallback):
        emit('833d'); word(ready); emit('01'); branch('0f85', fallback)
        emit('a1'); word(0x849B874); emit('85c0'); branch('0f84', fallback)
        emit('83780c00'); branch('0f85', fallback)

    def owned(fallback):
        # ECX 指向房间；离房、其他房间和被回收的 session 都不属于本适配。
        emit('3b0d'); word(state); branch('0f85', fallback)
        emit('8b411c 85c0'); branch('0f84', fallback)
        emit('3b05'); word(state+4); branch('0f85', fallback)
        emit('803800'); branch('0f84', fallback)
        emit('3988c8250000'); branch('0f85', fallback)

    label('create')
    emit('9c60')
    offline('create_original')
    # 原 create 已完成 config 深拷贝和会话分配，EBX=room、EDI=session。
    emit('85db'); branch('0f84', 'create_original')
    emit('85ff'); branch('0f84', 'create_original')
    emit('3b7b1c'); branch('0f85', 'create_original')
    emit('399fc8250000'); branch('0f85', 'create_original')
    if deferred:
        if profile_capture is not None:
            native('e8', profile_capture)
        traps[entry+len(code)] = 'create'
        emit('cc 619d b001 c3')  # 原生任务保持等待，完成由主线程帧回调投递。
        label('create_complete'); emit('9c60')
    emit('891d'); word(state)
    emit('893d'); word(state+4)
    # 单机房间没有远程会话 ID/广告，也不设置 connected 或 DW 在线标志。
    emit('c7471000000000 c7471400000000 c7471801000000')
    emit('c7472000000000 c7472400000000 c7474000000000 c7474400000000')
    emit('8d4720 50'); native('e8', 0x801600); emit('83c404')
    # 保留可用的原生身份；无登录对象时用独立本地身份，零是原版空槽哨兵。
    emit('8b4720 0b4724'); branch('0f85', 'session_identity')
    emit('c74720'); word(LOCAL_MEMBER_ID)
    label('session_identity')
    if identity_address is not None:
        emit('a1'); word(identity_address); emit('894720')
        emit('a1'); word(identity_address+4); emit('894724')
    emit('8b4720 894740 8b4724 894744')
    emit('c6838102000000')
    emit('6a01 57'); native('e8', 0x633FE0); emit('83c408')
    emit('61 9d b001 c3')
    label('create_original'); emit('619d'); native('e9', 0x7E7E30)

    label('valid')
    emit('9c50')
    offline('valid_original')
    owned('valid_original')
    emit('589d b801000000 c3')
    label('valid_original'); emit('589d'); code.extend(CONTRACT[VALID])
    native('e9', VALID+len(CONTRACT[VALID]))

    label('identity')
    emit('9c60')
    offline('identity_original')
    emit('8b0d'); word(0x82E13FC)
    emit('85c9'); branch('0f84', 'identity_original')
    owned('identity_original')
    # 外层发布与内层 GFx 成员转换必须读取同一身份；只改房间调用边。
    emit('8b542428 85d2'); branch('0f84', 'identity_original')
    emit('8b8168e30000 8902 8b816ce30000 894204')
    emit('619d b001 c3')
    label('identity_original'); emit('619d'); native('e9', 0x801600)

    label('member_init')
    # 原生成员初始化是 stdcall(room)，必须先保留完整的原生成员构造。
    emit('ff742404'); native('e8', 0x62AF10)
    emit('9c60')
    offline('member_done')
    emit('8b4c2428')
    owned('member_done')
    # owned 留 EAX=session。成员、会话房主和会话成员使用相同64位标识。
    emit('8b5020 899168e30000 8b5024 89916ce30000')
    if deferred:
        traps[entry+len(code)] = 'member_initialized'
        emit('cc')
    label('member_done'); emit('619d c20400')

    label('player_id')
    emit('9c60')
    offline('player_id_done')
    # UI 在进房前就缓存 PlayerObj.id；本地档案期间保持稳定，不能只在房间内替换。
    # 原生身份非零时完整保留 EAX:EDX，不改变任何登录对象。
    if identity_address is None:
        emit('8b44241c 0b442414'); branch('0f85', 'player_id_done')
        emit('c7442414'); word(LOCAL_MEMBER_ID)
    else:
        # 原版用 EDX 保存低位、EAX 保存高位；与 session 的小端64位字段对应。
        emit('a1'); word(identity_address); emit('89442414')
        emit('a1'); word(identity_address+4); emit('8944241c')
    label('player_id_done'); emit('619d')
    code.extend(CONTRACT[PLAYER_ID]); native('e9', PLAYER_ID+len(CONTRACT[PLAYER_ID]))

    label('member_refresh')
    # 原生刷新只置 dirty；发布原本依赖未运行的在线周期。仅补本地发布。
    emit('9c60')
    offline('member_refresh_done')
    emit('8bcf')  # 原生6305F0在尾部仍以EDI持有room。
    owned('member_refresh_done')
    emit('833d'); word(state+8); emit('00'); branch('0f85','member_refresh_done')
    emit('c705'); word(state+8); word(1)
    # 原版靠下一帧重试500ms节流；本地事件没有在线tick，确保当前变更可发布。
    # 重入保护避免发布回调同步触发刷新时递归。
    native('e8',0x77FFB0)
    emit('8b0d'); word(0x8249B78)
    emit('85c9'); branch('0f84','member_publish')
    emit('2b410c a3'); word(0x8249B74)
    label('member_publish')
    native('e8',0x7CAD60)
    emit('c705'); word(state+8); word(0)
    label('member_refresh_done'); emit('619d'); code.extend(CONTRACT[MEMBER_REFRESH])

    label('avatar_identity')
    # 被替换的是JE；真实登录对象存在时必须保留原分支，不能改写其身份。
    branch('0f85','avatar_native_login')
    emit('9c60')
    offline('avatar_missing')
    emit('8b0d'); word(0x82E13FC)
    emit('85c9'); branch('0f84','avatar_missing')
    owned('avatar_missing')
    emit('8b8168e30000 89442404 8b816ce30000 890424')
    emit('619d'); native('e9',0x7C64C0)
    label('avatar_missing'); emit('619d'); native('e9',0x7C6581)
    label('avatar_native_login'); native('e9',0x7C64B5)

    label('leave')
    emit('9c60')
    offline('leave_original')
    emit('8b0d'); word(0x82E13FC)
    emit('85c9'); branch('0f84', 'leave_original')
    owned('leave_original')
    if deferred:
        traps[entry+len(code)] = 'leave'
        emit('cc 619d c3')
        label('leave_complete'); emit('9c60')
    # 保留原生公共清理；离线状态下它跳过房间对象，随后补调原生离房。
    emit('ff742428'); native('e8', 0x63E620); emit('83c404')
    emit('8b0d'); word(state)
    owned('leave_done')
    native('e8', 0x62F640)
    label('leave_done')
    emit('c705'); word(state); word(0)
    emit('c705'); word(state+4); word(0)
    # 返回原调用点，让 GFx 入口自行检查离房结果并完成同步通知。
    # 不能跳过 7CF9D1：原生对象清理完成不等同于页面任务已经完成。
    emit('619d c3')
    label('leave_original'); emit('619d'); native('e9', 0x63E620)

    for position, name in refs:
        struct.pack_into('<i', code, position, labels[name]-position-4)
    patches = []
    for address, name, opcode in ((CREATE,'create',0xe8),(VALID,'valid',0xe9),(IDENTITY,'identity',0xe8),(LEAVE,'leave',0xe8),
                                  (MEMBER_INIT,'member_init',0xe8),(MEMBER_IDENTITY,'identity',0xe8),(PLAYER_ID,'player_id',0xe9),
                                  (MEMBER_REFRESH,'member_refresh',0xe9),(AVATAR_IDENTITY,'avatar_identity',0xe9)):
        before = CONTRACT[address]
        after = bytes([opcode])+struct.pack('<I',(entry+labels[name]-address-5)&0xffffffff)
        patches.append({'va':address,'before':before,'after':after+b'\x90'*(len(before)-5)})
    entries = ('create','valid','identity','leave','member_init','player_id','member_refresh','avatar_identity')
    if deferred:
        entries += ('create_complete', 'leave_complete')
    return {'code':bytes(code),'patches':patches,'traps':traps, 'ownership': state,
            'entries':{k:entry+labels[k] for k in entries}}


# AS_JoinRoom 普通房间分支：数字转换前仍持有原始 id 和密码。
JOIN_INPUT = 0x5D3216
# 联机通信页中的入房失败码；由版本桥在失败通知前写入。
JOIN_ERROR_OFFSET = 40
# 禁入专用码不占用原版 0..47；文本沿用现有失败任务的展示流程。
JOIN_BLOCKED_ERROR = 0x10000
JOIN_ERROR_MESSAGE_OFFSET = 128
JOIN_ERROR_TEXT_CALL = 0x7C46EB
# 设置回显独立于建房、入房、离房任务槽，避免完成通知互相覆盖。
SETTINGS_READY_OFFSET, SETTINGS_ROOM_OFFSET = 44, 48
JOIN_CONTRACT = {
    JOIN_ERROR_TEXT_CALL: bytes.fromhex('e850cdf6ff'),
    0x731440: bytes.fromhex('83f82f'),
    JOIN_INPUT: bytes.fromhex('e8fbc06600'),
    0x623770: bytes.fromhex('5333db568bf1'),
    0x7E7910: bytes.fromhex('33d233c0b95046370e'),
    0xB75B10: bytes.fromhex('558bec538b5d'),
    0x630480: bytes.fromhex('568b710485f6'),
}
MEMBER_CAPACITY = (0x105D8 - 0xE368) // 0x1D0


def validate_join_contract(read):
    """加入入口、成员构造及字符串赋值 ABI 不符时拒绝安装。"""
    for address, expected in JOIN_CONTRACT.items():
        if read(address, len(expected)) != expected:
            raise ValueError(f'加入房间 ABI 不匹配：{address:#x}')


def build_join_adapter(entry, state, scratch, profile_capture=None):
    """生成加入专用主线程桩；成员数据由暂停点发布，通知沿原生监听器。"""
    code, labels, refs, traps = bytearray(), {}, [], {}
    def emit(value): code.extend(bytes.fromhex(value))
    def word(value): code.extend(struct.pack('<I', value))
    def label(name): labels[name] = len(code)
    def branch(op, name):
        emit(op); refs.append((len(code), name)); word(0)
    def call(address):
        emit('e8'); word((address-entry-len(code)-4) & 0xffffffff)
    def trap(name):
        traps[entry+len(code)] = name; emit('cc')

    label('input')
    if profile_capture is not None:
        call(profile_capture)
    trap('join_input')
    # 在线非本适配路径继续原数字转换；适配路径由处理器跳到 AS 公共清理。
    emit('e9'); word((0xC3F316-entry-len(code)-4) & 0xffffffff)
    label('join_complete')
    emit('9c60 8b1d'); word(state+8)  # EBX=room，保持调用者寄存器。
    call(0x7E7910)
    emit('85c0'); branch('0f84', 'allocation_failed')
    emit('89431c 8998c8250000 a3'); word(state+12)
    trap('join_allocated')
    # 字符串使用原生引用计数赋值函数，不能把 Python 分配的裸串冒充原生字符串。
    for offset, source in ((0, 0), (8, 256), (0x10, 512)):
        emit('68'); word(scratch+source)
        emit('8d8b'); word(0x105F0+offset)
        call(0xB75B10)
    emit('8dbb'); word(0xE368)
    emit('be'); word(MEMBER_CAPACITY)
    label('init_member')
    emit('8bcf'); call(0x623770)
    emit('81c7'); word(0x1D0)
    emit('4e'); branch('0f85', 'init_member')
    emit('53'); call(0x62AF10)  # 只对第一个（本机）槽填本地角色资料。
    trap('join_snapshot')
    # 与 634E79 的入房成功通知一致，成员/设置已完整发布后才通知 UI。
    emit('8b7304')
    label('notify_join')
    emit('85f6'); branch('0f84', 'notify_settings_start')
    emit('8b0e 8b01 8b4004 8b7604 6a00 6a01 ffd0')
    branch('e9', 'notify_join')
    label('notify_settings_start'); emit('8b7304')
    label('notify_settings')
    emit('85f6'); branch('0f84', 'refresh')
    emit('8b0e 8b01 8b4048 8b7604 6a00 8d93'); word(0x105F0)
    emit('52 ffd0'); branch('e9', 'notify_settings')
    label('refresh')
    emit('6a01 8bcb'); call(0x6305F0)
    emit('619d c3')
    label('allocation_failed'); trap('join_allocation_failed')
    branch('e8', 'join_failed'); emit('619d c3')

    label('join_failed')
    emit('9c60 8b1d'); word(state+8)
    emit('8b7304')
    label('notify_failure')
    emit('85f6'); branch('0f84', 'failed_done')
    emit('8b0e 8b01 8b4004 8b7604 ff35'); word(state+JOIN_ERROR_OFFSET)
    emit('6a00 ffd0')
    branch('e9', 'notify_failure')
    label('failed_done'); emit('619d c3')
    label('settings_apply')
    emit('9c60 8b1d'); word(state+SETTINGS_ROOM_OFFSET)
    # 仅更新模式/地图引用与设置通知；不进入用户提交、入房或成员刷新路径。
    for offset, source in ((0, 0), (8, 256)):
        emit('68'); word(scratch+source)
        emit('8d8b'); word(0x105F0+offset)
        call(0xB75B10)
    emit('8bcb'); call(0x630480)
    emit('619d c3')
    # 只适配入房失败通知中的文本转换调用；其他错误原样交给原生函数。
    label('join_error_text')
    emit('3d'); word(JOIN_BLOCKED_ERROR)
    branch('0f85', 'native_join_error_text')
    emit('b8'); word(state+JOIN_ERROR_MESSAGE_OFFSET)
    emit('c3')
    label('native_join_error_text')
    emit('e9'); word((0x731440-entry-len(code)-4) & 0xffffffff)
    for offset, target in refs:
        struct.pack_into('<i', code, offset, labels[target]-offset-4)
    return {'code': bytes(code), 'traps': traps,
            'entries': {name: entry+labels[name] for name in ('join_complete', 'join_failed', 'settings_apply')},
            'patches': [{'va': JOIN_INPUT, 'before': JOIN_CONTRACT[JOIN_INPUT],
                         'after': b'\xe8'+struct.pack('<I', (entry-JOIN_INPUT-5) & 0xffffffff)},
                        {'va': JOIN_ERROR_TEXT_CALL, 'before': JOIN_CONTRACT[JOIN_ERROR_TEXT_CALL],
                         'after': b'\xe8'+struct.pack('<I', (entry+labels['join_error_text']-JOIN_ERROR_TEXT_CALL-5) & 0xffffffff)}]}
