# SPDX-License-Identifier: AGPL-3.0-or-later
"""宿主侧同版本资源只读来源及 IWI 按需补件；不运行客户端或 DLL。

AES-CTR 算法与密钥常量来自已固定摘要的旧研究 codec.py（AGPL-3.0-or-later），
密钥已内联为模块常量，不读取任何旧版游戏资源。每次补件均记录原包与内容摘要。
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from copy import deepcopy
import hashlib
import json
from pathlib import Path, PurePosixPath
import struct
import threading
import zlib
from Crypto.Cipher import AES

EXE_SHA = '36997674b18f2896de5e9d755686f6922af10708f355175714058d0a0172b537'
# 版本绑定的 24 字节 IFS 资源 AES-CTR 密钥，内联自旧研究 codec.py（AGPL）。
RESOURCE_KEY = bytes.fromhex('159a0325e0752e80c6c0942a505c1c688c17ef5399f8683c')
MARKETPLACE = 'lf_init_common_marketplace_4_V46.ifs'
MARKETPLACE_SHA = 'e24ecf12fd89d67b1d1a7c2eb38b81b5c610a7f4433b26a7824ddfcab682d973'
ARCHIVES = {
    'lf_init_common_mp_3_V38.3.ifs': 'dfbbc07d631fe4ea9f98d63baae2eab675577248184a4eac454a0e957e8242a6',
    'lf_init_common_mp_4_V31.2.ifs': '4059c2d1862d15f281ac2680804e2bc4050ab8185b80ec50f8c0fa60a1a1c08e',
    'lf_init_common_mp_5_V36.2.ifs': 'fadef6940e506ccaa54f1c5489df79a683738a6e18e7ea72a54d6dce51bec2cf',
    'lf_init_common_mp_2_V35.1.ifs': 'b1e2afa8cfc4d1590909f60c3a44bc468e157c3ac0668279356ad35fbfc3299a',
    'lf_init_common_mp_5_V36.1.ifs': '381b968d8aa500eeb656e80a358ab0e0b10d461687b38123a032499265ab4635',
    'lf_init_common_marketplace_1_V49.1.ifs': '0d63d96897f43f75b7dfd7fad97ac05dd2cb794cb093b8110d0848f6f58f540f',
    'lf_init_common_marketplace_2_V46.1.ifs': '41038a32aa7d56e3c0bf1eb620eb890732ff58ffe9a8a2e5fb8d79c6ef9a6c36',
    'lf_init_common_marketplace_2_V46.2.ifs': 'fc28d5ba6270a911bf227c0d5726e4375ed90c9063458fdad84d268f8ce71af2',
    'lf_init_common_marketplace_2_V46.3.ifs': '2c66a4e4b63cf4d1f77e8b666749237b99cd136bffd1d57472de9cc7d2266db1',
    'lf_init_common_marketplace_3_V50.1.ifs': '1688198bf0a9cd4bf40daed33d67a3d08a5fdbb05e69f789fceb533c5df84a51',
    'lf_init_common_marketplace_3_V50.2.ifs': '1c4c67188b222e9627157945c58e1d440578d564c6b7b5da0020eaeed545f908',
    'lf_init_common_marketplace_3_V50.3.ifs': '2c2453c9347aa416a5ce4353431ff49d619041c04366c5567a156dc5d8f5bb25',
    'lf_init_common_marketplace_4_V46.1.ifs': '96e165c1b7e0a6397c6aa565fb9ceddedcdca95bdced491da8a6ce417c8af1fe',
    'lf_init_common_marketplace_4_V46.2.ifs': 'ace0d5bc8c280b49bf82bf8d1b59220446faa5f7f9446e1b2a8b65d590569d15',
    'lf_init_common_marketplace_4_V46.3.ifs': 'f67adb0737c4e35e8ef511328aa550ef36da17b5f01cdba5ecce18333548b411',
    'lf_init_common_marketplace_5_V45.1.ifs': '9705b8bbfd58965595221376812ca24533d7c75ddee7619c012e14f66ee1890a',
    'lf_init_common_marketplace_5_V45.2.ifs': 'e98de90a68700737f7f5ae7a48f565b55bdb1a910cd88427f837e8317b743cd4',
    'lf_init_common_marketplace_5_V45.3.ifs': '4e64bfda5d231c5dc9da8f2520571238afef41bf4d7cb18b5508bb92b78f83ca',
    'lf_init_common_mp_2_V35.2.ifs': 'a0910bdf622fdf556470e4bddded0375bc7b91cc888978050599b94f9ee2d1b5',
    'lf_init_common_mp_3_V38.1.ifs': '51eff2e0a8784900279e3503122ca79d44c56bf6532c7fee4b14518eadac7536',
    'lf_init_common_mp_3_V38.2.ifs': 'd74577ff1b0df80a6b4458f79bc0f903e8c7dbf3ad261126a4388679e50deae3',
    'lf_init_common_mp_4_V31.1.ifs': '692d9bf5a5d70928a6c9c9b3ab644df49763c6f5e657a0fa377b2b31e4571234',
    'lf_init_common_mp_3_V38.ifs': '207a9165cc911eab223594a0d7e4b7a3ee7d4268419ee39288e0a6ec7022145d',
    'lf_init_common_mp_1_V35.ifs': '117a95b44395d10d18e46d8be210afd214773d2ce7b18391753a887a8adf6d0d',
    'lf_init_common_mp_2_V35.ifs': '0d9335afb8ede33ae9307aa89f32364379b4eaeda2dca706e064e13ebe14719b',
    'lf_init_common_mp_4_V31.ifs': '895d654179a7fc47ba6409eef936ffbc37f3a2beb13a4ed34a6a3588e979922d',
    'lf_init_common_mp_5_V36.ifs': 'c303010ad6094a699e7915e301f4dfd78ad11f8dfb329b7d93394fadefd66543',
    'lf_init_common_marketplace_5_V45.ifs': '2509097276fdc7493b9bcc6c3b6bfd91a1d42746c5a795566a5c458c68890c32',
    MARKETPLACE: MARKETPLACE_SHA,
    'lf_init_common_marketplace_3_V50.ifs': '1e631ade6fe5778c08098ccf73577b03e4b8d4b9e4f01884b844e32611547e10',
    'lf_init_common_marketplace_2_V46.ifs': '85a36ac727ec46c9ab6f67e265a07bf26dfd982d11cd0533a1bec4b3078561ef',
    'lf_init_common_marketplace_1_V49.ifs': '4a05f961670a8590c0473af363bce85fb3666c8ea67c51b9cb071fab5f3740b6',
    'ff_init_common_marketplace_V95.ifs': '867258212cfc7c715bb1ec106f363d159c8688b7deb7f336e3bf0f1b56589d22',
    'lf_init_common_gfx_V63.ifs': 'b544f992fe962247845f57094d4c8a52ab616c4b003f8c2dcb73b87c89a842b4',
    'ff_init_common_gfx_V109.ifs': 'c3a561e41ca05b9c0ba7f8a3f8fd9235784e827cbb323e349e633f182dbf512d',
    'ff_init_common_mp_V111.ifs': 'd6a0b3613c3c7a7c9e0b6eebb383ed03dfd546d7229b6119b38b559233004aed',
}
RAW_INVENTORIES = {
    'lf_init_common_mp_3_V38.3.ifs': '620325177d7c37e91422866cc855f8334ceb800cd499ba1de7414bf33c1f2362',
    'lf_init_common_mp_4_V31.2.ifs': '0c172d0baa5039267a8f1bc101e26b60984b1826a55de324aec6f90a789028a2',
    'lf_init_common_mp_5_V36.2.ifs': 'e87894950845a91434b7906f69f3e6c8062f0fc3728646437b15d67fab94973c',
    'lf_init_common_mp_2_V35.1.ifs': 'cf05ea4071f2f1c9602861c3ae876a46239a2ca9286b4c7a6634252abf43deb2',
    'lf_init_common_mp_5_V36.1.ifs': '9e9fc39522e5a6b3411bb0b576f5c6927bb5a10a438cc857b90c6b644d645f47',
    'lf_init_common_gfx_V63.ifs': 'a2caeac1ad25c02026ac1f5803ecc5f820334cc39f0af11bb2f4c32d2a27d1ee',
    'lf_init_common_marketplace_1_V49.1.ifs': '335a1b58db520d476e968944102bb962d219128c6793ccc9b969144786824330',
    'lf_init_common_marketplace_2_V46.1.ifs': 'f64b5c924742973cb54530ffb7d686bfa1743b4e42501674ff1fea0e8c23f4da',
    'lf_init_common_marketplace_2_V46.2.ifs': 'c8749a99ca146462e4bb7d0a52399445ef6a28cc6fac46c1063134600fea38cf',
    'lf_init_common_marketplace_2_V46.3.ifs': '34e21156910ef9bb88dae477192d42214b674013b36fe898ba20afb86a56f8ed',
    'lf_init_common_marketplace_3_V50.1.ifs': 'cac79415c4c29070f3fa4815a5b4a31f371bd2b0bdeeac19e5b80d3c05698af6',
    'lf_init_common_marketplace_3_V50.2.ifs': 'b866213c59743c824db2cb648d9d6aadc1f6d6c2fb569608e170c4688cef3781',
    'lf_init_common_marketplace_3_V50.3.ifs': '53342debb34613dc5758a5cb2bae9aa1be06650e86d341950eef5f946ed53b9b',
    'lf_init_common_marketplace_4_V46.1.ifs': '35765c645df3d0576f79cc9deb5857445d505740c490d3948f2b64e0e7af38aa',
    'lf_init_common_marketplace_4_V46.2.ifs': '40e9dc781137544e48fdc3294e1aebc9a56ce8e30db3e7445805e19a3f355da5',
    'lf_init_common_marketplace_4_V46.3.ifs': 'd0b051732d69b90269239f82d25ce40341a84354a75e0859b683ea1bca925ec3',
    'lf_init_common_marketplace_5_V45.1.ifs': '9328fe7efc7d2198411c02efdc362f6fa2ae6b3620f241b26dfd93ebfb871237',
    'lf_init_common_marketplace_5_V45.2.ifs': 'c5a82204741972a2e512532f3440358803aefe954915e467e775cb6ce9e63e37',
    'lf_init_common_marketplace_5_V45.3.ifs': '5d44d418f6d568c39fa03cf8bf2403fde8eb6d89b12116e2a34c8f3a6576e949',
    'lf_init_common_mp_2_V35.2.ifs': 'e9dd44a9055e2fe3b6d76243333f5c2a1f62951aea90ce3cf8711f22711dbddf',
    'lf_init_common_mp_3_V38.1.ifs': 'bf0f4b7e65bde2b7fd3f3bdbae550a2e1a527916e12fbef7d6c85719bda4d895',
    'lf_init_common_mp_3_V38.2.ifs': 'fa2b0bf26998c8b424ba3ff2db2413d1e0a53c5c183ad8d841d611b2fdd3ccc2',
    'lf_init_common_mp_4_V31.1.ifs': '1646090bd04ed24648267042f710e6425ff23b6e84b0d496a72f55d7c52dc71f',
    'lf_init_common_mp_3_V38.ifs': 'bacd3c1fd33b7b17b09063b1a862eeb98f47f1afdf73b36f2b20419942933074',
    'lf_init_common_mp_1_V35.ifs': 'f72caff4d895258bd59374f812fafd2ef1ffbcca2d894e9eb336b693612c495d',
    'lf_init_common_mp_2_V35.ifs': '90f221ed38bb76748f6434d2a372d07a6d38fa40d9d055eebb70a0db5b81c399',
    'lf_init_common_mp_4_V31.ifs': '4155edad9abbc124c8dd5403a5f96766723df8e9f873c0949e32e4444454a881',
    'lf_init_common_mp_5_V36.ifs': 'cf017e6249e01bc031ce194b59ea268808019d594d2afffcaf44e554c3c477a2',
    'lf_init_common_marketplace_5_V45.ifs': '4a17876d83542f4d374d4db08b939d4e38a72d7e965c31c0b0a79de5ab9feba7',
    'lf_init_common_marketplace_3_V50.ifs': '3f19f1125827ef21abdebaa0679826fe40bf7cecd37fd9f72138cb9f41a4d11d',
    'lf_init_common_marketplace_2_V46.ifs': 'f83f8163dd96f21f83c822b361c68fed254c315e800e44481cfcd23692c8c0fd',
    'lf_init_common_marketplace_4_V46.ifs': 'a432d16d1555c3d5075a6b70bedd39be5fc285ae0b4dda87a59410142b9f9080',
    'lf_init_common_marketplace_1_V49.ifs': '054ccdbc429d81b914eafc97c35c391593f128ab62c15c549d843ea49ec5897d',
}
# 仅登记经过独立审查且精确指纹绑定的六项原生覆盖规则。
RESOLUTIONS = {'main/images/compassping_sentry_enemy.iwi': {'selected_archive': 'lf_init_common_mp_3_V38.3.ifs',
                                              'file_item_index': 818,
                                              'scope': '仅此已验证资源的精确候选集合；依据原始挂载顺序和直接/缓存两条原生查询路径',
                                              'evidence': {'path': 'client/resources/v1649370/iips-patch-priority-final.json',
                                                           'sha256': '18426be6fca7f966033df8f3a871359f227be2e4e7ace5b3004a207c1d3f19d9'},
                                              'candidates': [{'archive': 'lf_init_common_mp_3_V38.ifs',
                                                              'encoded_sha256': 'ed7f0025231ba21b4f485db6c7718cf449f794123cc93669540205f40a245545',
                                                              'decoded_sha256': '23b7f326dae06da398f1ceb5c1419848edccd4316a5de8148c6412b696a6f12d',
                                                              'decoded_size': 1056},
                                                             {'archive': 'lf_init_common_mp_3_V38.3.ifs',
                                                              'encoded_sha256': '154cd366ac8bfcd6708c6f65b72d27992192441924b610e8394a76e1501b8c8e',
                                                              'decoded_sha256': '6ef6222170b9f41c90b91a85c46b358f92f5bb858c195b33e9f74b53dbd33fd7',
                                                              'decoded_size': 1056}]},
 'main/images/compassping_sentry_friendly.iwi': {'selected_archive': 'lf_init_common_mp_3_V38.3.ifs',
                                                 'file_item_index': 818,
                                                 'scope': '仅此已验证资源的精确候选集合；依据原始挂载顺序和直接/缓存两条原生查询路径',
                                                 'evidence': {'path': 'client/resources/v1649370/iips-patch-priority-final.json',
                                                              'sha256': '18426be6fca7f966033df8f3a871359f227be2e4e7ace5b3004a207c1d3f19d9'},
                                                 'candidates': [{'archive': 'lf_init_common_mp_3_V38.ifs',
                                                                 'encoded_sha256': '73ff6f0a18f55b323ee4421f7cdf4c6d8e3e4c8a1ff138686c697fcd25a0338c',
                                                                 'decoded_sha256': '77be686e798097a538c4aa5b87b401d8c33775ce0bde82bdb6c4b32fa36d62bd',
                                                                 'decoded_size': 1056},
                                                                {'archive': 'lf_init_common_mp_3_V38.3.ifs',
                                                                 'encoded_sha256': '0b2df688118c4af8e838085d4d6533d218cd833bab89b29ef805bc21ab5926ac',
                                                                 'decoded_sha256': '8937a534fd6f7fa4babf7f53702d9bec672df104d5d6958859689877ce26296a',
                                                                 'decoded_size': 1056}]},
 'main/images/compassping_sentry_friendly_firing.iwi': {'selected_archive': 'lf_init_common_mp_3_V38.3.ifs',
                                                        'file_item_index': 818,
                                                        'scope': '仅此已验证资源的精确候选集合；依据原始挂载顺序和直接/缓存两条原生查询路径',
                                                        'evidence': {'path': 'client/resources/v1649370/iips-patch-priority-final.json',
                                                                     'sha256': '18426be6fca7f966033df8f3a871359f227be2e4e7ace5b3004a207c1d3f19d9'},
                                                        'candidates': [{'archive': 'lf_init_common_mp_3_V38.ifs',
                                                                        'encoded_sha256': '0f3c5e31f34c3ede171c605ef68ba37f762b179039428efdfc158dc9ee2982c8',
                                                                        'decoded_sha256': '8a206307d10f9a2a14f44e4735b067ff1f77ab93781b8733e3611e03001354b5',
                                                                        'decoded_size': 1056},
                                                                       {'archive': 'lf_init_common_mp_3_V38.3.ifs',
                                                                        'encoded_sha256': 'dc826e1b67df6ded06259ed339725b1bc4d3bb20072956efae118554e1c9e6af',
                                                                        'decoded_sha256': 'a71dabc88aadf4891c1200e5639e8716861cce71828d7649a7d71e8589ba6105',
                                                                        'decoded_size': 1056}]},
 'main/images/impact_asphalt01_col#0.iwi': {'selected_archive': 'lf_init_common_mp_4_V31.2.ifs',
                                            'file_item_index': 821,
                                            'scope': '仅此已验证资源的精确候选集合；依据原始挂载顺序和直接/缓存两条原生查询路径',
                                            'evidence': {'path': 'client/resources/v1649370/iips-patch-priority-final.json',
                                                         'sha256': '18426be6fca7f966033df8f3a871359f227be2e4e7ace5b3004a207c1d3f19d9'},
                                            'candidates': [{'archive': 'lf_init_common_mp_4_V31.ifs',
                                                            'encoded_sha256': 'd16ce62c30ae804192f9a4ff77c4ff813151db2f1a7f6c02aad7ae5caf7b3230',
                                                            'decoded_sha256': 'faf7b1e8de79d79fba58bc93bd8e543fcc54275fc987f8734b645a39756e8fcb',
                                                            'decoded_size': 1424},
                                                           {'archive': 'lf_init_common_mp_4_V31.2.ifs',
                                                            'encoded_sha256': 'cd2cb29837e0de69d42b1d279446497a27cecc56037ccd24beb8a7d1eeb9c59d',
                                                            'decoded_sha256': 'fe34d01011d17e5a1b5a25ccfb299b1da8753aa339666e9c1355617067d02b0b',
                                                            'decoded_size': 1424}]},
 'main/images/defusebomb.iwi': {'selected_archive': 'lf_init_common_mp_5_V36.2.ifs',
                                'file_item_index': 824,
                                'scope': '仅此已验证资源的精确候选集合；依据原始挂载顺序和直接/缓存两条原生查询路径',
                                'evidence': {'path': 'client/resources/v1649370/iips-patch-priority-final.json',
                                             'sha256': '18426be6fca7f966033df8f3a871359f227be2e4e7ace5b3004a207c1d3f19d9'},
                                'candidates': [{'archive': 'lf_init_common_mp_5_V36.ifs',
                                                'encoded_sha256': 'fdc17eeb9309db2671659b61b9d647267b8a345a25db511bc8f85fb234b23856',
                                                'decoded_sha256': '73231351eee76b8efd41ef17baf790d58b86cc445fe1c9b61084c10004e70663',
                                                'decoded_size': 16416},
                                               {'archive': 'lf_init_common_mp_5_V36.2.ifs',
                                                'encoded_sha256': '89ef0ccb4aac2570c4b9a2b1c56c93a648d35fb09b4911db257a69b07403554a',
                                                'decoded_sha256': 'd918e8223d6c02df78af1cd6b57d5ed806bc9b852e910be8e450bd2a697830d8',
                                                'decoded_size': 16416}]},
 'main/images/hud_cannonball.iwi': {'selected_archive': 'lf_init_common_mp_5_V36.2.ifs',
                                    'file_item_index': 824,
                                    'scope': '仅此已验证资源的精确候选集合；依据原始挂载顺序和直接/缓存两条原生查询路径',
                                    'evidence': {'path': 'client/resources/v1649370/iips-patch-priority-final.json',
                                                 'sha256': '18426be6fca7f966033df8f3a871359f227be2e4e7ace5b3004a207c1d3f19d9'},
                                    'candidates': [{'archive': 'lf_init_common_mp_5_V36.ifs',
                                                    'encoded_sha256': 'e49090a434449eddb546105e89666f596be99fe8bbb792280867b04d7ee733af',
                                                    'decoded_sha256': '5e2c53dfb370c111d0ea86f865a14665ee8574243f983991f4ee7fda12efcb27',
                                                    'decoded_size': 16416},
                                                   {'archive': 'lf_init_common_mp_5_V36.1.ifs',
                                                    'encoded_sha256': '7708b082c815f13f6c1761e25bb9b45da47592bb74a228206168fbf918d6c315',
                                                    'decoded_sha256': '48cecc642114fd2e0c751cf07e427e297119b61ef3ab8e962873f2ae86980442',
                                                    'decoded_size': 16416},
                                                   {'archive': 'lf_init_common_mp_5_V36.2.ifs',
                                                    'encoded_sha256': 'e49090a434449eddb546105e89666f596be99fe8bbb792280867b04d7ee733af',
                                                    'decoded_sha256': '5e2c53dfb370c111d0ea86f865a14665ee8574243f983991f4ee7fda12efcb27',
                                                    'decoded_size': 16416}]}}
MAX_RESOURCE = 512 * 1024 * 1024


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_digest(path: Path) -> str:
    """流式校验大型新版源包，避免把整个 IFS 放入内存。"""
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def validate_iwi(data: bytes) -> None:
    """与新版加载器一致：完整 32 字节头、IWi 签名及版本 9。"""
    if len(data) < 32 or data[:4] != b'IWi\x09':
        raise ValueError('不是具有完整头部的 IWi09 资源')


def decode_loose(raw: bytes, filename: str, key: bytes) -> bytes:
    """通用松散资源外层：严格验证 AES-CTR 解码后的 zlib 长度、EOF 和尾部。"""
    if len(raw) < 5 or len(raw) > MAX_RESOURCE:
        raise ValueError('编码资源大小无效')
    size = struct.unpack_from('<I', raw, len(raw)-4)[0]
    if not 1 <= size <= MAX_RESOURCE:
        raise ValueError('资源声明大小无效')
    name = filename.encode('utf-8')
    nonce = zlib.crc32(name, len(name))
    packed = bytearray()
    for offset in range(0, len(raw)-4, 32768):
        chunk = raw[offset:min(offset+32768, len(raw)-4)]
        iv = struct.pack('<4I', nonce, size, offset, len(chunk))
        packed.extend(AES.new(key, AES.MODE_CTR, nonce=b'', initial_value=int.from_bytes(iv, 'big')).decrypt(chunk))
    inflater = zlib.decompressobj()
    data = inflater.decompress(packed, size+1)
    if len(data) != size or not inflater.eof or inflater.unused_data or inflater.unconsumed_tail:
        raise ValueError('zlib 完整性或声明尺寸校验失败')
    return data


def decode_iwi(raw: bytes, filename: str, key: bytes) -> bytes:
    """IWI 在共享外层完整性验证后额外验证完整版本 9 头部。"""
    data = decode_loose(raw, filename, key)
    validate_iwi(data)
    return data


def canonical_iwi(name: str) -> str | None:
    """只接受 main/images 精确逻辑路径，拒绝路径穿越和 Windows 特殊名。"""
    if not isinstance(name, str) or not name or any(ord(c) < 32 for c in name):
        raise ValueError('请求路径为空或含 NUL')
    name = name.replace('\\', '/')
    while name.startswith('./'):
        name = name[2:]
    parts = name.split('/')
    if name.startswith('/') or ':' in name or any(p in ('', '.', '..') for p in parts):
        raise ValueError('请求路径不是安全的相对路径')
    for part in parts:
        if part.rstrip(' .') != part or any(c in part for c in '<>"|?*'):
            raise ValueError('请求含 Windows 特殊路径字符')
        if part.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*(f'COM{i}' for i in range(1,10)),*(f'LPT{i}' for i in range(1,10))}:
            raise ValueError('请求含 Windows 保留名称')
    if parts[0].lower() == 'images':
        parts.insert(0, 'main')
    if len(parts) < 3 or [p.lower() for p in parts[:2]] != ['main','images'] or not parts[-1].lower().endswith('.iwi'):
        return None
    return '/'.join(parts).lower()


class ResourceSource(ABC):
    """只读资源来源：校验源包与候选、选择已证明的覆盖结果，不加载类型专用清单或写入文件。"""
    encoded_verification = '固定新版源包/提取收据/编码资源摘要 + 严格解码校验'

    def __init__(self, project_root: Path):
        self.project = Path(project_root).resolve()
        self.workspace = (self.project/'client/workspaces').resolve()
        self._verified = {}
        self._receipts = {}
        self.manifest = None

    @staticmethod
    @abstractmethod
    def canonical_resource(name: str) -> str | None:
        """由具体来源限定资源类型并规范化逻辑路径。"""
        raise NotImplementedError

    @abstractmethod
    def resolution_for(self, name: str) -> dict | None:
        """由具体来源提供该资源已确认的覆盖证据。"""
        raise NotImplementedError

    @abstractmethod
    def _decode_resource(self, raw: bytes, filename: str) -> bytes:
        """由具体来源按资源类型选择严格解码器。"""
        raise NotImplementedError

    def indexed_names(self) -> tuple[str, ...]:
        """返回已登记资源名称的独立、有序快照。"""
        return tuple(sorted(self._receipts))

    def cached_entry(self, name: str) -> tuple[Path, dict] | None:
        """仅收据来源没有已解码缓存；支持缓存的具体来源返回独立快照。"""
        return None

    def receipt_entries(self, name: str) -> tuple:
        """返回提取收据候选的独立快照，调用者不能修改来源索引。"""
        return deepcopy(tuple(self._receipts.get(name, ())))

    def library_constraints(self) -> dict | None:
        """返回已审完整集合的数量和尺寸约束；未要求完整集合的来源返回空值。"""
        return None

    def _source(self, archive: str) -> dict:
        # 源包已随资源输入副本固化到项目 workspaces，不再引用外部原客户端目录。
        path = (self.project/'client/workspaces/offline-resource-inputs'/archive).resolve()
        allowed = (self.project/'client/workspaces/offline-resource-inputs').resolve()
        if path.parent != allowed or archive not in ARCHIVES:
            raise ValueError('源包不在新版允许范围内')
        stat = path.stat(); stamp = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        if self._verified.get(path) != stamp:
            if file_digest(path) != ARCHIVES[archive]:
                raise ValueError('新版源包摘要不符')
            self._verified[path] = stamp
        return {'archive':archive,'source_archive_path':str(path),'archive_sha256':ARCHIVES[archive]}

    def select_candidate(self, name: str, candidates: list[tuple[bytes, dict]]) -> tuple[bytes, dict]:
        """按需与批量共享选择规则；不同内容只能使用逐候选指纹绑定的原生证据。"""
        if not candidates:
            raise ValueError('资源没有可验证候选')
        resolution = self.resolution_for(name)
        if resolution is None:
            if any(raw != candidates[0][0] for raw, _ in candidates[1:]):
                raise ValueError('同名候选内容冲突，尚无已证实原生覆盖规则')
            return candidates[0]
        proof = (self.project/resolution['evidence']['path']).resolve()
        if not proof.is_relative_to(self.project/'client/resources/v1649370'):
            raise ValueError('覆盖规则证据路径越界')
        if digest(proof.read_bytes()) != resolution['evidence']['sha256']:
            raise ValueError('覆盖规则证据摘要不符')
        observed = {(p['archive'],p.get('encoded_sha256'),digest(raw),len(raw)) for raw,p in candidates}
        expected = {(p['archive'],p['encoded_sha256'],p['decoded_sha256'],p['decoded_size']) for p in resolution['candidates']}
        if observed != expected:
            raise ValueError('覆盖规则实际候选与已证明指纹不符')
        selected = [(raw,p) for raw,p in candidates if p['archive']==resolution['selected_archive']]
        if len(selected) != 1:
            raise ValueError('覆盖规则没有唯一已验证赢家')
        raw,p = selected[0]
        return raw,{**p,'resolution':resolution}

    def _load_encoded(self, candidate: tuple) -> tuple[bytes, dict]:
        """校验单个实际候选，供按需供件和批量冲突比对共同使用。"""
        archive, folder, entry, receipt_hash = candidate
        path = (folder/entry['path']).resolve()
        if not path.is_relative_to(folder):
            raise ValueError('提取收据资源路径越界')
        provenance = self._source(archive)
        if path.stat().st_size != entry['encoded_size'] or path.stat().st_size > MAX_RESOURCE:
            raise ValueError('提取收据编码资源尺寸不符')
        raw = path.read_bytes()
        if digest(raw) != entry['encoded_sha256']:
            raise ValueError('提取收据编码资源摘要不符')
        data = self._decode_resource(raw, entry['decode_filename'])
        provenance.update(cache_path=str(path), source_archive_resource=entry['path'], inventory_sha256=receipt_hash, encoded_sha256=digest(raw), encoded_size=len(raw), verification=self.encoded_verification)
        return data, provenance

    def load_candidates(self, name: str, *, allow_missing: bool = False) -> list[tuple[bytes, dict]]:
        """批量模式验证全部候选；调用方比较真实字节后才能发布，不按顺序择一。"""
        if self.canonical_resource(name) != name:
            raise ValueError('候选逻辑路径必须已规范化')
        candidates = []
        for candidate in self._receipts.get(name, []):
            if allow_missing and not (candidate[1]/candidate[2]['path']).is_file():
                continue
            candidates.append(self._load_encoded(candidate))
        return candidates


class OfflineResourceSupplier(ResourceSource):
    """图片清单与 IWI 校验、独立 runtime 补件；只读建库可省略 runtime。"""
    canonical_resource = staticmethod(canonical_iwi)
    encoded_verification = '固定新版源包/提取收据/编码资源摘要 + AES-CTR/严格 zlib/IWi09 校验'

    def __init__(self, project_root: Path, runtime_root: Path | None = None):
        super().__init__(project_root)
        # 不提供 runtime 时仅加载来源索引，供批量素材库复用，不具有补件写入目标。
        self.runtime = Path(runtime_root).resolve() if runtime_root is not None else None
        if self.runtime is not None:
            allowed = self.workspace/'offline-runs'
            if not self.runtime.is_relative_to(allowed) or self.runtime.name != 'runtime' or self.runtime.parent.parent != allowed:
                raise ValueError('补件目标必须是本项目 offline-runs/<run>/runtime')
            if not self.runtime.is_dir():
                raise ValueError('独立 runtime 尚未创建')
        self._lock = threading.RLock()
        self._decoded = {}
        for name in ('offline-resources-manifest-v3.json','offline-resources-manifest-v2.json','offline-resources-manifest.json'):
            manifest = self.project/'client/resources/v1649370'/name
            if not manifest.is_file():
                continue
            data = manifest.read_bytes(); report = json.loads(data)
            if report.get('source_exe_sha256') != EXE_SHA or report.get('source_resource_version') != 461 or report.get('old_version_resources_used') is not False:
                raise ValueError('缓存清单未绑定新版资源')
            # 清单中的 output_root 保留生成时历史路径；部署位置由项目目录确定。
            resource_root = self.project/'client/resources/v1649370'
            output = (resource_root/'offline-resources').resolve()
            if not output.is_relative_to(resource_root.resolve()):
                raise ValueError('缓存根目录越界')
            self.manifest = {'path':str(manifest),'sha256':digest(data)}
            for entry in report['resources']:
                name = canonical_iwi(entry['game_path'])
                if name:
                    if entry['archive'] not in ARCHIVES:
                        raise ValueError('清单包含未固定版本的资源包')
                    if name in self._decoded and self._decoded[name][1]['sha256'] != entry['sha256']:
                        raise ValueError('清单含同名不同内容资源')
                    self._decoded[name] = (output/entry['game_path'], entry)
            break
        # 固定摘要的提取收据逐项记录实际承载资源；不把公共清单命中当作实际文件存在。
        for archive, receipt_hash in RAW_INVENTORIES.items():
            folder = (self.workspace/'offline-resource-raw'/Path(archive).stem).resolve()
            if not folder.is_relative_to(self.workspace):
                raise ValueError('提取收据缓存目录越界')
            receipt_path = folder/'extraction-inventory.json'
            if not receipt_path.is_file():
                continue
            receipt_data = receipt_path.read_bytes()
            if digest(receipt_data) != receipt_hash:
                raise ValueError('提取收据摘要不符')
            receipt = json.loads(receipt_data)
            if receipt['archive']['name'] != archive or receipt['archive']['sha256'] != ARCHIVES[archive]:
                raise ValueError('提取收据源包绑定不符')
            for logical, entry in receipt['entries'].items():
                name = canonical_iwi(logical)
                if name:
                    if canonical_iwi(entry['path']) != name or PurePosixPath(entry['path']).name != entry['decode_filename']:
                        raise ValueError('提取条目路径与解码文件名不符')
                    self._receipts.setdefault(name, []).append((archive, folder, entry, receipt_hash))

    def indexed_names(self) -> tuple[str, ...]:
        """合并图片缓存和提取收据中的资源名称。"""
        return tuple(sorted(set(self._decoded) | set(super().indexed_names())))

    def cached_entry(self, name: str) -> tuple[Path, dict] | None:
        """返回图片缓存路径与清单条目的独立快照。"""
        return deepcopy(self._decoded.get(name))

    def load_candidates(self, name: str, *, allow_missing: bool = False) -> list[tuple[bytes, dict]]:
        """先校验图片缓存，再读取收据候选，保留原有候选顺序和失败行为。"""
        if self.canonical_resource(name) != name:
            raise ValueError('候选逻辑路径必须已规范化')
        candidates = []
        if name in self._decoded:
            candidates.append(self._load_cached(name))
        candidates.extend(super().load_candidates(name, allow_missing=allow_missing))
        return candidates

    def _load_cached(self, name: str) -> tuple[bytes, dict] | None:
        if name in self._decoded:
            path, entry = self._decoded[name]; path = path.resolve()
            # 已解码资源固定部署在 resources；workspaces 仅承载输入、中间材料和运行会话。
            resource_root = (self.project/'client/resources/v1649370/offline-resources').resolve()
            if not path.is_relative_to(resource_root):
                raise ValueError('已解码缓存路径越界')
            provenance = self._source(entry['archive'])
            if path.stat().st_size != entry['size'] or path.stat().st_size > MAX_RESOURCE:
                raise ValueError('已解码缓存尺寸不符')
            data = path.read_bytes()
            if digest(data) != entry['sha256']:
                raise ValueError('已解码缓存摘要不符')
            validate_iwi(data)
            provenance.update(cache_path=str(path), manifest=self.manifest, encoded_sha256=entry.get('encoded_sha256'), verification='固定版本来源 + 历史严格解码清单 + 本次缓存尺寸/摘要/IWi09复核')
            return data, provenance
        return None

    def _load(self, name: str) -> tuple[bytes, dict] | None:
        candidates = self.load_candidates(name, allow_missing=True)
        if not candidates:
            return None
        return self.select_candidate(name, candidates)

    def resolution_for(self, name: str) -> dict | None:
        return RESOLUTIONS.get(name)

    def _decode_resource(self, raw: bytes, filename: str) -> bytes:
        return decode_iwi(raw, filename, RESOURCE_KEY)

    def supply(self, requested_path: str) -> dict:
        """允许相对 .\\main\\images 或 runtime 内绝对路径；只排他创建真实资源。"""
        with self._lock:
            result = {'requested_path':str(requested_path)}
            try:
                if self.runtime is None:
                    raise ValueError('只读来源实例没有 runtime，不能补件')
                request = Path(requested_path)
                if request.is_absolute():
                    resolved = request.resolve()
                    if not resolved.is_relative_to(self.runtime):
                        raise ValueError('绝对请求路径不在本轮 runtime 内')
                    requested_path = resolved.relative_to(self.runtime).as_posix()
                name = canonical_iwi(requested_path)
                if name is None:
                    return {**result,'status':'not_found','reason':'仅提供 main/images 下 IWi09 资源'}
                result['logical_path'] = name
                loaded = self._load(name)
                if loaded is None:
                    return {**result,'status':'not_found','reason':'已确认缓存不承载该精确资源'}
                data, provenance = loaded
                target = (self.runtime/name).resolve()
                if not target.is_relative_to(self.runtime/'main/images'):
                    raise ValueError('输出路径越界')
                result.update(destination=str(target),size=len(data),sha256=digest(data),provenance=provenance)
                if target.exists():
                    if target.read_bytes() != data:
                        raise ValueError('现有目标内容不同，拒绝覆盖')
                    result['status'] = 'already_present'
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    # 客户端停在诊断门期间写入；排他打开保证不覆盖已有文件。
                    with target.open('xb') as stream:
                        stream.write(data)
                    result['status'] = 'supplied'
                audit = (self.runtime.parent/'resource-supply.jsonl').resolve()
                if audit.parent != self.runtime.parent:
                    raise ValueError('审计文件路径越界')
                with audit.open('a',encoding='utf-8') as stream:
                    stream.write(json.dumps(result,ensure_ascii=False)+'\n')
                return result
            except (OSError, ValueError, TypeError, KeyError, zlib.error) as exc:
                return {**result,'status':'error','reason':str(exc)}
