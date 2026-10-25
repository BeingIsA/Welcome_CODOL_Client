"""房间桥固定版本及跳转完整性验证；实际界面由用户手动验收。"""
from pathlib import Path
import unittest
import struct
import capstone
import pefile
from codol_rebuild.adapters.game.v1649370.room import build_room_adapter,validate_room_contract


class RoomTests(unittest.TestCase):
    def test_join_error_codes_match_native_localization(self):
        """核对固定镜像跳转表，避免把服务端错误映射到错误的原生文案。"""
        image = Path(__file__).resolve().parents[1]/'bin/v1649370/codol1649370-offline.exe'
        pe = pefile.PE(str(image))
        base = pe.OPTIONAL_HEADER.ImageBase
        for code, key in ((3, b'@SF_UI_ERROR_ROOM_NONEXISTENT'),
                          (5, b'@SF_UI_ERROR_ROOM_WRONG_PASSWORD')):
            index = pe.get_data(0x7314FC-base+code, 1)[0]
            target = struct.unpack('<I', pe.get_data(0x7314B8-base+index*4, 4))[0]
            self.assertEqual(pe.get_data(target-base, 1), b'\xb8')
            text = struct.unpack('<I', pe.get_data(target-base+1, 4))[0]
            self.assertEqual(pe.get_string_at_rva(text-base), key)

    def test_join_image_contract_and_generated_branches(self):
        from codol_rebuild.adapters.game.v1649370.room import build_join_adapter, validate_join_contract
        image = Path(__file__).resolve().parents[1]/'bin/v1649370/codol1649370-offline.exe'
        pe = pefile.PE(str(image))
        validate_join_contract(lambda a, n: pe.get_data(a-0x400000, n))
        with self.assertRaises(ValueError):
            validate_join_contract(lambda a, n: bytes(n))
        for entry in (0x20000000, 0xF0000000):
            plan = build_join_adapter(entry, 0x21000000, 0x22000000)
            rows = list(capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32).disasm(plan['code'], entry))
            self.assertEqual(sum(i.size for i in rows), len(plan['code']))
            starts = {i.address for i in rows}
            for instruction in rows:
                if (instruction.mnemonic.startswith('j') or instruction.mnemonic == 'call') and instruction.op_str.startswith('0x'):
                    self.assertIn(int(instruction.op_str, 16), starts | {0xC3F316, 0x7E7910, 0xB75B10, 0x623770, 0x62AF10, 0x6305F0, 0x630480, 0x731440})
            # 新增补丁仅覆盖入房失败文本转换的五字节 CALL。
            self.assertEqual([patch['va'] for patch in plan['patches']], [0x5D3216, 0x7C46EB])
            for patch in plan['patches']:
                self.assertEqual(pe.get_data(patch['va']-0x400000, 5), patch['before'])
                self.assertEqual(len(patch['after']), 5)

    def test_image_contract(self):
        image=Path(__file__).resolve().parents[2]/'client/bin/v1649370/codol1649370-offline.exe'
        pe=pefile.PE(str(image))
        validate_room_contract(lambda a,n:pe.get_data(a-0x400000,n))

    def test_reject_wrong_image(self):
        with self.assertRaises(ValueError):
            validate_room_contract(lambda a,n:bytes(n))

    def test_all_branches_land_on_instructions(self):
        decoder=capstone.Cs(capstone.CS_ARCH_X86,capstone.CS_MODE_32)
        for entry in (0x20000000,0xf0000000):
            plan=build_room_adapter(entry,0x21000000,0x22000000)
            rows=list(decoder.disasm(plan['code'],entry))
            self.assertEqual(sum(i.size for i in rows),len(plan['code']))
            starts={i.address for i in rows}
            for i in rows:
                if i.mnemonic.startswith('j') or i.mnemonic=='call':
                    self.assertIn(int(i.op_str,16),starts|{0x633fe0,0x801600,0x7e7e30,0x62fde6,0x63e620,0x62f640,0x62af10,0x67ec54,0x7cad60,0x7c64c0,0x7c6581,0x7c64b5,0x77ffb0})
            for p in plan['patches']:
                self.assertEqual(len(p['before']),len(p['after']))
                target=int(next(decoder.disasm(p['after'],p['va'])).op_str,16)
                self.assertIn(target,plan['entries'].values())
