# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fastfile 的密码学坏样本、边界与输出保护回归；不读取或保存真实私钥。"""
from contextlib import redirect_stdout
import io
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

from Crypto.Cipher import AES, PKCS1_OAEP, PKCS1_v1_5
from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15

from codol_rebuild.resources import fastfile as ff


def make_zone(payload=b'asset-payload'):
    """创建一个目录可完整覆盖的微型合成 Zone，不包含游戏资源。"""
    zone = bytearray(72 + len(payload))
    struct.pack_into('<I', zone, 0, len(zone) - 40)
    struct.pack_into('<II', zone, 48, 1, 0xFFFFFFFF)
    struct.pack_into('<4I', zone, 56, 36, 32, 0xFFFFFFFF, len(payload))
    zone[72:] = payload
    return bytes(zone)


def signed_body(blocks, material, signer):
    """生成带独立签名和哈希链的合成块，可覆盖 256 块跨表边界。"""
    header = bytearray(ff.BLOCK_SIZE)
    header[:8] = b'IWffs100'
    encrypted = []
    for section, start in enumerate(range(0, len(blocks), 256)):
        group = blocks[start:start + 256]
        table = bytearray(ff.BLOCK_SIZE)
        for index, block in enumerate(group):
            table[index * 32:(index + 1) * 32] = SHA256.new(block).digest()
        table_hash = SHA256.new(table).digest()
        header[0x150 + section * 32:0x170 + section * 32] = table_hash
        encrypted.append(ff._ctr(bytes(table), material[:24], table_hash))
        encrypted.extend(ff._ctr(block, material[:24], SHA256.new(block).digest()) for block in group)
    digest = SHA256.new(header[0x12C:0x1FD0])
    header[0xC:0x2C] = digest.digest()
    header[0x2C:0x12C] = pkcs1_15.new(signer).sign(digest)
    return ff._ctr(bytes(header), material[:24], material[24:]) + b''.join(encrypted)


