"""执行原电影的频道创建与历史字节码，验证局部修复；不替代游戏 UI 验收。"""
from pathlib import Path
import struct
import sys
import unittest
from codol_rebuild.adapters.game.v1649370 import movie_identity, ui_chat

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT/'research'))
from trace_matchmaking_debugline import Reader, abc_parse, disassemble


def movie_methods(movie, class_name):
    """解析指定原生 UI 类，保持方法控制流与常量池引用。"""
    reader = Reader(movie)
    reader.p = 8 + (5 + 4 * (movie[8] >> 3) + 7) // 8 + 4
    while reader.p < len(movie):
        header = struct.unpack('<H', reader.raw(2))[0]
        kind, length = header >> 6, header & 63
        if length == 63:
            length = struct.unpack('<I', reader.raw(4))[0]
        payload = reader.raw(length)
        if kind != 82:
            continue
        parser = Reader(payload)
        parser.raw(4)
        if parser.cstr() != class_name:
            continue
        methods, names, strings = abc_parse(payload[parser.p:])
        if class_name == 'codo/controllers/ChattingController':
            methods = [method for method in methods if method.get('trait', '').split('::')[-1]
                       in ('cppOnMessageReceived', 'addChatChannel', 'receiveChatMsg',
                           'cppOnTryJoinGlobalChatComplete', 'getChannelObjByID', 'onInitialized', 'getChannelNameByID')]
        if class_name == 'codo/views/ChatView':
            methods = [method for method in methods if method.get('trait', '').split('::')[-1]
                       == 'getAppropriateChatChannel']
        return {method['trait'].split('::')[-1]: (method, disassemble(method, names, strings))
                for method in methods if 'trait' in method and 'code' in method}, names
    raise AssertionError(class_name)


