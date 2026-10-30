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

def render_round(p,font):
    im=Image.new('1',(360,360),0);d=ImageDraw.Draw(im)
    def fit(value,size,width):
        value=str(value)
        if font(size).getlength(value)<=width:return value
        while value and font(size).getlength(value+'…')>width:value=value[:-1]
        return value+'…'
    def text(x,y,value,size=18,width=260):d.text((x,y),fit(value,size,width),font=font(size),fill=1)
    def center(y,value,size=18,width=260):
        value=fit(value,size,width);d.text(((360-font(size).getlength(value))/2,y),value,font=font(size),fill=1)
    battery=p.device.get('battery')
    center(28,'CODEX POCKET'+(f' · {battery}%' if isinstance(battery,(int,float)) and battery>=0 else ''),14)
    location=p.current_location();weather=p.current_weather()
    try:clock=datetime.now(ZoneInfo(location['timezone'])).strftime('%H:%M') if location else '--:--'
    except (KeyError,ValueError):clock='--:--'
    center(52,clock+' · '+(location['city'] if location else '定位中')+(f' {weather["temperature"]:.0f}°C' if weather else ''),13)
    reading=p.recommendation_reading();idle=p.is_idle_dashboard();view=2 if reading and p.recommendation_expanded() else 1 if reading else 0 if idle else 3
    if reading:
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
        center(80,'目标：'+p.recommendation_cards[p.recommendation_index].topic,16)
        for i,card in enumerate(p.recommendation_cards):
            y=114+i*72;selected=i==p.recommendation_index
            d.rounded_rectangle((54,y,306,y+60),16,outline=1,width=3 if selected else 1)
            text(70,y+18,'AB'[i],18,22)
            text(103,y+16,choice_label(card),22,191)
        center(258,'旋转选择 · 轻触确认',12)
    else:
        center(80,p.status,22)
        pages=task_pages(p,font);index=min(p.round_detail_page,len(pages)-1)
        for row,line in enumerate(pages[index]):text(64,110+row*26,line,18,232)
        center(220,f'{index+1}/{len(pages)} · 旋转翻页',12)
        center(246,p.menu_items()[p.menu%len(p.menu_items())][0],16)
    text(62,285,'选项' if view==3 else '返回',14,48);center(285,'按住说话',15,110);text(270,285,'设置',14,42)
    # No content beyond the circular bezel safe area.
    mask=Image.new('1',im.size,0);ImageDraw.Draw(mask).ellipse((7,7,353,353),fill=1)
    im=ImageChops.logical_and(im,mask)
    return im,{'roundView':view,'roundSelection':p.recommendation_index,'roundExpanded':bool(reading and p.recommendation_expanded()),'roundBusy':bool(p.turn or p.draft or p.approvals or p.voice_busy),'roundCanRecord':not bool(p.draft or p.approvals or p.voice_busy)}

def round_packet(p,packet,font):
    image,metadata=render_round(p,font);packed=pack_bitmap(image.tobytes())
    result=dict(packet,**metadata)
    if len(packed)<=9600:
        result.pop('data',None);result['roundRle']=base64.b64encode(packed).decode()
    return result
