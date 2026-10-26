"""1649370 离线全解锁档案的字段解析与纯字节构造。

独立模块：只依赖「已捕获的 schema JSON」（profile_schema.capture_schema 的产物）
与一段基础档案缓冲（bytes）+ 标准库。不读写进程、不启动游戏，也不导入
运行宿主 / 启动入口 / schema_scan——把档案注入回客户端是将来另一个独立
模块的职责，本模块只负责产出「全解锁」档案字节；持久化位于 storage.profile_archive。

偏移语义（与旧版 2.6.9.7 已跑通的 progression.py / profile_store.py 一致，字段偏移
取自运行期捕获的新版 schema，见 evidence/profile-schema-*-1649370.json）：
- prop['offset'] 是 offsets 表的**索引**，不是字节偏移；
- 标量整数（type[0] ∈ {0:int32, 1:uint8, 9:int16}）：byte_offset = offsets[prop.offset]，
  按对应宽度小端写入；
- 位域数组（type[0]==6 且 indexed_arrays[type[1]] 的 elemKind==2）：count 为位数，
  第 i 位的位位置 = offsets[prop.offset + i]（位位置非连续，必须逐个查表），置 1。
- elemKind==9 的定长数组是 int16 槽（如 unlockFlagsNew），不是位域；其「已解锁」取值
  未经证实，默认不写，只有 config 显式给出 int16 值时才逐槽写入。
"""
from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

# 新版档案 DDL 数据缓冲容量（0x7f69d0 要求 schema size <= 0x7ffc）；持久化整段容量。
BUFFER_CAPACITY = 0x7FFC

# 标量整数类型种类 → struct 格式（来源：旧版 profile_store 的整数格式映射）。
_INT_FORMATS = {0: '<i', 1: '<B', 9: '<h'}

# 固定版本身份断言，防止用错结构解释内存；数值取自运行期捕获（非旧版 2697 常量）。
SECURE_IDENTITY = {'version': 19, 'checksum': 0xa34378b4, 'size': 15124}
PUBLIC_IDENTITY = {'version': 233, 'checksum': 0x4a6cc345, 'size': 15767}

# 满级 = 70：来自新版 mp/rankexptable.csv 表头行数(0x46)，与旧版已跑通种子 rank=70 一致。
MAX_RANK = 70

# 默认全解锁配置：只写有把握的字段；experience/rankVersion 留空表示沿用基础档案原值，
# unlock_flags_new 留空表示不触碰（int16 槽语义未证实）。
DEFAULT_FULL_UNLOCK = {
    'rank': MAX_RANK,
    'experience': None,
    'rank_version': None,
    'fill_unlock_flags': True,
    'unlock_flags_new_value': None,
}


from codol_rebuild.application.accounts.contracts import ProfileFormatError as UnlockError


def load_schema(path: str | Path, identity: dict = SECURE_IDENTITY) -> dict:
    """读取并校验一份捕获的 schema JSON；版本/校验和/大小三者必须与身份一致。"""
    schema = json.loads(Path(path).read_text(encoding='utf-8'))
    header = schema.get('header')
    if not isinstance(header, list) or len(header) < 15:
        raise UnlockError('schema 缺少完整头部字')
    got = {'version': int(header[0], 0), 'checksum': int(header[1], 0), 'size': schema.get('size')}
    if got != identity:
        raise UnlockError(f'schema 身份不符：得到 {got}，要求 {identity}')
    return schema


def _root_properties(schema: dict) -> dict:
    """返回根结构体（root_type 指向的 struct）的属性字典 name->prop。"""
    kind, index = schema['root_type']
    if kind != 5:
        raise UnlockError(f'根类型 {kind} 不是结构体，无法定位进度字段')
    props = schema['structs'][index]['properties']
    return {prop['name']: prop for prop in props}


def resolve_scalar(schema: dict, name: str) -> tuple:
    """把标量整数字段名解析成 (struct格式, 字节偏移)；非整数或越界即报错。"""
    prop = _root_properties(schema).get(name)
    if prop is None:
        raise UnlockError(f'schema 根结构体没有字段：{name}')
    kind, _index = prop['type']
    fmt = _INT_FORMATS.get(kind)
    if fmt is None:
        raise UnlockError(f'字段 {name} 类型种类 {kind} 不是标量整数')
    offset = schema['offsets'][prop['offset']]
    if offset < 8 or offset + struct.calcsize(fmt) > schema['size']:
        raise UnlockError(f'字段 {name} 字节偏移 {offset} 超出 schema 缓冲')
    return fmt, offset


