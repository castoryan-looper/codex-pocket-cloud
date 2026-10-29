"""Exercise the firmware's actual bounded HID decoder with consumer/keyboard reports."""
from pathlib import Path
import shutil, subprocess, tempfile, unittest

class KnobHidTests(unittest.TestCase):
 def test_report_maps_edges_and_bounds(self):
  compiler=shutil.which('cl') or shutil.which('g++') or shutil.which('clang++')
  if not compiler:self.skipTest('host C++ compiler required')
  header=(Path(__file__).resolve().parents[1]/'firmware/Pocket/knob_hid.h').as_posix()
  source='#include "'+header+'"\n'+r'''
#include <cassert>
int main(){
 KnobHid hid;
 // Consumer bitfield: volume +, -, mute, brightness down/up. Report ID 3.
 const uint8_t map[]={0x05,0x0c,0x85,3,0x15,0,0x25,1,0x75,1,0x95,5,0x09,0xe9,0x09,0xea,0x09,0xe2,0x09,0x70,0x09,0x6f,0x81,2,0x75,3,0x95,1,0x81,1};
 assert(hid.parse(map,sizeof(map)));uint8_t report=1;
 assert(hid.decode(3,&report,1)==1);assert(hid.decode(3,&report,1)==0);
 report=0;assert(hid.decode(3,&report,1)==0);report=2;assert(hid.decode(3,&report,1)==2);
 report=4;assert(hid.decode(3,&report,1)==4);report=8;assert(hid.decode(3,&report,1)==8);report=16;assert(hid.decode(3,&report,1)==16);
 assert(hid.decode(4,&report,1)==0);assert(hid.decode(3,&report,0)==0);
 report=0;hid.decode(3,&report,1);report=3;assert(hid.decode(3,&report,1)==0);
 // Common 16-bit consumer usage array, zero release.
 const uint8_t array[]={0x05,0x0c,0x75,16,0x95,1,0x81,0};assert(hid.parse(array,sizeof(array)));
 uint8_t volume[]={0xe9,0};assert(hid.decode(0,volume,2)==1);assert(hid.decode(0,volume,2)==0);
 uint8_t keyboard[]={0x05,7,0x75,8,0x95,6,0x81,0};assert(hid.parse(keyboard,sizeof(keyboard)));
 uint8_t keys[]={0x4f,0,0,0,0,0};assert(hid.decode(0,keys,6)==1);keys[0]=0x28;assert(hid.decode(0,keys,6)==4);keys[0]=4;assert(hid.decode(0,keys,6)==0);
 // Relative signed Generic Desktop Dial; repeated nonzero values are pulses.
 const uint8_t dial[]={0x05,1,0x09,0x37,0x15,0x81,0x75,8,0x95,1,0x81,6};assert(hid.parse(dial,sizeof(dial)));
 report=0xff;assert(hid.decode(0,&report,1)==2);report=1;assert(hid.decode(0,&report,1)==1);assert(hid.decode(0,&report,1)==1);
 const uint8_t truncated[]={0x76,8};assert(!hid.parse(truncated,sizeof(truncated)));
 const uint8_t overflow[]={0x75,32,0x96,255,255,0x81,2};assert(!hid.parse(overflow,sizeof(overflow)));
 const uint8_t vendor[]={0x06,0,0xff,0x09,1,0x75,8,0x95,1,0x81,2};assert(!hid.parse(vendor,sizeof(vendor)));
}
'''
  with tempfile.TemporaryDirectory() as directory:
   src=Path(directory)/'test.cpp';src.write_text(source,encoding='utf-8');exe=Path(directory)/'hid-test.exe'
   if Path(compiler).name.lower()=='cl.exe':args=[compiler,'/nologo','/utf-8','/EHsc',str(src),'/Fe:'+str(exe),'/Fo:'+str(Path(directory)/'test.obj')]
   else:args=[compiler,'-std=c++17',str(src),'-o',str(exe)]
   subprocess.run(args,check=True,capture_output=True);subprocess.run([str(exe)],check=True,capture_output=True)
if __name__=='__main__':unittest.main()
