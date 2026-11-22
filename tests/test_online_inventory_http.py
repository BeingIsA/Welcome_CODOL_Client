"""验证仓库 HTTP 回执在工作线程完成校验及同号重放。"""
import json
import unittest
from unittest.mock import Mock

from codol_rebuild.adapters.transport.http import HttpGateway
from codol_rebuild.application.errors import OnlineError
from test_multiplayer_http import Response


class InventoryHttpTests(unittest.TestCase):
    def setUp(self):
        self.gateway = HttpGateway('http://127.0.0.1:8080')
        self.gateway._opener = Mock()
        self.inventory = {'revision': 4, 'gp': 10, 'cp': 8, 'items': []}
        self.content = {'item_id': 813, 'quantity': 2, 'quantity_delta': 0}
        self.reward = {'product_ids': [0], 'contents': [self.content]}

    @staticmethod
    def response(result):
        return Response({'apiVersion': '1', 'data': result})

    def test_timeout_and_malformed_receipt_retry_with_original_command(self):
        confirmed = {'inventory': self.inventory, 'reward': self.reward}
        for first_response in (TimeoutError('timeout'), self.response({'inventory': self.inventory})):
            with self.subTest(first_response=type(first_response).__name__):
                opened = self.gateway._opener.open
                opened.reset_mock()
                opened.side_effect = [first_response, self.response(confirmed)]
                self.assertEqual(self.gateway.open_supply_drop('pl_one', 11861, 'same-draw'), confirmed)
                self.assertEqual(opened.call_count, 2)
                requests = [call.args[0] for call in opened.call_args_list]
                self.assertEqual([json.loads(request.data) for request in requests],
                                 [{'crateItemId': 11861, 'operationId': 'same-draw'}] * 2)
                self.assertEqual([request.get_header('X-player-id') for request in requests], ['pl_one'] * 2)

    def test_business_rejection_is_returned_once(self):
        self.gateway._opener.open.return_value = Response(
            {'error': {'code': 'INVENTORY_REJECTED', 'message': '余额不足'}}, 409)
        with self.assertRaises(OnlineError) as failure:
            self.gateway.purchase('pl_one', 56, 'purchase')
        self.assertEqual(failure.exception.code, 'INVENTORY_REJECTED')
        self.assertFalse(failure.exception.uncertain)
        self.gateway._opener.open.assert_called_once()

    def test_bad_reward_never_reaches_native_consumer(self):
        invalid_rewards = [None, {}, {**self.reward, 'contents': []}, {**self.reward, 'product_ids': []}]
        for bad_content in (None, {}, {'item_id': 813, 'quantity': 1},
                            {**self.content, 'quantity_delta': 3}, {**self.content, 'quantity': True},
                            {**self.content, 'item_id': 65535}):
            invalid_rewards.append({**self.reward, 'contents': [bad_content]})
        for invalid_reward in invalid_rewards:
            with self.subTest(reward=invalid_reward):
                result = {'inventory': self.inventory, 'reward': invalid_reward}
                self.gateway._opener.open.side_effect = [self.response(result), self.response(result)]
                with self.assertRaises(OnlineError) as failure:
                    self.gateway.open_supply_drop('pl_one', 11861, 'draw')
                self.assertEqual(failure.exception.code, 'INVALID_RESPONSE')
                self.assertTrue(failure.exception.uncertain)

    def test_rename_checks_player_and_submitted_name_and_preserves_operation_id(self):
        confirmed = {'inventory': self.inventory, 'playerId': 'pl_one', 'displayName': '新名'}
        for malformed in ({'inventory': self.inventory}, {**confirmed, 'playerId': 'pl_other'},
                          {**confirmed, 'displayName': '旧名'}):
            with self.subTest(malformed=malformed):
                opened = self.gateway._opener.open
                opened.reset_mock()
                opened.side_effect = [self.response(malformed), self.response(confirmed)]
                self.assertEqual(self.gateway.rename('pl_one', '新名'), confirmed)
                requests = [json.loads(call.args[0].data) for call in opened.call_args_list]
                self.assertEqual(len(requests), 2)
                self.assertTrue(requests[0]['operationId'])
                self.assertEqual(requests[0], requests[1])
