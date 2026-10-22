"""档案结构解析与字段读写；本模块负责 fields。

Checked DDL field access; independent of processes, network and build addresses.
"""
from __future__ import annotations
import struct


class Fields:
    def __init__(self, schema):
        self.schema = schema

    def leaf_count(self, kind, index):
        if kind == 5:
            return self.schema['structs'][index]['header'][4]
        if kind == 6:
            count, k, i, _ = self.schema['indexed_arrays'][index]
            return count * self.leaf_count(k, i)
        return 1

    def resolve(self, path):
        kind, index = self.schema['root_type']
        flat = 0
        for component in path.split('.'):
            if kind == 5:
                properties = self.schema['structs'][index]['properties']
                prop = next((p for p in properties if p['name'] == component), None)
                if prop is None: raise ValueError('Unknown profile field: '+path)
                flat += prop['offset']; kind, index = prop['type']
            elif kind == 6:
                count, kind, index, _ = self.schema['indexed_arrays'][index]
                element = int(component)
                if not 0 <= element < count: raise ValueError('Profile array index out of bounds: '+path)
                flat += element * self.leaf_count(kind, index)
            else:
                raise ValueError('Profile path continues past scalar: '+path)
        if kind in (5, 6): raise ValueError('Profile path does not select a scalar: '+path)
        offset = self.schema['offsets'][flat]
        width = {0:4, 1:1, 2:1, 3:index, 4:2, 8:4, 9:2}.get(kind)
        byte_offset = offset//8 if kind == 2 else offset
        if width is None or not 8 <= byte_offset or byte_offset+width > self.schema['size']:
            raise ValueError('Profile field outside schema: '+path)
        return kind, index, offset

    def write(self, blob, path, value):
        kind, index, offset = self.resolve(path)
        if len(blob) < self.schema['size']: raise ValueError('Profile buffer too short')
        if kind == 2:
            mask=1 << (offset%8)
            blob[offset//8]=(blob[offset//8] & ~mask) | (mask if value else 0)
        elif kind == 3:
            raw=value.encode('utf-8')
            if len(raw)>=index:raise ValueError('Profile string too long: '+path)
            blob[offset:offset+index]=raw.ljust(index,b'\0')
        else:
            struct.pack_into({0:'<i',1:'<B',4:'<H',8:'<f',9:'<h'}[kind],blob,offset,value)

    def read(self, blob, path):
        kind, index, offset = self.resolve(path)
        if len(blob) < self.schema['size']: raise ValueError('Profile buffer too short')
        if kind == 2:return bool(blob[offset//8] & (1 << (offset%8)))
        if kind == 3:return blob[offset:offset+index].split(b'\0')[0].decode('utf-8')
        return struct.unpack_from({0:'<i',1:'<B',4:'<H',8:'<f',9:'<h'}[kind],blob,offset)[0]
