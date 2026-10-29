import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock
import test_bridge
from goal_flow import GoalFlow

class GoalStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.path=Path(self.tmp.name)/'goal.json';self.flow=GoalFlow(self.path)
    def test_goal_is_sticky_until_explicit_switch_and_survives_restart(self):
        f=self.flow;f.request('让终端稳定完成语音任务','direct');f.request('检查一下蓝牙','direct')
        self.assertEqual(f.active['objective'],'让终端稳定完成语音任务')
        f.request('验收标准：连续三次上传识别成功','direct');loaded=GoalFlow(self.path)
        self.assertEqual(loaded.active['criteria'],'连续三次上传识别成功')
        loaded.request('新目标：改进屏幕阅读交互','direct');self.assertEqual(loaded.active['objective'],'改进屏幕阅读交互');self.assertEqual(loaded.data['archive'][0]['objective'],'让终端稳定完成语音任务')
    def test_spoken_goal_and_acceptance_commands(self):
        f=self.flow;f.request('新目标是改善语音体验','direct');self.assertEqual(f.active['objective'],'改善语音体验')
        f.request('验收标准是连续三次成功','direct');self.assertEqual(f.active['criteria'],'连续三次成功')

    def test_both_options_use_goal_and_actual_result_without_claiming_verification(self):
        f=self.flow;f.start('提高语音可靠性');f.finish('completed','已改成小块上传，真实麦克风尚未验证',turn_id='1')
        a,b=f.pair();self.assertEqual(a.topic,b.topic);self.assertIn('小块上传',a.prompt);self.assertIn('尚未验证',b.prompt);self.assertEqual(a.strategy,'goal_verify')
        f.selected(a);f.finish('completed','上传测试通过，识别仍失败',turn_id='2');a,b=f.pair();self.assertEqual(a.strategy,'goal_advance');self.assertEqual(b.strategy,'goal_accept');self.assertIn('识别仍失败',a.context)
        self.assertTrue(f.active);self.assertTrue(all(row['verification']=='not_reported' for row in f.active['results']))
    def test_failures_and_duplicate_completion_are_grounded(self):
        f=self.flow;f.start('完成部署');self.assertTrue(f.finish('failed','',error='permission denied',turn_id='1'))
        self.assertEqual(f.pair()[0].strategy,'goal_repair');self.assertIn('permission denied',f.pair()[0].prompt)
        self.assertFalse(f.finish('completed','late duplicate',turn_id='1'));self.assertEqual(len(f.active['results']),1)
        f.start('另一个目标');self.assertFalse(f.finish('completed','old goal event',turn_id='1'))
    def test_skipping_never_changes_goal_or_blacklists_continuation(self):
        f=self.flow;f.start('修好设备');a,b=f.pair();f.selected(a);f.skip();c,d=f.pair()
        self.assertNotEqual(a.id,c.id);self.assertEqual(c.topic,a.topic);self.assertEqual(f.active['objective'],'修好设备')
    def test_close_is_user_acceptance_not_automatic_verification(self):
        f=self.flow;f.start('修好设备');f.finish('completed','模型说完成',turn_id='1');self.assertTrue(f.active)
        f.archive();loaded=GoalFlow(self.path);self.assertIsNone(loaded.active);self.assertTrue(loaded.data['archive'][0]['acceptedByUser']);self.assertEqual(loaded.closed_pair()[0].strategy,'goal_new')
    def test_corrupt_file_is_preserved_and_not_silently_overwritten(self):
        self.path.write_text('{broken',encoding='utf-8');f=GoalFlow(self.path);f.start('仍可在内存中讨论');self.assertTrue(f.storage_error);self.assertEqual(self.path.read_text(),'{broken')
    def test_results_are_bounded_and_compact_details_keep_full_context(self):
        f=self.flow;f.start('目标'*100)
        for i in range(35):f.finish('completed','结果'*1000,turn_id=str(i))
        self.assertEqual(len(f.active['results']),30);card=f.pair()[0];self.assertLess(len(card.detail),len(card.context));self.assertIn('结果'*1000,card.context)

class GoalInteractionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture=test_bridge.Tests();self.fixture.setUp();self.addCleanup(self.fixture.tearDown);self.p=self.fixture.p;self.p.status='待命';self.p.goal_flow.start('把语音终端做好');self.p.refresh_goal_followup()
    async def test_completion_immediately_offers_next_steps_and_preserves_result(self):
        p=self.p;p.reply='已完成上传修复，录音尚未验收';p.turn='t1'
        await p.notification({'method':'turn/completed','params':{'threadId':p.thread,'turn':{'id':'t1','status':'completed'}}})
        self.assertTrue(p.is_idle_dashboard());self.assertIn('录音尚未验收',p.recommendation_cards[0].detail);self.assertEqual(p.recommendation_cards[0].strategy,'goal_verify')
        await p.action('rec_a');frozen=p.recommendation_cards
        await p.notification({'method':'turn/completed','params':{'threadId':p.thread,'turn':{'id':'t1','status':'completed'}}})
        self.assertEqual(p.recommendation_cards,frozen);self.assertTrue(p.recommendation_reading())
    async def test_failed_turn_can_open_and_execute_repair_card(self):
        p=self.p;p.turn='bad';p.reply='';await p.notification({'method':'turn/completed','params':{'threadId':p.thread,'turn':{'id':'bad','status':'failed','error':{'message':'权限不足'}}}})
        self.assertTrue(p.is_idle_dashboard());await p.action('rec_a');self.assertTrue(p.recommendation_reading());self.assertEqual(len(p.render().tobytes()),5000)
        p.submit=AsyncMock();await p.action('rec_run');p.submit.assert_awaited_once()

    async def test_review_and_two_step_close_never_submit(self):
        p=self.p;p.submit=AsyncMock()
        with self.assertRaises(ValueError):await p.action('goal_finish')
        await p.action('goal_review');self.assertEqual(p.recommendation_cards[0].strategy,'goal_review')
        with self.assertRaises(ValueError):await p.action('rec_run')
        await p.action('goal_finish');self.assertEqual(p.recommendation_cards[0].strategy,'goal_close');self.assertTrue(p.goal_flow.active)
        await p.action('rec_run');self.assertIsNone(p.goal_flow.active);p.submit.assert_not_awaited();self.assertEqual(p.recommendation_cards[0].strategy,'goal_new')
    async def test_skip_from_reader_and_busy_guard(self):
        p=self.p;p.submit=AsyncMock();await p.action('rec_a');objective=p.goal_flow.active['objective'];await p.action('rec_skip');self.assertFalse(p.recommendation_reading());self.assertEqual(p.goal_flow.active['objective'],objective);p.submit.assert_not_awaited()
        p.turn='busy'
        with self.assertRaises(ValueError):await p.action('goal_review')
        p.turn=None;p.draft='unsent'
        with self.assertRaises(ValueError):await p.action('goal_review')
    async def test_goal_result_submission_carries_context_once(self):
        p=self.p;p.goal_flow.finish('completed','唯一执行结果',turn_id='old');p.refresh_goal_followup();await p.action('rec_a');card=p.recommendation_cards[0]
        p.rpc.call=AsyncMock(return_value={'turn':{'id':'new','status':'inProgress'}});await p.action('rec_run')
        params=p.rpc.call.await_args.args[1];text=params['input'][0]['text'];self.assertEqual(text,card.prompt);self.assertEqual(text.count('以下目标档案'),1)

if __name__=='__main__':unittest.main()
