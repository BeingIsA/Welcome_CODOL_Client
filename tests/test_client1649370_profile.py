"""回归验证：检查 client1649370_profile 的行为与边界，不作为客户端实测结果。"""
import struct
import unittest
from pathlib import Path
import pefile
from capstone import Cs, CS_ARCH_X86, CS_MODE_32
from codol_rebuild.adapters.game.v1649370.accounts.profile import build_initializer,inventory_relocations,CAPACITY,STRIDE,populate_inventory,publish_empty_local_store

ROOT=Path(__file__).resolve().parents[2]


class BridgeTests(unittest.TestCase):
    def test_local_welcome_patch_preserves_gfx_boolean_cleanup_and_callback_abi(self):
        from codol_rebuild.adapters.game.v1649370.frontend.local_ui_services import local_welcome_page_patch
        pe=pefile.PE(str(ROOT/'client/bin/v1649370/codol1649370-offline.exe'))
        # Verify the registered callback and its entire fixed-build body, not
        # merely that six plausible bytes happen to occur at the patch site.
        self.assertEqual(struct.unpack('<I',pe.get_data(0xDC823C-0x400000,4))[0],0x5CC4F0)
        original=pe.get_data(0x5CC4F0-0x400000,0x41)
        self.assertEqual(original,bytes.fromhex(
            '558bec8b4508538a1d28d74a08568b308b4e0cc1e906f6c1017416'
            '8b4e088b46108b118b52085056ffd2c7460800000000885e10'
            'c7460c020000005e5b5dc20400'))
        va,before,after=local_welcome_page_patch()
        offset=va-0x5CC4F0
        self.assertEqual(original[offset:offset+len(before)],before)
        self.assertEqual(len(before),len(after))
        patched=original[:offset]+after+original[offset+len(before):]
        decoder=Cs(CS_ARCH_X86,CS_MODE_32)
        rows=list(decoder.disasm(patched,0x5CC4F0))
        self.assertEqual(sum(r.size for r in rows),len(original))
        self.assertEqual([(r.mnemonic,r.op_str) for r in decoder.disasm(after,va)],
                         [('mov','bl, 1')]+[('nop','')]*4)
        # All instructions outside the replacement keep their exact addresses
        # and bytes, including cleanup's branch target and the Boolean store.
        unchanged=lambda body:[(r.address,bytes(r.bytes)) for r in decoder.disasm(body,0x5CC4F0)
                               if not va<=r.address<va+len(before)]
        self.assertEqual(unchanged(patched),unchanged(original))
        self.assertEqual([(r.mnemonic,r.op_str) for r in rows[-6:]],
                         [('mov','byte ptr [esi + 0x10], bl'),
                          ('mov','dword ptr [esi + 0xc], 2'),
                          ('pop','esi'),('pop','ebx'),('pop','ebp'),('ret','4')])

    def test_optional_credit_request_keeps_void_callback_stack_cleanup(self):
        from codol_rebuild.adapters.game.v1649370.frontend.local_ui_services import local_optional_service_patches
        pe=pefile.PE(str(ROOT/'client/bin/v1649370/codol1649370-offline.exe'))
        patches=local_optional_service_patches()
        self.assertEqual(len(patches),1)
        va,before,after=patches[0]
        original=pe.get_data(va-0x400000,8)
        self.assertEqual(original[:5],before)
        rows=list(Cs(CS_ARCH_X86,CS_MODE_32).disasm(after+original[5:],va))
        self.assertEqual([(r.mnemonic,r.op_str) for r in rows],[('nop','')]*5+[('ret','4')])

    def test_stats_provider_is_scoped_and_fail_closed(self):
        from codol_rebuild.adapters.game.v1649370.accounts.profile import build_stats_ready_adapter
        entry,data=0x20000c00,0x21000000
        plan=build_stats_ready_adapter(entry,data)
        pe=pefile.PE(str(ROOT/'client/bin/v1649370/codol1649370-offline.exe'))
        self.assertEqual(pe.get_data(plan['patch_va']-0x400000,5),plan['before'])
        # Leave the native ui_mp check and GFx cleanup/Boolean conversion intact.
        self.assertEqual(pe.get_data(0x67EB0E-0x400000,5),bytes.fromhex('68585fdb00'))
        rows=list(Cs(CS_ARCH_X86,CS_MODE_32).disasm(plan['code'],entry))
        self.assertEqual(sum(r.size for r in rows),len(plan['code']))
        self.assertEqual([int(r.op_str,16) for r in rows if r.mnemonic=='jne'],[rows[-1].address]*2)
        patch=list(Cs(CS_ARCH_X86,CS_MODE_32).disasm(plan['after'],plan['patch_va']))
        self.assertEqual(patch[0].op_str,hex(entry))
        # Execute the small predicate's supported instruction set for both
        # provider publication and native inventory state, including invalid 2.
        for published in (0,1,2):
            for inventory in (0,1,2):
                memory={data+72:published,0x82C4A86:inventory}
                eax=0;equal=False;pc=0
                while pc<len(rows):
                    r=rows[pc]
                    if r.mnemonic=='xor':eax=0
                    elif r.mnemonic=='cmp':
                        address=int(r.op_str.split('[')[1].split(']')[0],16)
                        equal=memory[address]==1
                    elif r.mnemonic=='jne' and not equal:
                        pc=next(i for i,x in enumerate(rows) if x.address==int(r.op_str,16));continue
                    elif r.mnemonic=='inc':eax+=1
                    elif r.mnemonic=='ret':break
                    pc+=1
                self.assertEqual(eax,int(published==1 and inventory==1))

    def test_view_results_use_frame_params_and_preserve_gfx_type(self):
        from codol_rebuild.adapters.game.v1649370.frontend.viewer import view_result
        raw=bytearray(32)
        struct.pack_into('<III',raw,8,0x123400,0x48,0x567800)
        memory={(0x1008,4):struct.pack('<I',0x2000),
                (0x2000,4):struct.pack('<I',0x3000),(0x3000,32):bytes(raw)}
        self.assertEqual(view_result(lambda a,n:memory[a,n],0x1000),
                         {'type':0x48,'base_type':8,'interface':'0x123400','payload':'0x567800'})

    def test_view_result_points_match_native_frame_lifetime(self):
        pe=pefile.PE(str(ROOT/'client/bin/v1649370/codol1649370-offline.exe'))
        for a,expected in [(0x6B9DE4,'8b7f0c'),(0x6B9E2D,'5f5e5d'),
                           (0x7DB8F8,'8b7d08'),(0x7DB933,'83c408'),
                           (0x5CC9E3,'a138f72708')]:
            data=bytes.fromhex(expected)
            self.assertEqual(pe.get_data(a-0x400000,len(data)),data,hex(a))

    def test_local_fetch_completion_preserves_inventory_and_other_bits(self):
        memory={0x82C4A8C:21738,0x82C4AA4:0x81}
        writes=[]
        def read(a,n):
            return b'\x01' if a==0x82C4A86 else struct.pack('<I',memory.get(a,0))
        def write(a,b):
            writes.append(a);memory[a]=struct.unpack('<I',b)[0]
        result=publish_empty_local_store(read,write,21738)
        self.assertEqual(memory[0x82C4AA4],0x87)
        self.assertEqual(memory[0x82C4A8C],21738)
        self.assertFalse(result['purchases_supported'])
        self.assertEqual(set(writes),{0x82C4AEC,0x82C4A88,0x82C4AA4})
        for address,value in [(0x82C4A90,1),(0x82C4A94,1),(0x82C4AA0,1),(0x82C4A88,1),(0x82C4A8C,0)]:
            old=memory.get(address,0);memory[address]=value;writes.clear()
            with self.assertRaises(ValueError):publish_empty_local_store(read,write,21738)
            self.assertEqual(writes,[])
            memory[address]=old

    def test_initial_fetch_predicate_and_native_completion_match_image(self):
        pe=pefile.PE(str(ROOT/'client/bin/v1649370/codol1649370-offline.exe'))
        for a,expected in [(0x5CCA33,'a1a44a2c08'),(0x5CCA3C,'83e007'),
                           (0x5CCA45,'3c07'),(0x4A7E29,'830da44a2c0806'),
                           (0x4A7E37,'c705884a2c0803000000')]:
            data=bytes.fromhex(expected)
            self.assertEqual(pe.get_data(a-0x400000,len(data)),data,hex(a))

    def test_constructor_loop_and_abi(self):
        entry,data,pool=0x20000000,0x21000000,0x22000000
        bridge=build_initializer(entry,data,pool)
        instructions=list(Cs(CS_ARCH_X86,CS_MODE_32).disasm(bridge['code'],entry))
        rows=[(i.mnemonic,i.op_str) for i in instructions]
        calls=[int(i.op_str,16) for i in instructions if i.mnemonic=='call']
        self.assertEqual(calls,[0x49CF30]*8+[0xB8CE80,0x5E66F0,0x80D770,0x50C1A0,0x4A9260,0x6B71E0,0x6B71B0])
        selection=next(i for i,x in enumerate(instructions) if x.mnemonic=='call' and int(x.op_str,16)==0x6B71E0)
        self.assertEqual(rows[selection-1],('push','1'))
        self.assertEqual(rows[selection+1:selection+3],[('add','esp, 4'),('xor','eax, eax')])
        self.assertEqual(rows[selection+4],('mov','dword ptr [0x21000044], eax'))
        self.assertEqual(rows[:4],[('push','ebp'),('mov','ebp, esp'),('pushal',''),('and','esp, 0xfffffff0')])
        self.assertEqual(rows[-5:],[('lea','esp, [ebp - 0x20]'),('popal',''),('mov','esp, ebp'),('pop','ebp'),('ret','')])
        index=calls.index(0xB8CE80)
        native=next(i for i,x in enumerate(instructions) if x.mnemonic=='call' and int(x.op_str,16)==0xB8CE80)
        self.assertEqual(rows[native-4:native],[('mov','esi, 0x22000000'),('mov','edi, 0x5dc0'),('push','1'),('mov','ecx, esi')])
        self.assertEqual(rows[native+1:native+3],[('add','esi, 0xb8'),('dec','edi')])
        self.assertEqual(int(instructions[native+3].op_str,16),instructions[native-2].address)
        # RET 4 constructor pops its own single argument; lookup and publish are cdecl.
        self.assertEqual(sum(r==('add','esp, 0xc') for r in rows),8)
        self.assertIn(('add','esp, 8'),rows)
        self.assertEqual(sum(i.mnemonic=='int3' for i in instructions),2)
        self.assertEqual(sum(i.size for i in instructions),len(bridge['code']))

    def test_relocations_match_pinned_image(self):
        pe=pefile.PE(str(ROOT/'client/bin/v1649370/codol1649370-offline.exe'))
        for address,before,after in inventory_relocations(0x20000000):
            self.assertEqual(pe.get_data(address-0x400000,len(before)),before,hex(address))
            self.assertEqual(len(before),len(after))

    def test_population_keeps_native_object_members(self):
        raw=bytearray(CAPACITY*STRIDE)
        for i in range(CAPACITY):struct.pack_into('<I',raw,i*STRIDE,0xD59C78)
        raw[16]=123
        def read(a,n):return bytes(raw[a:a+n])
        def write(a,b):raw[a:a+len(b)]=b
        rows=[{'id':10001,'type_bytes':[0,0,0,0]},{'id':545,'type_bytes':[0,2,0,0]}]
        info=populate_inventory(read,write,0,rows,121230)
        self.assertEqual(info['items'],2);self.assertEqual(raw[16],123)
        self.assertEqual(struct.unpack_from('<III',raw,0x40),(10001,1,121230))
        self.assertEqual(struct.unpack_from('<III',raw,STRIDE+0x40),(545,1,0))
