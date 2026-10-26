"""1649370 玩家档案传递：双档案及原生生成的两阵营装备展示快照。

本模块只解释版本格式并通过显式进程端口采样，不负责房间权威规则。
数据块不含 C++ 对象头或跨进程指针，不修改原生界面的查看权限。
"""
import base64
import binascii
import struct

from codol_rebuild.adapters.game.v1649370 import layout
from codol_rebuild.adapters.game.v1649370.accounts.profile import PUBLIC, SECURE

VERSION = 'codol-1649370'
SIZES = {'public': layout.BUFFER_CAPACITY, 'secure': layout.BUFFER_CAPACITY,
         'tf141': 0xA0, 'scar': 0xA0}
CAPTURE_CONTRACT = {0x4F73B0: bytes.fromhex('568bf06a088d46106a00'),
                    0x7C5D60: bytes.fromhex('558bec83e4f883ec64')}
QUERY_SITE = 0x68D52A
QUERY_BEFORE = bytes.fromhex('a1209de500')
CONTAINER_SIZE = 0x22290
PROFILE_SUBMIT_SITE = 0x7C6576
PROFILE_SUBMIT_BEFORE = bytes.fromhex('8b118b8224010000')
PROFILE_SUBMIT_RETURN = 0x7C6581


def build_profile_submit(entry, capture_entry):
    """仅替换原生变化判断后的虚调用取址，保留比较、缓存和模型更新。"""
    code = bytearray(b'\xcc')
    traps = {entry: 'profile_submit'}

    def branch(opcode, target):
        code.extend(bytes([opcode]) + struct.pack('<I', (target-entry-len(code)-5) & 0xffffffff))

    fallback = entry + len(code)
    code.extend(PROFILE_SUBMIT_BEFORE)
    branch(0xe9, PROFILE_SUBMIT_SITE + len(PROFILE_SUBMIT_BEFORE))
    capture = entry + len(code)
    code.extend(bytes.fromhex('9c60'))
    branch(0xe8, capture_entry)
    traps[entry + len(code)] = 'profile_captured'
    code.append(0xcc)
    code.extend(bytes.fromhex('619d'))
    branch(0xe9, PROFILE_SUBMIT_RETURN)
    patch = b'\xe9' + struct.pack('<I', (entry-PROFILE_SUBMIT_SITE-5) & 0xffffffff) + b'\x90'*3
    return {'code': bytes(code), 'traps': traps, 'fallback': fallback,
            'capture': capture, 'patch': patch}


class ProfileUpdateBridge:
    """在原生已完成本地修改的上行边采集，房外回退原虚调用。"""

    def __init__(self, process, capture_entry, display_buffer):
        self.process = process
        self.capture_entry = capture_entry
        self.display_buffer = display_buffer
        self.captured_room_id = None

    def install(self):
        """校验专用提交边及未修改的 push/call，只覆盖八字节取址指令。"""
        expected_bytes = PROFILE_SUBMIT_BEFORE + bytes.fromhex('57ffd0')
        if self.process.read(PROFILE_SUBMIT_SITE, len(expected_bytes)) != expected_bytes:
            raise ValueError('房内档案提交 ABI 不匹配')
        entry = self.process.allocate(4096)
        self.plan = build_profile_submit(entry, self.capture_entry)
        self.process.write(entry, self.plan['code'])
        self.process.executable(entry, 4096)
        self.process.write(PROFILE_SUBMIT_SITE, self.plan['patch'])
        return self.plan['traps']

    def handle(self, phase, context, room_id, suppress):
        """调用者保证主线程和房间生命周期；返回独立快照，不访问网络。"""
        if phase == 'profile_submit':
            self.captured_room_id = None
            continuation = self.plan['fallback']
            if room_id is not None:
                continuation = PROFILE_SUBMIT_RETURN if suppress else self.plan['capture']
                if not suppress:
                    self.captured_room_id = room_id
            struct.pack_into('<I', context, 184, continuation)
            return None
        # 原生采集返回后恢复保存的寄存器，由 host 的陷阱分派继续到下一条指令。
        captured_room_id = self.captured_room_id
        self.captured_room_id = None
        if captured_room_id is not None and captured_room_id == room_id and not suppress:
            return capture_snapshot(self.process.read, self.display_buffer)
        return None

# 固定版本 DDL 字节偏移；回归测试用原生捕获的 schema 校验。
# 6236F0 的五个 getter 顺序对应房间成员 +B2 起的五个 uint16。
CARD_FIELDS = (('cardTitle', 3854), ('cardIcon', 3846),
               ('cardBackground', 3842), ('cardNameplate', 3850),
               ('cardFrame', 15527))
RANK_OFFSET = 4883
PRESTIGE_OFFSET = 4879


