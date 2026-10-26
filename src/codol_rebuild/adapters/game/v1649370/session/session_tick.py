"""1649370 公共前端帧调度：会话、事件分派及各原生业务发布顺序。"""
import struct
from codol_rebuild.adapters.game.v1649370.rooms.room import SETTINGS_READY_OFFSET
from codol_rebuild.adapters.game.v1649370.native_state import (
    POLL, ACTION, ROOM, SESSION, LEAVE_ARG, UNDEFINED, MESSAGE,
)


def build_tick(entry, state, ready, preview_entry, room_entries, list_publish=None, name_poll=None, broadcast_poll=None, chat_poll=None):
    """包装既有前端帧；网络等待时立即返回，仅就绪结果进入原生回调。"""
    code, labels, refs, traps = bytearray(), {}, [], {}
    def emit(value): code.extend(bytes.fromhex(value))
    def word(value): code.extend(struct.pack('<I', value))
    def label(name): labels[name] = len(code)
    def branch(op, name):
        emit(op); refs.append((len(code), name)); word(0)
    def call(address):
        emit('e8'); word((address-entry-len(code)-4) & 0xffffffff)
    def trap(name):
        emit('8be5'); traps[entry+len(code)] = name; emit('cc')
    emit('8d6424f4'); call(preview_entry); emit('8d64240c 9c60 8bec')
    emit('833d'); word(ready); emit('01'); branch('0f85', 'done')
    # 按前端帧节流，避免每帧跨进程暂停。开始请求时重置计数。
    emit('ff05'); word(state+36)
    emit('833d'); word(state+36); emit('06'); branch('0f82', 'done')
    emit('c705'); word(state+36); word(0)
    trap('session_poll')
    if broadcast_poll is not None:
        # 独立于房间请求锁轮询；原生操作仍由游戏主线程执行。
        emit('83e4f0'); call(broadcast_poll); emit('8be5')
    if name_poll is not None:
        emit('83e4f0'); call(name_poll); emit('8be5')
    if 'settings_apply' in room_entries:
        trap('realtime')
        if 'notifications' in room_entries:
            emit('83e4f0'); call(room_entries['notifications']); emit('8be5')
        if 'members_apply' in room_entries:
            emit('833d'); word(state+52); emit('00')
            branch('0f84', 'members_done')
            emit('c705'); word(state+52); word(0)
            emit('83e4f0'); call(room_entries['members_apply']); emit('8be5')
            if 'ready_notify' in room_entries:
                emit('83e4f0'); call(room_entries['ready_notify']); emit('8be5')
            trap('members_applied')
            label('members_done')
        if 'ready_notify' in room_entries:
            emit('833d'); word(state+56); emit('00')
            branch('0f84', 'ready_done')
            emit('c705'); word(state+56); word(0)
            emit('83e4f0'); call(room_entries['ready_notify']); emit('8be5')
            label('ready_done')
        emit('833d'); word(state+SETTINGS_READY_OFFSET); emit('00')
        branch('0f84', 'poll_command')
        emit('c705'); word(state+SETTINGS_READY_OFFSET); word(0)
        emit('83e4f0'); call(room_entries['settings_apply']); emit('8be5')
        trap('settings_applied')
        label('poll_command')
    emit('833d'); word(state+POLL); emit('00'); branch('0f84', 'chat')
    trap('poll')
    if list_publish is not None:
        emit('83e4f0'); call(list_publish); emit('8be5')
    emit('a1'); word(state+ACTION)
    actions = [(1, 'created'), (2, 'create_failed'), (3, 'left'), (4, 'leave_failed'),
               (7, 'departed')]
    if 'join_complete' in room_entries:
        actions += [(5, 'joined'), (6, 'join_failed')]
    for value, target in actions:
        emit('83f8'+f'{value:02x}'); branch('0f84', target)
    branch('e9', 'chat')
    if 'join_complete' in room_entries:
        label('joined')
        emit('83e4f0'); call(room_entries['join_complete'])
        branch('e9', 'completed')
        label('join_failed')
        emit('83e4f0'); call(room_entries['join_failed'])
        branch('e9', 'completed')
    label('created')
    emit('8b1d'); word(state+ROOM); emit('8b3d'); word(state+SESSION)
    emit('83e4f0'); call(room_entries['create_complete'])
    branch('e9', 'completed')
    label('create_failed')
    emit('83e4f0 83ec08 6a00 ff35'); word(state+SESSION); call(0x633FE0)
    branch('e9', 'completed')
    label('left')
    emit('83e4f0 83ec0c ff35'); word(state+LEAVE_ARG); call(room_entries['leave_complete'])
    emit('83c404 83ec0c 6a00 b9'); word(0xE69CB0); call(0x7CB7A0)
    branch('e9', 'completed')
    label('departed')
    # 被动离房没有等待中的任务；原生 listener+0x4C 已投递成功通知，不能重复投递。
    emit('83e4f0 83ec0c 6a00'); call(room_entries['leave_complete'])
    branch('e9', 'completed')
    label('leave_failed')
    # 仅完成离房任务的失败响应，不能调用会清空界面的成功通知。
    emit('83e4f0 68'); word(state+UNDEFINED)
    emit('68'); word(state+MESSAGE); emit('6a00 6a64'); call(0x730280)
    label('completed'); trap('completed')
    label('chat')
    if chat_poll is not None:
        emit('83e4f0'); call(chat_poll); emit('8be5')
    label('done'); emit('8be5 619d c3')
    for offset, target in refs:
        struct.pack_into('<i', code, offset, labels[target]-offset-4)
    return {'code': bytes(code), 'traps': traps}


