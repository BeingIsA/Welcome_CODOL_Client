"""对局原生边界：房主保留预订流程，成员应用已确认的连接通知。

查找限62932F专用调用边；控制消息限当前房间及已分配DS身份。所有进程操作
发生在主线程暂停点，各阶段使用应用会话已确认资料。未接管消息继续原方法。
"""
import base64
import struct
from codol_rebuild.adapters.game.v1649370.matches.allocation_record import allocation_fields

ROOM_POINTER = 0x82E13FC
FIND_CALL = 0x62932F
BINARY_SLOT = 0xDC7274 + 0x14C
NOTIFICATION_SLOT = 0xDC7274 + 0x148
NOTIFICATION_FRAME_CALL = 0x808259
IDENTITY_CALLS = ((0x5BF864, bytes.fromhex('e8d7812400')),
                  (0x5C07BE, bytes.fromhex('e87d722400')))


class MatchBridge:
    """一个受控原生记录对应一个分配；不拥有玩家或房间权威状态。"""
    def __init__(self, process, session, current_room, server_address, record):
        self.process, self.session, self.current_room = process, session, current_room
        self.server_address, self.record = server_address, record
        self.traps = {}
        self.allocation = None
        self.room_id = None
        self.room_pointer = None
        self.pending_start_phase = None
        self.constructed = False
        self.callback_fields = None
        self.start_notification = None
        self.delivered_allocation = None
        self.notification_ready = False
        self.member_start_id = None
        self.notify = lambda message: self.record({'match_start_error': message})

    def integer(self, address):
        return struct.unpack('<I', self.process.read(address, 4))[0]

    def install(self, identity_address):
        """核对原始调用；由装配层注入大厅既有身份地址。"""
        if self.process.read(FIND_CALL, 5) != bytes.fromhex('e87cb11c00'):
            raise ValueError('原生分配专用调用边不匹配')
        if self.integer(BINARY_SLOT) != 0x629730:
            raise ValueError('原生房间二进制发送虚表不匹配')
        if self.integer(NOTIFICATION_SLOT) != 0x62C580:
            raise ValueError('原生房间通知发送虚表不匹配')
        if self.process.read(NOTIFICATION_FRAME_CALL, 5) != bytes.fromhex('e8726efeff'):
            raise ValueError('原生通知帧调度调用边不匹配')
        for call_address, original_bytes in IDENTITY_CALLS:
            if self.process.read(call_address, 5) != original_bytes:
                raise ValueError('原生游戏连接身份调用边不匹配')
        self.identity_address = identity_address
        self.code = self.process.allocate(4096)
        self.native_record = self.process.allocate(0x238)
        self.message = self.process.allocate(256)
        self.notification_flag = self.process.allocate(4)
        self.member_message = self.process.allocate(16)
        self.process.write(self.message, b'mmbook accepted\0CODOL Local\0')
        # trap处尚未执行指令，寄存器/栈保持原始调用约定。
        for offset, phase, original, cleanup in ((0, 'find', 0x7F44B0, 0), (32, 'send', 0x629730, 24),
                                                  (64, 'notification', 0x62C580, 4),
                                                  (96, 'identity', 0x807A40, 0)):
            entry = self.code + offset
            code = b'\xcc\xe9' + struct.pack('<i', original-entry-6)
            code += b'\xc2'+struct.pack('<H', cleanup) if cleanup else b'\xc3'
            self.process.write(entry, code)
            self.traps[entry] = phase
        self.process.executable(self.code, 4096)
        self.process.write(FIND_CALL, b'\xe8'+struct.pack('<i', self.code-FIND_CALL-5))
        self.process.write(BINARY_SLOT, struct.pack('<I', self.code+32))
        self.process.write(NOTIFICATION_SLOT, struct.pack('<I', self.code+64))
        for call_address, original_bytes in IDENTITY_CALLS:
            self.process.write(call_address, b'\xe8'+struct.pack('<i', self.code+96-call_address-5))
        # 在原生帧调度点消费一次确认；UI 帧内只排队，避免释放仍用于本帧绘制的世界。
        frame_entry = self.code+1024
        frame_code = b'\x9c\x60\x83\x3d'+struct.pack('<I', self.notification_flag)+bytes.fromhex('00 7401 cc 619d e9')
        frame_code += struct.pack('<i', 0x7EF0D0-frame_entry-len(frame_code)-4)
        self.process.write(frame_entry, frame_code)
        self.frame_trap = frame_entry+11
        self.traps[self.frame_trap] = 'notification_frame'
        self.process.write(NOTIFICATION_FRAME_CALL, b'\xe8'+struct.pack('<i', frame_entry-NOTIFICATION_FRAME_CALL-5))

    def handle(self, event, address):
        """识别原生请求来源，接受排队不等同于服务端业务成功。"""
        if event.tid != self.process.main_thread_id:
            raise ValueError('对局适配必须在原生主线程执行')
        thread, context = self.process.context(event.tid)
        try:
            phase = self.traps[address]
            stack = struct.unpack_from('<I', context, 196)[0]
            next_instruction = address+1
            if phase == 'identity':
                # 仅已确认开局的当前房间连接使用既有映射；其他调用仍进入原生 getter。
                if (self.allocation is not None and self.room_id is not None
                        and self.delivered_allocation == self.allocation['allocationId']
                        and self.current_room() == self.room_id
                        and self.integer(ROOM_POINTER) == self.room_pointer):
                    identity_low, identity_high = struct.unpack('<II', self.process.read(self.identity_address, 8))
                    if identity_low or identity_high:
                        # 游戏连接 getter 使用 EAX 低位、EDX 高位，与房间适配约定不同。
                        struct.pack_into('<I', context, 168, identity_high)
                        struct.pack_into('<I', context, 176, identity_low)
                        next_instruction = address+6
            elif phase == 'notification_frame':
                accepted = (self.notification_ready and self.current_room() == self.room_id
                            and self.integer(ROOM_POINTER) == self.room_pointer)
                next_instruction = self.notification_callback(address+1, accepted)
            elif phase == 'notification':
                native_room = struct.unpack_from('<I', context, 172)[0]
                # 仅预订回复中 632809 的专用调用；其他房间通知原样执行。
                if (self.integer(stack) == 0x63280B and native_room == self.room_pointer
                        and self.current_room() == self.room_id and self.pending_start_phase is not None
                        and self.start_notification is None and self.allocation is not None
                        and self.delivered_allocation != self.allocation['allocationId']):
                    container = self.integer(stack+4)
                    if self.integer(container) != 0xDC74F8 or self.integer(container+16) != 1:
                        raise ValueError('原生开局通知容器与采集约定不一致')
                    node = self.integer(container+12)
                    buffer_pointer = self.integer(node)
                    if (self.integer(buffer_pointer) != 0xD59834
                            or self.integer(buffer_pointer+28) != 1
                            or self.process.read(self.integer(buffer_pointer+8), 3) != bytes.fromhex('068600')):
                        raise ValueError('原生 type=3 通知与采集约定不一致')
                    self.start_notification = buffer_pointer
                    # 用原生 InterlockedIncrement 保留对象；随后照常序列化原有外发队列。
                    entry = self.code+512
                    instructions = b'\x9c\x60\x68'+struct.pack('<I', buffer_pointer+4)
                    instructions += bytes.fromhex('ff152881ca00 619d e9')
                    instructions += struct.pack('<i', 0x62C580-entry-len(instructions)-4)
                    self.process.write(entry, instructions)
                    next_instruction = entry
                    self.record({'match_start_notification': 'retained'})
            elif phase == 'member_serialized':
                self.start_notification = self.integer(self.member_message+12)
                self.notification_ready = True
                self.process.write(self.notification_flag, struct.pack('<I', 1))
                self.session.acknowledge_start(self.room_id, self.member_start_id)
                self.member_start_id = None
            elif phase == 'constructed':
                for offset, value in self.callback_fields:
                    self.process.write(self.native_record+offset, value)
                self.constructed = True
            elif phase == 'find':
                room_id = self.current_room()
                room_pointer = self.integer(ROOM_POINTER)
                parameters = struct.unpack('<5I', self.process.read(stack+4, 20))
                if room_id and parameters[0] == 0x27 and parameters[3] == room_pointer+0x7470 and parameters[4] == 0x637930:
                    accepted = self.session.begin('allocate', room_id)
                    if accepted:
                        self.room_id, self.room_pointer = room_id, room_pointer
                        self.record({'match_request': 'allocate', 'room_id': room_id})
                    struct.pack_into('<I', context, 176, int(accepted))
                    next_instruction = address+6
            elif phase == 'send' and self.allocation is not None:
                native_room = struct.unpack_from('<I', context, 172)[0]
                pointer, length, identity_low, identity_high, _, _ = struct.unpack('<6I', self.process.read(stack+4, 24))
                identity = identity_low | identity_high << 32
                if native_room == self.room_pointer and identity == int(self.allocation['serverUserId']):
                    accepted = False
                    body = self.process.read(pointer, length) if 0 < length <= 4096 else b''
                    if body.startswith(b'mmbook '):
                        fields = body.split()
                        if len(fields) == 4 and base64.b64decode(fields[2], validate=True).hex() == self.allocation['securityId']:
                            accepted = self.session.begin('book', self.room_id, self.allocation['allocationId'], self.allocation['securityId'])
                            self.record({'match_request': 'book', 'queued': accepted})
                    elif body.startswith(b'mmstart '):
                        if self.pending_start_phase is None and self.current_room() == self.room_id:
                            native_command = body.decode('ascii')
                            self.pending_start_phase = 'start_received'
                            accepted = True
                            self.record({'match_native_start': native_command})
                    elif body.startswith(b'mmban\0'):
                        if self.pending_start_phase == 'start_received':
                            self.pending_start_phase = 'item_bans_received'
                            accepted = True
                            self.record({'match_native_item_bans': body.hex()})
                    else:
                        struct.pack_into('<I', context, 184, next_instruction)
                        self.process.set_context(thread, context)
                        return
                    struct.pack_into('<I', context, 176, int(accepted))
                    next_instruction = address+6
            struct.pack_into('<I', context, 184, next_instruction)
            self.process.set_context(thread, context)
        finally:
            self.process.close_thread(thread)

    def callback(self, resume_address, success, booking=False):
        """原生调用在帧暂停点执行；保存并恢复完整整数寄存器和标志。"""
        entry = self.code+128
        instructions = bytearray(b'\x9c\x60')
        def push(value):
            instructions.extend(b'\x68'+struct.pack('<I', value))
        def call(value):
            instructions.extend(b'\xe8'+struct.pack('<i', value-entry-len(instructions)-5))
        if booking:
            identity = int(self.allocation['serverUserId'])
            for value in (15, self.message, self.message+16, identity >> 32, identity & 0xFFFFFFFF):
                push(value)
            instructions.extend(b'\xb9'+struct.pack('<I', self.room_pointer))
            call(0x632680)
        else:
            if success:
                if not self.constructed:
                    push(1)
                    instructions.extend(b'\xb9'+struct.pack('<I', self.native_record))
                    call(0x806B50)
                    self.traps[entry+len(instructions)] = 'constructed'
                    instructions.extend(b'\xcc')
                else:
                    for offset, value in self.callback_fields:
                        self.process.write(self.native_record+offset, value)
            for value in (self.native_record if success else 0, int(success), 0, int(success), 0):
                push(value)
            call(0x637930)
            instructions.extend(b'\x83\xc4\x14')
        instructions.extend(b'\x61\x9d\xe9'+struct.pack('<i', resume_address-entry-len(instructions)-7))
        self.process.write(entry, bytes(instructions))
        return entry

    def member_notification(self, resume_address):
        """复用原生对象构造与 type=3 序列化，持有一个引用交给下一帧接收器。"""
        entry = self.code+1536
        instructions = bytearray(b'\x9c\x60')
        def push(value):
            instructions.extend(b'\x68'+struct.pack('<I', value))
        def call(value):
            instructions.extend(b'\xe8'+struct.pack('<i', value-entry-len(instructions)-5))
        if not self.constructed:
            push(1)
            instructions.extend(b'\xb9'+struct.pack('<I', self.native_record))
            call(0x806B50)
            self.traps[entry+len(instructions)] = 'constructed'
            instructions.extend(b'\xcc')
        else:
            for offset, value in self.callback_fields:
                self.process.write(self.native_record+offset, value)
        self.process.write(self.member_message, struct.pack('<4I', 0xDC751C, 3, self.native_record, 0))
        # 与 62ABF0 相同的分配器和 bdBitBuffer(0, false)，构造后引用数为零。
        push(0x24)
        call(0xB74220)
        instructions.extend(bytes.fromhex('83c404 6a00 6a00 8bc8'))
        call(0xB778E0)
        instructions.extend(b'\xa3'+struct.pack('<I', self.member_message+12))
        instructions.extend(bytes.fromhex('83c004 50 ff152881ca00'))
        push(self.member_message+12)
        instructions.extend(b'\xb9'+struct.pack('<I', self.member_message))
        call(0x625BE0)
        self.traps[entry+len(instructions)] = 'member_serialized'
        instructions.extend(b'\xcc\x61\x9d\xe9'+struct.pack('<i', resume_address-entry-len(instructions)-8))
        self.process.write(entry, bytes(instructions))
        return entry

    def poll(self, resume_address):
        """每轮检查生命周期和事件；结果只消费一次，仅原生调用暂停本轮调度。"""
        next_address = self.clear_departed_room(resume_address)
        if next_address is not None:
            return next_address
        start = self.session.poll_start()
        if start is not None:
            next_address = self.apply_start_event(start, resume_address)
            if next_address is not None:
                return next_address
        completion = self.session.poll()
        if completion is not None:
            next_address = self.apply_completion(completion, resume_address)
            if next_address is not None:
                return next_address
        return self.poll_start_confirmation(resume_address)

    def clear_departed_room(self, resume_address):
        """不等待网络返回就撤销旧房间状态；持有通知时先执行原生释放。"""
        if self.current_room() == self.room_id and self.integer(ROOM_POINTER) == self.room_pointer:
            return None
        self.member_start_id = None
        self.pending_start_phase = None
        self.allocation = None
        self.delivered_allocation = None
        if self.start_notification is not None:
            return self.notification_callback(resume_address, False)
        return None

    def apply_start_event(self, start, resume_address):
        """分流权威开局结果；返回 None 表示本轮仍需检查房主请求结果。"""
        if self.current_room() != start['roomId']:
            return None
        if self.member_start_id is not None and (start['status'] == 'failed'
                or start['startId'] != self.member_start_id):
            self.member_start_id = None
        if start['status'] == 'failed':
            self.pending_start_phase = None
            self.notify(start.get('error') or '开局失败，请重新准备')
            self.session.acknowledge_start(start['roomId'], start['startId'])
            if self.start_notification is not None:
                return self.notification_callback(resume_address, False)
            return None
        room_pointer = self.integer(ROOM_POINTER)
        if room_pointer < 0x10000 or not self.integer(room_pointer + 0x1C):
            return None
        if not self.process.read(room_pointer+0x278, 1)[0]:
            self.begin_member_connection(start, room_pointer)
            return None
        if self.member_start_id is not None:
            return None
        entry = self.build_host_start_call(room_pointer, resume_address)
        self.session.acknowledge_start(start['roomId'], start['startId'])
        self.record({'formal_start': start['startId'], 'roomId': start['roomId']})
        return entry

    def begin_member_connection(self, start, room_pointer):
        """成员只排队一次；结果应用或明确失败前保留开局事件。"""
        if self.member_start_id is None and self.session.begin(
                'member_connect', start['roomId'], start_id=start['startId']):
            self.room_id, self.room_pointer = start['roomId'], room_pointer
            self.member_start_id = start['startId']

    def build_host_start_call(self, room_pointer, resume_address):
        """生成房主 StartMatch 调用桩，仅负责原生约定和现场恢复。"""
        if self.process.read(0x62F840, 8) != bytes.fromhex('558becb814200000'):
            raise ValueError('原生StartMatch调用约定不匹配')
        entry = self.code + 1280
        instructions = bytearray(b'\x9c\x60\xb9' + struct.pack('<I', room_pointer))
        instructions.extend(bytes.fromhex('6a006aff6a01'))
        instructions.extend(b'\xe8' + struct.pack('<i', 0x62F840-entry-len(instructions)-5))
        instructions.extend(b'\x61\x9d\xe9' + struct.pack('<i', resume_address-entry-len(instructions)-7))
        self.process.write(entry, bytes(instructions))
        return entry

    def apply_completion(self, completion, resume_address):
        """检查房间生命周期后按请求种类分派；None 保留后续待提交处理。"""
        self.record({'match_completion': completion.operation, 'succeeded': completion.error is None,
                     'error': str(completion.error) if completion.error else None})
        if self.current_room() != self.room_id or self.integer(ROOM_POINTER) != self.room_pointer:
            return None
        if completion.operation == 'member_connect':
            return self.apply_member_completion(completion, resume_address)
        if completion.operation == 'allocate':
            return self.apply_allocation_completion(completion, resume_address)
        if completion.operation == 'book':
            return self.apply_booking_completion(completion, resume_address)
        if completion.operation == 'start':
            return self.apply_start_completion(completion, resume_address)
        return None

    def apply_allocation_completion(self, completion, resume_address):
        """转换分配字段并通过原生回调报告成功或失败。"""
        success = completion.error is None
        if success:
            allocation = completion.data
            try:
                self.callback_fields = allocation_fields(self.server_address, allocation['port'],
                    bytes.fromhex(allocation['securityId']), bytes.fromhex(allocation['securityKey']),
                    int(allocation['serverUserId']), allocation['serverName'], allocation['maxPlayers'])
                self.allocation = allocation
            except (KeyError, TypeError, ValueError) as error:
                self.record({'match_allocation_rejected': str(error)})
                success = False
        return self.callback(resume_address, success)

    def apply_booking_completion(self, completion, resume_address):
        """匹配本人预订后安排原生回复；失败沿用后续轮询流程。"""
        if completion.error is None:
            if completion.data.get('booked') is True and completion.data.get('allocationId') == self.allocation['allocationId']:
                return self.callback(resume_address, True, booking=True)
            self.record({'match_booking_rejected': '服务端确认与当前分配不匹配'})
        return None

    def apply_start_completion(self, completion, resume_address):
        """开局确认只安排安全帧投递；未确认时释放保留通知。"""
        accepted = (completion.error is None and completion.data.get('started') is True
                    and completion.data.get('allocationId') == self.allocation['allocationId'])
        if accepted and self.start_notification is not None:
            self.notification_ready = True
            self.process.write(self.notification_flag, struct.pack('<I', 1))
            self.record({'match_start_notification': 'queued_for_native_frame'})
            return None
        if self.start_notification is not None:
            return self.notification_callback(resume_address, accepted)
        return None

    def poll_start_confirmation(self, resume_address):
        """原生已生成开局消息后，按既有轮询时机交付本局确认。"""
        if self.pending_start_phase is not None:
            if self.session.begin('start', self.room_id, self.allocation['allocationId']):
                self.pending_start_phase = None
                self.record({'match_request': 'start'})
        return resume_address

    def apply_member_completion(self, completion, resume_address):
        """成员失败明确提示，成功只构造通知；连接仍由原生帧接收器执行。"""
        if self.member_start_id is None:
            return None
        try:
            if completion.error is not None:
                raise ValueError(str(completion.error))
            allocation = completion.data
            if completion.operation != 'member_connect' or allocation.get('startId') != self.member_start_id:
                raise ValueError('成员连接结果与当前开局不匹配')
            self.callback_fields = allocation_fields(self.server_address, allocation['port'],
                bytes.fromhex(allocation['securityId']), bytes.fromhex(allocation['securityKey']),
                int(allocation['serverUserId']), allocation['serverName'], allocation['maxPlayers'])
            self.allocation = allocation
        except (KeyError, TypeError, ValueError) as error:
            self.notify('成员入局失败：'+str(error))
            self.session.acknowledge_start(self.room_id, self.member_start_id)
            self.member_start_id = None
            return None
        return self.member_notification(resume_address)

    def notification_callback(self, resume_address, deliver):
        """主线程转交或释放原生消息；接收器负责解码、通知和连接及最终引用释放。"""
        self.notification_ready = False
        self.process.write(self.notification_flag, bytes(4))
        if self.start_notification is None:
            return resume_address
        buffer_pointer, self.start_notification = self.start_notification, None
        entry = self.code+768
        instructions = bytearray(b'\x9c\x60')
        def push(value):
            instructions.extend(b'\x68'+struct.pack('<I', value))
        if deliver and self.delivered_allocation != self.allocation['allocationId']:
            # 634870(room,senderLow,senderHigh,metadataLow,metadataHigh,bitBuffer)，ret24。
            # type=3 不消费 metadata/ECX；身份取本房间既有 session，不创建第二套身份。
            session_pointer = self.integer(self.room_pointer+0x1C)
            identity_low, identity_high = struct.unpack('<II', self.process.read(session_pointer+0x20, 8))
            for value in (buffer_pointer, 0, 0, identity_high, identity_low, self.room_pointer):
                push(value)
            instructions.extend(b'\x31\xc9\xe8')
            instructions.extend(struct.pack('<i', 0x634870-entry-len(instructions)-4))
            self.delivered_allocation = self.allocation['allocationId']
            self.record({'match_start_notification': 'scheduled'})
        else:
            # 与原生接收/容器销毁相同的 release：减至零才调用虚析构。
            push(buffer_pointer+4)
            instructions.extend(bytes.fromhex('ff151481ca00 85c0 750b b9'))
            instructions.extend(struct.pack('<I', buffer_pointer))
            instructions.extend(bytes.fromhex('8b01 6a01 ff10'))
            self.record({'match_start_notification': 'released'})
        instructions.extend(b'\x61\x9d\xe9'+struct.pack('<i', resume_address-entry-len(instructions)-7))
        self.process.write(entry, bytes(instructions))
        return entry
