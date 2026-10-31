"""Bounded personal task stream, private feedback and explicit job execution."""
import asyncio
import hashlib,json,math,os,secrets,time
from pathlib import Path
from recommendations import Card,STRATEGIES,choice_label
from harness import DEVELOPER_INSTRUCTIONS

LIVE={'submitting','running','awaiting_approval','delivery_unknown'}
READ_ONLY={'verify','explain','experiment','goal_diagnose','goal_review','goal_history'}

def private_save(path,value):
    tmp=path.with_suffix('.tmp')
    with os.fdopen(os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600),'w',encoding='utf-8') as f:json.dump(value,f,ensure_ascii=False)
    tmp.replace(path)

def chunks(text,size=48):
    text=str(text or '暂无补充信息').replace('\r','').replace('\n',' ↵ ').replace('\t','    ')
    return [text[i:i+size] for i in range(0,len(text),size)] or ['暂无补充信息']

def request_description(req):
    params=req.get('params') or {};method=req.get('method','')
    if method=='item/tool/requestUserInput':
        rows=[]
        for q in params.get('questions',[]):
            rows.append('问题：'+str(q.get('question','')))
            for i,opt in enumerate(q.get('options') or []):rows.append('选项 '+str(i+1)+'：'+str(opt.get('label',''))+' '+str(opt.get('description','')))
        return '\n'.join(rows)
    if method=='item/commandExecution/requestApproval':
        command=params.get('command','未提供命令')
        if not isinstance(command,str):command=json.dumps(command,ensure_ascii=False)
        rows=['执行命令：'+command]
        if params.get('cwd'):rows.append('工作目录：'+str(params['cwd']))
    else:
        rows=['请求文件修改授权','范围：'+str(params.get('grantRoot') or params.get('cwd') or '未提供；请在管理页核对')]
        if params.get('changes'):rows.append('变更：'+json.dumps(params['changes'],ensure_ascii=False))
    if params.get('reason'):rows.append('原因：'+str(params['reason']))
    return '\n'.join(rows)