class ChatBytecode:
    """解释目标方法所需指令；频道去重及历史追加执行原方法，不使用业务替身。"""

    def __init__(self, methods):
        self.methods = methods
        self.state = {'_chatChannels': [], '_bigLoudSpeakerPostBuffer': []}
        self.events = []
        self.view_ready = True
        self.query_provider = None
        self.providers = {
            'ChattingChannelType': {'GLOBAL': 0, 'PARTY': 1, 'CLAN': 2, 'ROOM': 3, 'PRIVATE': 4, 'ROOM_TEAM': 5},
            'ChatSpeakerType': {'SPEAKER_TYPE_BIG': 0, 'SPEAKER_TYPE_SMALL': 1},
            'ChatMessageObj': 'ChatMessageObj', 'ChatChannelObj': 'ChatChannelObj',
            'PlayerObj': {}, 'MAX_BIG_LOUD_SPEAKER_SIZE': 100,
            'BCCommonEvent': {'EVT_SHOW_SPEAKER_ANIMATION': 'animation'},
            'BCEvent': {'EVT_ON_CHAT_MSG_RECEIVED': 'received'},
            'DataID': {'CHAT_CHANNELS': 'channels', 'CHAT_CHANNEL_OBJ': 'channel_object'},
            'ChatView': {'GLOBAL_CHANNEL_INDEX': 0, 'CHANNEL_NAMES': ['大厅{0}']},
            'DictChatting': {'NO_GLOBAL_CHAT_SERVICE': '服务关闭', 'NO_GLOBAL_CHANNEL': '大厅聊天加入中', 'CHATTING_CHANNEL_CHANNEL': '频道',
                             'CHATTING_CHANNEL_PRIVATE': '私聊', 'CHATTING_CHANNEL_ROOM': '房间',
                             'CHATTING_CHANNEL_CLAN': '战队'},
            'LocalizeUtil': {},
            'DictParty': {'PARTY': '队伍'},
        }

    def call(self, receiver, name, arguments):
        """外部展示和查询作为观察点，核心三个业务方法执行实际字节码。"""
        if name in ('addChatChannel', 'receiveChatMsg'):
            return self.run(name, arguments)
        if name == 'getObj':
            return {'id': '123'}
        if name == 'isChatMessageViewReady':
            return self.view_ready
        if name == 'push':
            receiver.extend(arguments)
            return len(receiver)
        if name == 'toString':
            return str(receiver)
        if name == 'getChannelNameByID':
            return self.run(name, arguments)
        if name == 'localize':
            return arguments[0]
        if name == 'getPlayerLevel':
            return 0
        if name == 'param':
            return arguments[0].format(*arguments[1])
        if name == 'getDP':
            return self.query_provider(*arguments)
        if name in ('broadcast', 'setBindData', 'setChannelLabel', 'bind'):
            self.events.append((name, arguments))
            return None
        raise AssertionError(('unexpected call', name))

    def run(self, name, arguments=(), start_offset=0, stop_offset=None):
        """保留原分支目标、局部变量和操作数栈，检查返回栈平衡。"""
        method, instructions = self.methods[name]
        if name == 'addChatChannel':
            arguments = [*arguments, *['', '', True, False][len(arguments)-1:]]
        local_values = [None] * method['limits'][1]
        local_values[:len(arguments)+1] = [self.state, *arguments]
        operand_stack = []
        offsets = {row['offset']: index for index, row in enumerate(instructions)}
        instruction_index = offsets[start_offset]
        for step_index in range(10000):
            row = instructions[instruction_index]
            if row['offset'] == stop_offset:
                return None
            operation, operands = row['instruction'], row['args']
            instruction_index += 1
            if operation in ('debugfile', 'debugline', 'debug', 'label', 'nop', 'coerce', 'coerce_s', 'convert_i'):
                continue
            if operation.startswith('getlocal'):
                local_index = operands[0] if operation == 'getlocal' else int(operation[-1])
                operand_stack.append(local_values[local_index])
            elif operation.startswith('setlocal'):
                local_index = operands[0] if operation == 'setlocal' else int(operation[-1])
                local_values[local_index] = operand_stack.pop()
            elif operation in ('pop', 'pushscope'):
                operand_stack.pop()
            elif operation in ('pushtrue', 'pushfalse', 'pushnull'):
                operand_stack.append({'pushtrue': True, 'pushfalse': False, 'pushnull': None}[operation])
            elif operation == 'pushshort':
                operand_stack.append(operands[0])
            elif operation == 'kill':
                local_values[operands[0]] = None
            elif operation == 'lookupswitch':
                case_index = operand_stack.pop()
                target = operands[1][case_index] if 0 <= case_index < len(operands[1]) else operands[0]
                instruction_index = offsets[target]
            elif operation == 'pushbyte':
                operand_stack.append(operands[0] if operands[0] < 128 else operands[0]-256)
            elif operation == 'pushstring':
                operand_stack.append(operands[0])
            elif operation == 'getlex':
                operand_stack.append(self.providers[operands[0].split('::')[-1]])
            elif operation == 'findpropstrict':
                operand_stack.append(self.providers)
            elif operation == 'getproperty':
                property_name = operands[0].split('::')[-1]
                if property_name == '<late-multiname>':
                    property_name = operand_stack.pop()
                receiver = operand_stack.pop()
                operand_stack.append(len(receiver) if property_name == 'length' else receiver[property_name])
            elif operation == 'setproperty':
                value = operand_stack.pop()
                operand_stack.pop()[operands[0].split('::')[-1]] = value
            elif operation == 'dup':
                operand_stack.append(operand_stack[-1])
            elif operation == 'astypelate':
                operand_stack.pop()
            elif operation in ('not', 'convert_b'):
                value = bool(operand_stack.pop())
                operand_stack.append(not value if operation == 'not' else value)
            elif operation == 'equals':
                operand_stack.append(operand_stack.pop() == operand_stack.pop())
            elif operation == 'increment_i':
                operand_stack.append(operand_stack.pop()+1)
            elif operation == 'add':
                right = operand_stack.pop()
                operand_stack.append(operand_stack.pop()+right)
            elif operation == 'newarray':
                values = [operand_stack.pop() for argument_index in range(operands[0])][::-1]
                operand_stack.append(values)
            elif operation in ('jump', 'iftrue', 'iffalse', 'ifne', 'ifeq', 'iflt', 'ifnge', 'ifstrictne'):
                if operation == 'jump':
                    take_branch = True
                elif operation in ('iftrue', 'iffalse'):
                    take_branch = bool(operand_stack.pop()) == (operation == 'iftrue')
                else:
                    right, left = operand_stack.pop(), operand_stack.pop()
                    take_branch = {'ifstrictne': left != right, 'ifne': left != right, 'ifeq': left == right,
                                   'iflt': left < right, 'ifnge': not left >= right}[operation]
                if take_branch:
                    instruction_index = offsets[operands[0]]
            elif operation in ('constructprop', 'callproperty', 'callpropvoid'):
                call_arguments = [operand_stack.pop() for argument_index in range(operands[1])][::-1]
                receiver = operand_stack.pop()
                method_name = operands[0].split('::')[-1]
                if operation == 'constructprop':
                    value = dict(call_arguments[0]) if method_name == 'ChatMessageObj' else {'msgHistory': []}
                else:
                    value = self.call(receiver, method_name, call_arguments)
                if operation != 'callpropvoid':
                    operand_stack.append(value)
            elif operation in ('returnvoid', 'returnvalue'):
                result = operand_stack.pop() if operation == 'returnvalue' else None
                if operand_stack:
                    raise AssertionError(('unbalanced stack', name, operand_stack))
                return result
            else:
                raise AssertionError((name, operation))
        raise AssertionError('bytecode did not terminate')


class BroadcastHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original = (PROJECT/'research/client/ui-investigation/codo.gfx').read_bytes()
        cls.patched = movie_identity.apply_rules(cls.original, ui_chat.replacements())
        cls.before, unused_names = movie_methods(cls.original, 'codo/controllers/ChattingController')
        cls.after, unused_names = movie_methods(cls.patched, 'codo/controllers/ChattingController')

    def message(self, speaker_type=0, channel_id=0):
        return {'channelID': channel_id, 'loudSpeakerType': speaker_type, 'senderID': '123',
                'senderName': '玩家', 'message': '中文广播', 'isSystem': False, 'targetID': '0'}

    def initialize(self, machine):
        """执行初始化末尾的真实绑定与创建指令；此前依赖注册作为测试前置条件。"""
        machine.state['bind_demonwareChattingStatusChange'] = 'original_binding'
        machine.providers['DataID']['EAsyncTaskID_Demonware'] = 'demonware'
        machine.run('onInitialized', start_offset=651)

    def test_initialization_creates_empty_channel_before_any_push(self):
        machine = ChatBytecode(self.after)
        self.initialize(machine)
        channel = machine.state['_chatChannels'][0]
        self.assertEqual(channel['id'], 0)
        self.assertEqual(channel['name'], '大厅聊天室')
        self.assertEqual(channel['msgHistory'], [])
        self.assertEqual(machine.state['_bigLoudSpeakerPostBuffer'], [])
        self.assertEqual(machine.events[0], ('bind', ['demonware', 'original_binding']))
        self.initialize(machine)
        self.assertEqual(len(machine.state['_chatChannels']), 1)
        for sender_id in ('123', '456', '456'):
            message = self.message()
            message['senderID'] = sender_id
            machine.run('cppOnMessageReceived', [message])
        self.assertEqual(len(channel['msgHistory']), 3)
        self.assertEqual([message['selfSend'] for message in channel['msgHistory']], [True, False, False])

    def test_receive_and_original_history_methods_are_restored_exactly(self):
        for name in ('cppOnMessageReceived', 'receiveChatMsg', 'addChatChannel',
                     'cppOnTryJoinGlobalChatComplete'):
            self.assertEqual(self.before[name], self.after[name])
        before_code = bytes.fromhex(self.before['onInitialized'][0]['code'])
        after_code = bytes.fromhex(self.after['onInitialized'][0]['code'])
        self.assertEqual(before_code[:651], after_code[:651])
        self.assertEqual(len(before_code), len(after_code))

    def test_only_global_channel_name_changes(self):
        original = ChatBytecode(self.before)
        patched = ChatBytecode(self.after)
        self.assertEqual(original.run('getChannelNameByID', [0]), '频道')
        self.assertEqual(patched.run('getChannelNameByID', [0]), '大厅聊天室')
        for channel_id in (1, 2, 3, 4, 5):
            self.assertEqual(patched.run('getChannelNameByID', [channel_id]),
                             original.run('getChannelNameByID', [channel_id]))

    def test_label_reads_actual_channel_name_without_replacing_text_pool(self):
        methods, unused_names = movie_methods(self.patched, 'codo/views/chat_parts/ControlPanel')
        for channels, selected, expected in (([], 0, []),
                ([{'id': 0, 'name': '真实频道名称'}], 0, [('setChannelLabel', ['真实频道名称'])]),
                ([{'id': 0, 'name': '真实频道名称'}], 1, []),
                ([{'id': 3, 'name': '房间'}], 0, [])):
            machine = ChatBytecode(methods)
            machine.state = {'mCurrentIndex': selected}
            machine.run('refreshUnread', [channels], start_offset=12, stop_offset=51)
            self.assertEqual(machine.events, expected)
        original_methods, unused_names = movie_methods(self.original, 'codo/views/chat_parts/ControlPanel')
        self.assertEqual(methods['updateChannelLabel'], original_methods['updateChannelLabel'])
        self.assertEqual(self.patched[2164572:2164584], b'buttonXSoFar')


if __name__ == '__main__':
    unittest.main()
