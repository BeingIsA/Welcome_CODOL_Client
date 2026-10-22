"""1649370 无赛季玩法适配：校验原生 ABI 并生成设置与有效性查询补丁。

只返回机器码和补丁计划；不访问进程，不发布赛季对象或修改原生玩法配置。
"""
from __future__ import annotations

import struct

LOCAL_PLAYLIST_CONTRACT = {
    0x6A9EA8: bytes.fromhex('a1b013330c'),  # UI WarGod setting: load playlist ID.
    0x5D0AFE: bytes.fromhex('e8dd1a1900'),  # AS_PlayListValid -> native predicate.
}


def validate_local_playlist_contract(read):
    """只适配已核对的设置读取和 UI 有效性查询调用边。"""
    for address, expected in LOCAL_PLAYLIST_CONTRACT.items():
        if read(address, len(expected)) != expected:
            raise ValueError(f'本地玩法数据 ABI 不匹配：{address:#x}')


def build_local_playlist_adapter(entry, ready):
    """无赛季的本地 UI 使用无效玩法 ID，避免默认0匹配空赛季。

    原版匹配条比较房间玩法与 WarGod 配置玩法，再读取赛季名称。无赛季且
    配置仍为0时仅调整 UI getter 的返回值，不写原生配置或构造赛季对象。
    AS 会缓存该设置；唯一把它传回原生的 AS_PlayListValid 同时拒绝本地-1，
    即使随后玩法表就绪，也不会让它进入没有下标检查的原生查询。
    """
    if any(type(a) is not int or not 0x10000 <= a <= 0xffffe000 for a in (entry, ready)):
        raise ValueError('本地玩法适配地址必须为有效32位地址')
    code, labels, refs = bytearray(), {}, []

    def emit(value):
        code.extend(bytes.fromhex(value))

    def word(value):
        code.extend(struct.pack('<I', value))

    def label(name):
        labels[name] = len(code)

    def branch(op, name):
        emit(op); refs.append((len(code), name)); word(0)

    def jump(address):
        emit('e9'); word((address-entry-len(code)-4) & 0xffffffff)

    def offline(fallback):
        emit('833d'); word(ready); emit('01'); branch('0f85', fallback)
        emit('a1'); word(0x849B874); emit('85c0'); branch('0f84', fallback)
        emit('83780c00'); branch('0f85', fallback)

    label('setting')
    emit('9c60')
    # 重放原始读取并先保存返回值；所有回退均恢复这个原生 ID。
    code.extend(LOCAL_PLAYLIST_CONTRACT[0x6A9EA8])
    emit('8944241c')
    offline('setting_done')
    emit('833d'); word(0xC3313A0); emit('00'); branch('0f85', 'setting_done')
    emit('837c241c00'); branch('0f85', 'setting_done')
    emit('c744241cffffffff')
    label('setting_done'); emit('619d'); jump(0x6A9EAD)

    label('valid')
    emit('9c60')
    offline('valid_original')
    # ECX 是玩法 ID，原查询无栈参数。只拦截本地供应的无效哨兵。
    emit('83f9ff'); branch('0f85', 'valid_original')
    emit('c744241c00000000 619d c3')
    label('valid_original'); emit('619d'); jump(0x7625E0)

    for position, name in refs:
        struct.pack_into('<i', code, position, labels[name]-position-4)
    patches = []
    for address, name, opcode in ((0x6A9EA8, 'setting', 0xe9), (0x5D0AFE, 'valid', 0xe8)):
        after = bytes([opcode])+struct.pack('<I', (entry+labels[name]-address-5) & 0xffffffff)
        patches.append({'va':address, 'before':LOCAL_PLAYLIST_CONTRACT[address], 'after':after})
    return {'code':bytes(code), 'patches':patches,
            'entries':{name:entry+labels[name] for name in ('setting', 'valid')}}