def member_summary(snapshot):
    """将已校验的档案转换为房间悬停摘要，只返回待写入的值字段。"""
    rank = struct.unpack_from('<i', snapshot['secure'], RANK_OFFSET)[0]
    prestige = struct.unpack_from('<i', snapshot['secure'], PRESTIGE_OFFSET)[0]
    # 与原生 50C390 相同：存档等级减一，负值回到零。
    level_index = max(0, rank - 1)
    cards = [struct.unpack_from('<i', snapshot['public'], offset)[0]
             for _, offset in CARD_FIELDS]
    return ((0x98, struct.pack('<BB', level_index & 0xff, prestige & 0xff)),
            (0xB2, struct.pack('<5H', *(value & 0xffff for value in cards))))


def build_query(entry, container, identity):
    """只接管远端资料来源，完成后回到原生转换及 cppShowClanMemberInfo 通知。"""
    code = bytearray(b'\xcc')
    traps = {entry: 'profile_query'}
    def branch(opcode, target):
        code.extend(bytes([opcode])+struct.pack('<I', (target-entry-len(code)-5) & 0xffffffff))
    fallback = entry+len(code)
    code.extend(QUERY_BEFORE)
    branch(0xe9, QUERY_SITE+5)
    initialize = entry+len(code)
    code.extend(bytes.fromhex('9c60')+b'\x68'+struct.pack('<I', identity)+b'\x6a\x00\xb9'+struct.pack('<I', container))
    branch(0xe8, 0x7F8E10)  # thiscall，原生 ret 8 清理参数。
    code.extend(bytes.fromhex('619d'))
    traps[entry+len(code)] = 'profile_initialized'
    code.append(0xcc)
    branch(0xe9, 0x68D5CC)
    patch = b'\xe9'+struct.pack('<I', (entry-QUERY_SITE-5) & 0xffffffff)
    return {'code': bytes(code), 'traps': traps, 'fallback': fallback,
            'initialize': initialize, 'patch': patch}


class ProfileQueryBridge:
    """主线程资料查询适配；快照通过显式回调获取，不修改本机账号或 UI 权限。"""
    def __init__(self, process, snapshots):
        self.process, self.snapshots = process, snapshots
        self.selected = None

    def install(self):
        """验证固定版本调用边后安装查询桥；独立容器在进程退出时回收。"""
        for address, expected in {QUERY_SITE: QUERY_BEFORE,
                                  0x7F8E10: bytes.fromhex('558bec83ec54'),
                                  0x68D5CC: bytes.fromhex('3bfb')}.items():
            if self.process.read(address, len(expected)) != expected:
                raise ValueError(f'玩家资料查询 ABI 不匹配：{address:#x}')
        self.container = self.process.allocate(CONTAINER_SIZE)
        self.process.write(self.container, bytes(CONTAINER_SIZE))
        self.identity = self.process.allocate(4096)
        entry = self.process.allocate(4096)
        self.plan = build_query(entry, self.container, self.identity)
        self.process.write(entry, self.plan['code'])
        self.process.executable(entry, 4096)
        self.process.write(QUERY_SITE, self.plan['patch'])
        return self.plan['traps']

    def handle(self, phase, ctx):
        """调用者保证主线程暂停；初始化后才填入档案，保留原生清理生命周期。"""
        if phase == 'profile_query':
            frame = struct.unpack_from('<I', ctx, 196)[0]
            identity = self.process.read(frame+0x18, 8)
            self.selected = self.snapshots().get(identity)
            target = self.plan['fallback']
            if self.selected is not None:
                self.process.write(self.identity, identity)
                target = self.plan['initialize']
            struct.pack_into('<I', ctx, 184, target)
        else:
            if self.selected is None:
                raise ValueError('玩家资料初始化缺少快照')
            for key, offset in (('public', 0xA0), ('secure', 0x111E0)):
                self.process.write(self.container+offset, self.selected[key])
            struct.pack_into('<I', ctx, 156, self.container)  # CONTEXT.Edi
            self.selected = None
            struct.pack_into('<I', ctx, 184, 0x68D5CC)


