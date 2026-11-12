"""训练模式目录与房间身份回归；模拟内存不替代原生 UI 验收。"""
import struct
import unittest
from unittest.mock import Mock
from codol_rebuild.adapters.game.v1649370.matches.training_catalog import TrainingCatalog,text_hash
from test_match_native_bridge import MemoryProcess


class TableMemoryProcess(MemoryProcess):
    """表容量超过一页，按真实申请容量保留不重叠的测试内存。"""
    def allocate(self, length):
        address = self.next_address
        self.next_address += ((length + 4095) // 4096) * 4096
        return address


class TrainingRoomModeTests(unittest.TestCase):
    def test_catalog_preserves_existing_modes_and_pve(self):
        process=TableMemoryProcess()
        catalog=TrainingCatalog(process,Mock())
        def table(name,columns,rows,values):
            cells=b''.join(catalog.allocate_cell(values.get((row,column),b'')) for row in range(rows) for column in range(columns))
            pointer=process.allocate(len(cells));process.write(pointer,cells)
            name_pointer=process.allocate(len(name)+1);process.write(name_pointer,name+b'\0')
            header=struct.pack('<IIII',name_pointer,columns,rows,pointer)
            address=process.allocate(16);process.write(address,header)
            return address,(header,cells)
        mode_values={(row,3):str(row+1).encode() for row in range(45)}
        mode_values.update({(3,4):b'dm',(3,9):b'pvp'})
        mode_table=table(b'mp/cogametypestable.csv',24,45,mode_values)
        map_table=table(b'mp/comaplist.csv',38,130,{(0,4):b'mp_firingrange',(0,8):b'pvp',(0,9):b'dm;war',(1,4):b'zm_test',(1,8):b'zm',(1,9):b'zm',(2,4):b'za_coliseum',(2,8):b'pvp',(2,9):b'zarena'})
        catalog.find_tables=Mock(return_value={b'mp/cogametypestable.csv':mode_table,b'mp/comaplist.csv':map_table})
        catalog.install();catalog.install()
        catalog.find_tables.assert_called_once()
        header=process.read(mode_table[0],16)
        self.assertEqual(struct.unpack_from('<I',header,8)[0],46)
        pointer=struct.unpack_from('<I',header,12)[0]
        cells=process.read(pointer,46*24*8)
        self.assertEqual(cells[:45*24*8],mode_table[1][1])
        self.assertEqual(catalog.cell(cells,24,45,4),b'training')
        map_pointer=struct.unpack_from('<I',map_table[1][0],12)[0]
        map_cells=process.read(map_pointer,130*38*8)
        self.assertEqual(catalog.cell(map_cells,38,0,9),b'dm;war;training')
        self.assertEqual(map_cells[38*8:],map_table[1][1][38*8:])


if __name__=='__main__':unittest.main()
