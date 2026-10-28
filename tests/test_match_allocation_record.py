"""终版分配记录字段边界及原生地址编码回归；不代替原生运行验收。"""
import sys
import struct
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from codol_rebuild.adapters.game.v1649370.matches.allocation_record import allocation_fields

class AllocationRecordTests(unittest.TestCase):
    def test_native_layout(self):
        fields = dict(allocation_fields('127.0.0.2', 27760, bytes(range(8)), bytes(range(16)), 0x123456789, 'Local DS', 12))
        self.assertEqual(fields[0x11C], struct.pack('<III', 0, 12, 0))
        self.assertEqual(fields[0x18][:6], b'\x7f\x00\x00\x02' + struct.pack('<H',27760))
        self.assertEqual(fields[0x18][60:66], fields[0x18][:6])
        self.assertEqual(fields[0x118], struct.pack('<I',67))
        self.assertEqual(fields[0x168], struct.pack('<Q',0x123456789))
        self.assertEqual(fields[0x1CC], bytes(range(16)))
        ranges = sorted((offset, offset+len(value)) for offset,value in fields.items())
        self.assertTrue(all(end<=next_start for (_,end),(next_start,_) in zip(ranges,ranges[1:])))
        self.assertTrue(all(start>=0x10 and end<=0x22C for start,end in ranges))

    def test_invalid_key(self):
        with self.assertRaises(ValueError):
            allocation_fields('127.0.0.2',27760,b'',bytes(16),1,'Local DS', 12)
