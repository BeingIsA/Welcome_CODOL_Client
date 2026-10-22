"""验证 1649370 启动命令白名单，分别生成运行补丁清单与分析报告；不执行游戏。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct

import capstone
import pefile

ROOT = Path(__file__).resolve().parents[1]
IMAGE_SHA = '55f067ac24e7d5ea9bea5acd46a1514eb1b7780a627b73c2094f41133dce3ffd'


def main() -> None:
    image = ROOT / 'bin/v1649370/codol1649370-offline.exe'
    patch_path = ROOT / 'resources/v1649370/offline-menu-patches.json'
    report_path = ROOT.parent / 'research/client/evidence/startup-command-whitelist.json'
    if patch_path.exists() or report_path.exists():
        raise ValueError('输出已存在，拒绝覆盖补丁或证据')
    raw = image.read_bytes()
    if hashlib.sha256(raw).hexdigest() != IMAGE_SHA:
        raise ValueError('诊断镜像指纹不符')
    pe = pefile.PE(data=raw)
    if pe.OPTIONAL_HEADER.ImageBase != 0x400000 or pe.FILE_HEADER.Machine != 0x14c:
        raise ValueError('PE 基址或架构不符')

    def offset(va):
        return pe.get_offset_from_rva(va-0x400000)

    def read(va, size):
        return raw[offset(va):offset(va)+size]

    def string(va):
        value = read(va, 128)
        if b'\0' not in value:raise ValueError('字符串未结束')
        return value.split(b'\0', 1)[0].decode('ascii')

    def occurrences(value):
        found = []; cursor = 0
        while (cursor := raw.find(value, cursor)) >= 0:
            found.append(hex(pe.get_rva_from_offset(cursor)+0x400000)); cursor += 1
        return found

    assert occurrences(b'set 2439') == []
    assert occurrences(b'set 2451') == ['0xdbccf8']
    assert occurrences(b'switchtomainmenu') == ['0xdc084c']
    # 检查整个字符串及其 NUL 的每个内部字节地址，避免漏掉指向数字子串的绝对引用。
    references = {hex(va): occurrences(struct.pack('<I', va)) for va in range(0xdbccf8, 0xdbcd01)}
    assert references['0xdbccf8'] == ['0xe579f8']
    assert all(not rows for va, rows in references.items() if va != '0xdbccf8')
    assert string(0xdc084c) == 'switchtomainmenu'
    assert read(0xe579f0, 4) == struct.pack('<I', 0xdbcd18)
    assert read(0xdbccf8, 9) == b'set 2451\0'
    assert read(0x4f8840, 3) == bytes.fromhex('c60700')
    table = [{'slot':hex(va),'pointer':hex(struct.unpack('<I',read(va,4))[0]),
              'text':string(struct.unpack('<I',read(va,4))[0])}
             for va in range(0xe579ec,0xe57a08,4)]
    assert [row['text'] for row in table] == ['connect_lobby ', 'set dw_username ',
        'set r_displayMode ', 'set 2451', 'set 2624', 'set 938', 'set 1436']
    new_table = [row['text'] for row in table]
    new_table[1] = 'switchtomainmenu'; new_table[3] = 'set 2439'

    # 对照 0x7745e0 的有界 ASCII 忽略大小写比较；这里只验证下面有限命令样例。
    def compare(a, b, limit):
        for i in range(limit):
            ca = ord(a[i].lower()) if i < len(a) else 0
            cb = ord(b[i].lower()) if i < len(b) else 0
            if ca != cb:return False
            if ca == 0:return True
        return True

    def allowed(command, entries):
        for entry in entries:
            if compare(command,entry,len(entry)):return True
            if compare(command,'set ',4):
                end = spaces = 0
                while end < len(command) and spaces < 2:
                    spaces += command[end] == ' ';end += 1
                if compare(command,entry,end):return True
        return False

    checks = [{'command':text,'original_allowed':allowed(text,[r['text'] for r in table]),
               'patched_allowed':allowed(text,new_table)} for text in
              ('set 2439 0','switchtomainmenu','set logfile 2','set r_fullscreen 0',
               'set r_mode 800x600','set dw_username user','set 2451 1','set r_displayMode 0')]
    assert [(x['original_allowed'],x['patched_allowed']) for x in checks] == [
        (False,True),(False,True),(False,False),(False,False),
        (False,False),(True,False),(True,False),(True,True)]
    original_patches = ROOT.parent / 'research/client/evidence/offline-integration-patches.json'
    patches = json.loads(original_patches.read_text(encoding='utf-8'))
    assert [row['va'] for row in patches] == ['0x4fdadc','0x77eea5','0x7e993d']
    patches.extend([
        {'va':'0xe579f0','before':read(0xe579f0,4).hex(),
         'after':struct.pack('<I',0xdc084c).hex(),
         'name':'离线启动白名单以原生菜单命令替换在线用户名参数',
         'diagnostic_only':True,
         'scope':'只替换一个白名单指针，保留原生过滤器及 switchtomainmenu 处理器；不伪造 UI 就绪。'},
        {'va':'0xdbccf8','before':b'set 2451'.hex(),'after':b'set 2439'.hex(),
         'name':'离线启动白名单等长允许2439原生dvar设置',
         'diagnostic_only':True,
         'scope':'只替换白名单独占字符串，保留 set/注册/类型转换；不直接写入登录状态或dvar值。'}])
    candidate = bytearray(raw); changed = set()
    for patch in patches:
        at = offset(int(patch['va'],0)); before=bytes.fromhex(patch['before']);after=bytes.fromhex(patch['after'])
        assert len(before)==len(after) and raw[at:at+len(before)]==before
        area = set(range(at,at+len(before)))
        assert not changed.intersection(area)
        candidate[at:at+len(before)]=after;changed.update(area)
    assert all(a==b or i in changed for i,(a,b) in enumerate(zip(raw,candidate)))
    windows = [(0x77ee64,0x77ee71,'WinMain third parameter to EDI'),
               (0x77eef1,0x77ef3c,'copy command line to fixed buffer'),
               (0x77f5d7,0x77f61e,'q argument sets launcher flag'),
               (0x77f6e8,0x77f727,'q or qa passes launcher gate'),
               (0x77f8b1,0x77f8b8,'call no-argument engine wrapper'),
               (0x4fd9d5,0x4fda08,'wrapper uses fixed buffer then ECX'),
               (0x4fcef6,0x4fcf1a,'Com_Init copies and parses ECX input'),
               (0x4f86e0,0x4f8845,'plus splitting and seven-entry whitelist'),
               (0x7745e0,0x774638,'ASCII case-insensitive bounded comparison')]
    decoder = capstone.Cs(capstone.CS_ARCH_X86,capstone.CS_MODE_32)
    decoded = []
    for start,end,name in windows:
        code=read(start,end-start);rows=list(decoder.disasm(code,start))
        assert sum(r.size for r in rows)==len(code)
        decoded.append({'name':name,'va':hex(start),'bytes':code.hex(),
                        'instructions':[{'va':hex(r.address),'bytes':r.bytes.hex(),
                                         'mnemonic':r.mnemonic,'operands':r.op_str} for r in rows]})
    report = {'static_only':True,'game_executed':False,'image_written':False,
              'input_image':{'path':str(image),'sha256':IMAGE_SHA},
              'input_patches_sha256':hashlib.sha256(original_patches.read_bytes()).hexdigest(),
              'original_whitelist':table,'patched_whitelist':new_table,
              'set2451_absolute_address_references':references,
              'finite_filter_checks':checks,'instruction_windows':decoded,
              'simulated_patched_image_sha256':hashlib.sha256(candidate).hexdigest(),
              'outside_patch_ranges_unchanged':True,'new_patch_bytes':12,
              'runtime_verification_needed':['在0x4f86e0前读取EAX输入；返回后读0x33e9990与0x337eac8指针表。',
                  '原生注册之后及引擎初始化返回时读取2439值；确认switchtomainmenu实际命中及UI显示。'],
              'limits':['绝对引用扫描覆盖文件中所有DWORD地址；不声称穷尽运行时计算指针。',
                  'logfile/r_fullscreen/r_mode仍被原白名单过滤；不猜r_displayMode枚举。',
                  '不是-q清空整个命令行；过滤证据独立于-q入口校验。',
                  '补丁不设登录成功，不写ready，不移除全量命令过滤，尚未实测主菜单。']}
    report['patches'] = patches
    runtime_patches = [
        {key: patch[key] for key in ('va', 'before', 'after')}
        for patch in patches
    ]
    for path,value in ((patch_path,runtime_patches),(report_path,report)):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('x',encoding='utf-8') as stream:
            json.dump(value,stream,ensure_ascii=False,indent=2);stream.write('\n')
    print(json.dumps({'patches':str(patch_path),'report':str(report_path),'total_patches':len(patches),
                      'new_patch_bytes':12,'game_executed':False},ensure_ascii=False))


if __name__ == '__main__':
    main()
