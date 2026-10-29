"""Exercise the actual bounded NimBLE HID descriptor discovery adapter."""
from pathlib import Path
import shutil, subprocess, tempfile, unittest
class KnobGattTests(unittest.TestCase):
 def test_bounds_reference_and_cccd_subscription(self):
  compiler=shutil.which('cl') or shutil.which('g++') or shutil.which('clang++')
  if not compiler:self.skipTest('host C++ compiler required')
  header=(Path(__file__).resolve().parents[1]/'firmware/Pocket/knob_gatt.h').as_posix()
  source=r'''
#include <cstdint>
#include <cassert>
#include <cstring>
#define CONFIG_NIMBLE_ENABLED 1
#define BLE_UUID_TYPE_16 16
#define BLE_HS_EDONE 14
#define pdTRUE 1
#define pdMS_TO_TICKS(x) (x)
using SemaphoreHandle_t=int*;
SemaphoreHandle_t xSemaphoreCreateBinary(){static int token=0;return &token;}
int xSemaphoreTake(SemaphoreHandle_t s,int){if(*s){*s=0;return 1;}return 0;}
void xSemaphoreGive(SemaphoreHandle_t s){*s=1;}
struct ble_uuid_t{int type;};struct ble_uuid16_t{ble_uuid_t u;uint16_t value;};struct ble_gatt_svc{uint16_t start_handle,end_handle;};
struct ble_gatt_error{int status;};struct mbuf{unsigned len;uint8_t data[2];};
struct ble_gatt_attr{mbuf *om;};
#define OS_MBUF_PKTLEN(x) ((x)->len)
void os_mbuf_copydata(mbuf *m,int,unsigned len,uint8_t *out){memcpy(out,m->data,len);}
struct ble_gatt_chr{uint16_t def_handle,val_handle;uint8_t properties;struct {struct {int type;} u;struct {uint16_t value;} u16;} uuid;};
struct ble_gatt_dsc{uint16_t handle;struct {struct {int type;} u;struct {uint16_t value;} u16;} uuid;};
struct {uint16_t getConnId(){return 7;}void disconnect(){}} client;
auto knobClient=&client;
struct BLERemoteCharacteristic{uint16_t getHandle(){return 42;}};
int mode=0,writes=0;uint16_t observedEnd=0;
using Discover=int(*)(uint16_t,const ble_gatt_error*,uint16_t,const ble_gatt_dsc*,void*);
using Value=int(*)(uint16_t,const ble_gatt_error*,ble_gatt_attr*,void*);
int ble_gattc_disc_svc_by_uuid(uint16_t conn,const ble_uuid_t*,int(*cb)(uint16_t,const ble_gatt_error*,const ble_gatt_svc*,void*),void *arg){ble_gatt_error ok{0};ble_gatt_svc svc{30,48};cb(conn,&ok,&svc,arg);ble_gatt_error done{BLE_HS_EDONE};cb(conn,&done,nullptr,arg);return 0;}
int ble_gattc_disc_all_dscs(uint16_t conn,uint16_t start,uint16_t end,Discover cb,void *arg){
 assert(conn==7&&start==42&&end==48);observedEnd=end;ble_gatt_error ok{0};
 ble_gatt_dsc d{};d.handle=43;d.uuid.u.type=16;d.uuid.u16.value=0x2908;cb(conn,&ok,start,&d,arg);
 if(mode!=1){d.handle=44;d.uuid.u16.value=0x2902;cb(conn,&ok,start,&d,arg);}
 ble_gatt_error done{BLE_HS_EDONE};cb(conn,&done,start,nullptr,arg);return 0;
}
int ble_gattc_read(uint16_t conn,uint16_t handle,Value cb,void *arg){assert(handle==43);mbuf data{mode==2?1u:2u,{3,static_cast<uint8_t>(mode==3?2:1)}};ble_gatt_attr attr{&data};ble_gatt_error ok{0};cb(conn,&ok,&attr,arg);return 0;}
int ble_gattc_write_flat(uint16_t conn,uint16_t handle,const void *data,uint16_t len,Value cb,void *arg){assert(conn==7&&handle==44&&len==2);auto bytes=static_cast<const uint8_t*>(data);assert(bytes[0]==1&&bytes[1]==0);writes++;if(mode==4)return 5;ble_gatt_error ok{0};cb(conn,&ok,nullptr,arg);return 0;}
int ble_gattc_disc_all_chrs(uint16_t conn,uint16_t start,uint16_t end,int(*cb)(uint16_t,const ble_gatt_error*,const ble_gatt_chr*,void*),void *arg){assert(start==30&&end==48);ble_gatt_error ok{0};ble_gatt_chr chr{};chr.def_handle=40;chr.val_handle=42;chr.properties=0x10;chr.uuid.u.type=16;chr.uuid.u16.value=0x2a4d;cb(conn,&ok,&chr,arg);ble_gatt_error done{BLE_HS_EDONE};cb(conn,&done,nullptr,arg);return 0;}
int ble_gattc_read_long(uint16_t conn,uint16_t handle,uint16_t,Value cb,void *arg){mbuf data{2,{1,2}};ble_gatt_attr attr{&data};ble_gatt_error ok{0};cb(conn,&ok,&attr,arg);cb(conn,&ok,&attr,arg);ble_gatt_error done{BLE_HS_EDONE};cb(conn,&done,nullptr,arg);return 0;}
'''+ '#include "'+header+'"\n'+r'''
int main(){BLERemoteCharacteristic report;uint8_t id=0;uint16_t cccd=0;
 assert(knobHidServiceEnd(30)==48);assert(knobHidServiceEnd(31)==0);assert(knobHidServiceEnd(0)==48);assert(knobDiscoverCharacteristics());assert(knobGattCharacteristicCount==1&&knobGattCharacteristics[0].handle==42);assert(knobReadReportMap(42)&&knobGatt.length==4&&knobGatt.bytes[2]==1);assert(knobReadReference(&report,48,id,cccd));assert(id==3&&cccd==44&&observedEnd==48);assert(knobEnableNotifications(cccd));assert(writes==1);
 mode=1;assert(!knobReadReference(&report,48,id,cccd));mode=2;assert(!knobReadReference(&report,48,id,cccd));mode=3;assert(!knobReadReference(&report,48,id,cccd));mode=4;assert(!knobEnableNotifications(44));
 assert(!knobReadReference(&report,42,id,cccd));
}
'''
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory)
   for name in ('host/ble_gatt.h','os/os_mbuf.h'):
    file=root/name;file.parent.mkdir(parents=True,exist_ok=True);file.touch()
   src=root/'test.cpp';src.write_text(source,encoding='utf-8');exe=root/'gatt-test.exe'
   if Path(compiler).name.lower()=='cl.exe':args=[compiler,'/nologo','/utf-8','/EHsc','/I'+str(root),str(src),'/Fe:'+str(exe),'/Fo:'+str(root/'test.obj')]
   else:args=[compiler,'-std=c++17','-I',str(root),str(src),'-o',str(exe)]
   subprocess.run(args,check=True,capture_output=True);subprocess.run([str(exe)],check=True,capture_output=True)
if __name__=='__main__':unittest.main()
