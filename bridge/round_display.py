"""Negotiated, bounded 360x360 layout. Legacy devices keep 200x200 frames."""
import base64
from PIL import Image,ImageDraw,ImageChops
from datetime import datetime
from zoneinfo import ZoneInfo
from recommendations import choice_label,paginate

PROFILE='taiji360'

def task_pages(p,font):
    body=p.draft or p.detail
    if p.page==1:
        body=' '.join(f'{q["label"]}剩余{q["remaining"]:g}%' for q in p.remaining()[:2])+'\n'+body
    return paginate(body,lambda t:font(18).getlength(t),width=232,rows=4)

def pack_bitmap(data):
    out=bytearray();i=0
    while i<len(data):
        end=i+1
        while end<len(data) and data[end]==data[i] and end-i<128:end+=1
        if end-i>=3:out.extend((128+end-i-1,data[i]));i=end;continue
        start=i;i=end
        while i<len(data) and i-start<128:
            end=i+1
            while end<len(data) and data[end]==data[i] and end-i<3:end+=1
            if end-i>=3:break
            i+=min(end-i,128-(i-start))
        out.append(i-start-1);out.extend(data[start:i])
    return bytes(out)

def render_round(p,font,stream=False):
    im=Image.new('1',(360,360),0);d=ImageDraw.Draw(im)
    def fit(value,size,width):
        value=str(value)
        if font(size).getlength(value)<=width:return value
        while value and font(size).getlength(value+'…')>width:value=value[:-1]
        return value+'…'
    def text(x,y,value,size=18,width=260):d.text((x,y),fit(value,size,width),font=font(size),fill=1)
    def center(y,value,size=18,width=260):
        value=fit(value,size,width);d.text(((360-font(size).getlength(value))/2,y),value,font=font(size),fill=1)
    # Code-native Codex terminal mark; the device supplies the colored tile.
    d.line([(109,30),(115,36),(109,42)],fill=1,width=2)
    d.line([(119,42),(126,42)],fill=1,width=2)
    text(140,27,'CODEX',20,112)
    battery=p.device.get('battery')
    if isinstance(battery,(int,float)) and battery>=0:text(244,34,f'{battery}%',11,42)
    location=p.current_location();weather=p.current_weather()
    try:clock=datetime.now(ZoneInfo(location['timezone'])).strftime('%H:%M') if location else '--:--'
    except (KeyError,ValueError):clock='--:--'
    center(52,clock+' · '+(location['city'] if location else '定位中')+(f' {weather["temperature"]:.0f}°C' if weather else ''),13)
    reading=p.recommendation_reading();idle=p.is_idle_dashboard();view=2 if reading and p.recommendation_expanded() else 1 if reading else 0 if idle else 3
    stream_meta={}
    if stream:
        item,index,total=p.stream.current();view=4
        kinds={'recommendation':'为你推荐','decision':'需要你决定','review':'先读请求详情','job':'任务进度','draft':'语音草稿','notice':'任务流','status':'执行中','question':'请回答这个问题','stop':'任务操作'}
        pending=sum(len(state.get('approvals',[])) for _,state in p.stream.owners())
        header=('待决 '+str(pending)+' 项' if pending else kinds.get(item['kind'],'任务流'))
        center(77,header+' · '+str(index+1)+'/'+str(total),14)
        center(106,item['title'],23,248)
        body_pages=paginate(item.get('body',''),lambda t:font(16).getlength(t),width=240,rows=4)
        for row,line in enumerate(body_pages[0][:4]):text(60,145+row*21,line,16,240)
        center(235,p.stream.notice or item.get('label') or '上下滑继续',13,238)
        center(253,'上下滑浏览 · 双击确认',11)
        selection={'recommendation':0,'decision':1,'review':1,'job':2,'status':2}.get(item['kind'],3)
        stream_meta={'streamToken':p.stream.token,'roundSelection':selection}
    elif reading:
        reader=p.recommendation_reader;card=p.recommendation_cards[reader['card']]
        if p.recommendation_expanded():
            center(78,'目标档案' if reader['context'] else '详细说明',18)
            pages=p.recommendation_pages();index=min(reader['page'],len(pages)-1)
            # Existing shared pages use 184px at 13pt. 252px at 17pt retains
            # every glyph while staying inside the circular safe area.
            for row,line in enumerate(pages[index]):text(54,112+row*27,line,17,252)
            center(252,f'{index+1} / {len(pages)} · 旋转翻页',13)
        else:
            center(80,'你的选择 · '+('AB'[reader['card']]),16)
            center(112,choice_label(card),25)
            center(155,card.topic,16)
            selected=p.menu_items()[p.menu%len(p.menu_items())][0]
            d.rounded_rectangle((78,185,282,215),15,outline=1)
            center(187,selected,17,190)
            center(232,'了解详情',15)
    elif idle:
        center(80,p.recommendation_cards[p.recommendation_index].topic,17,246)
        for i,card in enumerate(p.recommendation_cards):
            y=114+i*72;selected=i==p.recommendation_index
            d.rounded_rectangle((54,y,306,y+60),16,outline=1,width=3 if selected else 1)
            text(70,y+10,'AB'[i],28,26)
            text(103,y+16,choice_label(card),22,191)
        center(251,'转动选 A/B · 左滑换一组',12)
    else:
        center(80,p.status,22)
        pages=task_pages(p,font);index=min(p.round_detail_page,len(pages)-1)
        for row,line in enumerate(pages[index]):text(64,110+row*26,line,18,232)
        center(220,f'{index+1}/{len(pages)} · 旋转翻页',12)
        center(246,p.menu_items()[p.menu%len(p.menu_items())][0],16)
    text(61,289,str(sum(j['state'] in ('running','awaiting_approval') for j in p.stream.jobs))+'运行' if view==4 else '选项' if view==3 else '换一组' if view==0 else '返回',12,42)
    text(267,289,'设置',12,38)
    # Spacious 144 x 50 voice target matches the native touch hit region.
    d.rounded_rectangle((176,276,184,289),4,outline=1,width=2)
    d.arc((172,281,188,293),0,180,fill=1,width=2)
    d.line([(180,293),(180,296)],fill=1,width=2)
    center(299,'按住说话',14,128)
    # No content beyond the circular bezel safe area.
    mask=Image.new('1',im.size,0);ImageDraw.Draw(mask).ellipse((7,7,353,353),fill=1)
    im=ImageChops.logical_and(im,mask)
    return im,{'roundView':view,'roundSelection':p.recommendation_index,'roundExpanded':bool(reading and p.recommendation_expanded()),'roundBusy':bool(p.turn or p.draft or p.approvals or p.voice_busy),'roundCanRecord':not bool(p.draft or p.approvals or p.voice_busy),**stream_meta}

def round_packet(p,packet,font,stream=False):
    image,metadata=render_round(p,font,stream=stream);packed=pack_bitmap(image.tobytes())
    result=dict(packet,**metadata)
    if len(packed)<=9600:
        result.pop('data',None);result['roundRle']=base64.b64encode(packed).decode()
    return result
