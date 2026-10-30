"""Regression checks for multi-round hardware sessions, without a live model."""
import time
import unittest
from unittest.mock import AsyncMock

import test_bridge


class ContinuityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixture = test_bridge.Tests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        self.p = fixture.p
        self.p.status = '待命'

    async def event(self, method, turn_id, **fields):
        await self.p.notification({'method': method, 'params': {
            'threadId': self.p.thread, 'turn': {'id': turn_id, **fields}}})

    async def test_old_completion_cannot_clear_new_turn_or_approvals(self):
        p = self.p
        p.goal_flow.start('完成语音闭环')
        await self.event('turn/started', 'first')
        p.reply = '第一轮结果'
        await self.event('turn/completed', 'first', status='completed')
        await self.event('turn/started', 'second')
        p.reply = '第二轮正在执行'
        p.approvals = [{'id': 9}]
        snapshot = (p.task, p.status, p.detail, p.recommendation_cards)
        for stale in ('first', 'unseen-old'):
            await self.event('turn/completed', stale, status='completed')
            self.assertEqual(p.turn, 'second')
            self.assertEqual(p.approvals, [{'id': 9}])
            self.assertEqual((p.task, p.status, p.detail, p.recommendation_cards), snapshot)
        self.assertEqual(len(p.goal_flow.active['results']), 1)

    async def test_replayed_lifecycle_after_completion_is_a_noop(self):
        p = self.p
        await self.event('turn/started', 'first')
        p.reply = '已完成'
        await self.event('turn/completed', 'first', status='completed')
        p.draft = '用户尚未发送的补充'
        snapshot = (p.task, p.status, p.reply, p.detail)
        await self.event('turn/started', 'first')
        await self.event('turn/completed', 'first', status='failed')
        self.assertIsNone(p.turn)
        self.assertEqual((p.task, p.status, p.reply, p.detail), snapshot)

    def test_old_result_never_overrides_busy_states(self):
        p = self.p
        for status in ('已完成', '已停止'):
            p.status = status
            p.last_event = time.monotonic() - 181
            self.assertTrue(p.is_idle_dashboard())
            for field, busy in (('turn', 'new-turn'), ('draft', 'unsent'),
                                ('approvals', [{'id': 1}]), ('voice_busy', True)):
                original = getattr(p, field)
                setattr(p, field, busy)
                self.assertFalse(p.is_idle_dashboard(), (status, field))
                setattr(p, field, original)
            p.page = 1
            self.assertFalse(p.is_idle_dashboard())
            p.page = 0

    async def test_two_round_goal_verification_and_explicit_closure(self):
        p = self.p
        p.rpc.call = AsyncMock(side_effect=[
            {'turn': {'id': str(i), 'status': 'inProgress'}} for i in range(1, 5)])
        await p.submit('新目标：完成可验证的语音上传')
        async def complete(turn_id, reply):
            p.reply = reply
            await self.event('turn/completed', turn_id, status='completed')
            self.assertTrue(p.is_idle_dashboard())
        await complete('1', '已检查现状，上传有待修复')
        for action, turn_id, reply in (
            ('rec_b', '2', '已修复上传分块'),
            ('rec_a', '3', '模拟上传通过，真实麦克风未验收')):
            await p.action(action)
            await p.action('rec_run')
            await complete(turn_id, reply)
        await p.submit('验收标准：真实麦克风连续三次成功')
        await complete('4', '需要连接设备验证，尚未达标')
        self.assertEqual(len(p.goal_flow.active['results']), 4)
        self.assertEqual(p.goal_flow.active['criteria'], '真实麦克风连续三次成功')
        self.assertIn('尚未达标', p.goal_flow.summary())
        await p.action('goal_review')
        with self.assertRaises(ValueError):
            await p.action('rec_run')
        await p.action('goal_finish')
        self.assertIsNotNone(p.goal_flow.active)
        await p.action('rec_run')
        self.assertIsNone(p.goal_flow.active)
        archived = p.goal_flow.data['archive'][-1]
        self.assertTrue(archived['acceptedByUser'])
        self.assertTrue(all(r['verification'] == 'not_reported' for r in archived['results']))
        self.assertEqual(p.rpc.call.await_count, 4)