def decode_snapshot(payload):
    """拒绝版本、容量及 DDL 身份不匹配的档案，返回独立不可变字节。"""
    if (not isinstance(payload, dict) or set(payload) != {'contentVersion', *SIZES}
            or payload['contentVersion'] != VERSION):
        raise ValueError('成员档案版本或字段无效')
    result = {}
    for key, size in SIZES.items():
        value = payload[key]
        if not isinstance(value, str) or len(value) != 4*((size+2)//3):
            raise ValueError('成员档案容量无效：'+key)
        try:
            raw = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError('成员档案编码无效：'+key) from None
        if len(raw) != size:
            raise ValueError('成员档案容量无效：'+key)
        result[key] = raw
    for key, identity in (('public', layout.PUBLIC_IDENTITY), ('secure', layout.SECURE_IDENTITY)):
        if result[key][:8] != struct.pack('<II', identity['version'], identity['checksum']):
            raise ValueError('成员档案 DDL 身份不匹配：'+key)
    return result


def capture_snapshot(read, display_buffer):
    """仅在主线程暂停点采样；展示缓冲须由本次原生采集调用完整生成。"""
    raw = {'public': read(PUBLIC+0x90, layout.BUFFER_CAPACITY),
           'secure': read(SECURE+0x90, layout.BUFFER_CAPACITY),
           'tf141': read(display_buffer, 0xA0), 'scar': read(display_buffer+0xA0, 0xA0)}
    result = {'contentVersion': VERSION,
              **{key: base64.b64encode(value).decode('ascii') for key, value in raw.items()}}
    decode_snapshot(result)
    return result


def build_capture(entry, buffer):
    """调用原生展示包构造与采集器；分别采集141、SCAR，保留调用者寄存器。"""
    code = bytearray(bytes.fromhex('9c60'))
    def call(address):
        code.extend(b'\xe8'+struct.pack('<I', (address-entry-len(code)-5) & 0xffffffff))
    for offset, team in ((0, 2), (0xA0, 1)):
        target = buffer+offset
        code.extend(b'\xb8'+struct.pack('<I', target))
        call(0x4F73B0)
        code.extend(b'\x6a'+bytes([team])+b'\x68'+struct.pack('<I', target))
        call(0x7C5D60)
        code.extend(bytes.fromhex('83c408'))
    code.extend(bytes.fromhex('619dc3'))
    return bytes(code)


def build_member_publish(entry, buffer):
    """成员已写入后发布原生列表，再依次调用原生远端装备接收入口。"""
    code = bytearray(bytes.fromhex('9c60'))
    def word(value):
        code.extend(struct.pack('<I', value))
    def call(address):
        code.extend(b'\xe8'); word((address-entry-len(code)-4) & 0xffffffff)
    # 换队后由原生分配同组槽位；ESI=成员数，EDI=当前成员，栈参数=成员数组。
    code.extend(b'\x83\x3d'); word(buffer+8); code.extend(bytes.fromhex('000f84'))
    slots_skip = len(code); word(0)
    code.extend(b'\x8b\x1d'); word(buffer)
    code.extend(bytes.fromhex('8babd8050100 8dbb68e30000'))
    slots_loop = len(code)
    code.extend(bytes.fromhex('85ed0f84')); slots_end = len(code); word(0)
    code.extend(bytes.fromhex('8bb3d8050100 8d8368e30000 50')); call(0x629FF0)
    code.extend(bytes.fromhex('83c404 8887b0000000 81c7d0010000 4d e9'))
    word((slots_loop-len(code)-4) & 0xffffffff)
    struct.pack_into('<i', code, slots_end, len(code)-slots_end-4)
    struct.pack_into('<i', code, slots_skip, len(code)-slots_skip-4)
    # 本人阵营确认变化后，先同步原生展示状态，再沿原生通知重建模型与选择。
    # 成员发布约定第一个槽为本人；重复快照及他人换队不覆盖原生选择状态。
    code.extend(b'\x83\x3d'); word(buffer+12); code.extend(bytes.fromhex('000f84'))
    team_skip = len(code); word(0)
    code.extend(b'\xa1'); word(buffer)
    code.extend(bytes.fromhex('8b80f0e30000 a3')); word(0x0849B618)
    struct.pack_into('<i', code, team_skip, len(code)-team_skip-4)
    code.extend(b'\x8b\x0d'); word(buffer)
    code.extend(bytes.fromhex('6a01')); call(0x6305F0)
    code.extend(b'\x8b\x1d'); word(buffer+4)
    code.extend(b'\xbe'); word(buffer+16)
    start = len(code)
    code.extend(bytes.fromhex('85db0f84')); end_jump = len(code); word(0)
    code.extend(b'\x56'); call(0x7CBAB0)  # 接收入口自行 ret 4。
    code.extend(bytes.fromhex('81c6')); word(0xA0)
    code.extend(bytes.fromhex('4be9')); word((start-len(code)-4) & 0xffffffff)
    struct.pack_into('<i', code, end_jump, len(code)-end_jump-4)
    # 原生自身阵营展示更新入口也处理观察者；不复用用户 ChangeTeam 命令。
    code.extend(b'\x83\x3d'); word(buffer+12); code.extend(bytes.fromhex('007405'))
    call(0x7CB290)
    code.extend(bytes.fromhex('619dc3'))
    return bytes(code)
