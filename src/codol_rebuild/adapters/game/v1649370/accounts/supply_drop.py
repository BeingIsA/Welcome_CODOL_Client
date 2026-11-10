"""1649370 单箱适配：GFx 用户入口、类型化奖励对象和原生任务完成通知。"""
import csv
import struct

from codol_rebuild.adapters.game.v1649370.native_state import checked_write, u32

HANDLER_SLOT = 0xDCD8C8
HANDLER = 0x6A7970
SUCCESS, ARRAY, RESPONSE, ITEMS, REWARD, UNDEFINED = 0, 32, 64, 96, 128, 160
FIELDS, MESSAGE, CAPACITY = 2048, 1536, 16384
REWARD_FIELDS = ('itemID', 'productID', 'rentalDurationSeconds', 'wasCrushed',
                 'crushCurrencyID', 'crushAmount', 'isFeatured', 'quantity',
                 'quantityDelta', 'isPartialCrushed', 'showOff')
CRATE_IDS = (60216, 60217, 11861)
CONTRACT = {
    HANDLER_SLOT: struct.pack('<I', HANDLER),
    HANDLER: bytes.fromhex('558bec8b45088b4810668b4130'),
    0x72FDD0: bytes.fromhex('558beca198bb770c53'),
    0x436750: bytes.fromhex('558bec518bc1'),
    0x436660: bytes.fromhex('568bf18b460c'),
    0x730280: bytes.fromhex('558bec83ec4ca198bb770c'),
    0xDD5654: b'codo.objects.ConsumeSupplyDropResponseObj\0',
    0xDD5590: b'codo.objects.SupplyDropGrantedItemObj\0',
    0x830B80: bytes.fromhex('558b'),
}


def load_reward_pool(path, catalog):
    """读取商品内容或武器物品表，按原生目录识别可叠加物品。"""
    rewards = []
    seen_ids = set()
    with path.open(encoding='utf-8-sig', newline='') as stream:
        for row in csv.DictReader(stream):
            product_pool = '商品ID' in row
            reward_id = int(row['商品ID'] if product_pool else row['物品ID'])
            if reward_id in seen_ids:
                raise ValueError(f'奖池编号重复：{reward_id}')
            seen_ids.add(reward_id)
            entries = row['物品列表（ID:数量）'].split(';') if product_pool else [f'{reward_id}:1']
            contents = []
            for entry in entries:
                item_id, count = map(int, entry.strip().split(':'))
                if item_id not in catalog or count <= 0:
                    raise ValueError(f'奖池物品或数量无效：{item_id}')
                stackable = bool(bytes.fromhex(catalog[item_id]['raw'])[32] & 2)
                contents.append({'item_id': item_id, 'stackable': stackable,
                                 'count': count if stackable else 1})
            rewards.append({'product_id': reward_id if product_pool else 0, 'contents': contents})
    if not rewards:
        raise ValueError('奖池为空')
    return tuple(rewards)


def encode_result(state, crate_id, reward):
    """将商品展开为原生物品奖励，保留实际物品编号及数量变化。"""
    contents = reward['contents']
    payload = bytearray(max(MESSAGE + 384, FIELDS + (len(contents) * len(REWARD_FIELDS) + 1) * 36))
    if len(payload) > CAPACITY:
        raise ValueError('开箱奖励结果超出数据页')
    struct.pack_into('<I', payload, 4, len(contents))
    for offset in (ARRAY, RESPONSE, ITEMS, REWARD, UNDEFINED):
        struct.pack_into('<IIII', payload, offset, 0xffffffff, 0xffffffff, 0, 0)
    names_offset = 768
    name_addresses = {}
    for name in (*REWARD_FIELDS, 'supplyDropID'):
        name_bytes = name.encode('ascii') + b'\0'
        payload[names_offset:names_offset + len(name_bytes)] = name_bytes
        name_addresses[name] = state + names_offset
        names_offset += len(name_bytes)
    field_values = []
    for content in contents:
        # 原生 UI 将非零 productID 解释为礼包；逐物品回显必须走 GetItemInfo。
        values = (content['item_id'], 0, 0, False, 0, 0, False,
                  content['quantity'], content['quantity_delta'], False, False)
        field_values.extend(zip(REWARD_FIELDS, values))
    field_values.append(('supplyDropID', crate_id))
    for field_index, (name, value) in enumerate(field_values):
        struct.pack_into('<IIIIII', payload, FIELDS + field_index * 36,
                         name_addresses[name], 0xffffffff, 0xffffffff, 0,
                         2 if type(value) is bool else 3, int(value))
    payload[1200:1213] = b'grantedItems\0'
    error_bytes = '开箱结果构造失败，未消耗军火箱'.encode('utf-8') + b'\0'
    payload[MESSAGE:MESSAGE + len(error_bytes)] = error_bytes
    return bytes(payload)


