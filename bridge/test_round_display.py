import base64,json,random,unittest
import test_bridge
from bridge import ui_font
from round_display import pack_bitmap,round_packet,render_round

def unpack(data):
    out=bytearray();i=0
    while i<len(data):
        tag=data[i];i+=1;size=(tag&127)+1
        if tag&128:out.extend(bytes([data[i]])*size);i+=1
        else:out.extend(data[i:i+size]);i+=size
    return bytes(out)

class RoundTests(unittest.IsolatedAsyncioTestCase):
    setUp=test_bridge.Tests.setUp
    tearDown=test_bridge.Tests.tearDown

    async def test_profile_is_negotiated_only_for_explicit_hello(self):
        await self.p.device_packet({'type':'hello'},'old')
        await self.p.device_packet({'type':'hello','displayProfile':'taiji360'},'round')
        await self.p.device_packet({'type':'hello','displayProfile':'unknown'},'unknown')
        self.assertEqual(self.p.peer_profiles,{'old':'legacy','round':'taiji360','unknown':'legacy'})

    async def test_native_frames_are_bounded_readable_and_do_not_mutate_legacy_packet(self):
        p=self.p;p.status='待命';p.goal_flow.start('让语音上传稳定可靠');p.recommendation_cards=p.recommendation_pair()
        packet={'type':'frame','data':'legacy','hash':'frame1'}
        for action in (None,'rec_a','rec_details','rec_context'):
            if action:await p.action(action)
            image,meta=render_round(p,ui_font);self.assertEqual(image.size,(360,360))
            self.assertEqual(image.getpixel((0,0)),0);self.assertEqual(image.getpixel((359,359)),0)
            result=round_packet(p,packet,ui_font)
            self.assertIn('roundRle',result);self.assertNotIn('data',result)
            self.assertEqual(unpack(base64.b64decode(result['roundRle'])),image.tobytes())
            self.assertLess(len(json.dumps(result,ensure_ascii=False).encode()),15000)
        self.assertEqual(packet['data'],'legacy')

    def test_round_task_draft_and_approval_render_without_executing(self):
        p=self.p;p.status='工作中';p.turn='task';p.detail='检查实际证据。'*300
        _,meta=render_round(p,ui_font);self.assertTrue(meta['roundBusy']);self.assertEqual(meta['roundView'],3)
        self.assertIn('roundRle',round_packet(p,{'data':'legacy'},ui_font))
        self.assertTrue(meta['roundCanRecord'])  # voice steering remains available

    async def test_long_reply_paging_is_read_only_and_menu_remains_independent(self):
        p=self.p;p.status='工作中';p.turn='busy';p.detail='一段需要完整阅读的执行说明。'*100
        await p.action('round_next');self.assertEqual(p.round_detail_page,1)
        await p.action('round_prev');self.assertEqual(p.round_detail_page,0)
        await p.action('round_prev');self.assertEqual(p.round_detail_page,0)
        p.rpc.call.assert_not_awaited()
        p.approvals=[{'id':'approval'}]
        _,meta=render_round(p,ui_font);self.assertFalse(meta['roundCanRecord'])

    def test_packbits_preserves_long_runs_literals_and_boundaries(self):
        rng=random.Random(12)
        for data in [b'',bytes(16200),bytes([255])*16200,bytes(range(256))*64,bytes(rng.randrange(256) for _ in range(16200)),b'aaabbaa'*300]:
            self.assertEqual(unpack(pack_bitmap(data)),data)

if __name__=='__main__':unittest.main()
