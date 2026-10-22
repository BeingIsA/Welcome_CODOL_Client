"""按调用方指纹与容量限制，只读定位已支持的 32 位 DDL 定义对象。

本模块不访问任何进程、不启动游戏：宿主在晚期断点暂停主线程后，枚举已提交可读
内存区，把 (base, size) 列表与 read(address, size)->bytes 回调交给这里。用
version+checksum 这 8 字节唯一指纹定位候选定义对象，并区分两种命中：

- relocated：各表指针是真实地址（引擎在 zone 加载时已解析并重定位 DDL 资产），
  可直接喂给 profile_schema.capture_schema 导出字段名/类型/字节偏移；
- raw：表指针仍是磁盘占位 0xffffffff（离线未触发 DDL 解析），此时无法运行期导出，
  应回退到对已提取 .def 的静态解析。

版本适配提供经过验证的 version+checksum 指纹及档案容量；本模块不内置版本身份。
定义对象头部 16 个 32 位字的索引含义与 schema 模块保持一致。
"""
from __future__ import annotations

import struct

# 定义对象头部索引（与 profile_schema 对齐）：count 在偶数位，table 指针紧随其后。
_IDX_SIZE = 12
_TABLES = (  # (label, count_index, pointer_index)
    ('enums', 2, 3),
    ('structs', 4, 5),
    ('indexed_arrays', 6, 7),
    ('enumed_arrays', 8, 9),
    ('offsets', 13, 14),
)
_UNRELOCATED = 0xFFFFFFFF
_HEADER_WORDS = 16

# 分块读取，兼容单次 ReadProcessMemory 无法吞下的大区；重叠避免跨块漏掉指纹。
_CHUNK = 1 << 22


def fingerprint(version: int, checksum: int) -> bytes:
    """由 DDL 版本号与校验和构造 8 字节定位指纹（小端，紧邻头部字 0/1）。"""
    return struct.pack('<II', version, checksum)


def find_fingerprints(read, regions, needles: dict) -> dict:
    """在给定内存区里查每个指纹的全部命中地址；只读，命中按地址升序去重。

    regions 为 (base, size) 可迭代对象，调用方须保证这些区已提交且可读。
    read 在读取失败时抛 OSError；本函数按块读取，单块不可读则跳过该块继续。
    返回 {label: [address, ...]}。
    """
    found = {label: set() for label in needles}
    for base, size in regions:
        if size <= 0:
            continue
        offset = 0
        while offset < size:
            length = min(_CHUNK, size - offset)
            try:
                block = read(base + offset, length)
            except OSError:
                offset += length
                continue
            for label, needle in needles.items():
                if not needle:
                    continue
                start = 0
                while True:
                    hit = block.find(needle, start)
                    if hit < 0:
                        break
                    found[label].add(base + offset + hit)
                    start = hit + 1
            # 回退 len(needle)-1 字节，保证跨界指纹不被切断。
            step = length - (max(len(n) for n in needles.values()) - 1)
            offset += step if step > 0 else length
    return {label: sorted(addresses) for label, addresses in found.items()}


def classify_definition(read, address: int, *, max_profile_size: int) -> dict:
    """判定命中处是否为合法定义对象，并区分已重定位与磁盘原始形态。

    relocated 为真表示所有 count>0 的表指针都是可信地址，可交给 capture_schema；
    只要有一张非空表指针仍是 0xffffffff，即为 raw，运行期无法导出。
    max_profile_size 由版本适配提供，用于判断定义对象的大小是否可接受。
    """
    info = {'address': address}
    try:
        header = struct.unpack('<' + 'I' * _HEADER_WORDS, read(address, 4 * _HEADER_WORDS))
    except OSError as exc:
        info.update(readable=False, error=str(exc), plausible=False, relocated=False)
        return info
    version, checksum, size = header[0], header[1], header[_IDX_SIZE]
    info.update(readable=True, version=version, checksum=checksum, size=size)
    counts = {label: header[ci] for label, ci, _ in _TABLES}
    pointers = {label: header[pi] for label, _, pi in _TABLES}
    info['counts'] = counts
    info['pointers'] = {label: hex(value) for label, value in pointers.items()}
    size_ok = 0 < size <= max_profile_size
    counts_ok = all(counts[label] < 4096 for label, _, _ in _TABLES if label != 'offsets') \
        and counts['offsets'] < 65536
    raw_tables = [label for label, ci, pi in _TABLES
                  if header[ci] > 0 and header[pi] == _UNRELOCATED]
    bad_tables = [label for label, ci, pi in _TABLES
                  if header[ci] > 0 and header[pi] != _UNRELOCATED
                  and not 0x10000 < header[pi] < 0x100000000]
    relocated = size_ok and counts_ok and not raw_tables and not bad_tables
    info.update(plausible=size_ok and counts_ok, raw_tables=raw_tables,
                bad_tables=bad_tables, relocated=relocated)
    return info


def select_definition(read, addresses, *, max_profile_size: int) -> dict:
    """在一组命中里挑出可用于导出的已重定位定义对象；没有则如实报告形态。

    返回 {'selected': address|None, 'candidates': [classify_definition, ...]}，
    宿主据 selected 决定是否调用 capture_schema，据 candidates 记录证据；
    max_profile_size 原样传给每个候选对象的大小检查。
    """
    candidates = [classify_definition(read, address, max_profile_size=max_profile_size) for address in addresses]
    relocated = [candidate for candidate in candidates if candidate.get('relocated')]
    selected = relocated[0]['address'] if relocated else None
    return {'selected': selected, 'candidates': candidates}