def build_adapter(entry, state, ready_address):
    """保留主线程寄存器；先构造回显，再提交库存，最后刷新并完成任务。"""
    instructions, labels, branches, traps = bytearray(), {}, [], {}
    def emit(value): instructions.extend(bytes.fromhex(value))
    def word(value): instructions.extend(struct.pack('<I', value & 0xffffffff))
    def push(value): emit('68'); word(value)
    def call(address): emit('e8'); word(address - entry - len(instructions) - 4)
    def branch(opcode, name):
        emit(opcode); branches.append((len(instructions), name)); word(0)
    def phase(name):
        emit('8be5'); traps[entry + len(instructions)] = name; emit('cc')
    def object_pointer(offset): emit('b9'); word(state + offset)
    def check_result(): emit('84c0'); branch('0f84', 'failed')
    def create_named(offset, class_address):
        call(0x5F1BC0); emit('89c7 6a00 6a00'); push(class_address); push(state + offset)
        emit('8b0d'); word(0xC77BB98); call(0x830B80)
        emit('85ff'); branch('0f84', 'unlocked_' + str(offset))
        call(0x5F1C80); labels['unlocked_' + str(offset)] = len(instructions)
        emit('a1'); word(state + offset + 12); emit('258f000000 83f801')
        branch('0f86', 'failed')
    def append(array_offset, object_offset):
        object_pointer(array_offset + 8); emit('8b09 8b11')
        push(state + object_offset); emit('ff35'); word(state + array_offset + 16)
        emit('ff523c'); check_result()

    emit('9c60 8bec 833d'); word(ready_address); emit('01'); branch('0f85', 'original')
    emit('a1'); word(0x849B874); emit('85c0'); branch('0f84', 'original')
    emit('83780c00'); branch('0f85', 'original')
    phase('request')
    emit('833d'); word(state + SUCCESS); emit('01'); branch('0f85', 'failed')
    emit('83e4f0')
    for offset in (ARRAY, ITEMS):
        push(state + offset); call(0x72FDD0); emit('83c404'); check_result()
    create_named(RESPONSE, 0xDD5654)
    emit('be'); word(state + FIELDS)
    labels['reward'] = len(instructions)
    create_named(REWARD, 0xDD5590)
    emit('bf'); word(len(REWARD_FIELDS))
    labels['field'] = len(instructions)
    emit('8d4604 50 ff36'); object_pointer(REWARD); call(0x436750); check_result()
    emit('83c624 4f'); branch('0f85', 'field')
    append(ITEMS, REWARD)
    object_pointer(REWARD); call(0x436660)
    emit('ff0d'); word(state + 4); branch('0f85', 'reward')
    emit('8d4604 50 ff36'); object_pointer(RESPONSE); call(0x436750); check_result()
    push(state + ITEMS); push(state + 1200); object_pointer(RESPONSE); call(0x436750); check_result()
    append(ARRAY, RESPONSE)
    phase('commit')
    emit('833d'); word(state + SUCCESS); emit('01'); branch('0f85', 'failed')
    emit('83e4f0'); call(0x5E9720)
    push(state + ARRAY); push(state + MESSAGE); emit('6a01'); push(1200); call(0x730280)
    emit('83c410'); branch('e9', 'cleanup')
    labels['failed'] = len(instructions)
    emit('c705'); word(state + SUCCESS); word(0)
    emit('83e4f0'); push(state + UNDEFINED); push(state + MESSAGE); emit('6a00'); push(1200)
    call(0x730280); emit('83c410')
    labels['cleanup'] = len(instructions)
    for offset in (REWARD, RESPONSE, ITEMS, ARRAY):
        object_pointer(offset); call(0x436660)
    phase('completed')
    emit('8be5 619d c20400')
    labels['original'] = len(instructions)
    emit('8be5 619d e9'); word(HANDLER - entry - len(instructions) - 4)
    for offset, label_name in branches:
        struct.pack_into('<i', instructions, offset, labels[label_name] - offset - 4)
    return {'code': bytes(instructions), 'traps': traps}


