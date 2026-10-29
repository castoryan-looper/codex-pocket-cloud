import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

import test_bridge
from recommendations import Feed, paginate, recent_topics


def turn(text, age=10, kind='userMessage'):
    return {'startedAt': time.time()-age, 'items': [{'type': kind, 'content': [{'type': 'text', 'text': text}]}]}


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'feedback.json'
        self.feed = Feed(self.path)

    def test_uses_actual_user_topic_and_different_recent_topic_for_b(self):
        self.feed.update([turn('帮我改进墨水屏推荐卡片的阅读交互'), turn('请研究语音上传耗时的优化')])
        a, b = self.feed.pair()
        self.assertIn('墨水屏', a.title)
        self.assertIn('语音', b.title)
        self.assertIn('帮我改进墨水屏', a.prompt)
        self.assertNotEqual(a.topic, b.topic)

    def test_one_topic_offers_two_different_angles(self):
        self.feed.update([turn('检查 USB 配置模式')])
        a, b = self.feed.pair()
        self.assertEqual(a.topic, b.topic)
        self.assertNotEqual(a.strategy, b.strategy)
        self.assertNotEqual(a.prompt, b.prompt)

    def test_no_invented_history_and_filters_expired_agent_messages(self):
        self.feed.update([turn('不存在的用户兴趣', kind='agentMessage'), turn('过期话题', age=604801)])
        self.assertTrue(all(card.source == '起步建议' for card in self.feed.pair()))

    def test_context_markup_is_not_presented_as_a_topic(self):
        topics = recent_topics([turn('<environment_context>隐藏目录信息</environment_context>帮我设计分页阅读')], time.time())
        self.assertIn('分页阅读', topics[0][1])
        self.assertNotIn('隐藏目录', topics[0][2])

    def test_skip_rotates_without_sending_and_selection_survives_restart(self):
        self.feed.update([turn('讨论 ESP32 的屏幕交互')])
        first = self.feed.pair(); self.feed.record(first)
        second = self.feed.pair()
        self.assertFalse({x.id for x in first} & {x.id for x in second})
        self.feed.record(second, chosen=second[0])
        restored = Feed(self.path); restored.update([turn('讨论 ESP32 的屏幕交互')])
        self.assertIn(second[0].id, restored.feedback['used'])
        self.assertNotIn(second[0].id, [x.id for x in restored.pair()])

    def test_only_one_unused_card_still_produces_pair(self):
        self.feed.update([turn('讨论 ESP32 的屏幕交互')])
        for _ in range(3):
            cards = self.feed.pair(); self.feed.record(cards, chosen=cards[0])
        cards = self.feed.pair()
        self.assertEqual(len(cards), 2)
        self.assertNotEqual(cards[0].id, cards[1].id)

    def test_pagination_keeps_all_text_and_obeys_pixel_width(self):
        value = '这是一个很长的推荐任务，包含中文、英文 USB 和换行。\n下一段也必须完整显示。'*8
        pages = paginate(value, lambda text: len(text)*7, width=70, rows=5)
        self.assertGreater(len(pages), 1)
        self.assertEqual(''.join(line for page in pages for line in page), value.replace('\n', ''))
        self.assertTrue(all(len(page) <= 5 and all(len(line)*7 <= 70 for line in page) for page in pages))


class RecommendationInteractionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = test_bridge.Tests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.p = self.fixture.p
        self.p.status = '待命'
        self.p.recommendations.update([turn('帮我设计推荐任务的分页交互')])
        self.p.recommendation_cards = self.p.recommendations.pair()

    async def test_short_click_opens_reader_without_starting_task(self):
        self.p.submit = AsyncMock()
        await self.p.action('b1_click')
        self.assertTrue(self.p.recommendation_reading())
        self.assertEqual(self.p.recommendation_reader['card'], 0)
        self.p.submit.assert_not_awaited()

    async def test_full_detail_paging_source_view_and_back(self):
        await self.p.action('rec_a')
        pages = self.p.recommendation_pages()
        for _ in range(len(pages)+3): await self.p.action('rec_next')
        self.assertEqual(self.p.recommendation_reader['page'], len(pages)-1)
        await self.p.action('rec_context')
        self.assertTrue(self.p.recommendation_reader['context'])
        self.assertEqual(self.p.recommendation_reader['page'], 0)
        self.assertEqual(len(self.p.render().tobytes()), 5000)
        await self.p.action('b2_long')
        self.assertFalse(self.p.recommendation_reading())

    async def test_execute_is_explicit_and_cannot_be_repeated(self):
        self.p.submit = AsyncMock()
        with self.assertRaises(ValueError): await self.p.action('rec_run')
        await self.p.action('rec_b')
        prompt = self.p.recommendation_cards[1].prompt
        await self.p.action('rec_run')
        self.p.submit.assert_awaited_once_with(prompt, source='recommendation')
        with self.assertRaises(ValueError): await self.p.action('rec_run')

    async def test_delivery_failure_preserves_exact_task_as_draft(self):
        self.p.submit = AsyncMock(side_effect=TimeoutError('delivery uncertain'))
        await self.p.action('rec_a')
        expected = self.p.recommendation_cards[0].prompt
        with self.assertRaises(TimeoutError): await self.p.action('rec_run')
        self.assertEqual(self.p.draft, expected)
        self.assertEqual(self.p.menu_items()[0][1], 'send_draft')
        self.p.submit.assert_awaited_once()

    async def test_reading_does_not_change_when_background_context_refreshes(self):
        await self.p.action('rec_a')
        before = self.p.recommendation_cards
        self.p.rpc.call.return_value = {'data': [turn('全新的不同话题') ]}
        await self.p.refresh_chat_advice()
        self.assertEqual(self.p.recommendation_cards, before)

    async def test_task_draft_and_approvals_take_precedence(self):
        for field, value in [('turn', 'working'), ('draft', 'draft'), ('approvals', [{'id': 1}]), ('voice_busy', True)]:
            original = getattr(self.p, field)
            setattr(self.p, field, value)
            with self.assertRaises(ValueError): await self.p.action('rec_a')
            setattr(self.p, field, original)

    async def test_single_card_feed_advances_and_opening_never_submits(self):
        self.p.submit = AsyncMock()
        self.assertEqual(self.p.menu_items()[0][1], 'rec_open')
        first = self.p.recommendation_cards[0]
        second = self.p.recommendation_cards[1]
        await self.p.action('b2_click')
        self.assertEqual(self.p.recommendation_index, 1)
        self.assertIn(first.id, self.p.recommendations.feedback['seen'])
        self.assertNotIn(second.id, self.p.recommendations.feedback['seen'])
        await self.p.action('b1_click')
        self.assertEqual(self.p.recommendation_reader['card'], 1)
        self.p.submit.assert_not_awaited()
        self.assertEqual(len(self.p.render().tobytes()), 5000)

    async def test_feed_advances_pair_and_status_is_accessible(self):
        self.p.submit = AsyncMock()
        await self.p.action('b2_click')
        await self.p.action('b2_click')
        self.assertEqual(self.p.recommendation_index, 0)
        self.assertEqual(self.p.recommendation_generation, 1)
        await self.p.action('b2_long')
        self.assertEqual(self.p.page, 1)
        self.p.submit.assert_not_awaited()

    async def test_running_task_keeps_stop_control(self):
        self.p.turn = 'turn-1'
        await self.p.action('b2_long')
        self.p.rpc.call.assert_awaited_with('turn/interrupt', {'threadId': self.p.thread, 'turnId': 'turn-1'})


if __name__ == '__main__':
    unittest.main()
