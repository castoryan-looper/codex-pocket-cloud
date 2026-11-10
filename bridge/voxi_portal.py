"""Owner-scoped web API. actorUserId is supplied by the authenticated gateway.

This endpoint remains Docker-internal, never directly exposed by ingress.
Its response deliberately does not reuse the administrator's global snapshot.
"""
import hmac
import json
import re
from aiohttp import web
from voxi_agent_store import CHANNEL_LIMIT
from voxi_models import PROVIDERS
from voxi_voices import catalog,validate_voice


class UserPortal:
    def __init__(self, radio):self.radio=radio

    def snapshot(self, uid):
        r=self.radio;hub=r.agent;cfg=hub.store.settings(uid)
        return {**r.memory.owned_snapshot(uid),'role':'user','serviceEnabled':cfg['enabled'],
                'channels':cfg['channels'],'inheritsChannels':uid not in hub.store.channel_overrides(),
                'preferences':hub.store.preferences(uid),'connections':hub.openclaw.public(uid),
                'modelConnections':hub.models.public(uid),'modelProviders':PROVIDERS,'channelLimit':CHANNEL_LIMIT,
                'voiceSettings':r.voice_preview.enrich(catalog(r.settings.snapshot()['voice'],hub.store.preferences(uid)['voice'])),
                'tasks':[hub.store.public_task(t) for t in hub.store.list_tasks(uid)],
                'usage':hub.store.user_usage(uid),'channelTools':hub.capabilities.catalog(uid),
                'capabilities':{'openclaw':True,'museCode':True,'capabilityLibrary':1,'userPortal':1,'channelVoices':True,'contextSync':True}}

    async def changed_context(self, uid):
        r=self.radio;r.memory_revision[uid]=r.memory_revision.get(uid,0)+1
        for session in tuple(r.active):
            if session.memory_user==uid:await session.ws.close(code=1012,message=b'Personal context updated')

    async def handle(self, request):
        r=self.radio
        if request.headers.get('Origin') or not hmac.compare_digest(request.headers.get('Authorization',''),'Bearer '+r.owner.config['device_token']):raise web.HTTPForbidden()
        if not r.agent or not r.memory:raise web.HTTPServiceUnavailable()
        try:
            raw=await request.read()
            if len(raw)>131072:raise ValueError('请求过大')
            body=json.loads(raw)
            if not isinstance(body,dict):raise ValueError('请求无效')
            action=body.get('action');hub=r.agent
            if action=='context_ingest':
                if 'actorUserId' in body:raise ValueError('网页会话不能模拟同步连接器')
                result=hub.context.ingest(body.get('token'),body.get('enabledUsers'),body.get('chat'))
                return web.json_response(result,headers={'Cache-Control':'no-store'})
            uid=body.get('actorUserId')
            if not isinstance(uid,str) or not re.fullmatch(r'[a-f0-9]{32}',uid) or not r.memory.user(uid):raise ValueError('账户未绑定有效用户档案')
            if 'userId' in body and body['userId']!=uid:raise ValueError('不能操作其他用户')
            if action=='snapshot':pass
            elif action=='tasks':
                before=body.get('before')
                if before is not None and (not isinstance(before,str) or not re.fullmatch(r'[a-f0-9]{32}',before)):
                    raise ValueError('任务分页游标无效')
                return web.json_response(hub.store.portal_tasks(uid,before),headers={'Cache-Control':'no-store'})
            elif action=='profile':
                r.memory.change({'action':'profile','userId':uid,'name':body.get('name'),'profile':body.get('profile'),'enabled':body.get('enabled')})
                await self.changed_context(uid)
            elif action=='preferences':
                hub.store.save_preferences(uid,body.get('preferences'));await self.changed_context(uid)
            elif action=='voice_preference':
                voice=validate_voice(body.get('voice'),inherit=True)
                hub.store.save_preferences(uid,{**hub.store.preferences(uid),'voice':voice})
            elif action=='channels':
                hub.check_routes(body.get('channels'),uid);hub.store.save_settings(body.get('channels'),uid)
                await r.publish_channel_catalog(uid)
            elif action=='reset_channels':
                hub.store.reset_channels(uid)
                await r.publish_channel_catalog(uid)
            elif action=='rename_device':r.memory.change({'action':action,'userId':uid,'deviceId':body.get('deviceId'),'name':body.get('name')})
            elif action=='connect_openclaw':
                result=hub.openclaw.create(uid,body.get('name'),body.get('transport'),body.get('url',''),body.get('agent','main'),body.get('token',''))
                return web.json_response(result,headers={'Cache-Control':'no-store'})
            elif action=='connect_muse':
                result=hub.openclaw.create_muse(uid,body.get('name'))
                return web.json_response(result,headers={'Cache-Control':'no-store'})
            elif action=='test_openclaw':
                return web.json_response(await hub.openclaw.probe(uid,body.get('connectionId')),headers={'Cache-Control':'no-store'})
            elif action=='delete_openclaw':hub.openclaw.delete(uid,body.get('connectionId'))
            elif isinstance(action,str) and action.startswith('capability_'):
                result=await hub.capabilities.manage(uid,body)
                if action in ('capability_github_connect','capability_github_remove','capability_remote_remove'):
                    await r.publish_channel_catalog(uid)
                return web.json_response(result,headers={'Cache-Control':'no-store'})
            elif action=='connect_model':
                return web.json_response(await hub.models.create(uid,body),headers={'Cache-Control':'no-store'})
            elif action=='test_model':
                return web.json_response(await hub.models.probe(uid,body.get('connectionId')),headers={'Cache-Control':'no-store'})
            elif action=='delete_model':hub.models.delete(uid,body.get('connectionId'))
            elif action in ('context_import','context_authorize','context_list','context_revoke','context_detail','context_remove','context_pin'):
                return web.json_response(hub.context.manage(uid,body),headers={'Cache-Control':'no-store'})
            elif action=='submit':
                task,_=hub.submit(uid,None,body.get('channel'),body.get('requestId'),body.get('text'))
                return web.json_response({'task':task},headers={'Cache-Control':'no-store'})
            elif action=='detail':
                task=hub.store.task(body.get('taskId'),uid)
                if not task:raise ValueError('任务不存在或不可访问')
                return web.json_response({'task':hub.store.public_task(task,True)},headers={'Cache-Control':'no-store'})
            elif action in ('cancel','approve','decline','answer','continue'):
                task=await hub.action(uid,body.get('taskId'),action,text=body.get('text'),request_id=body.get('requestId'),answers=body.get('answers'),approval_id=body.get('approvalId'))
                return web.json_response({'task':task},headers={'Cache-Control':'no-store'})
            else:raise ValueError('普通用户不能修改此项配置')
            return web.json_response(self.snapshot(uid),headers={'Cache-Control':'no-store'})
        except (ValueError,TypeError,KeyError) as e:
            return web.json_response({'error':str(e)},status=400,headers={'Cache-Control':'no-store'})
        except OSError:
            return web.json_response({'error':'保存失败，请联系管理员检查存储'},status=500)