def resolve_bitfield(schema: dict, name: str) -> list:
    """把位域数组字段解析成全部位的「位位置」列表（逐个查 offsets 表）。"""
    prop = _root_properties(schema).get(name)
    if prop is None:
        raise UnlockError(f'schema 根结构体没有字段：{name}')
    kind, index = prop['type']
    if kind != 6:
        raise UnlockError(f'字段 {name} 不是定长数组（type 种类 {kind}）')
    count, elem_kind = schema['indexed_arrays'][index][0], schema['indexed_arrays'][index][1]
    if elem_kind != 2:
        raise UnlockError(f'字段 {name} 元素种类 {elem_kind} 不是位域，不能用置位处理')
    offsets = schema['offsets']
    base = prop['offset']
    if base + count > len(offsets):
        raise UnlockError(f'字段 {name} 位域索引越界：{base}+{count} > {len(offsets)}')
    return [offsets[base + i] for i in range(count)]


def resolve_int16_array(schema: dict, name: str) -> list:
    """把 elemKind==9 的定长数组解析成各元素的 (格式'<h', 字节偏移) 列表。"""
    prop = _root_properties(schema).get(name)
    if prop is None:
        raise UnlockError(f'schema 根结构体没有字段：{name}')
    kind, index = prop['type']
    if kind != 6:
        raise UnlockError(f'字段 {name} 不是定长数组')
    entry = schema['indexed_arrays'][index]
    count, elem_kind = entry[0], entry[1]
    if elem_kind != 9:
        raise UnlockError(f'字段 {name} 元素种类 {elem_kind} 不是 int16 数组')
    offsets = schema['offsets']
    base = prop['offset']
    if base + count > len(offsets):
        raise UnlockError(f'字段 {name} 数组索引越界')
    return [('<h', offsets[base + i]) for i in range(count)]


def apply_full_unlock(base: bytes, schema: dict, config: dict | None = None) -> tuple:
    """在基础档案缓冲上构造全解锁档案，返回 (新缓冲, 变更报告)；纯函数不改入参。

    base 长度须为 BUFFER_CAPACITY（不足右侧补零、超出报错）。只写 config 指定的字段；
    标量按宽度小端写入，unlockFlags 全部位位置 1，unlockFlagsNew 仅在给定 int16 值时逐槽写。
    """
    config = {**DEFAULT_FULL_UNLOCK, **(config or {})}
    if len(base) > BUFFER_CAPACITY:
        raise UnlockError(f'基础档案 {len(base)} 字节超过缓冲容量 {BUFFER_CAPACITY}')
    data = bytearray(base.ljust(BUFFER_CAPACITY, b'\x00'))
    changes = {}

    for name, key in (('rank', 'rank'), ('experience', 'experience'), ('rankVersion', 'rank_version')):
        value = config.get(key)
        if value is None:
            continue
        fmt, offset = resolve_scalar(schema, name)
        before = struct.unpack_from(fmt, data, offset)[0]
        struct.pack_into(fmt, data, offset, value)
        changes[name] = {'before': before, 'value': value, 'byte_offset': offset, 'format': fmt}

    if config.get('fill_unlock_flags'):
        bits = resolve_bitfield(schema, 'unlockFlags')
        set_count = 0
        for bit in bits:
            byte_index, shift = divmod(bit, 8)
            if not data[byte_index] & (1 << shift):
                set_count += 1
            data[byte_index] |= 1 << shift
        changes['unlockFlags'] = {'bits': len(bits), 'newly_set': set_count,
                                  'bit_range': [min(bits), max(bits)] if bits else None}

    new_value = config.get('unlock_flags_new_value')
    if new_value is not None:
        slots = resolve_int16_array(schema, 'unlockFlagsNew')
        written = 0
        for fmt, offset in slots:
            struct.pack_into(fmt, data, offset, new_value)
            written += 1
        changes['unlockFlagsNew'] = {'slots': written, 'value': new_value}

    report = {'schema_size': schema['size'], 'buffer_capacity': BUFFER_CAPACITY,
              'sha256': hashlib.sha256(bytes(data)).hexdigest(), 'fields': changes}
    return bytes(data), report
