"""固定版本启动观察断点：由运行入口与独立诊断共享。

Fixed-build startup observation points shared by launcher and diagnostics.
"""
import struct


BREAKPOINTS = (
    '0x4fd9b0:Com Init',
    '0x4fcc00:Initial fastfiles',
    '0x7ef0d0:Frontend tick',
    '0x77f8b8:Engine init returned',
    '0x4706d0:Switch to main menu',
    '0x4f82e0:Com Error',
    '0x77c930:Fatal error',
)


def capture_loadout_fault(process, row):
    """在已知终版武器读异常时，记录查询编号、定义表登记情况与动态配置。"""
    if (row.get('exception') != '0xc0000005' or row.get('address') != '0x4287eb'
            or row.get('parameters', [])[:2] != ['0x0', '0xee1']):
        return

    def read_unsigned(address):
        return struct.unpack('<I', process.read(address, 4))[0]

    sample = row['loadout_crash'] = {}
    try:
        registers = row['registers']
        caller = read_unsigned(int(registers['esp'], 16))
        sample['caller'] = hex(caller)
        if caller != 0x5b19d6:
            return
        weapon_index = read_unsigned(int(registers['edi'], 16) + 0x404)
        sample['weapon_index'] = weapon_index
        sample['last_registered_weapon_index'] = read_unsigned(0x827f738)
        if 0 < weapon_index < 1700:
            sample['definition'] = hex(read_unsigned(0x1988b50 + 4 * weapon_index))
            config_index = 0x1748 + weapon_index
            offset = read_unsigned(0xc86dd28 + 4 * config_index)
            if offset == 0:
                sample['dynamic_config'] = None
                return
            pool_size = read_unsigned(0xc89a48c)
            if not 0 < offset < pool_size <= 0x20000:
                raise ValueError('动态配置偏移超出已用字符串池')
            content = process.read(0xc87a48c + offset, min(512, pool_size - offset))
            text, terminator, _ = content.partition(b'\0')
            sample['dynamic_config'] = text.decode('ascii', errors='replace')
            sample['terminated'] = bool(terminator)
    except (KeyError, OSError, ValueError, struct.error) as error:
        sample['read_error'] = str(error)
