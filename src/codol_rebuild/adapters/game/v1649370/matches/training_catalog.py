"""终版训练模式资源登记：在主电影解析前扩展原生模式与 PVP 地图表。"""
import struct


def text_hash(value):
    """沿用原生 StringTable 的小写有符号字符哈希。"""
    result = 0
    for character in value.lower():
        result = (result * 31 + (character if character < 128 else character - 256)) & 0xffffffff
    return result


class TrainingCatalog:
    """仅主线程资源加载暂停点使用；所有新增内存与游戏进程同寿命。"""
    def __init__(self, process, record):
        self.process = process
        self.record = record
        self.installed = False

    def string(self, pointer):
        characters = bytearray()
        for offset in range(2048):
            character = self.process.read(pointer + offset, 1)
            if character == b'\0':
                return bytes(characters)
            characters.extend(character)
        raise ValueError('Unterminated training resource string')

    def find_tables(self):
        """按固定表形状定位，并核对表名；不把仅含同名字符串的内存当成表。"""
        expected = {b'mp/cogametypestable.csv': (24, 45), b'mp/comaplist.csv': (38, 130)}
        found = {name: {} for name in expected}
        for base, size in self.process.regions():
            for offset in range(0, size, 1024 * 1024):
                try:
                    block = self.process.read(base + offset, min(size-offset, 1024*1024+16))
                except OSError:
                    continue
                for name, shape in expected.items():
                    marker = struct.pack('<II', *shape)
                    position = 0
                    while (position := block.find(marker, position)) >= 0:
                        address = base + offset + position - 4
                        position += 1
                        try:
                            header = self.process.read(address, 16)
                            name_pointer, columns, rows, cells_pointer = struct.unpack('<IIII', header)
                            if self.string(name_pointer).lower() != name:
                                continue
                            cells = self.process.read(cells_pointer, columns*rows*8)
                            found[name][address] = (header, cells)
                        except (OSError, ValueError):
                            continue
        if any(len(matches) != 1 for matches in found.values()):
            raise ValueError('Training native table location is not unique: '+str({name.decode():len(matches) for name,matches in found.items()}))
        return {name: next(iter(matches.items())) for name,matches in found.items()}

    def cell(self, cells, columns, row, column):
        pointer, stored_hash = struct.unpack_from('<II', cells, (row*columns+column)*8)
        value = self.string(pointer) if pointer else b''
        if stored_hash != text_hash(value):
            raise ValueError('Training native table cell hash mismatch')
        return value

    def allocate_cell(self, value):
        pointer = self.process.allocate(len(value)+1)
        self.process.write(pointer, value+b'\0')
        return struct.pack('<II', pointer, text_hash(value))

    def install(self):
        """发布完整新模式表，再按单元格扩展 PVP 模式关联；原有模式行保持不变。"""
        if self.installed:
            return
        tables = self.find_tables()
        mode_address, (mode_header, mode_cells) = tables[b'mp/cogametypestable.csv']
        map_address, (map_header, map_cells) = tables[b'mp/comaplist.csv']
        mode_rows = [row for row in range(45) if self.cell(mode_cells,24,row,4)==b'dm']
        if len(mode_rows)!=1:
            raise ValueError('Native multiplayer mode template is not unique')
        mode_row = bytearray(mode_cells[mode_rows[0]*192:(mode_rows[0]+1)*192])
        number = max(int(self.cell(mode_cells,24,row,3)) for row in range(45))+1
        fields = {3:str(number).encode(),4:b'training',5:b'training',6:b'SF_UI_LAUNCH_TRAINING_TITLE',
                  7:b'SF_UI_LAUNCH_TRAINING_DESC',10:b'1',11:b'18',12:b'18',16:b'0',18:b'',19:b'',20:b'',21:b'0',22:b'',23:b'1'}
        for column,value in fields.items():
            mode_row[column*8:column*8+8] = self.allocate_cell(value)
        map_updates = []
        for row in range(130):
            if self.cell(map_cells,38,row,8)!=b'pvp':
                continue
            map_name = self.cell(map_cells,38,row,4)
            # 原表把两张丧尸竞技图误列为 pvp；本任务仅开放多人战斗地图。
            if not map_name.startswith(b'mp_'):
                continue
            available = self.cell(map_cells,38,row,9)
            # mp_bridge/mp_con_spring 是合作任务基础图，没有任何 PVP 模式关联。
            if not available:
                continue
            map_updates.append((row, map_name, self.allocate_cell(available+b';training')))
        mode_pointer=self.process.allocate(len(mode_cells)+len(mode_row))
        self.process.write(mode_pointer,mode_cells+mode_row)
        # 原生查询共享同一张表头；只更新行数与数据指针，不替换资产对象。
        self.process.write(mode_address+12,struct.pack('<I',mode_pointer))
        self.process.write(mode_address+8,struct.pack('<I',46))
        map_pointer=struct.unpack_from('<I',map_header,12)[0]
        for row,map_name,cell in map_updates:
            self.process.write(map_pointer+(row*38+9)*8,cell)
        self.installed=True
        self.record({'training_catalog':True,'mode':'training','pvp_maps':len(map_updates)})
