"""验证本地频道状态适配和原生显示检查的恢复；不代替实机验收。"""
from pathlib import Path
import itertools
import struct
import sys
import unittest
import zlib

from codol_rebuild.adapters.game.v1649370 import movie_identity
from codol_rebuild.adapters.game.v1649370.chat import ui_chat
from codol_rebuild.adapters.game.v1649370.session import ui_movie

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / 'research'))
from trace_matchmaking_debugline import Reader, abc_parse, disassemble


def read_chat_methods(movie_bytes):
    """从真实电影解析 ChatView 的方法和指令边界。"""
    reader = Reader(movie_bytes)
    reader.p = 8 + (5 + 4 * (movie_bytes[8] >> 3) + 7) // 8 + 4
    while reader.p < len(movie_bytes):
        header = struct.unpack('<H', reader.raw(2))[0]
        tag_kind, payload_size = header >> 6, header & 63
        if payload_size == 63:
            payload_size = struct.unpack('<I', reader.raw(4))[0]
        payload = reader.raw(payload_size)
        payload_reader = Reader(payload)
        if tag_kind != 82:
            continue
        payload_reader.raw(4)
        if payload_reader.cstr() != 'codo/views/ChatView':
            continue
        methods, names, strings = abc_parse(payload[payload_reader.p:])
        return {method['trait'].split('::')[-1]:
                (method, disassemble(method, names, strings))
                for method in methods if 'trait' in method and 'code' in method}
    raise AssertionError('缺少 ChatView')


def evaluate_method(instructions, view_state, providers, parameter=None, stop_offset=None,
                    start_offset=0):
    """执行目标 UI 判断的指令子集，记录属性写入与通知参数。"""
    instruction_map = {entry['offset']: index for index, entry in enumerate(instructions)}
    value_stack = []
    calls = []
    instruction_index = instruction_map[start_offset]
    while instruction_index < len(instructions):
        entry = instructions[instruction_index]
        if entry['offset'] == stop_offset:
            return None, calls
        operation, operands = entry['instruction'], entry['args']
        instruction_index += 1
        if operation in ('debugfile', 'debugline', 'debug', 'nop', 'label'):
            continue
        if operation == 'getlocal0':
            value_stack.append(view_state)
        elif operation == 'getlocal1':
            value_stack.append(parameter)
        elif operation in ('pop', 'pushscope'):
            value_stack.pop()
        elif operation == 'pushtrue':
            value_stack.append(True)
        elif operation == 'pushfalse':
            value_stack.append(False)
        elif operation == 'pushnull':
            value_stack.append(None)
        elif operation == 'pushbyte':
            value_stack.append(operands[0] if operands[0] < 128 else operands[0] - 256)
        elif operation == 'getlex':
            value_stack.append(providers[operands[0].split('::')[-1]])
        elif operation == 'newarray':
            value_stack.append([value_stack.pop() for unused_index in range(operands[0])][::-1])
        elif operation == 'findpropstrict':
            value_stack.append(providers)
        elif operation == 'getproperty':
            property_name = operands[0].split('::')[-1]
            value_stack.append(value_stack.pop()[property_name])
        elif operation in ('setproperty', 'initproperty'):
            property_value = value_stack.pop()
            value_stack.pop()[operands[0].split('::')[-1]] = property_value
        elif operation == 'dup':
            value_stack.append(value_stack[-1])
        elif operation in ('not', 'convert_b'):
            property_value = bool(value_stack.pop())
            value_stack.append(not property_value if operation == 'not' else property_value)
        elif operation == 'equals':
            value_stack.append(value_stack.pop() == value_stack.pop())
        elif operation in ('iftrue', 'iffalse', 'ifne', 'jump'):
            if operation == 'jump':
                should_jump = True
            elif operation == 'ifne':
                should_jump = value_stack.pop() != value_stack.pop()
            else:
                should_jump = bool(value_stack.pop()) == (operation == 'iftrue')
            if should_jump:
                instruction_index = instruction_map[operands[0]]
        elif operation in ('callproperty', 'callpropvoid'):
            argument_count = operands[1]
            arguments = [value_stack.pop() for unused_index in range(argument_count)][::-1]
            value_stack.pop()
            method_name = operands[0].split('::')[-1]
            calls.append((method_name, arguments))
            if operation != 'callpropvoid':
                value_stack.append(providers[arguments[0]] if method_name == 'getDP' else None)
        elif operation in ('returnvoid', 'returnvalue'):
            result = value_stack.pop() if operation == 'returnvalue' else None
            if value_stack:
                raise AssertionError('返回时操作数栈未清空')
            return result, calls
        else:
            raise AssertionError(f'未支持的指令：{operation}')
    raise AssertionError('方法未完成')


class ChatUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original = (PROJECT_ROOT / 'research/client/ui-investigation/codo.gfx').read_bytes()
        cls.patched = movie_identity.apply_rules(cls.original, ui_chat.replacements())
        cls.before = read_chat_methods(cls.original)
        cls.after = read_chat_methods(cls.patched)

    def test_only_approved_spans_change_and_combined_movie_fits(self):
        """范围外逐字节不变，组合电影仍能在原生压缩容量内发布。"""
        covered_offsets = set()
        for offset, original_bytes, replacement_bytes in ui_chat.replacements():
            self.assertEqual(len(original_bytes), len(replacement_bytes))
            covered_offsets.update(range(offset, offset + len(original_bytes)))
        changed_offsets = {index for index, (original_byte, patched_byte)
                           in enumerate(zip(self.original, self.patched))
                           if original_byte != patched_byte}
        self.assertTrue(changed_offsets <= covered_offsets)
        self.assertLessEqual(len(zlib.compress(ui_movie.patch_movie(self.original), 9)),
                             movie_identity.COMPRESSED_SIZE)
        for method_name, (method, instructions) in self.after.items():
            self.assertEqual(method['limits'], self.before[method_name][0]['limits'])
            boundaries = {entry['offset'] for entry in instructions}
            for entry in instructions:
                if entry['instruction'].startswith('if') or entry['instruction'] == 'jump':
                    self.assertIn(entry['args'][0], boundaries)
        for method_name in ('onPostMessage', 'onLoudSpeakerBtnClicked',
                            'onLoudSpeakerSendSuccess', 'onShowSpeakAnimation',
                            'enableData', 'onBigSpeakerChannelStateChanged',
                            'refreshHistoryList', 'checkCanChangeMainChatPanel'):
            self.assertEqual(self.before[method_name][0]['code'], self.after[method_name][0]['code'])

    def test_status_checks_follow_local_channel_existence_and_preserve_locks(self):
        """覆盖已加入、未加入、关闭、故障组合以及输入展开状态。"""
        for joined, locked, broken, expanded in itertools.product((False, True), repeat=4):
            providers = {'GLOBAL': 0, 'INPUT_STATE': 1}
            for provider_name, provider_value in (
                ('IS_IN_GLOBAL_CHANNEL', joined), ('IS_IN_BIG_SPEAKER_CHANNEL', joined),
                ('IS_GLOBAL_CHAT_LOCKED', locked), ('IS_GLOBAL_CHAT_BROKEN', broken),
                ('JOINED_GLOBAL_CHAT_GROUP', 7), ('CHAT_CHANNEL_OBJ', {'id': 0} if joined else None)):
                providers[provider_name] = provider_name
                providers[provider_name + '_value'] = provider_value
            provider_keys = {name: name + '_value' for name in providers if name.startswith(('IS_', 'JOINED_', 'CHAT_')) and not name.endswith('_value')}
            providers['DataID'] = provider_keys
            providers['ChattingChannelType'] = {'GLOBAL': 0}
            view_state = {'mCurrentChannelType': 0, 'mState': int(expanded),
                          'myChatList': {}, 'myControlPanel': {},
                          'mySmallSpeakerBtn': {}, 'myBigSpeakerBtn': {}, 'mAllRawChannels': []}
            unused_result, calls = evaluate_method(self.after['onGlobalChatChannelStateChanged'][1], view_state, providers)
            self.assertEqual(view_state['mGlobalChatJoined'], joined)
            self.assertEqual(view_state['mGlobalChatLocked'], locked or broken)
            self.assertEqual(view_state['mySmallSpeakerBtn']['enabled'], joined)
            self.assertIn(('switchJoiningGlobalChatNote', [not joined or locked or broken]), calls)
            self.assertEqual(any(name == 'tryToCloseInput' for name, arguments in calls), not joined and expanded)
            evaluate_method(self.after['onBigSpeakerChannelStateChanged'][1], view_state, providers)
            self.assertEqual(view_state['myBigSpeakerBtn']['enabled'], joined and not locked and not broken)
            evaluate_method(self.after['enableData'][1], view_state, providers,
                            start_offset=58, stop_offset=106)
            self.assertEqual(view_state['mySmallSpeakerBtn']['enabled'], joined)
            self.assertEqual(view_state['myBigSpeakerBtn']['enabled'], joined and not locked)
            unused_result, history_calls = evaluate_method(
                self.after['refreshHistoryList'][1], view_state, providers,
                start_offset=56, stop_offset=115)
            self.assertEqual(history_calls, [('switchJoiningGlobalChatNote', [not joined])])
            for want_to_expand in (False, True):
                result, unused_calls = evaluate_method(self.after['checkCanChangeMainChatPanel'][1], view_state, providers, want_to_expand)
                self.assertEqual(result, not want_to_expand or joined)


if __name__ == '__main__':
    unittest.main()
