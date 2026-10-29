#pragma once
#include <stdint.h>
#include <stddef.h>
#include <string.h>
// Bounded HID report-map decoder. Accept only navigation/media controls;
// keyboard text, macros, mouse buttons and arbitrary vendor commands are ignored.
struct KnobHid {
 struct Field {uint16_t offset,page,usage;uint8_t id,bits,count;bool array,relative,signedValue;};
 Field fields[64];unsigned used=0;uint8_t previous[256]={};
 static uint8_t action(uint16_t page,uint16_t usage){
  if(page==12){if(usage==0xe9)return 1;if(usage==0xea)return 2;if(usage==0xe2||usage==0xcd)return 4;if(usage==0x70)return 8;if(usage==0x6f)return 16;}
  if(page==7){if(usage==0x4f||usage==0x51)return 1;if(usage==0x50||usage==0x52)return 2;if(usage==0x28)return 4;if(usage==0x29)return 8;}
  return 0;
 }
 bool parse(const uint8_t *data,size_t length){
  used=0;memset(previous,0,sizeof(previous));uint16_t offsets[256]={};
  struct Global {uint16_t page=0;uint8_t size=0,id=0;uint16_t count=0;bool sign=false;} g,stack[4];unsigned depth=0;
  uint16_t usages[64]={},minimum=0,maximum=0;unsigned n=0;bool range=false;
  for(size_t pos=0;pos<length;){
   uint8_t tag=data[pos++];if(tag==0xfe)return false;unsigned bytes=tag&3;if(bytes==3)bytes=4;if(pos+bytes>length)return false;
   uint32_t value=0;for(unsigned i=0;i<bytes;i++)value|=(uint32_t)data[pos++]<<(8*i);
   unsigned type=(tag>>2)&3,key=tag>>4;
   if(type==1){if(key==0)g.page=value;else if(key==1)g.sign=bytes && (value&(1UL<<(bytes*8-1)));else if(key==7){if(value>32)return false;g.size=value;}else if(key==8){if(!value||value>255)return false;g.id=value;}else if(key==9){if(value>128)return false;g.count=value;}else if(key==10){if(depth==4)return false;stack[depth++]=g;}else if(key==11){if(!depth)return false;g=stack[--depth];}}
   else if(type==2){if(key==0){if(n==64)return false;usages[n++]=value;}else if(key==1){minimum=value;range=true;}else if(key==2)maximum=value;}
   else if(type==0){
    if(key==8){if(!g.size||!g.count||g.size>32||g.count>128||offsets[g.id]+(uint32_t)g.size*g.count>2048)return false;
     if(!(value&1)){
      bool array=!(value&2);
      if(array&&(g.page==7||g.page==12)){if(used==64)return false;fields[used++]={offsets[g.id],g.page,0,g.id,g.size,(uint8_t)g.count,true,false,false};}
      else for(unsigned i=0;i<g.count;i++){
       uint16_t usage=i<n?usages[i]:range&&minimum+i<=maximum?minimum+i:0;
       if(action(g.page,usage)||(g.page==1&&usage==0x37&&(value&4))){if(used==64)return false;fields[used++]={(uint16_t)(offsets[g.id]+i*g.size),g.page,usage,g.id,g.size,1,false,(bool)(value&4),g.sign};}
      }
     }offsets[g.id]+=g.size*g.count;
    }n=0;range=false;
   }
  }return used>0;
 }
 uint8_t decode(uint8_t id,const uint8_t *data,size_t length){
  uint8_t held=0,pulse=0;
  for(unsigned i=0;i<used;i++){const Field &f=fields[i];if(f.id!=id)continue;
   for(unsigned k=0;k<f.count;k++){unsigned bit=f.offset+k*f.bits;if(bit+f.bits>length*8)continue;uint32_t value=0;for(unsigned b=0;b<f.bits;b++)if(data[(bit+b)/8]&(1<<((bit+b)%8)))value|=1UL<<b;
    if(f.page==1&&f.usage==0x37){if(value){bool negative=f.signedValue&&(value&(1UL<<(f.bits-1)));pulse|=negative?2:1;}continue;}
    uint8_t a=action(f.page,f.array?value:f.usage);if(value){if(f.relative)pulse|=a;else held|=a;}
   }
  }uint8_t edges=(held&~previous[id])|pulse;previous[id]=held;
  // Opposing rotation bits in a malformed report are never interpreted.
  if((edges&3)==3)edges&=~3;return edges;
 }
};
