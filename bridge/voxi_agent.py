"""Voxi's task service, reusing the installed Codex app-server and login.

Its lifetime belongs to the bridge, not to a radio websocket. Model prose never
decides task success. Restarted/uncertain work is never silently resubmitted.
"""
import asyncio
import contextlib
import json
from pathlib import Path
import os
import re
import time
from collections import defaultdict

from harness import DEVELOPER_INSTRUCTIONS
from voxi_agent_store import AgentStore, LIVE, TERMINAL
from voxi_openclaw import OpenClaw


class AgentHub:
    def __init__(self, directory, workspace, rpc_factory):
        self.store=AgentStore(directory)
        self.openclaw=OpenClaw(self)
        self.workspace=Path(workspace)/'voxi-users'
        self.workspace.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.rpc_factory=rpc_factory
        self.rpc=None
        self.rpc_lock=asyncio.Lock()
        self.active={}
        self.listeners=set()
        self.loop=None
        self.closing=False
        self.runtime_error=''
        self.action_locks=defaultdict(asyncio.Lock)
        self.text_buffers={}
        self.context_provider=None

    def start(self):
        if self.loop is None:
            self.loop=asyncio.create_task(self.dispatch())

    async def close(self):
        self.closing=True
        if self.loop:
            self.loop.cancel()
            await asyncio.gather(self.loop,return_exceptions=True)
        jobs=list(self.active.values())
        for job in jobs:job.cancel()
        await asyncio.gather(*jobs,return_exceptions=True)
        if self.rpc:
            await self.close_rpc(self.rpc)
            self.rpc=None
        self.store.close()

    @staticmethod
    async def close_rpc(rpc):
        await rpc.close()
        process=getattr(rpc,'process',None)
        if process and process.returncode is None:
            process.kill()
            await process.wait()
        readers=[getattr(rpc,name,None) for name in ('reader','errors')]
        readers=[t for t in readers if isinstance(t,asyncio.Task)]
        for task in readers:task.cancel()
        await asyncio.gather(*readers,return_exceptions=True)

    async def ensure_rpc(self):
        async with self.rpc_lock:
            if self.rpc:return self.rpc
            rpc=self.rpc_factory(self.notification)
            try:
                await rpc.start()
                account=await rpc.call('account/read',{'refreshToken':False})
                if not account.get('account'):raise RuntimeError('Codex login unavailable')
            except Exception:
                await self.close_rpc(rpc)
                self.runtime_error='Codex 执行器不可用，请检查服务器现有登录及 CLI 版本'
                raise
            self.rpc=rpc;self.runtime_error=''
            return rpc

    async def emit(self, task):
        if not task:return
        packet=self.store.public_task(task)
        for listener in tuple(self.listeners):
            try:await listener(packet)
            except Exception:pass  # A disconnected radio never owns the task.

    async def change(self, ident, **values):
        if values.get('state') in TERMINAL and ident in self.text_buffers:
            text,_=self.text_buffers.pop(ident)
            self.store.update(ident,result=text)
        task=self.store.update(ident,**values)
        await self.emit(task)
        return task

    def submit(self, user, device, channel, request_id, prompt, thread=None, *, original=None):
        if not re.fullmatch(r'[a-f0-9]{32}',user or ''):raise ValueError('设备尚未绑定有效用户')
        settings=self.store.settings(user)
        if not settings['enabled']:raise ValueError('管理员尚未启用 Voxi Agent')
        if type(channel) is not int or not 0<=channel<4 or settings['channels'][channel]['mode'] not in ('agent','openclaw'):raise ValueError('当前频道未配置 Agent')
        route=settings['channels'][channel]
        engine=original['engine'] if original else ('openclaw' if route['mode']=='openclaw' else 'codex')
        connection=original['connection_id'] if original else route.get('connectionId')
        if engine=='openclaw' and not self.openclaw.connection(connection,user):raise ValueError('请先接入 OpenClaw 并绑定到此频道')
        task,created=self.store.submit(user,device,channel,request_id,prompt,thread,engine=engine,connection=connection)
        self.start()
        return self.store.public_task(task,True),created

    async def dispatch(self):
        while True:
            settings=self.store.settings()
            if settings['enabled']:
                busy={(self.store.task(i)['user_id'],self.store.task(i)['channel']) for i in self.active}
                for task in reversed(self.store.list_tasks(limit=200)):
                    key=(task['user_id'],task['channel'])
                    if task['state']!='queued' or task['id'] in self.active or key in busy:continue
                    if not self.openclaw.available(task):continue
                    if len(self.active)>=settings['maxConcurrent']:break
                    busy.add(key)
                    job=asyncio.create_task(self.run(task['id']))
                    self.active[task['id']]=job
                    job.add_done_callback(lambda _,ident=task['id']:self.active.pop(ident,None))
            await asyncio.sleep(.2)

    async def run(self, ident):
        submitted=False
        try:
            task=self.store.task(ident)
            if task['state']!='queued':return
            if task['engine']=='openclaw':
                await self.openclaw.run(task)
                return
            # Keep queued until the runtime is ready; no side effect was admitted yet.
            rpc=await self.ensure_rpc()
            task=self.store.task(ident)
            if task['state']!='queued':return
            cfg=self.store.settings(task['user_id'])
            cwd=self.workspace/task['user_id']
            cwd.mkdir(parents=True,exist_ok=True,mode=0o700)
            params={'cwd':str(cwd),'approvalPolicy':'on-request','sandbox':'workspace-write',
                    'developerInstructions':DEVELOPER_INSTRUCTIONS+'\n'+cfg['instructions']}
            if self.context_provider:
                params['developerInstructions']+=self.context_provider(task['user_id'])
            if cfg['model']:params['model']=cfg['model']
            if task['thread_id']:
                reply=await rpc.call('thread/resume',{**params,'threadId':task['thread_id']})
            else:
                reply=await rpc.call('thread/start',params)
            thread=reply['thread']['id']
            if self.store.task(ident)['state']!='queued':return
            await self.change(ident,state='submitting',thread_id=thread,summary='正在向 Codex 提交任务')
            params={'threadId':thread,'input':[{'type':'text','text':task['prompt']}]}
            if os.environ.get('POCKET_EXTERNAL_SANDBOX')=='docker':
                params['sandboxPolicy']={'type':'externalSandbox','networkAccess':'enabled'}
            submitted=True
            reply=await rpc.call('turn/start',params)
            current=self.store.task(ident)
            if current['state']=='submitting':
                await self.change(ident,state='running',turn_id=reply['turn']['id'],summary='正在执行')
            deadline=time.monotonic()+cfg['maxTaskSeconds']
            while self.store.task(ident)['state'] not in TERMINAL:
                if time.monotonic()>=deadline:
                    current=self.store.task(ident)
                    if current['turn_id']:
                        with contextlib.suppress(Exception):
                            await rpc.call('turn/interrupt',{'threadId':thread,'turnId':current['turn_id']})
                    await self.change(ident,state='interrupted',summary='超过任务时间上限，已请求停止；请核对已产生的结果')
                    break
                await asyncio.sleep(.2)
        except asyncio.CancelledError:
            if self.store.task(ident)['state'] not in TERMINAL:
                await self.change(ident,state='interrupted',summary='服务正在停止；未自动重放任务')
            raise
        except Exception:
            if self.store.task(ident)['state'] not in TERMINAL:
                await self.change(ident,state='delivery_unknown' if submitted else 'failed',
                                  summary='提交结果尚未确认，请核对后再继续' if submitted else '执行器未就绪，请检查服务器登录或模型配置')

    def for_thread(self, thread):
        if not thread:return None
        for ident in self.active:
            task=self.store.task(ident)
            if task['engine']=='codex' and task['thread_id']==thread and task['state'] in LIVE-{'queued'}:return task
        return None

    async def notification(self, msg):
        if self.closing:return
        method=msg.get('method','');params=msg.get('params') or {}
        if method=='pocket/disconnected':
            self.rpc=None
            self.runtime_error='Codex 连接已断开，运行中任务未自动重放'
            for ident in tuple(self.active):
                if self.store.task(ident)['engine']=='codex' and self.store.task(ident)['state'] in LIVE:
                    await self.change(ident,state='interrupted',summary=self.runtime_error)
            return
        task=self.for_thread(params.get('threadId'))
        if not task:
            if 'id' in msg and self.rpc:
                await self.rpc.send({'id':msg['id'],'error':{'code':-32601,'message':'No active Voxi task'}})
            return
        ident=task['id']
        turn_ref=params.get('turnId') or (params.get('turn') or {}).get('id')
        if turn_ref:
            previous=self.store.db.execute('SELECT id FROM tasks WHERE thread_id=? AND turn_id=? AND id<>?',
                                           (task['thread_id'],turn_ref,ident)).fetchone()
            if previous:return
        if params.get('turnId') and task['turn_id'] and params['turnId']!=task['turn_id']:return
        if 'id' in msg:
            if method in ('item/commandExecution/requestApproval','item/fileChange/requestApproval','item/tool/requestUserInput'):
                first=self.store.approval(ident,msg)
                needs_input=first['method'].endswith('requestUserInput')
                await self.change(ident,state='needs_input' if needs_input else 'awaiting_approval',
                                  summary='需要补充信息' if needs_input else '需要确认操作')
            else:
                await self.rpc.send({'id':msg['id'],'error':{'code':-32601,'message':'Unsupported by Voxi task client'}})
            return
        if method=='turn/started':
            if task['turn_id'] and task['turn_id']!=params['turn']['id']:return
            await self.change(ident,state='running',turn_id=params['turn']['id'],summary='正在执行')
        elif method=='turn/completed':
            turn=params.get('turn',{})
            if task['turn_id'] and turn.get('id')!=task['turn_id']:return
            state={'completed':'completed','interrupted':'cancelled','failed':'failed'}.get(turn.get('status'),'interrupted')
            result=self.text_buffers.get(ident,(task['result'],0))[0]
            summary=(' '.join(result.split())[:180] or '任务完成，请查看详情') if state=='completed' else {'cancelled':'任务已停止；已产生的修改不会自动撤销','failed':'任务执行失败，请查看详情','interrupted':'执行结果尚未确认'}.get(state)
            await self.change(ident,state=state,summary=summary,turn_id=turn.get('id'))
        elif method=='item/agentMessage/delta':
            text,flushed=self.text_buffers.get(ident,(task['result'],0))
            text=(text+str(params.get('delta','')))[-64000:]
            now=time.monotonic()
            if now-flushed>=1:
                self.store.update(ident,result=text);flushed=now
            self.text_buffers[ident]=(text,flushed)
        elif method=='item/completed' and params.get('item',{}).get('type')=='agentMessage':
            text=params['item'].get('text')
            if text:
                self.text_buffers.pop(ident,None)
                self.store.update(ident,result=str(text)[-64000:])
        elif method=='item/started':
            label={'commandExecution':'正在执行命令','fileChange':'正在修改文件','webSearch':'正在查找资料',
                   'mcpToolCall':'正在调用工具','reasoning':'正在思考','agentMessage':'正在整理结果'}.get(params.get('item',{}).get('type'),'正在执行')
            if task['state']=='running' and task['summary']!=label:
                await self.change(ident,summary=label)
        elif method=='thread/tokenUsage/updated':
            self.store.set_usage(ident,(params.get('tokenUsage') or {}).get('last'))

    async def action(self, user, ident, action, *, text=None, request_id=None, answers=None):
        async with self.action_locks[ident]:
            return await self._action(user,ident,action,text=text,request_id=request_id,answers=answers)

    async def _action(self, user, ident, action, *, text=None, request_id=None, answers=None):
        task=self.store.task(ident,user)
        if not task:raise ValueError('任务不存在或不属于当前用户')
        if action=='continue':
            if task['state'] not in TERMINAL or not task['thread_id']:raise ValueError('任务尚未结束或没有可继续的会话')
            return self.submit(user,task['device_id'],task['channel'],request_id,text,task['thread_id'],original=task)[0]
        if action=='cancel':
            if task['state'] in TERMINAL:return self.store.public_task(task,True)
            connection=self.openclaw.connection(task['connection_id'],user) if task['engine']=='openclaw' else None
            if task['state']=='queued' or (task['state']=='submitting' and connection and connection['transport']=='relay'):
                task=await self.change(ident,state='cancelled',summary='任务在开始前已取消')
            elif task['engine']=='openclaw':
                await self.change(ident,state='delivery_unknown',summary='已停止等待；直连已断开请求，家中连接器可能仍在执行，请在 OpenClaw 核对或停止')
                job=self.active.get(ident)
                if job:job.cancel()
            elif task['turn_id'] and self.rpc:
                await self.change(ident,state='cancelling',summary='已请求停止，等待执行器确认')
                try:await self.rpc.call('turn/interrupt',{'threadId':task['thread_id'],'turnId':task['turn_id']})
                except Exception:await self.change(ident,state='delivery_unknown',summary='停止请求尚未确认，请核对运行状态')
            else:
                raise ValueError('任务正在提交，请等待执行器返回后再停止')
            return self.store.public_task(self.store.task(ident),True)
        if action in ('approve','decline','answer'):
            request=self.store.approval(ident)
            if task['state'] not in ('awaiting_approval','needs_input') or not request or not self.rpc:
                raise ValueError('该请求已失效，请重新读取任务')
            if request['method'].endswith('requestUserInput'):
                if action!='answer':raise ValueError('此任务需要补充答案，不能批准替代')
                questions=request.get('params',{}).get('questions',[])
                if not isinstance(answers,dict) or set(answers)!={q['id'] for q in questions}:raise ValueError('请回答所有问题')
                if any(not isinstance(v,dict) or not isinstance(v.get('answers'),list) or not v['answers'] or
                       any(not isinstance(x,str) or len(x)>4000 for x in v['answers']) for v in answers.values()):raise ValueError('答案格式无效')
                result={'answers':answers}
            else:
                if action=='answer':raise ValueError('此请求需要批准或拒绝')
                result={'decision':'accept' if action=='approve' else 'decline'}
            try:
                await self.rpc.send({'id':request['id'],'result':result})
            except Exception:
                await self.change(ident,state='delivery_unknown',summary='确认决定的送达状态未知，请核对执行器后再操作')
                return self.store.public_task(self.store.task(ident),True)
            self.store.pop_approval(ident,request['id'])
            task=self.store.task(ident)
            if task['state'] not in TERMINAL:
                pending=self.store.approval(ident)
                state='needs_input' if pending and pending['method'].endswith('requestUserInput') else 'awaiting_approval' if pending else 'running'
                task=await self.change(ident,state=state,summary='仍有请求需要处理' if pending else '已提交决定，正在继续')
            return self.store.public_task(task,True)
        raise ValueError('不支持的任务操作')

    def snapshot(self, memory):
        users=memory.snapshot() if memory else {'users':[],'devices':[]}
        tasks=self.store.list_tasks()
        return {'settings':self.store.settings(),'users':users.get('users',[]),'devices':users.get('devices',[]),
                'channelsByUser':{u['id']:self.store.settings(u['id'])['channels'] for u in users.get('users',[])},
                'channelOverrides':self.store.channel_overrides(),'connections':self.openclaw.public(),
                'tasks':[self.store.public_task(t) for t in tasks], 'credentials':self.store.credentials(),
                'runtime':{'engine':'codex-pocket-cloud','ready':self.rpc is not None,'lastError':self.runtime_error,
                           'credentialMode':'existing-codex-login','providerKeysActive':False,
                           'deployment':'personal-pilot','externalOpenClaw':'responses-https-or-relay'},
                'usage':{'tasks':len(tasks),'live':sum(t['state'] in LIVE for t in tasks),
                         'note':'记录实际报告的 token；不将 Codex 订阅额度换算为 API 账单'}}
