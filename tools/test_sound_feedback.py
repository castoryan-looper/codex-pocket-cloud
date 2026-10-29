"""Run actual speaker feedback code against PCM/queue hardware fakes."""
from pathlib import Path
import shutil,subprocess,tempfile,unittest
class SoundFeedbackTests(unittest.TestCase):
 def test_capture_gating_stale_events_pcm_and_edges(self):
  compiler=shutil.which('cl') or shutil.which('g++') or shutil.which('clang++')
  if not compiler:self.skipTest('host C++ compiler required')
  header=(Path(__file__).resolve().parents[1]/'firmware/Pocket/sound_feedback.h').as_posix()
  source=r'''
#include <cstdint>
#include <cstring>
#include <cmath>
#include <algorithm>
#include <vector>
#include <deque>
#include <string>
#include <cassert>
using std::min;using String=std::string;
#define ESP_OK 0
#define pdTRUE 1
#define pdMS_TO_TICKS(x) (x)
#define portMUX_INITIALIZER_UNLOCKED 0
#define portENTER_CRITICAL(x) ((void)0)
#define portEXIT_CRITICAL(x) ((void)0)
using portMUX_TYPE=int;
struct Queue{unsigned size;std::deque<std::vector<uint8_t>> data;};using QueueHandle_t=Queue*;
QueueHandle_t xQueueCreate(unsigned,unsigned size){return new Queue{size,{}};}
int xQueueSend(QueueHandle_t q,void *data,int){if(q->data.size()==6)return 0;q->data.emplace_back((uint8_t*)data,(uint8_t*)data+q->size);return 1;}
int xQueueReceive(QueueHandle_t q,void *out,int){if(q->data.empty())return 0;memcpy(out,q->data.front().data(),q->size);q->data.pop_front();return 1;}
struct JsonDocument{struct Value{template<class T>void operator=(const T&) {}};Value operator[](const char*){return {};}};
bool recording=false,captureRequested=false,uploadPending=false,netConnected=false,online=false,knobConnected=false,failPlayback=false;
uint32_t now=100;uint32_t millis(){return now;}
std::vector<int16_t> samples;
int Codec_PlaybackData(uint8_t *data,unsigned bytes){if(failPlayback)return 1;auto pcm=(int16_t*)data;for(unsigned i=0;i<bytes/2;i+=2){assert(pcm[i]==pcm[i+1]);assert(std::abs((int)pcm[i])<=2100);samples.push_back(pcm[i]);}return 0;}
'''+ '#include "'+header+'"\n'+r'''
int main(){
 soundQueue=xQueueCreate(6,sizeof(SoundRequest));soundEnabled=false;soundEnqueue(SoundConfirm);assert(soundQueue->data.empty());soundEnabled=true;soundVolume=0;soundEnqueue(SoundConfirm);assert(soundQueue->data.empty());soundVolume=35;
 recording=true;soundEnqueue(SoundConfirm);assert(soundQueue->data.empty());recording=false;captureRequested=true;soundEnqueue(SoundConfirm);assert(soundQueue->data.empty());captureRequested=false;
 soundEnqueue(SoundConfirm);soundEnqueue(SoundConfirm);assert(soundQueue->data.size()==1);now+=151;soundEnqueue(SoundConfirm);assert(soundQueue->data.size()==2);
 now=10000;soundIdleTick();assert(soundQueue->data.empty()&&soundPlayed==0);
 soundEnqueue(SoundDone);soundIdleTick();assert(soundPlayed==1&&!samples.empty()&&samples.front()==0&&samples.back()==0);
 size_t count=samples.size();recording=true;soundPlay(SoundDone);assert(samples.size()==count);JsonDocument result;assert(soundTest(result)==409);recording=false;
 now+=2000;soundRemoteState("工作中");assert(soundQueue->data.empty());soundRemoteState("已完成");assert(soundQueue->data.size()==1);soundRemoteState("已完成");assert(soundQueue->data.size()==1);soundQueue->data.clear();
 now+=6000;online=true;netConnected=true;knobConnected=true;soundObserve();assert(soundQueue->data.size()==1);soundQueue->data.clear();soundObserve();assert(soundQueue->data.empty());netConnected=false;soundObserve();now+=4000;soundObserve();assert(soundQueue->data.empty());now+=1100;soundObserve();assert(soundQueue->data.size()==1);soundQueue->data.clear();soundObserve();assert(soundQueue->data.empty());
 now+=2000;failPlayback=true;soundEnqueue(SoundError);soundIdleTick();assert(soundFailures==1&&soundPlayed==1);
 soundEnabled=false;assert(soundTest(result)==409);
 delete soundQueue;
}
'''
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory);src=root/'test.cpp';src.write_text(source,encoding='utf-8');exe=root/'sound.exe'
   if Path(compiler).name.lower()=='cl.exe':args=[compiler,'/nologo','/utf-8','/EHsc',str(src),'/Fe:'+str(exe),'/Fo:'+str(root/'test.obj')]
   else:args=[compiler,'-std=c++17',str(src),'-o',str(exe)]
   subprocess.run(args,check=True,capture_output=True);subprocess.run([str(exe)],check=True,capture_output=True)
if __name__=='__main__':unittest.main()
