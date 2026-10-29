"""Exercise the actual firmware WebSocket sender with bounded transport fakes."""
from pathlib import Path
import shutil,subprocess,tempfile,unittest
class UploadFrameTests(unittest.TestCase):
 def test_masked_frames_small_allocation_and_partial_write(self):
  compiler=shutil.which('cl') or shutil.which('g++') or shutil.which('clang++')
  if not compiler:self.skipTest('host C++ compiler required')
  root=Path(__file__).resolve().parents[1]
  source=(root/'firmware/Pocket/Pocket.ino').read_text(encoding='utf-8')
  method=source[source.index('  const char *lastSendFailure='):source.index('  void logTransport')]
  code=r'''
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>
#include <algorithm>
#include <cassert>
using String=std::string;using std::min;
const int WSop_text=1;
void esp_fill_random(uint8_t *out,unsigned n){for(unsigned i=0;i<n;i++)out[i]=i+1;}
unsigned allocations=0,releases=0;bool failAllocation=false;
void *trackedMalloc(size_t n){allocations++;return failAllocation?nullptr:std::malloc(n);}
void trackedFree(void *p){releases++;std::free(p);}
struct Base{
 int _client=0;std::vector<uint8_t> wire;unsigned writes=0;int failAt=-1;
 uint8_t createHeader(uint8_t *out,int opcode,size_t size,bool mask,uint8_t key[4],bool fin){assert(opcode==1&&mask&&fin);out[0]=0x81;unsigned h=2;if(size<126)out[1]=0x80|size;else{out[1]=0xfe;out[2]=size>>8;out[3]=size&255;h=4;}memcpy(out+h,key,4);return h+4;}
 size_t write(int*,uint8_t *data,size_t size){assert(size<=512);if((int)writes++==failAt)return 0;wire.insert(wire.end(),data,data+size);return size;}
};
#define malloc trackedMalloc
#define free trackedFree
class Sender:public Base{public:
'''+method+r'''
};
#undef malloc
#undef free
int main(){
 for(size_t length:{size_t(0),size_t(120),size_t(125),size_t(126),size_t(354),size_t(419),size_t(8192)}){
  Sender s;String text(length,'x');unsigned before=allocations;assert(s.sendPackedText(text));assert(s.wire[0]==0x81&&(s.wire[1]&0x80));unsigned header=length<126?6:8;unsigned decodedLength=length<126?s.wire[1]&127:(s.wire[2]<<8)|s.wire[3];assert(decodedLength==length&&s.wire.size()==header+length);
  for(unsigned i=0;i<length;i++)assert((s.wire[header+i]^s.wire[header-4+i%4])=='x');
  if(length<=419){assert(allocations==before&&s.writes==1);}else assert(allocations==before+1);
 }
 Sender broken;broken.failAt=1;assert(!broken.sendPackedText(String(8192,'x')));assert(broken.lastSendOffset==512);assert(std::string(broken.lastSendFailure)=="TLS SHORT WRITE");
 failAllocation=true;Sender noMemory;assert(!noMemory.sendPackedText(String(8192,'x')));assert(std::string(noMemory.lastSendFailure)=="TX NO MEMORY");
}
'''
  with tempfile.TemporaryDirectory() as directory:
   d=Path(directory);src=d/'test.cpp';src.write_text(code,encoding='utf-8');exe=d/'frames.exe'
   if Path(compiler).name.lower()=='cl.exe':args=[compiler,'/nologo','/utf-8','/EHsc',str(src),'/Fe:'+str(exe),'/Fo:'+str(d/'test.obj')]
   else:args=[compiler,'-std=c++17',str(src),'-o',str(exe)]
   subprocess.run(args,check=True,capture_output=True);subprocess.run([str(exe)],check=True,capture_output=True)
 def test_audio_json_fits_one_tls_write(self):
  import base64,json
  for raw_size in (240,288):
   text=json.dumps({'type':'audio_chunk','data':base64.b64encode(bytes(raw_size)).decode()},separators=(',',':'))
   self.assertLessEqual(len(text)+8,512)
if __name__=='__main__':unittest.main()
