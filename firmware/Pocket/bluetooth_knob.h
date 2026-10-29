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
static volatile unsigned knobDetail=0;
static volatile unsigned knobStatus=0; // 0 unpaired, 1 reconnecting, 2 scanning, 3 ready, 4 unsupported/error
static QueueHandle_t knobQueue=nullptr;
static KnobHid knobDecoder;
static BLEClient *knobClient=nullptr;
static char knobAddress[18]={};
struct KnobInput {uint8_t action;uint32_t at,epoch;};
struct KnobReport {uint16_t handle;uint8_t id;uint16_t cccd;};
static KnobReport knobReports[8];static unsigned knobReportCount=0;
static portMUX_TYPE knobLock=portMUX_INITIALIZER_UNLOCKED;
struct KnobDiscovery {char address[18],name[49];int rssi;bool hid;};
static KnobDiscovery knobDiscovered[20];static unsigned knobDiscoveredCount=0;
static volatile unsigned knobDiscoveryState=0; // idle, requested, scanning, done, failed
static char knobSelectedAddress[18]={};
static uint32_t knobDiscoveryAt=0;
static int knobScanStart(JsonDocument &out){
 if(!knobQueue){out["error"]="蓝牙初始化失败";return 503;}
 if(recording||uploadPending||captureRequested){out["error"]="请先结束录音和上传";return 409;}
 if(knobPairUntil&&(int32_t)(knobPairUntil-millis())>0){out["error"]="正在配对，请等待完成后搜索";return 409;}
 if(knobDiscoveryState!=1&&knobDiscoveryState!=2){knobDiscoveryState=1;knobDiscoveryAt=millis();}
 out["state"]="scanning";return 200;
}
static void knobScanResults(JsonDocument &out){
 unsigned state=knobDiscoveryState;
 if((state==1||state==2)&&millis()-knobDiscoveryAt>20000){out["state"]="error";out["error"]="蓝牙搜索超时，请稍后重试";return;}
 out["state"]=state==1||state==2?"scanning":state==3?"done":state==4?"error":"idle";
 if(state!=3)return;
 KnobDiscovery rows[20];unsigned count;
 portENTER_CRITICAL(&knobLock);count=knobDiscoveredCount;memcpy(rows,knobDiscovered,sizeof(rows));portEXIT_CRITICAL(&knobLock);
 JsonArray list=out["devices"].to<JsonArray>();
 for(unsigned i=0;i<count;i++){JsonObject row=list.add<JsonObject>();row["address"]=rows[i].address;row["name"]=rows[i].name;row["rssi"]=rows[i].rssi;row["hid"]=rows[i].hid;}
}
static void knobReceiveHandle(uint16_t handle,const uint8_t *data,size_t length){
 if(!knobConnected||!knobQueue||length>256)return;
 uint8_t id=0;bool found=false;
 for(unsigned i=0;i<knobReportCount;i++)if(knobReports[i].handle==handle){id=knobReports[i].id;found=true;break;}
 if(!found)return;uint8_t actions=knobDecoder.decode(id,data,length);if(actions)knobInputCount++;
 for(uint8_t bit=1;bit<=16;bit<<=1)if(actions&bit){KnobInput input={bit,millis(),knobEpoch};xQueueSend(knobQueue,&input,0);}
}
static void knobNotification(BLERemoteCharacteristic *characteristic,uint8_t *data,size_t length,bool){knobReceiveHandle(characteristic->getHandle(),data,length);}
#if defined(CONFIG_NIMBLE_ENABLED)
// The bundled client dispatches notifications through a UUID-keyed map, losing
// duplicate 0x2a4d characteristics. Observe notifications by their real handle.
static ble_gap_event_listener knobGapListener;
static int knobGapEvent(ble_gap_event *event,void*){
 if(event->type==BLE_GAP_EVENT_NOTIFY_RX&&knobClient&&event->notify_rx.conn_handle==knobClient->getConnId()){
  unsigned length=OS_MBUF_PKTLEN(event->notify_rx.om);uint8_t bytes[256];
  if(length<=sizeof(bytes)){os_mbuf_copydata(event->notify_rx.om,0,length,bytes);knobReceiveHandle(event->notify_rx.attr_handle,bytes,length);}
 }return 0;
}
#endif
class KnobClientCallbacks:public BLEClientCallbacks{
 void onConnect(BLEClient*)override{}
 void onDisconnect(BLEClient*)override{knobConnected=false;if(knobDetail==7){knobDetail=17;knobStatus=1;}else if(knobDetail>=8)knobStatus=4;knobEpoch++;imageDirty=true;}
};
static KnobClientCallbacks knobClientCallbacks;
static void knobSaveAddress(const char *address){
 portENTER_CRITICAL(&knobLock);strlcpy(knobAddress,address,sizeof(knobAddress));portEXIT_CRITICAL(&knobLock);
 Preferences storage;if(storage.begin("pocket",false)){if(address[0])storage.putString("bt_knob",address);else storage.remove("bt_knob");storage.end();}
}
static bool knobFail(unsigned reason){knobDetail=reason;knobConnected=false;if(knobClient->isConnected())knobClient->disconnect();return false;}
#include "knob_gatt.h"
static bool knobBind(BLEAdvertisedDevice &device){
 knobDetail=2;
 if(!knobClient->connect(device.getAddress(),device.getAddressType(),7000))return knobFail(8);
 // HID report characteristics commonly require an encrypted bonded link.
 knobDetail=3;
 if(!knobClient->secureConnection())return knobFail(9);
 knobDetail=4;
#if defined(CONFIG_NIMBLE_ENABLED)
 if(!knobHidServiceEnd(0))return knobFail(10);
 if(!knobDiscoverCharacteristics())return knobFail(11);
 uint16_t mapHandle=0;for(unsigned i=0;i<knobGattCharacteristicCount;i++)if(knobGattCharacteristics[i].uuid==0x2a4b)mapHandle=knobGattCharacteristics[i].handle;
 knobDetail=5;if(!mapHandle||!knobReadReportMap(mapHandle))return knobFail(11);
 if(!knobDecoder.parse(knobGatt.bytes,knobGatt.length))return knobFail(12);
 knobReportCount=0;
 for(unsigned i=0;i<knobGattCharacteristicCount&&knobReportCount<8;i++){
  auto &row=knobGattCharacteristics[i];if(row.uuid!=0x2a4d||!(row.properties&0x10))continue;
  uint16_t end=i+1<knobGattCharacteristicCount?knobGattCharacteristics[i+1].declaration-1:knobGatt.serviceEnd;
  uint8_t id;uint16_t cccd;if(!knobReadReferenceHandle(row.handle,end,id,cccd))continue;
  knobReports[knobReportCount++]={row.handle,id,cccd};
 }
 if(!knobReportCount)return knobFail(13);
 knobDetail=6;knobEpoch++;xQueueReset(knobQueue);
 for(unsigned i=0;i<knobReportCount;i++)if(!knobEnableNotifications(knobReports[i].cccd))return knobFail(13);
 if(!knobClient->isConnected())return knobFail(14);knobConnected=true;
#else
 BLERemoteService *hid=knobClient->getService(BLEUUID((uint16_t)0x1812));
 if(!hid)return knobFail(10);
 auto map=hid->getCharacteristic(BLEUUID((uint16_t)0x2a4b));
 if(!map)return knobFail(11);
 knobDetail=5;
 String descriptor=map->readValue();
 if(!descriptor.length())return knobFail(11);
 if(descriptor.length()>2048||!knobDecoder.parse((const uint8_t*)descriptor.c_str(),descriptor.length()))return knobFail(12);
 knobReportCount=0;
 auto chars=hid->getCharacteristicsByHandle();
#if defined(CONFIG_NIMBLE_ENABLED)
 uint16_t serviceEnd=knobHidServiceEnd(hid->getHandle());if(!serviceEnd)return knobFail(13);
#endif
 if(chars)for(auto &entry:*chars){auto report=entry.second;
  if(report->getUUID()!=BLEUUID((uint16_t)0x2a4d)||!report->canNotify()||knobReportCount==8)continue;
#if defined(CONFIG_NIMBLE_ENABLED)
  uint16_t end=serviceEnd;auto next=chars->upper_bound(report->getHandle());if(next!=chars->end())end=next->first-2;
  uint8_t id;uint16_t cccd;if(!knobReadReference(report,end,id,cccd))continue;
  knobReports[knobReportCount++]={report->getHandle(),id,cccd};
#else
  auto reference=report->getDescriptor(BLEUUID((uint16_t)0x2908));if(!reference)continue;
  String ref=reference->readValue();if(ref.length()!=2||(uint8_t)ref[1]!=1)continue;
  knobReports[knobReportCount++]={report->getHandle(),(uint8_t)ref[0],0};
#endif
 }
 if(!knobReportCount)return knobFail(13);
 knobDetail=6;
 knobEpoch++;xQueueReset(knobQueue);knobConnected=true;
 for(auto &entry:*chars){auto report=entry.second;for(unsigned i=0;i<knobReportCount;i++)if(report->getHandle()==knobReports[i].handle){
#if defined(CONFIG_NIMBLE_ENABLED)
 if(!knobEnableNotifications(knobReports[i].cccd))return knobFail(13);
#else
 report->registerForNotify(knobNotification);
#endif
 }}
 if(!knobClient->isConnected())return knobFail(14);
#endif
 knobSaveAddress(device.getAddress().toString().c_str());knobStatus=3;knobDetail=7;imageDirty=true;return true;
}
static void knobTask(void*){
 BLEDevice::init("Codex Pocket");
#if defined(CONFIG_NIMBLE_ENABLED)
 ble_gap_event_listener_register(&knobGapListener,knobGapEvent,nullptr);
#endif
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
 }knobSaveAddress("");knobStatus=0;knobDetail=16;imageDirty=true;}
  if(knobDiscoveryState==1){
   if(recording||uploadPending||captureRequested){knobDiscoveryState=4;}
   else{
    knobDiscoveryState=2;knobScanning=true;
    BLEScanResults *found=scan->start(5,false);
    KnobDiscovery rows[20]={};unsigned count=0;
    if(found)for(unsigned i=0;i<found->getCount()&&count<20;i++){
     BLEAdvertisedDevice device=found->getDevice(i);String address=device.getAddress().toString();bool duplicate=false;
     for(unsigned k=0;k<count;k++)if(address.equalsIgnoreCase(rows[k].address)){duplicate=true;break;}if(duplicate)continue;
     strlcpy(rows[count].address,address.c_str(),sizeof(rows[count].address));strlcpy(rows[count].name,device.getName().c_str(),sizeof(rows[count].name));rows[count].rssi=device.getRSSI();rows[count].hid=device.isAdvertisingService(BLEUUID((uint16_t)0x1812));count++;
    }
    // Prefer stronger nearby devices; the UI still lets the user choose explicitly.
    for(unsigned i=0;i<count;i++)for(unsigned j=i+1;j<count;j++)if(rows[j].rssi>rows[i].rssi){KnobDiscovery temp=rows[i];rows[i]=rows[j];rows[j]=temp;}
    portENTER_CRITICAL(&knobLock);knobDiscoveredCount=count;memcpy(knobDiscovered,rows,sizeof(rows));portEXIT_CRITICAL(&knobLock);
    knobScanning=false;knobDiscoveryState=found?3:4;scan->clearResults();
   }
  }
  bool pairing=knobPairUntil&&(int32_t)(knobPairUntil-millis())>0;
  if(!pairing&&!knobConnected&&!knobAddress[0]&&knobStatus==2){knobStatus=4;knobDetail=15;}
  if(!knobConnected && (pairing||knobAddress[0]) && !recording&&!uploadPending&&!captureRequested){
   knobScanning=true;knobStatus=pairing?2:1;imageDirty=true;
   BLEScanResults *results=scan->start(3,false);knobScanning=false;
   if(results)for(unsigned i=0;i<results->getCount();i++){
    BLEAdvertisedDevice device=results->getDevice(i);String address=device.getAddress().toString();String name=device.getName();name.toUpperCase();
    bool saved=knobAddress[0]&&address.equalsIgnoreCase(knobAddress);
    // Pair only by explicit 60s user request and the manufacturer's advertised name.
    if(pairing){if(!address.equalsIgnoreCase(knobSelectedAddress))continue;}else if(!saved)continue;
    if(knobForgetRequested||recording||uploadPending||captureRequested)break;
    if(knobBind(device)){knobPairUntil=0;break;}knobStatus=4;if(pairing)knobPairUntil=0;imageDirty=true;break;
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
 const char *details[]={"","正在搜索所选设备，请按一下旋钮唤醒","正在建立蓝牙连接","正在加密并绑定旋钮","正在读取 HID 服务","正在解析旋钮键值","正在订阅旋转和按压事件","连接成功，可以旋转和按压","蓝牙连接失败，请断开其他主机并唤醒后重试","加密绑定失败，可能需要清除旋钮的旧配对","该设备未提供 BLE HID 服务","无法读取 HID 报告描述符","旋钮 HID 键值格式暂不支持","没有可订阅的 HID 输入报告","配对过程中蓝牙连接断开","60秒内未再次发现所选设备，请唤醒后重新搜索","已忘记旋钮","旋钮已断开，等待自动重连"};
 d["bluetoothDetail"]=details[knobDetail<18?knobDetail:0];
 const char *states[]={"unpaired","reconnecting","pairing","connected","unsupported_or_failed"};
 d["bluetoothInputCount"]=(uint32_t)knobInputCount;
 d["bluetoothConnected"]=(bool)knobConnected;d["bluetoothScanning"]=(bool)knobScanning;d["bluetoothState"]=states[knobStatus<5?knobStatus:4];
 portENTER_CRITICAL(&knobLock);char address[18];strlcpy(address,knobAddress,sizeof(address));portEXIT_CRITICAL(&knobLock);d["bluetoothPaired"]=address[0]!=0;d["bluetoothAddress"]=address;
}
static int knobCommand(bool forget,JsonDocument &out,const String &selected=""){
 if(recording||uploadPending||captureRequested){out["error"]="请先结束录音和上传";return 409;}
 if(!knobQueue){out["error"]="蓝牙初始化失败";return 503;}
 if(forget)knobForgetRequested=true;
 else{
  if(knobForgetRequested){out["error"]="正在忘记设备，请稍后配对";return 409;}
  if(knobDiscoveryState==1||knobDiscoveryState==2){out["error"]="正在搜索，请等待完成后选择设备";return 409;}
  if(knobConnected){out["error"]="旋钮已连接，请先忘记设备再重新配对";return 409;}bool found=false;
  portENTER_CRITICAL(&knobLock);for(unsigned i=0;i<knobDiscoveredCount;i++)if(selected.equalsIgnoreCase(knobDiscovered[i].address)){strlcpy(knobSelectedAddress,knobDiscovered[i].address,sizeof(knobSelectedAddress));found=true;break;}portEXIT_CRITICAL(&knobLock);
  if(!found){out["error"]="请先搜索并选择列表中的蓝牙设备";return 400;}
  knobPairUntil=millis()+60000;knobStatus=2;knobDetail=1;}
 out["message"]=forget?"已请求忘记旋钮；再次使用需重新配对。":"正在配对所选设备，请保持旋钮唤醒并断开其他主机。";return 200;
}
