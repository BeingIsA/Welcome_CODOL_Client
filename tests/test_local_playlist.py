"""核对本地无赛季玩法适配的固定镜像契约与机器码跳转边界。"""
from pathlib import Path
import struct
import unittest
import capstone
import pefile
from codol_rebuild.adapters.game.v1649370.playlist import build_local_playlist_adapter, validate_local_playlist_contract


class LocalPlaylistTests(unittest.TestCase):
    def test_original_image_contract(self):
        image=Path(__file__).resolve().parents[2]/'client/bin/v1649370/codol1649370-offline.exe'
        pe=pefile.PE(str(image));plan=build_local_playlist_adapter(0x20000000,0x21000000)
        validate_local_playlist_contract(lambda a,n:pe.get_data(a-pe.OPTIONAL_HEADER.ImageBase,n))
        expected={0x6a9ea8:bytes.fromhex('a1b013330c'),
                  0x5d0afe:b'\xe8'+struct.pack('<i',0x7625e0-0x5d0afe-5)}
        self.assertEqual({p['va']:p['before'] for p in plan['patches']},expected)
        for patch in plan['patches']:
            self.assertEqual(pe.get_data(patch['va']-pe.OPTIONAL_HEADER.ImageBase,len(patch['before'])),patch['before'])

    def test_reject_each_changed_call_site(self):
        expected={p['va']:p['before'] for p in build_local_playlist_adapter(0x20000000,0x21000000)['patches']}
        for changed in expected:
            def read(address,size):
                data=expected[address]
                return bytes([data[0]^1])+data[1:] if address==changed else data
            with self.assertRaises(ValueError):validate_local_playlist_contract(read)

    def test_branches_and_patch_targets(self):
        decoder=capstone.Cs(capstone.CS_ARCH_X86,capstone.CS_MODE_32)
        for entry in (0x20000000,0xf0000000):
            plan=build_local_playlist_adapter(entry,0x21000000)
            rows=list(decoder.disasm(plan['code'],entry));starts={i.address for i in rows}
            self.assertEqual(sum(i.size for i in rows),len(plan['code']))
            self.assertEqual(set(plan['entries']),{'setting','valid'})
            self.assertTrue(set(plan['entries'].values())<=starts)
            for ins in rows:
                if ins.mnemonic.startswith('j') or ins.mnemonic=='call':
                    self.assertIn(int(ins.op_str,16),starts|{0x6a9ead,0x7625e0})
            for patch in plan['patches']:
                self.assertEqual(len(patch['before']),len(patch['after']))
                ins=next(decoder.disasm(patch['after'],patch['va']))
                self.assertEqual(ins.mnemonic,'jmp' if patch['va']==0x6a9ea8 else 'call')
                self.assertEqual(int(ins.op_str,16),plan['entries']['setting' if patch['va']==0x6a9ea8 else 'valid'])

    def test_reject_invalid_addresses(self):
        for value in (-1,0,1,True,0x100000000):
            with self.assertRaises(ValueError):build_local_playlist_adapter(value,0x21000000)
            with self.assertRaises(ValueError):build_local_playlist_adapter(0x20000000,value)
