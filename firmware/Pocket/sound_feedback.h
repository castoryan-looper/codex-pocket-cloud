#pragma once
#include <math.h>
// The audio task alone writes speaker PCM and reads microphone PCM.
// No playback in an ISR, BLE callback, network task or during capture.
enum SoundCue:uint8_t {SoundConfirm=1,SoundConnected,SoundRecordStart,SoundRecordEnd,SoundCancel,SoundError,SoundDone};
static bool soundEnabled=true;
static unsigned soundVolume=35;
static QueueHandle_t soundQueue=nullptr;
static volatile uint32_t soundPlayed=0,soundFailures=0;
struct SoundRequest {uint8_t cue;uint32_t at;};
static portMUX_TYPE soundLock=portMUX_INITIALIZER_UNLOCKED;
static void soundEnqueue(uint8_t cue){
 if(!soundEnabled||!soundVolume||!soundQueue||recording||captureRequested||cue<1||cue>7)return;
 static uint32_t last[8]={};static bool seen[8]={};uint32_t now=millis();
 portENTER_CRITICAL(&soundLock);bool allowed=!seen[cue]||now-last[cue]>=(cue==SoundConfirm?150UL:cue==SoundConnected?5000UL:1000UL);if(allowed){seen[cue]=true;last[cue]=now;}portEXIT_CRITICAL(&soundLock);
 if(allowed){SoundRequest request={cue,now};xQueueSend(soundQueue,&request,0);}
}
static bool soundTone(unsigned hz,unsigned duration){
 int16_t pcm[128];const unsigned frames=duration*16;unsigned position=0;
 while(position<frames){
  if(!soundEnabled||!soundVolume||recording)return false;
  unsigned count=min(64U,frames-position);
  for(unsigned i=0;i<count;i++){
   unsigned n=position+i;float envelope=min(1.0f,min(n/80.0f,(frames-1-n)/80.0f));
   int16_t sample=hz?(int16_t)(6000.0f*(soundVolume/100.0f)*envelope*sinf(6.2831853f*hz*n/16000.0f)):0;
   pcm[i*2]=sample;pcm[i*2+1]=sample;
  }
  if(Codec_PlaybackData((uint8_t*)pcm,count*4)!=ESP_OK){soundFailures++;return false;}position+=count;
 }
 return true;
}
static void soundPlay(uint8_t cue){
 if(!soundEnabled||!soundVolume||recording)return;
 bool ok=false;
 switch(cue){
  case SoundConfirm:ok=soundTone(1000,25);break;
  case SoundConnected:ok=soundTone(740,45)&&soundTone(0,25)&&soundTone(1040,45);break;
  case SoundRecordStart:ok=soundTone(880,45);break;
  case SoundRecordEnd:ok=soundTone(660,45);break;
  case SoundCancel:ok=soundTone(480,55);break;
  case SoundError:ok=soundTone(360,60)&&soundTone(0,30)&&soundTone(300,60);break;
  case SoundDone:ok=soundTone(660,55)&&soundTone(0,25)&&soundTone(880,75);break;
 }
 // Drain playback before capture can begin; the tail is explicitly silent.
 if(ok){soundTone(0,20);soundPlayed++;}
}
static void soundIdleTick(){
 if(!soundQueue||recording||captureRequested)return;
 SoundRequest request;while(xQueueReceive(soundQueue,&request,0)==pdTRUE){if(millis()-request.at<1200){soundPlay(request.cue);break;}}
}
static void soundStatus(JsonDocument &d){d["soundEnabled"]=soundEnabled;d["soundVolume"]=soundVolume;d["soundPlayed"]=(uint32_t)soundPlayed;d["soundFailures"]=(uint32_t)soundFailures;}
static int soundTest(JsonDocument &out){
 if(recording||captureRequested||uploadPending){out["error"]="请先结束录音和上传";return 409;}
 if(!soundEnabled||!soundVolume){out["error"]="请先开启提示音、设置音量并保存重启";return 409;}
 if(!soundQueue){out["error"]="提示音初始化失败";return 503;}
 SoundRequest request={SoundConnected,millis()};if(xQueueSend(soundQueue,&request,0)!=pdTRUE){out["error"]="提示音队列忙，请稍后重试";return 409;}out["message"]="已请求试听当前保存的音量";return 200;
}
static void soundObserve(){
 static bool wasCloud=false,wasBluetooth=false;static uint32_t cloudLostAt=0,bluetoothLostAt=0;
 bool cloud=netConnected&&online,bluetooth=knobConnected;
 if((cloud&&!wasCloud)||(bluetooth&&!wasBluetooth))soundEnqueue(SoundConnected);
 if(wasCloud&&!cloud)cloudLostAt=millis();if(wasBluetooth&&!bluetooth)bluetoothLostAt=millis();
 if(cloud)cloudLostAt=0;else if(cloudLostAt&&millis()-cloudLostAt>5000){soundEnqueue(SoundError);cloudLostAt=0;}
 if(bluetooth)bluetoothLostAt=0;else if(bluetoothLostAt&&millis()-bluetoothLostAt>5000){soundEnqueue(SoundCancel);bluetoothLostAt=0;}
 wasCloud=cloud;wasBluetooth=bluetooth;
}
static void soundRemoteState(const String &state){
 static String previous;
 if(state==previous)return;
 bool active=previous=="工作中"||previous=="正在提交"||previous=="正在停止"||previous=="待确认";
 if(state=="已完成"&&active)soundEnqueue(SoundDone);
 else if((state=="提交失败"||state=="语音失败"||state=="录音失败")&&previous.length())soundEnqueue(SoundError);
 previous=state;
}
