import unittest
from unittest.mock import AsyncMock
from dataclasses import replace
import test_bridge
from recommendations import choice_label


class ChoiceTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_bridge.Tests.setUp
    tearDown = test_bridge.Tests.tearDown

    async def asyncSetUp(self):
        self.p.status='待命';self.p.page=0;self.p.submit=AsyncMock()

    async def test_choose_and_confirm_without_required_reading(self):
        p=self.p;await p.action('knob_right');pair=p.recommendation_cards
        await p.action('knob_click');p.submit.assert_not_awaited()
        self.assertFalse(p.recommendation_expanded())
        self.assertEqual(p.menu_items()[0][1],'rec_run')
        await p.action('knob_click')
        p.submit.assert_awaited_once_with(pair[1].prompt,source='recommendation')
        with self.assertRaises(ValueError):await p.action('rec_run')

    async def test_reader_turns_pages_and_press_returns_without_execution(self):
        p=self.p
        p.recommendation_cards=(replace(p.recommendation_cards[0],detail='这是详细说明。'*100),p.recommendation_cards[1])
        await p.action('rec_a');await p.action('rec_details')
        pages=len(p.recommendation_pages());self.assertGreater(pages,2)
        await p.action('knob_right');self.assertEqual(p.recommendation_reader['page'],1)
        await p.action('knob_left');self.assertEqual(p.recommendation_reader['page'],0)
        for _ in range(pages+2):await p.action('knob_right')
        self.assertEqual(p.recommendation_reader['page'],pages-1)
        with self.assertRaises(ValueError):await p.action('rec_run')
        await p.action('knob_click');self.assertFalse(p.recommendation_expanded())
        self.assertEqual(p.recommendation_reader['page'],0);p.submit.assert_not_awaited()

    async def test_back_and_buttons_preserve_pair_and_do_not_execute(self):
        p=self.p;pair=p.recommendation_cards
        for _ in range(4):await p.action('b2_click')
        self.assertEqual(p.recommendation_cards,pair);self.assertEqual(p.recommendation_index,0)
        await p.action('rec_a');await p.action('rec_context');await p.action('b2_click')
        await p.action('knob_back');self.assertTrue(p.recommendation_reading());self.assertFalse(p.recommendation_expanded())
        await p.action('knob_back');self.assertFalse(p.recommendation_reading());p.submit.assert_not_awaited()

    async def test_busy_reader_never_turns_or_executes_recommendation(self):
        p=self.p;await p.action('rec_a');await p.action('rec_details');p.voice_busy=True
        with self.assertRaises(ValueError):await p.action('rec_next')
        p.submit.assert_not_awaited()

    def test_labels_fit_and_distinct_for_goal_choices(self):
        p=self.p;p.goal_flow.start('非常长的目标内容'*50)
        for state in ('completed','failed','interrupted'):
            p.goal_flow.finish(state,'测试结果',turn_id=state)
            labels=[choice_label(c) for c in p.goal_flow.pair()]
            self.assertNotEqual(*labels);self.assertTrue(all(len(s)<=8 for s in labels))


if __name__=='__main__':unittest.main()