class FastfileTests(unittest.TestCase):
    """每条测试验证一种会影响格式信任或输入保护的独立行为。"""

    @classmethod
    def setUpClass(cls):
        """在内存生成测试用 RSA 对；测试结束后不导出任何私有材料。"""
        cls.rsa = RSA.generate(2048)
        cls.material = bytes(range(40))
        cls.zone = make_zone()
        compressed = zlib.compress(cls.zone)
        cls.block = compressed + b'\xAA' * (ff.BLOCK_SIZE - len(compressed))
        cls.body = signed_body([cls.block], cls.material, cls.rsa)

    def assert_stage(self, expected, function, *args, **kwargs):
        """同时断言拒绝结果和精确失败阶段，避免泛化异常掩盖缺陷。"""
        with self.assertRaises(ff.FastfileError) as caught:
            function(*args, **kwargs)
        self.assertEqual(expected, caught.exception.stage)

    def corrupt_header(self, offset):
        """在头明文指定位置翻转一位，再重新加密以隔离目标校验。"""
        header = bytearray(ff._ctr(self.body[:ff.BLOCK_SIZE], self.material[:24], self.material[24:]))
        header[offset] ^= 1
        return ff._ctr(bytes(header), self.material[:24], self.material[24:]) + self.body[ff.BLOCK_SIZE:]

    def test_signed_pipeline(self):
        """合成完整链要求 RSA 签名、哈希、zlib 和目录都真实通过。"""
        compressed, checks = ff._signed_payload(self.body, self.material, self.rsa.public_key())
        self.assertTrue(checks['publisher_signature_verified'])
        self.assertEqual(checks['verified_blocks_including_header'], 3)
        zone, zchecks = ff._inflate(compressed, True, 4096)
        self.assertEqual(zone, self.zone)
        self.assertTrue(zchecks['adler32_verified'])
        self.assertEqual(ff.validate_zone(zone)['asset_count'], 1)

    def test_rsa_padding_and_length(self):
        """验证版本填充选择，拒绝跨填充格式与错误的 39 字节材料。"""
        cipher = PKCS1_v1_5.new(self.rsa.public_key())
        encrypted = cipher.encrypt(self.material)
        self.assertEqual(ff._decrypt_material(encrypted, self.rsa, 461), self.material)
        self.assert_stage('rsa_padding', ff._decrypt_material, encrypted, self.rsa, 334)
        self.assert_stage('rsa_padding', ff._decrypt_material, cipher.encrypt(self.material[:39]), self.rsa, 461)
        oaep = PKCS1_OAEP.new(self.rsa.public_key(), hashAlgo=SHA256).encrypt(self.material)
        self.assertEqual(ff._decrypt_material(oaep, self.rsa, 334), self.material)

    def test_ctr_carry_is_little_endian(self):
        """用 ECB 独立构造跨 0xFF 进位的计数器，防止误用网络字节序。"""
        key = bytes(range(24))
        iv = b'\xff' + b'\0' * 15
        ecb = AES.new(key, AES.MODE_ECB)
        expected = ecb.encrypt(iv) + ecb.encrypt(b'\0\x01' + b'\0' * 14)
        self.assertEqual(ff._ctr(bytes(32), key, iv), expected)

    def test_header_hash_rejects_corruption(self):
        """签名覆盖区内容损坏必须在头哈希阶段停止。"""
        self.assert_stage('header_hash', ff._signed_payload, self.corrupt_header(0x12C), self.material, self.rsa.public_key())

    def test_publisher_signature_is_mandatory(self):
        """仅破坏 RSA 签名且保持所有 SHA256 正确，仍必须拒绝。"""
        self.assert_stage('publisher_signature', ff._signed_payload, self.corrupt_header(0x2C), self.material, self.rsa.public_key())

    def test_table_and_data_hash_corruption(self):
        """分别损坏哈希表及数据密文，检查对应阶段与块编号。"""
        for block_index, stage in ((1, 'table_hash'), (2, 'data_hash')):
            body = bytearray(self.body)
            body[block_index * ff.BLOCK_SIZE + 23] ^= 1
            self.assert_stage(stage, ff._signed_payload, bytes(body), self.material, self.rsa.public_key())

    def test_hash_table_boundary(self):
        """257 个数据块要求切换到第二哈希表，不能复用第一张表。"""
        blocks = [bytes([index % 251]) * ff.BLOCK_SIZE for index in range(257)]
        result, checks = ff._signed_payload(signed_body(blocks, self.material, self.rsa), self.material, self.rsa.public_key())
        self.assertEqual(result, b''.join(blocks))
        self.assertEqual(checks['verified_hash_tables'], 2)
        self.assertEqual(checks['verified_data_blocks'], 257)

    def test_zlib_checksum_eof_and_limit(self):
        """拒绝 Adler32 错误、截断流及超过配置大小的压缩炸弹。"""
        compressed = zlib.compress(self.zone)
        bad = compressed[:-1] + bytes([compressed[-1] ^ 1])
        self.assert_stage('zlib', ff._inflate, bad, False, 4096)
        self.assert_stage('zlib', ff._inflate, compressed[:-1], False, 4096)
        self.assert_stage('zone_size', ff._inflate, compressed, False, len(self.zone) - 1)

    def test_padding_policy(self):
        """签名末块允许非零填充，但整块尾随数据和未签名尾随数据均拒绝。"""
        compressed = zlib.compress(self.zone)
        self.assert_stage('zlib_padding', ff._inflate, compressed + bytes(ff.BLOCK_SIZE), True, 4096)
        self.assert_stage('zlib_padding', ff._inflate, compressed + b'x', False, 4096)

    def test_outer_header_boundaries(self):
        """拒绝截断头、未知版本和不是 8192 字节整数倍的签名体。"""
        self.assert_stage('outer_header', ff._inspect, b'IWff0100')
        raw = bytearray(ff.FF_HEADER + 3 * ff.BLOCK_SIZE)
        raw[:8] = b'IWff0100'
        struct.pack_into('<I', raw, 8, 999)
        self.assert_stage('version', ff._inspect, bytes(raw))
        struct.pack_into('<I', raw, 8, 461)
        self.assert_stage('outer_header', ff._inspect, bytes(raw[:-1]))

    def test_zone_extent_and_string_boundaries(self):
        """目录范围或字符串个数破坏时不能交给资产加载器。"""
        zone = bytearray(self.zone)
        struct.pack_into('<I', zone, 60, 33)
        self.assert_stage('zone_extent', ff.validate_zone, bytes(zone))
        zone = bytearray(self.zone)
        struct.pack_into('<I', zone, 68, 0xFFFFFFFF)
        self.assert_stage('zone_extent', ff.validate_zone, bytes(zone))
        zone = bytearray(self.zone)
        struct.pack_into('<I', zone, 40, 0xFFFFFFFF)
        self.assert_stage('zone_strings', ff.validate_zone, bytes(zone))

    def test_material_fingerprint_is_required(self):
        """错误客户端不能仅凭固定偏移取得可用 RSA 对象。"""
        self.assert_stage('rsa_material', ff._load_keys, bytes(100), 461, True)

    def test_output_is_exclusive_and_repeatable(self):
        """相同产物允许复跑；不同内容保持原样并返回结构化错误。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'evidence.json'
            ff._write_new_or_identical(path, b'original')
            ff._write_new_or_identical(path, b'original')
            self.assert_stage('output', ff._write_new_or_identical, path, b'new')
            self.assertEqual(path.read_bytes(), b'original')

    def test_original_client_directory_is_read_only(self):
        """即便输出文件不存在，也禁止写入给定客户端所在目录。"""
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / 'original'
            original.mkdir()
            client = original / 'game.exe'
            client.write_bytes(b'client')
            self.assert_stage('output', ff._check_output_path, original / 'new.json', (client,), client)

    def test_report_preserves_existing_file(self):
        """CLI 报告写入失败也应退出 1，并保留原有证据内容。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / 'original'
            original.mkdir()
            client = original / 'game.exe'
            client.write_bytes(b'client')
            source = root / 'sample.ff'
            source.write_bytes(b'source')
            report = root / 'report.json'
            report.write_bytes(b'existing-evidence')
            with patch.object(ff, 'decode_fastfile', return_value={'ok': True}), redirect_stdout(io.StringIO()) as stdout:
                code = ff.main(['decode', str(source), '--client-exe', str(client), '--report', str(report)])
            self.assertEqual(code, 1)
            self.assertIn('"stage": "output"', stdout.getvalue())
            self.assertEqual(report.read_bytes(), b'existing-evidence')


if __name__ == '__main__':
    unittest.main()
