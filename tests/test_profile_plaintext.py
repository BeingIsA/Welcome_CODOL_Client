"""公共档案表示转换回归：固定 DDL 夹具与合成数据，不代表原生界面验收。"""
import json
from pathlib import Path
import struct
import unittest

from codol_rebuild.adapters.game.profile_format.fields import Fields
from codol_rebuild.adapters.game.v1649370.accounts.profile_codec import public_profile_plaintext
from codol_rebuild.adapters.game.v1649370.layout import BUFFER_CAPACITY


def public_schema():
    """使用正式测试维护的固定版本 DDL。"""
    return json.loads((Path(__file__).parent / 'fixtures/local-profile-capture/playerdata/playerdata-schema.json').read_text(encoding='utf8'))


def encoded_offsets(schema):
    """按夹具叶字段筛选原生转换集合，独立于生产代码的路径生成器。"""
    fields = Fields(schema)
    offsets = []

    def visit(field_kind, field_index, path_parts):
        if field_kind == 5:
            for property_definition in schema['structs'][field_index]['properties']:
                visit(*property_definition['type'], path_parts + [property_definition['name']])
        elif field_kind == 6:
            count, element_kind, element_index, _ = schema['indexed_arrays'][field_index]
            for element_position in range(count):
                visit(element_kind, element_index, path_parts + [str(element_position)])
        else:
            selected = (
                path_parts[0] == 'characterClasses' and path_parts[2] in ('slots', 'emotes')
                or path_parts[0] == 'weaponClasses' and path_parts[2] in (
                    'primaryBase', 'secondaryBase', 'primaryAttach', 'secondaryAttach',
                    'swordWpn', 'lethal', 'meleeWpn', 'tactical', 'perks', 'killstreaks', 'wildcards')
                or path_parts[0] in ('cardIcon', 'cardBackground', 'cardTitle', 'cardNameplate',
                                     'cardFrame', 'acestreak', 'badge', 'badgeExtra'))
            # DDL 有第四个连杀槽，但原生整理循环仅处理前三个；不能按数组全长转换。
            if path_parts[0] == 'weaponClasses' and path_parts[2:] == ['killstreaks', '3']:
                selected = False
            if selected:
                offsets.append(fields.resolve('.'.join(path_parts))[2])

    visit(*schema['root_type'], [])
    return offsets


def encode_profile(plaintext, schema, encoding_key):
    """合成原生存储表示；使用字节重排表达右旋，避免复用生产解码算法。"""
    encoded_bytes = bytearray(plaintext)
    for byte_offset in encoded_offsets(schema):
        field_bytes = plaintext[byte_offset:byte_offset + 4]
        rotated_value = int.from_bytes(field_bytes[1:] + field_bytes[:1], 'little')
        struct.pack_into('<I', encoded_bytes, byte_offset, rotated_value ^ encoding_key)
    return bytes(encoded_bytes)


class PublicPlaintextTests(unittest.TestCase):
    def test_all_fields_round_trip_with_different_keys_and_untouched_neighbours(self):
        schema = public_schema()
        offsets = encoded_offsets(schema)
        self.assertEqual(len(offsets), 1969)
        self.assertEqual(len(set(offsets)), 1969)
        plaintext = bytearray((byte_index * 37) % 256 for byte_index in range(BUFFER_CAPACITY))
        for field_index, byte_offset in enumerate(offsets):
            struct.pack_into('<I', plaintext, byte_offset, (0, 1, 570, 20000, 0x7fffffff)[field_index % 5])
        for encoding_key in (0, 0xffffffff, 0xd321c7ab):
            with self.subTest(encoding_key=encoding_key):
                encoded = bytearray(encode_profile(plaintext, schema, encoding_key))
                original = bytes(encoded)
                decoded = public_profile_plaintext(encoded, schema, encoding_key, True)
                self.assertEqual(decoded, plaintext)
                self.assertEqual(encoded, original)

    def test_plaintext_is_unchanged_even_with_nonzero_key(self):
        plaintext = bytes(range(256)) * 127
        self.assertEqual(public_profile_plaintext(plaintext, None, 0xffffffff, False), plaintext)

    def test_known_native_character_vector(self):
        schema = public_schema()
        fields = Fields(schema)
        profile = bytearray(BUFFER_CAPACITY)
        fields.write(profile, 'characterClasses.0.slots.0', 620783051)
        decoded = public_profile_plaintext(profile, schema, 0x1f0065c9, True)
        self.assertEqual(fields.read(decoded, 'characterClasses.0.slots.0'), 570)
