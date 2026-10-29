#pragma once
#include <BLEDevice.h>
#include <BLEClient.h>
#include <BLEScan.h>
#include <BLESecurity.h>
#include "knob_hid.h"
#if defined(CONFIG_NIMBLE_ENABLED)
#include <host/ble_store.h>
#endif
static volatile bool knobConnected=false,knobScanning=false,knobForgetRequested=false;
static volatile uint32_t knobPairUntil=0,knobEpoch=0;
static volatile uint32_t knobInputCount=0;
static volatile unsigned knobStatus=0; // 0 unpaired, 1 reconnecting, 2 scanning, 3 ready, 4 unsupported/error
static QueueHandle_t knobQueue=nullptr;
static KnobHid knobDecoder;
static BLEClient *knobClient=nullptr;
static char knobAddress[18]={};
struct KnobInput {uint8_t action;uint32_t at,epoch;};
struct KnobReport {uint16_t handle;uint8_t id;};
static KnobReport knobReports[8];static unsigned knobReportCount=0;
static portMUX_TYPE knobLock=portMUX_INITIALIZER_UNLOCKED;
static void knobNotification(BLERemoteCharacteristic *characteristic,uint8_t *data,size_t length,bool){
 if(!knobConnected||!knobQueue||length>256)return;
 uint8_t id=0;bool found=false;
 for(unsigned i=0;i<knobReportCount;i++)if(knobReports[i].handle==characteristic->getHandle()){id=knobReports[i].id;found=true;break;}
 if(!found)return;uint8_t actions=knobDecoder.decode(id,data,length);if(actions)knobInputCount++;
 for(uint8_t bit=1;bit<=16;bit<<=1)if(actions&bit){KnobInput input={bit,millis(),knobEpoch};xQueueSend(knobQueue,&input,0);}
}
class KnobClientCallbacks:public BLEClientCallbacks{
 void onConnect(BLEClient*)override{}
 void onDisconnect(BLEClient*)override{knobConnected=false;knobStatus=1;knobEpoch++;imageDirty=true;}
};
static KnobClientCallbacks knobClientCallbacks;
static void knobSaveAddress(const char *address){
 portENTER_CRITICAL(&knobLock);strlcpy(knobAddress,address,sizeof(knobAddress));portEXIT_CRITICAL(&knobLock);
 Preferences storage;if(storage.begin("pocket",false)){if(address[0])storage.putString("bt_knob",address);else storage.remove("bt_knob");storage.end();}
}
static bool knobBind(BLEAdvertisedDevice &device){
 if(!knobClient->connect(device.getAddress(),device.getAddressType(),7000))return false;
 // HID report characteristics commonly require an encrypted bonded link.
 if(!knobClient->secureConnection()){knobClient->disconnect();return false;}
 BLERemoteService *hid=knobClient->getService(BLEUUID((uint16_t)0x1812));
 if(!hid){knobClient->disconnect();return false;}
 auto map=hid->getCharacteristic(BLEUUID((uint16_t)0x2a4b));
 if(!map){knobClient->disconnect();return false;}
 String descriptor=map->readValue();
 if(descriptor.length()>2048||!knobDecoder.parse((const uint8_t*)descriptor.c_str(),descriptor.length())){knobClient->disconnect();return false;}
 knobReportCount=0;
 auto chars=hid->getCharacteristicsByHandle();
 if(chars)for(auto &entry:*chars){auto report=entry.second;
  if(report->getUUID()!=BLEUUID((uint16_t)0x2a4d)||!report->canNotify()||knobReportCount==8)continue;
  auto reference=report->getDescriptor(BLEUUID((uint16_t)0x2908));if(!reference)continue;
  String ref=reference->readValue();if(ref.length()!=2||(uint8_t)ref[1]!=1)continue;
  knobReports[knobReportCount++]={report->getHandle(),(uint8_t)ref[0]};
 }
 if(!knobReportCount){knobClient->disconnect();return false;}
 knobEpoch++;xQueueReset(knobQueue);knobConnected=true;
 for(auto &entry:*chars){auto report=entry.second;for(unsigned i=0;i<knobReportCount;i++)if(report->getHandle()==knobReports[i].handle)report->registerForNotify(knobNotification);}
 if(!knobClient->isConnected()){knobConnected=false;return false;}
 knobSaveAddress(device.getAddress().toString().c_str());knobStatus=3;imageDirty=true;return true;
}
static void knobTask(void*){
 BLEDevice::init("Codex Pocket");
 #if defined(CONFIG_NIMBLE_ENABLED)
 // Arduino ESP32 3.3.0's NimBLE security setters reference undefined
 // static members. Configure the public NimBLE host fields directly.
 ble_hs_cfg.sm_bonding=1;ble_hs_cfg.sm_mitm=0;ble_hs_cfg.sm_sc=0;
 ble_hs_cfg.sm_io_cap=BLE_HS_IO_NO_INPUT_OUTPUT;
 ble_hs_cfg.sm_our_key_dist=BLE_SM_PAIR_KEY_DIST_ENC|BLE_SM_PAIR_KEY_DIST_ID;
 ble_hs_cfg.sm_their_key_dist=BLE_SM_PAIR_KEY_DIST_ENC|BLE_SM_PAIR_KEY_DIST_ID;
#else
 BLESecurity::setAuthenticationMode(true,false,false);BLESecurity::setCapability(ESP_IO_CAP_NONE);
#endif
 knobClient=BLEDevice::createClient();knobClient->setClientCallbacks(&knobClientCallbacks);
 auto scan=BLEDevice::getScan();scan->setActiveScan(true);scan->setInterval(160);scan->setWindow(48);
 for(;;){
  if(knobForgetRequested){knobForgetRequested=false;knobPairUntil=0;knobConnected=false;knobEpoch++;xQueueReset(knobQueue);if(knobClient->isConnected())knobClient->disconnect();if(knobAddress[0]){BLEAddress address{String(knobAddress)};
#if defined(CONFIG_NIMBLE_ENABLED)
 ble_addr_t peer;peer.type=address.getType();memcpy(peer.val,address.getNative(),6);ble_store_util_delete_peer(&peer);peer.type=1;ble_store_util_delete_peer(&peer);
#else
 esp_ble_remove_bond_device(address.getNative());
#endif
 }knobSaveAddress("");knobStatus=0;imageDirty=true;}
  bool pairing=knobPairUntil&&(int32_t)(knobPairUntil-millis())>0;
  if(!pairing&&!knobConnected&&!knobAddress[0]&&knobStatus==2)knobStatus=0;
  if(!knobConnected && (pairing||knobAddress[0]) && !recording&&!uploadPending&&!captureRequested){
   knobScanning=true;knobStatus=pairing?2:1;imageDirty=true;
   BLEScanResults *results=scan->start(3,false);knobScanning=false;
   if(results)for(unsigned i=0;i<results->getCount();i++){
    BLEAdvertisedDevice device=results->getDevice(i);String address=device.getAddress().toString();String name=device.getName();name.toUpperCase();
    bool saved=knobAddress[0]&&address.equalsIgnoreCase(knobAddress);
    // Pair only by explicit 60s user request and the manufacturer's advertised name.
    if(!saved&&!(pairing&&name.startsWith("ANTICATER")))continue;
    if(knobForgetRequested||recording||uploadPending||captureRequested)break;
    if(knobBind(device)){knobPairUntil=0;break;}knobStatus=4;imageDirty=true;
   }scan->clearResults();
  }vTaskDelay(pdMS_TO_TICKS(knobConnected?500:5000));
 }
}
static void initKnob(){
 Preferences storage;if(storage.begin("pocket",true)){String address=storage.getString("bt_knob","");strlcpy(knobAddress,address.c_str(),sizeof(knobAddress));storage.end();}
 knobQueue=xQueueCreate(16,sizeof(KnobInput));
 if(!knobQueue||xTaskCreate(knobTask,"ble-knob",12288,nullptr,1,nullptr)!=pdPASS){knobStatus=4;return;}
 knobStatus=knobAddress[0]?1:0;
}
static void knobStatusJson(JsonDocument &d){
 const char *states[]={"unpaired","reconnecting","pairing","connected","unsupported_or_failed"};
 d["bluetoothInputCount"]=(uint32_t)knobInputCount;
 d["bluetoothConnected"]=(bool)knobConnected;d["bluetoothScanning"]=(bool)knobScanning;d["bluetoothState"]=states[knobStatus<5?knobStatus:4];
 portENTER_CRITICAL(&knobLock);char address[18];strlcpy(address,knobAddress,sizeof(address));portEXIT_CRITICAL(&knobLock);d["bluetoothPaired"]=address[0]!=0;
}
static int knobCommand(bool forget,JsonDocument &out){
 if(recording||uploadPending||captureRequested){out["error"]="请先结束录音和上传";return 409;}
 if(!knobQueue){out["error"]="蓝牙初始化失败";return 503;}
 if(forget)knobForgetRequested=true;
 else{if(knobConnected){out["error"]="旋钮已连接，请先忘记设备再重新配对";return 409;}knobPairUntil=millis()+60000;}
 out["message"]=forget?"已请求忘记旋钮；再次使用需重新配对。":"60秒内搜索 ANTICATER 蓝牙旋钮，请唤醒旋钮并断开其他主机。";return 200;
}