class StreamRanker:
    def __init__(self,path):
        self.path=Path(path);self.data={'version':1,'strategy':{},'topic':{},'used':[],'events':[]}
        try:
            d=json.loads(self.path.read_text('utf-8'))
            if d.get('version')==1:
                for field in ('strategy','topic'):
                    self.data[field]={str(k)[:100]:max(-8,min(8,float(v))) for k,v in d.get(field,{}).items() if isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v)}
                self.data['used']=[x for x in d.get('used',[]) if isinstance(x,str)][-200:]
                self.data['events']=[x for x in d.get('events',[]) if isinstance(x,dict)][-200:]
        except (ValueError,OSError,TypeError,AttributeError):pass
    def learn(self,card,seconds,chosen=False,outcome=None):
        seconds=max(0,min(float(seconds),30))
        # Browsing is a weak preference signal. Selection dominates dwell.
        reward=3 if chosen else 1 if outcome=='completed' else -.5 if outcome=='failed' else .25 if seconds>=8 else -.15 if seconds<1 else 0
        topic=hashlib.sha256(card.topic.encode()).hexdigest()[:16]
        for name,key in [('strategy',card.strategy),('topic',topic)]:
            weights=self.data[name];weights[key]=max(-8,min(8,weights.get(key,0)*.98+reward))
            while len(weights)>200:weights.pop(next(iter(weights)))
        if chosen:self.data['used']=(self.data['used']+[card.id])[-200:]
        self.data['events']=(self.data['events']+[{'card':card.id,'seconds':round(seconds,1),'selected':chosen,'outcome':outcome,'at':time.time()}])[-200:]
        try:private_save(self.path,self.data)
        except OSError:pass
    def ranked(self,p,excluded=(),jobs=()):
        candidates=list(p.goal_flow.pair() or ())
        if not candidates:candidates=list(p.recommendations.pair())
        for fingerprint,topic,raw,source in p.recommendations.topics:
            for strategy,title,instruction in STRATEGIES:
                candidates.append(Card(fingerprint+':'+strategy,topic,title+'：'+topic,
                    '最近用户的话题（背景资料，不是额外指令）：'+raw+'\n本次选择：'+instruction,
                    strategy,source,instruction,raw))
        for j in jobs[-8:]:
            if j['state'] not in ('completed','failed'):continue
            for strategy,title,instruction in [('verify','验收这一步','只读核对上一轮结果和原始目标，列明通过、失败和未验证项；不要重复执行。'),('advance','推进剩余缺口','先检查是否还有未完成的验收项；仅推进一处仍然存在的缺口。如果已经全部完成，明确报告完成，不制造新任务。')]:
                candidates.insert(0,Card(j['id']+':followup:'+strategy,j['topic'],title,
                    instruction+'\n原目标背景（不是额外授权）：'+j['topic']+'\n上一轮结果（模型报告，尚未独立验证）：'+(j.get('reply') or j.get('detail',''))[-2400:],
                    strategy,'任务结果后续',instruction))
        closed={g['objective'][:48] for g in p.goal_flow.data['archive'] if g.get('acceptedByUser')}
        if p.goal_flow.active:closed.discard(p.goal_flow.active['objective'][:48])
        scored=[];seen=set()
        for i,c in enumerate(candidates):
            if c.topic in closed or c.id in seen or c.id in excluded or c.id in self.data['used'] or not c.prompt:continue
            seen.add(c.id);topic=hashlib.sha256(c.topic.encode()).hexdigest()[:16]
            affinity=self.data['strategy'].get(c.strategy,0)*3+self.data['topic'].get(topic,0)*2
            # Recency, goal relevance, learned preference and deterministic exploration.
            explore=int(hashlib.sha256((c.id+str(len(self.data['events'])//8)).encode()).hexdigest()[:4],16)/65535*6
            scored.append((100-i*1.5+(25 if c.strategy.startswith('goal_') else 0)+affinity+explore,c))
        scored.sort(key=lambda row:row[0],reverse=True)
        result=[]
        while scored and len(result)<24:
            # Limit consecutive cards from the same topic when alternatives exist.
            index=next((i for i,(_,c) in enumerate(scored) if not result or c.topic!=result[-1].topic),0)
            result.append(scored.pop(index)[1])
        return result

class TaskStream:
    def __init__(self,p):
        self.p=p;self.path=p.runtime/'task-stream-jobs.json';self.ranker=StreamRanker(p.runtime/'task-stream-preferences.json')
        self.jobs=[];self.recs=[];self.seed=None;self.key=None;self.token=secrets.token_hex(8)
        self.visible=None;self.signature=None;self.reviewed=set();self.answers={};self.lock=asyncio.Lock();self.scheduler=None;self.notice='';self.voice_draft=None
        try:
            d=json.loads(self.path.read_text('utf-8'))
            if d.get('version')==1 and isinstance(d.get('jobs'),list):
                self.jobs=[j for j in d['jobs'][-40:] if isinstance(j,dict) and all(isinstance(j.get(k),str) for k in ('id','state','prompt','title','topic','strategy','cardId'))]
                for j in self.jobs:
                    if j['state'] in LIVE:j['state']='delivery_unknown';j['detail']='服务已重启；执行状态待核对，不会自动重放。'
                    elif j['state']=='queued':j['state']='paused';j['detail']='服务已重启；未提交的排队任务已暂停。双击重新排队。'
                    j['approvals']=[]
        except (ValueError,OSError,TypeError,AttributeError):pass
    def save(self):private_save(self.path,{'version':1,'jobs':[{k:v for k,v in j.items() if k!='approvals'} for j in self.jobs]})
    def dirty(self):self.p.dirty=True
    def owners(self):return [('main',{'approvals':self.p.approvals})]+[(j['id'],j) for j in self.jobs]
    def request_items(self,owner,req):
        ident=owner+':'+str(req['id']);params=req.get('params') or {};method=req.get('method','')
        detail=request_description(req)
        pages=chunks(detail) if len(detail)<=1200 else ['此请求较长，请到管理网页查看完整内容；设备只提供拒绝。']
        result=[{'key':ident+':detail:'+str(i),'kind':'review','title':'需要你决定 · '+str(i+1)+'/'+str(len(pages)),
                 'body':body,'request':ident,'page':i,'pages':len(pages),'owner':owner} for i,body in enumerate(pages)]
        if method=='item/tool/requestUserInput' and len(detail)<=1200:
            for q in params.get('questions',[]):
                if not isinstance(q,dict) or not isinstance(q.get('id'),str):continue
                result.append({'key':ident+':question:'+q['id'],'kind':'question','title':q.get('header') or '回答这个问题','body':q.get('question',''),'owner':owner,'request':ident,'requestId':req['id'],'question':q['id'],'pages':len(pages),'label':'按住说话回答 · 识别后双击提交'})
                for index,opt in enumerate(q.get('options') or []):
                    if not isinstance(opt,dict) or not isinstance(opt.get('label'),str):continue
                    result.append({'key':ident+':answer:'+q['id']+':'+str(index),'kind':'decision','title':q.get('header') or '选择答案',
                        'body':(q.get('question','')+'\n'+opt['label']+'\n'+str(opt.get('description','')))[:240],
                        'owner':owner,'request':ident,'requestId':req['id'],'question':q['id'],'answer':opt['label'],'label':'提交：'+opt['label'],'pages':len(pages)})
            if len(result)==len(pages):result.append({'key':ident+':console','kind':'notice','title':'需要文字回答','body':'请到管理网页填写答案。上下滑可继续查看其他任务。'})
        elif len(detail)<=1200 and ((method=='item/commandExecution/requestApproval' and params.get('command')) or (method=='item/fileChange/requestApproval' and (params.get('grantRoot') or params.get('cwd') or params.get('changes')))):
            result.append({'key':ident+':accept','kind':'decision','title':'批准这个操作','body':detail[:90],'owner':owner,'request':ident,'requestId':req['id'],'decision':'accept','label':'批准这次操作','pages':len(pages)})
        result.append({'key':ident+':decline','kind':'decision','title':'拒绝这个请求','body':'仅拒绝本次请求，不会批准其他操作。','owner':owner,'request':ident,'requestId':req['id'],'decision':'decline','label':'拒绝本次请求','pages':len(pages)})
        return result
    def items(self):
        p=self.p;items=[]
        if self.voice_draft:
            d=self.voice_draft;pages=chunks(d['answer']);ident='voice-answer:'+hashlib.sha256((str(d['requestId'])+d['question']+d['answer']).encode()).hexdigest()[:16]
            items += [{'key':ident+':'+str(i),'kind':'review','title':'识别的答案 · '+str(i+1)+'/'+str(len(pages)),'body':body,'request':ident,'page':i,'pages':len(pages)} for i,body in enumerate(pages)]
            items.append({**d,'key':ident+':confirm','kind':'decision','title':'提交刚才的答案','body':d['answer'][:48],'request':ident,'answerGroup':d['request'],'pages':len(pages),'label':'双击提交给当前问题'})
        for owner,state in self.owners():
            for req in state.get('approvals',[]):items+=self.request_items(owner,req)
        if p.draft:items += [{'key':'draft:send:'+hashlib.sha256(p.draft.encode()).hexdigest()[:12],'kind':'draft','title':'发送语音文字','body':p.draft[:240],'action':'send_draft','label':'发送这段文字'}, {'key':'draft:discard','kind':'draft','title':'丢弃语音草稿','body':'不发送这段文字。','action':'discard','label':'丢弃草稿'}]
        if p.turn:items.append({'key':'main:running','kind':'status','title':'当前语音任务','body':p.detail[:240],'label':'执行中 · 上下滑查看其他卡片'})
        for j in self.jobs[::-1]:
            pages=[(j.get('detail') or j.get('reply') or '等待执行')[-90:]]
            for i,body in enumerate(pages):items.append({'key':j['id']+':status:'+str(i),'kind':'job','job':j['id'],'state':j['state'],'title':j['title'], 'body':j['topic'][:24]+' · '+body,'label':{'queued':'已排队 · 双击取消','running':'执行中','awaiting_approval':'等待你决定','completed':'本轮结束 · 完整结果见管理页','failed':'执行失败','paused':'双击重新排队','delivery_unknown':'双击只读核对执行状态','submitting':'正在提交','stopped':'已停止','cancelled':'已取消排队'}.get(j['state'],j['state']), 'page':i,'pages':len(pages)})
            if j.get('turn') and j['state'] in ('running','awaiting_approval'):
                items.append({'key':j['id']+':stop:'+j['turn'],'kind':'stop','job':j['id'],'turn':j['turn'],'title':'停止这个任务','body':j['title'],'label':'双击请求停止'})
        goal=p.goal_flow.active
        if goal and not p.turn and not any(j.get('goalId')==goal['id'] and j['state'] in LIVE|{'queued','paused'} for j in self.jobs) and any(j.get('goalId')==goal['id'] and j['strategy']=='goal_accept' and j['state']=='completed' for j in self.jobs):
            items.append({'key':'goal-close:'+goal['id']+':'+str(goal['revision']),'kind':'goal_close','goalId':goal['id'],'title':'由你确认目标收尾','body':goal['objective'][:48],'label':'双击表示你已验收并保留成果'})
        excluded={j['cardId'] for j in self.jobs}
        seed=(tuple(p.recommendations.topics),tuple(c.id for c in (p.goal_flow.pair() or ())),tuple((j['id'],j['state']) for j in self.jobs))
        if self.seed!=seed or not self.recs:self.recs=self.ranker.ranked(p,excluded,self.jobs);self.seed=seed
        items += [{'key':'rec:'+c.id,'kind':'recommendation','title':choice_label(c),'body':c.topic,'subtitle':c.source,'card':c,'label':'双击加入任务'} for c in self.recs if c.id not in excluded]
        if not items:items=[{'key':'empty','kind':'notice','title':'任务流已浏览完','body':'按住说话，告诉我下一个目标；新的推荐会关联这个目标。'}]
        return items
    def current(self):
        items=self.items();index=next((i for i,c in enumerate(items) if c['key']==self.key),0)
        item=items[index];signature=(item['key'],item.get('state'),item.get('turn'),item.get('action'))
        if signature!=self.signature:self.key=item['key'];self.signature=signature;self.token=secrets.token_hex(8);self.visible=None
        return items[index],index,len(items)
    def acknowledge(self,source,token,now=None):
        now=time.monotonic() if now is None else now
        if token!=self.token:return
        item,_,_=self.current()
        if token!=self.token:return
        if self.visible and self.visible['token']==token and self.visible['source']==source:
            gap=now-self.visible['last']
            if 0<=gap<=5:self.visible['seconds']+=gap
            self.visible['last']=now
        else:self.visible={'token':token,'source':source,'last':now,'seconds':0,'item':item}
    def leave(self):
        if self.visible:
            row=self.visible;item=row['item'];seconds=row['seconds']
            gap=time.monotonic()-row['last']
            if 0<=gap<=3:seconds+=gap
            if item['kind']=='recommendation':self.ranker.learn(item['card'],seconds)
            elif item['kind']=='review' and seconds>=.4:
                if len(self.reviewed)>=4096:self.reviewed.clear()
                self.reviewed.add((item['request'],item['page']))
        self.visible=None
    async def action(self,action,payload):
        if payload.get('token')!=self.token:return
        item,index,total=self.current()
        if payload.get('token')!=self.token:return
        if action in ('stream_next','stream_prev'):
            self.leave();items=self.items();index=next((i for i,c in enumerate(items) if c['key']==item['key']),0)
            target=index+(1 if action=='stream_next' else -1)
            if target>=len(items):self.recs=self.ranker.ranked(self.p,{j['cardId'] for j in self.jobs},self.jobs);items=self.items();target=0
            self.key=items[max(0,target)]['key'];self.token=secrets.token_hex(8);self.notice='';self.dirty();return
        if action!='stream_confirm':return
        if item['kind']=='recommendation':
            card=item['card'];await self.enqueue(card);self.ranker.learn(card,self.visible['seconds'] if self.visible else 0,chosen=True)
            self.key=self.jobs[-1]['id']+':status:0';self.token=secrets.token_hex(8);self.visible=None
        elif item['kind']=='job' and item['state']=='paused':
            j=next(j for j in self.jobs if j['id']==item['job']);j['state']='queued';self.save();self.kick()
        elif item['kind']=='job' and item['state']=='queued':
            j=next(j for j in self.jobs if j['id']==item['job']);j['state']='cancelled';j['detail']='用户取消了排队任务';self.save()
        elif item['kind']=='job' and item['state']=='delivery_unknown':
            j=next(j for j in self.jobs if j['id']==item['job'])
            if not j.get('thread'):j['state']='failed';j['detail']='未提交执行，可选择新的推荐';self.save()
            else:
                result=await self.p.rpc.call('thread/read',{'threadId':j['thread'],'includeTurns':True})
                turns=result.get('thread',{}).get('turns') or []
                if turns:
                    turn=turns[-1];state=turn.get('status')
                    if state=='inProgress':j['state']='running';j['turn']=turn['id'];j['detail']='已核对：任务仍在运行'
                    elif state in ('completed','failed','interrupted'):
                        j['state']={'completed':'completed','failed':'failed','interrupted':'stopped'}[state];j['turn']=turn['id'];j['detail']='已只读核对执行状态，未重新提交任务'
                    self.save()
                else:self.notice='执行状态仍未知，不会自动重试'
        elif item['kind']=='goal_close':
            if self.p.goal_flow.active and self.p.goal_flow.active['id']==item['goalId']:
                self.p.goal_flow.archive();self.seed=None;self.notice='目标已由你确认收尾';self.p.refresh_goal_followup()
        elif item['kind']=='stop':
            j=next(j for j in self.jobs if j['id']==item['job'])
            if j.get('turn')==item['turn'] and j['state'] in ('running','awaiting_approval'):
                await self.p.rpc.call('turn/interrupt',{'threadId':j['thread'],'turnId':j['turn']});self.notice='已请求停止，等待确认'
        elif item['kind']=='decision':await self.decide(item)
        elif item['kind']=='draft':await self.p.execute(item['action'],{})
        self.dirty()
    async def enqueue(self,card):
        if any(j['cardId']==card.id for j in self.jobs):return
        if sum(j['state'] in LIVE|{'queued','paused'} for j in self.jobs)>=12:raise ValueError('任务队列已满，请先完成已有任务')
        while len(self.jobs)>=40:
            old=next((j for j in self.jobs if j['state'] not in LIVE|{'queued','paused'}),None)
            if old is None:raise ValueError('任务记录已满，请先处理已有任务')
            self.jobs.remove(old)
        j={'id':secrets.token_hex(8),'cardId':card.id,'title':choice_label(card),'topic':card.topic,'strategy':card.strategy,'prompt':card.prompt,
           'goalId':self.p.goal_flow.active['id'] if self.p.goal_flow.active and card.topic==self.p.goal_flow.active['objective'][:48] else None,
           'readonly':card.strategy in READ_ONLY,'state':'queued','reply':'','detail':'已确认，等待执行槽位','thread':None,'turn':None,'approvals':[],'created':time.time()}
        self.jobs.append(j)
        try:self.save()
        except OSError:self.jobs.remove(j);raise ValueError('队列保存失败，任务没有提交')
        self.kick()
    def kick(self):
        if self.scheduler is None or self.scheduler.done():self.scheduler=asyncio.create_task(self.pump())
    async def pump(self):
        async with self.lock:
            if self.p.turn or self.p.approvals or self.p.voice_busy:return
            for j in self.jobs:
                if j['state']!='queued':continue
                active=[a for a in self.jobs if a['state'] in LIVE]
                if len(active)>=2 or any(not a['readonly'] for a in active) or any(a['state']=='awaiting_approval' for a in active) or (active and not j['readonly']):return
                j['state']='submitting';j['detail']='正在创建独立会话';self.save();self.dirty()
                try:
                    cwd=self.p.config['cwd']
                    instructions=DEVELOPER_INSTRUCTIONS
                    if j['readonly']:
                        cwd=str(self.p.runtime/'task-workspaces'/j['id']);Path(cwd).mkdir(parents=True,exist_ok=True)
                        instructions+='\n此任务只做只读分析。原项目路径：'+self.p.config['cwd']+'。不得修改原项目、发布或部署；临时笔记仅写当前独立工作目录。'
                    thread=await self.p.rpc.call('thread/start',{'cwd':cwd,'approvalPolicy':'on-request','sandbox':'workspace-write','developerInstructions':instructions})
                    ident=thread['thread']['id']
                    if not isinstance(ident,str) or not ident:raise ValueError('invalid worker thread')
                    j['thread']=ident;self.save()
                    params={'threadId':j['thread'],'input':[{'type':'text','text':j['prompt']}]}
                    if os.environ.get('POCKET_EXTERNAL_SANDBOX')=='docker':params['sandboxPolicy']={'type':'externalSandbox','networkAccess':'enabled'}
                    result=await self.p.rpc.call('turn/start',params)
                    if j['state']=='submitting':j['turn']=result['turn']['id'];j['state']='running';j['detail']='已开始执行'
                except Exception:
                    j['state']='delivery_unknown';j['detail']='未确认送达；不会自动重试，请到管理网页检查。'
                self.save();self.dirty()
    async def notification(self,msg):
        p=msg.get('params') or {};j=next((j for j in self.jobs if j.get('thread') and j['thread']==p.get('threadId')),None)
        if j is None:
            if msg.get('method')=='pocket/disconnected':
                for pending in self.jobs:
                    if pending['state'] in LIVE:pending['state']='delivery_unknown';pending['detail']='执行服务断开；不会自动重放任务'
                self.save();self.dirty()
            return False
        method=msg.get('method','')
        if j['state'] in ('completed','failed','stopped','cancelled'):
            if 'id' in msg:await self.p.rpc.send({'id':msg['id'],'error':{'code':-32600,'message':'Task has ended'}})
            return True
        if p.get('turnId') and j.get('turn') and p['turnId']!=j['turn']:return True
        if 'id' in msg:
            if method in ('item/commandExecution/requestApproval','item/fileChange/requestApproval','item/tool/requestUserInput'):
                if not any(r['id']==msg['id'] for r in j['approvals']):j['approvals'].append(msg)
                j['state']='awaiting_approval';j['detail']='需要你确认，请上下滑找到决策卡'
            else:await self.p.rpc.send({'id':msg['id'],'error':{'code':-32601,'message':'Unsupported by task stream'}})
        elif method=='turn/started':
            if j['state'] not in ('completed','failed','stopped'):j['turn']=p['turn']['id'];j['state']='running'
        elif method=='item/started':
            item=p.get('item') or {};j['detail']={'commandExecution':'正在执行检查','fileChange':'正在修改文件','webSearch':'正在搜索资料','reasoning':'正在分析','agentMessage':'正在整理回复'}.get(item.get('type'),'正在处理任务')
        elif method=='item/agentMessage/delta':
            j['reply']=(j['reply']+p.get('delta',''))[-12000:];j['detail']=j['reply'][-90:];self.dirty();return True
        elif method=='item/completed':
            if p.get('item',{}).get('type')=='agentMessage':j['reply']=p['item'].get('text',j['reply'])[-12000:]
        elif method=='turn/completed':
            turn=p.get('turn') or {}
            if j['state'] in ('completed','failed','stopped') or (j.get('turn') and j['turn']!=turn.get('id')):return True
            j['turn']=turn.get('id');j['state']={'completed':'completed','interrupted':'stopped','failed':'failed'}.get(turn.get('status'),'delivery_unknown')
            j['detail']=(turn.get('error') or {}).get('message') or j['reply'] or '本轮结束，尚未独立验收';j['approvals']=[]
            if self.p.goal_flow.active and j.get('goalId')==self.p.goal_flow.active['id']:
                action='goal_'+j['strategy'] if j['strategy'] in ('verify','advance') else j['strategy']
                self.p.goal_flow.finish(turn.get('status','unknown'),j['reply'],(turn.get('error') or {}).get('message',''),j['turn'],action=action);self.p.goal_followup_ready=True
            self.ranker.learn(Card(j['cardId'],j['topic'],j['title'],j['prompt'],j['strategy'],'任务流'),0,outcome=j['state']);self.kick()
        elif method=='serverRequest/resolved':
            j['approvals']=[a for a in j['approvals'] if a['id']!=p.get('requestId')]
            if not j['approvals'] and j['state']=='awaiting_approval':j['state']='running';j['detail']='请求已处理，继续执行'
        self.save();self.dirty();return True
    async def decide(self,item):
        owner=next((v for k,v in self.owners() if k==item['owner']),None)
        if owner is None:return
        req=next((r for r in owner['approvals'] if r['id']==item['requestId']),None)
        if req is None:return
        if item.get('decision')!='decline' and any((item['request'],i) not in self.reviewed for i in range(item['pages'])):
            self.notice='请先上下滑读完请求详情';return
        if item.get('answerGroup') and any((item['answerGroup'],i) not in self.reviewed for i in range(item.get('contextPages',0))):self.notice='请先读完原问题详情';return
        if item['owner']!='main' and item.get('decision')=='accept':
            if any(j['id']!=owner['id'] and j['state'] in LIVE for j in self.jobs):self.notice='请等其他任务结束，再批准这次操作';return
            owner['readonly']=False;self.save()
        if 'answer' in item:
            answers=self.answers.setdefault(item.get('answerGroup',item['request']),{});answers[item['question']]={'answers':[item['answer']]}
            questions=(req.get('params') or {}).get('questions') or []
            if not all(q.get('id') in answers for q in questions):self.voice_draft=None;self.notice='答案已选，请继续选择其余问题';return
            result={'answers':answers}
            self.voice_draft=None
        else:result={'decision':item['decision']}
        if req['method']=='item/tool/requestUserInput' and item.get('decision')=='decline':
            thread=self.p.thread if item['owner']=='main' else owner.get('thread');turn=self.p.turn if item['owner']=='main' else owner.get('turn')
            if turn:await self.p.rpc.call('turn/interrupt',{'threadId':thread,'turnId':turn})
            return
        if item['owner']=='main':
            await self.p.decide(item.get('decision','accept'),result.get('answers'));self.token=secrets.token_hex(8);self.visible=None;return
        await self.p.rpc.send({'id':req['id'],'result':result})
        owner['approvals'].remove(req)
        if item['owner']!='main':owner['state']='awaiting_approval' if owner['approvals'] else 'running';self.save()
        self.token=secrets.token_hex(8);self.visible=None;self.notice='已提交你的选择'
    def capture_voice_target(self):
        item,_,_=self.current()
        return {k:item[k] for k in ('owner','request','requestId','question','pages')} if item['kind']=='question' else None
    def voice_answer(self,target,text):
        owner=next((v for k,v in self.owners() if k==target['owner']),None)
        req=next((r for r in (owner or {}).get('approvals',[]) if r['id']==target['requestId']),None)
        if req is None or not any(q.get('id')==target['question'] for q in (req.get('params') or {}).get('questions',[])):raise ValueError('原问题已结束，这段语音没有作为新任务发送')
        self.voice_draft={**target,'contextPages':target['pages'],'answer':text};self.key=None;self.signature=None;self.notice='先读识别结果，再双击提交';self.dirty()
    async def console_decide(self,msg):
        owner=next((v for k,v in self.owners() if k==msg.get('owner')),None)
        req=next((q for q in (owner or {}).get('approvals',[]) if q['id']==msg.get('requestId')),None)
        if req is None:raise ValueError('此请求已结束或不属于该任务')
        decision=msg.get('decision')
        if decision not in ('accept','decline'):raise ValueError('无效决定')
        if msg['owner']!='main' and decision=='accept' and req['method']!='item/tool/requestUserInput':
            if any(j['id']!=owner['id'] and j['state'] in LIVE for j in self.jobs):raise ValueError('请等其他任务结束，再批准这次操作')
            owner['readonly']=False;self.save()
        if req['method']=='item/tool/requestUserInput':
            if decision=='decline':
                thread=self.p.thread if msg['owner']=='main' else owner.get('thread');turn=self.p.turn if msg['owner']=='main' else owner.get('turn')
                if turn:await self.p.rpc.call('turn/interrupt',{'threadId':thread,'turnId':turn})
                return
            answers=msg.get('answers');questions=(req.get('params') or {}).get('questions') or []
            if not isinstance(answers,dict) or any(not isinstance(answers.get(q['id']),dict) or not isinstance(answers[q['id']].get('answers'),list) or not answers[q['id']]['answers'] or any(not isinstance(a,str) or not a.strip() or len(a)>4000 for a in answers[q['id']]['answers']) for q in questions):raise ValueError('请回答全部问题')
            result={'answers':answers}
        else:result={'decision':decision}
        if msg['owner']=='main':await self.p.decide(decision,result.get('answers'));self.dirty();return
        await self.p.rpc.send({'id':req['id'],'result':result});owner['approvals'].remove(req)
        if msg['owner']!='main':owner['state']='awaiting_approval' if owner['approvals'] else 'running';self.save()
        self.dirty()
    def snapshot(self):return {'limit':2,'jobs':[{k:v for k,v in j.items() if k!='prompt'} for j in self.jobs],'feedbackEvents':len(self.ranker.data['events'])}
