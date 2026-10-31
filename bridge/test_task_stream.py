import asyncio,json,time,unittest
from unittest.mock import AsyncMock,patch
import test_bridge
from recommendations import Card
from task_stream import TaskStream,StreamRanker
from round_display import round_packet
from bridge import ui_font

class TaskStreamTests(unittest.IsolatedAsyncioTestCase):
    setUp=test_bridge.Tests.setUp
    tearDown=test_bridge.Tests.tearDown
    def card(self,ident='one',strategy='verify'):
        return Card(ident,'稳定录音上传','检查实际上传证据','请只读检查上传证据，不要更改文件。',strategy,'测试来源')
    def rpc(self):
        count=0
        async def call(method,params=None):
            nonlocal count
            if method=='thread/start':count+=1;return {'thread':{'id':'worker-'+str(count)}}
            if method=='turn/start':return {'turn':{'id':'turn-'+params['threadId'],'status':'inProgress'}}
            return {}
        self.p.rpc.call.side_effect=call
    async def flush(self):
        if self.p.stream.scheduler:await self.p.stream.scheduler
    def request(self,ident=17):
        return {'id':ident,'method':'item/commandExecution/requestApproval','params':{'threadId':self.p.thread,'command':'python tools/check.py'}}
    async def test_browse_dwell_never_executes_and_preferences_persist(self):
        s=self.p.stream;self.p.status='待命';item,_,_=s.current();token=s.token
        s.acknowledge('device',token,100);s.acknowledge('device',token,104);s.acknowledge('device',token,108)
        await s.action('stream_next',{'token':token})
        self.p.rpc.call.assert_not_awaited();self.p.rpc.send.assert_not_awaited()
        self.assertEqual(s.ranker.data['events'][-1]['seconds'],8)
        restored=StreamRanker(s.ranker.path);self.assertEqual(len(restored.data['events']),1)
        self.assertGreater(restored.data['strategy'][item['card'].strategy],0)
    async def test_background_gap_not_counted_as_engagement(self):
        s=self.p.stream;s.current();s.acknowledge('device',s.token,10);s.acknowledge('device',s.token,1000)
        self.assertEqual(s.visible['seconds'],0)
    async def test_card_confirmation_is_idempotent_and_stale_navigation_rejected(self):
        self.rpc()
        self.p.turn='main-busy';s=self.p.stream;s.recs=[self.card()];s.seed=(tuple(self.p.recommendations.topics),(),())
        # Normal seed generation may supply starter cards; the chosen card identity is still stable.
        item,_,_=s.current();token=s.token
        await s.action('stream_confirm',{'token':token});await self.flush()
        await s.action('stream_confirm',{'token':token});await self.flush()
        self.assertEqual(len(s.jobs),0)  # the main-task status card does not execute
        self.p.turn=None;s.key=None;item,_,_=s.current();token=s.token
        await s.action('stream_confirm',{'token':token});await self.flush()
        self.assertEqual(len(s.jobs),1)
        await s.action('stream_confirm',{'token':token});self.assertEqual(len(s.jobs),1)
    async def test_two_readonly_sessions_parallel_then_writer_waits(self):
        self.rpc();s=self.p.stream
        await s.enqueue(self.card('r1'));await self.flush()
        await s.enqueue(self.card('r2','explain'));await self.flush()
        await s.enqueue(self.card('write','advance'));await self.flush()
        self.assertEqual([j['state'] for j in s.jobs],['running','running','queued'])
        self.assertNotEqual(s.jobs[0]['thread'],s.jobs[1]['thread'])
        for j in s.jobs[:2]:
            await self.p.notification({'method':'turn/completed','params':{'threadId':j['thread'],'turn':{'id':j['turn'],'status':'completed'}}})
        s.kick();await self.flush();self.assertEqual(s.jobs[2]['state'],'running')
        starts=[a.args[1]['cwd'] for a in self.p.rpc.call.await_args_list if a.args[0]=='thread/start']
        self.assertNotEqual(starts[0],starts[1]);self.assertEqual(starts[2],self.p.config['cwd'])
    async def test_writer_excludes_other_workers_and_main_voice_queues(self):
        self.rpc();s=self.p.stream;await s.enqueue(self.card('w','advance'));await self.flush()
        await s.enqueue(self.card('r'));await self.flush();self.assertEqual(s.jobs[1]['state'],'queued')
        await self.p.submit('继续检查另一个问题');await self.flush()
        self.assertEqual(s.jobs[-1]['state'],'queued');self.assertEqual(s.jobs[-1]['strategy'],'direct')
    async def test_delivery_ambiguity_never_retries_and_restart_does_not_replay(self):
        self.rpc();s=self.p.stream
        orig=self.p.rpc.call.side_effect
        async def fail(method,params=None):
            if method=='turn/start':raise TimeoutError()
            return await orig(method,params)
        self.p.rpc.call.side_effect=fail
        await s.enqueue(self.card());await self.flush();s.kick();await self.flush()
        self.assertEqual(s.jobs[0]['state'],'delivery_unknown')
        self.assertEqual(sum(a.args[0]=='turn/start' for a in self.p.rpc.call.await_args_list),1)
        restored=TaskStream(self.p);self.assertEqual(restored.jobs[0]['state'],'delivery_unknown')
    async def test_disk_failure_prevents_submission(self):
        with patch.object(self.p.stream,'save',side_effect=OSError()):
            with self.assertRaises(ValueError):await self.p.stream.enqueue(self.card())
        self.assertFalse(self.p.stream.jobs);self.p.rpc.call.assert_not_awaited()
    async def test_approval_requires_review_then_explicit_choice(self):
        s=self.p.stream;self.p.approvals=[self.request()]
        detail,_,_=s.current();self.assertEqual(detail['kind'],'review')
        await s.action('stream_next',{'token':s.token});choice,_,_=s.current();self.assertEqual(choice['decision'],'accept')
        await s.action('stream_confirm',{'token':s.token});self.p.rpc.send.assert_not_awaited()
        s.reviewed.add((choice['request'],0));await s.action('stream_confirm',{'token':s.token})
        self.p.rpc.send.assert_awaited_once_with({'id':17,'result':{'decision':'accept'}})
    async def test_swipe_alone_never_approves_and_late_choice_is_invalid(self):
        s=self.p.stream;self.p.approvals=[self.request()];s.current()
        await s.action('stream_next',{'token':s.token});s.current();token=s.token
        self.p.approvals=[];await s.action('stream_confirm',{'token':token})
        self.p.rpc.send.assert_not_awaited()
    async def test_job_state_change_invalidates_confirmation(self):
        self.p.turn='blocking';s=self.p.stream;await s.enqueue(self.card());await self.flush()
        s.key=s.jobs[0]['id']+':status:0';s.current();token=s.token
        s.jobs[0]['state']='running';await s.action('stream_confirm',{'token':token})
        self.assertEqual(s.jobs[0]['state'],'running');self.p.rpc.call.assert_not_awaited()
    async def test_completion_is_not_replayed_and_creates_goal_related_followup(self):
        self.rpc();s=self.p.stream;await s.enqueue(self.card());await self.flush();j=s.jobs[0]
        msg={'method':'turn/completed','params':{'threadId':j['thread'],'turn':{'id':j['turn'],'status':'completed'}}}
        j['reply']='上传测试通过，但弱网场景仍未验证。';await self.p.notification(msg);await self.p.notification(msg)
        self.assertEqual(len([e for e in s.ranker.data['events'] if e['outcome']=='completed']),1)
        cards=s.ranker.ranked(self.p,{j['cardId']},s.jobs)
        self.assertTrue(any('弱网场景' in c.prompt for c in cards))
    async def test_stream_frames_bound_and_old_profile_preserved(self):
        self.p.status='待命';packet={'data':'legacy','type':'frame','hash':'test'}
        native=round_packet(self.p,packet,ui_font,stream=True)
        self.assertEqual(native['roundView'],4);self.assertIn('streamToken',native)
        self.assertLess(len(json.dumps(native,ensure_ascii=False).encode()),15000)
        old=round_packet(self.p,packet,ui_font);self.assertNotEqual(old['roundView'],4);self.assertNotIn('streamToken',old)
    async def test_only_negotiated_device_can_operate_stream(self):
        s=self.p.stream;s.current();token=s.token
        await self.p.device_packet({'type':'hello','displayProfile':'taiji360'},'old')
        await self.p.device_packet({'type':'event','action':'stream_confirm','payload':{'token':token}},'old')
        self.assertFalse(s.jobs)
        await self.p.device_packet({'type':'hello','displayProfile':'taiji360','taskStream':1},'new')
        self.assertTrue(self.p.peer_streams['new'])

    async def test_voice_answer_is_captured_for_question_and_requires_confirmation(self):
        req={'id':22,'method':'item/tool/requestUserInput','params':{'threadId':self.p.thread,'questions':[{'id':'q1','header':'选择方向','question':'你希望先改哪里？','options':[]}]}}
        self.p.approvals=[req];stream=self.p.stream
        question=next(x for x in stream.items() if x['kind']=='question');stream.key=question['key'];stream.current();target=stream.capture_voice_target()
        with patch.object(self.p,'whisper_transcribe',return_value='先改交互'):
            await self.p.transcribe(bytes(16000),stream_target=target)
        self.p.rpc.call.assert_not_awaited();self.p.rpc.send.assert_not_awaited()
        self.assertIsNotNone(stream.voice_draft)
        items=stream.items();confirm=next(x for x in items if x.get('answerGroup'))
        stream.key=confirm['key'];stream.current()
        for i in range(confirm['pages']):stream.reviewed.add((confirm['request'],i))
        for i in range(confirm['contextPages']):stream.reviewed.add((confirm['answerGroup'],i))
        await stream.action('stream_confirm',{'token':stream.token})
        self.p.rpc.send.assert_awaited_once_with({'id':22,'result':{'answers':{'q1':{'answers':['先改交互']}}}})
    async def test_stale_question_voice_never_becomes_a_new_task(self):
        target={'owner':'main','request':'main:22','requestId':22,'question':'q1','pages':1}
        with patch.object(self.p,'whisper_transcribe',return_value='回答旧问题'):
            await self.p.transcribe(bytes(16000),stream_target=target)
        self.p.rpc.call.assert_not_awaited();self.assertFalse(self.p.stream.jobs)
        self.assertEqual(self.p.status,'语音失败')
    async def test_multiline_request_is_fully_represented_in_review_cards(self):
        req=self.request();req['params']['command']='第一行\n第二行\n第三行'
        cards=self.p.stream.request_items('main',req)
        body=''.join(c['body'] for c in cards if c['kind']=='review')
        self.assertIn('第一行',body);self.assertIn('第二行',body);self.assertIn('第三行',body)
        self.assertTrue(all('\n' not in c['body'] for c in cards if c['kind']=='review'))
    async def test_worker_permission_waits_for_other_workers_and_is_bound_to_request(self):
        self.rpc();s=self.p.stream
        await s.enqueue(self.card('a'));await self.flush();await s.enqueue(self.card('b'));await self.flush()
        first,other=s.jobs;req=self.request(70);req['params']['threadId']=first['thread']
        await self.p.notification(req)
        choice=next(x for x in s.items() if x.get('requestId')==70 and x.get('decision')=='accept')
        for i in range(choice['pages']):s.reviewed.add((choice['request'],i))
        await s.decide(choice);self.p.rpc.send.assert_not_awaited()
        await self.p.notification({'method':'turn/completed','params':{'threadId':other['thread'],'turn':{'id':other['turn'],'status':'completed'}}})
        await s.decide(choice);self.p.rpc.send.assert_awaited_once_with({'id':70,'result':{'decision':'accept'}})
        self.assertFalse(first['readonly'])
    async def test_console_answer_rejects_stale_owner_and_incomplete_answers(self):
        self.p.approvals=[{'id':31,'method':'item/tool/requestUserInput','params':{'questions':[{'id':'q1'}]}}]
        with self.assertRaises(ValueError):await self.p.stream.console_decide({'owner':'wrong','requestId':31,'decision':'accept','answers':{}})
        with self.assertRaises(ValueError):await self.p.stream.console_decide({'owner':'main','requestId':31,'decision':'accept','answers':{}})
        self.p.rpc.send.assert_not_awaited()

    async def test_worker_result_advances_matching_goal_and_closure_remains_explicit(self):
        self.rpc();p=self.p;p.goal_flow.start('稳定录音上传');p.goal_flow.request('验收标准：上传不丢帧','direct')
        card=self.card('accept-goal','goal_accept');await p.stream.enqueue(card);await self.flush();j=p.stream.jobs[0]
        await p.notification({'method':'turn/completed','params':{'threadId':j['thread'],'turn':{'id':j['turn'],'status':'completed'}}})
        self.assertIsNotNone(p.goal_flow.active);self.assertEqual(p.goal_flow.active['criteria'],'上传不丢帧')
        self.assertEqual(p.goal_flow.active['results'][-1]['action'],'goal_accept')
        close=next(c for c in p.stream.items() if c['kind']=='goal_close');p.stream.key=close['key'];p.stream.current()
        await p.stream.action('stream_confirm',{'token':p.stream.token})
        self.assertIsNone(p.goal_flow.active);self.assertTrue(p.goal_flow.data['archive'][-1]['acceptedByUser'])
    async def test_long_question_does_not_offer_unread_approval_or_voice_answer(self):
        request={'id':90,'method':'item/tool/requestUserInput','params':{'questions':[{'id':'long','question':'长问题'*600,'options':[{'label':'同意'}]}]}}
        items=self.p.stream.request_items('main',request)
        self.assertFalse(any(c.get('answer') for c in items));self.assertFalse(any(c['kind']=='question' for c in items))
        self.assertTrue(any(c.get('decision')=='decline' for c in items))
    async def test_history_compaction_keeps_live_jobs(self):
        self.p.turn='blocked';s=self.p.stream;await s.enqueue(self.card('keep-live'));await self.flush();live=s.jobs[0]
        for i in range(39):s.jobs.append({**live,'id':'old'+str(i),'cardId':'old'+str(i),'state':'completed'})
        await s.enqueue(self.card('new'));await self.flush()
        self.assertIn(live,s.jobs);self.assertEqual(len(s.jobs),40)

if __name__=='__main__':unittest.main()