class SupplyDropBridge:
    """管理单个原生任务的计划，成功通知路径绝不重新抽取或发奖。"""
    def __init__(self, process, profiles, record, errors):
        self.process, self.profiles, self.record, self.errors = process, profiles, record, errors
        self.traps, self.pending = {}, None

    def install(self, ready_address):
        """核验固定版本后仅替换开箱专用虚表槽。"""
        for address, expected in CONTRACT.items():
            if self.process.read(address, len(expected)) != expected:
                raise ValueError(f'开箱原生约定不匹配：{address:#x}')
        self.state = self.process.allocate(CAPACITY)
        entry = self.process.allocate(CAPACITY)
        plan = build_adapter(entry, self.state, ready_address)
        if len(plan['code']) > CAPACITY:
            raise ValueError('开箱适配超出代码页')
        checked_write(self.process, entry, plan['code'])
        self.process.executable(entry, CAPACITY)
        checked_write(self.process, HANDLER_SLOT, struct.pack('<I', entry))
        self.traps = plan['traps']

    def handle(self, event, address):
        """在暂停的游戏主线程提取命令或提交已经构造完毕的结果。"""
        if event.tid != self.process.main_thread_id:
            raise ValueError('开箱必须在游戏主线程执行')
        thread, context = self.process.context(event.tid)
        phase = self.traps[address]
        try:
            try:
                if phase == 'request':
                    self.pending = None
                    checked_write(self.process, self.state, encode_result(self.state, 0, {'product_id': 0, 'contents': []}))
                    frame = struct.unpack_from('<I', context, 180)[0]
                    if frame != struct.unpack_from('<I', context, 196)[0]:
                        raise ValueError('开箱桥保存帧不匹配')
                    parameters = u32(self.process.read, frame + 40)
                    if u32(self.process.read, parameters + 20) != 2:
                        raise ValueError('开箱参数数量无效')
                    values = u32(self.process.read, parameters + 16)
                    if any(u32(self.process.read, values + offset + 12) not in (3, 4) for offset in (0, 32)):
                        raise ValueError('开箱参数类型无效')
                    crate_id = u32(self.process.read, values + 16)
                    if crate_id not in CRATE_IDS or u32(self.process.read, values + 48) != 1:
                        raise ValueError('本地开箱每次只支持一个军火箱')
                    self.pending = self.profiles.prepare_supply_drop(crate_id)
                    checked_write(self.process, self.state, encode_result(self.state, crate_id, self.pending['reward']))
                    checked_write(self.process, self.state + SUCCESS, struct.pack('<I', 1))
                elif phase == 'commit':
                    if self.pending is None:
                        raise ValueError('没有待提交的开箱结果')
                    self.profiles.commit_supply_drop(self.pending)
                    checked_write(self.process, self.state + MESSAGE, b'\0')
                    self.record({'supply_drop_committed': self.pending['reward']})
                    self.pending = None
                else:
                    self.pending = None
                    self.record({'supply_drop_completed': bool(u32(self.process.read, self.state + SUCCESS))})
            except self.errors as error:
                self.pending = None
                message = str(error).encode('utf-8')[:360].decode('utf-8', errors='ignore').encode('utf-8') + b'\0'
                checked_write(self.process, self.state + SUCCESS, bytes(4))
                checked_write(self.process, self.state + MESSAGE, message)
                self.record({'supply_drop_error': str(error)})
            struct.pack_into('<I', context, 184, address + 1)
            self.process.set_context(thread, context)
        finally:
            self.process.close_thread(thread)
