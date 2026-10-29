#pragma once
#if defined(CONFIG_NIMBLE_ENABLED)
#include <host/ble_gatt.h>
#include <os/os_mbuf.h>
// Arduino-ESP32 3.3.0 initializes remote characteristic end handles to zero
// before descriptor discovery. Discover report reference/CCCD within the real
// characteristic bounds instead; never infer a report ID or write arbitrary handles.
struct KnobGattDiscovery {
 SemaphoreHandle_t done=nullptr;uint16_t connection=0,characteristic=0,reference=0,cccd=0;
 uint16_t serviceStart=0,serviceEnd=0;int status=0;uint8_t bytes[8]={};unsigned length=0;
};
static KnobGattDiscovery knobGatt;
static int knobGattDescriptors(uint16_t connection,const ble_gatt_error *error,uint16_t characteristic,const ble_gatt_dsc *descriptor,void*){
 if(connection!=knobGatt.connection||characteristic!=knobGatt.characteristic)return 0;
 if(!error->status&&descriptor){
  if(descriptor->uuid.u.type==BLE_UUID_TYPE_16){uint16_t uuid=descriptor->uuid.u16.value;if(uuid==0x2908)knobGatt.reference=descriptor->handle;else if(uuid==0x2902)knobGatt.cccd=descriptor->handle;}
 }else{knobGatt.status=error->status;xSemaphoreGive(knobGatt.done);}return 0;
}
static int knobGattValue(uint16_t connection,const ble_gatt_error *error,ble_gatt_attr *attribute,void*){
 if(connection!=knobGatt.connection)return 0;
 knobGatt.status=error->status;
 if(!error->status&&attribute&&attribute->om){unsigned length=OS_MBUF_PKTLEN(attribute->om);knobGatt.length=length>sizeof(knobGatt.bytes)?sizeof(knobGatt.bytes):length;os_mbuf_copydata(attribute->om,0,knobGatt.length,knobGatt.bytes);}
 xSemaphoreGive(knobGatt.done);return 0;
}
static bool knobGattWait(int start){
 if(start)return false;
 if(xSemaphoreTake(knobGatt.done,pdMS_TO_TICKS(15000))!=pdTRUE){knobClient->disconnect();return false;}
 return knobGatt.status==0||knobGatt.status==BLE_HS_EDONE;
}
static void knobGattPrepare(uint16_t connection){
 if(!knobGatt.done)knobGatt.done=xSemaphoreCreateBinary();
 if(knobGatt.done)xSemaphoreTake(knobGatt.done,0);knobGatt.connection=connection;knobGatt.status=0;knobGatt.length=0;
}
static int knobGattService(uint16_t connection,const ble_gatt_error *error,const ble_gatt_svc *service,void*){
 if(connection!=knobGatt.connection)return 0;
 if(!error->status&&service){if(service->start_handle==knobGatt.serviceStart)knobGatt.serviceEnd=service->end_handle;}
 else{knobGatt.status=error->status;xSemaphoreGive(knobGatt.done);}return 0;
}
static uint16_t knobHidServiceEnd(uint16_t start){
 knobGattPrepare(knobClient->getConnId());if(!knobGatt.done)return 0;knobGatt.serviceStart=start;knobGatt.serviceEnd=0;
 ble_uuid16_t uuid={{BLE_UUID_TYPE_16},0x1812};
 if(!knobGattWait(ble_gattc_disc_svc_by_uuid(knobGatt.connection,&uuid.u,knobGattService,nullptr)))return 0;
 return knobGatt.serviceEnd;
}
static bool knobReadReference(BLERemoteCharacteristic *report,uint16_t end,uint8_t &id,uint16_t &cccd){
 knobGattPrepare(knobClient->getConnId());if(!knobGatt.done)return false;
 knobGatt.characteristic=report->getHandle();knobGatt.reference=0;knobGatt.cccd=0;
 if(end<=report->getHandle()||!knobGattWait(ble_gattc_disc_all_dscs(knobGatt.connection,report->getHandle(),end,knobGattDescriptors,nullptr)))return false;
 if(!knobGatt.reference||!knobGatt.cccd)return false;
 knobGattPrepare(knobClient->getConnId());
 if(!knobGattWait(ble_gattc_read(knobGatt.connection,knobGatt.reference,knobGattValue,nullptr))||knobGatt.length!=2||knobGatt.bytes[1]!=1)return false;
 id=knobGatt.bytes[0];cccd=knobGatt.cccd;return true;
}
static bool knobEnableNotifications(uint16_t cccd){
 knobGattPrepare(knobClient->getConnId());if(!knobGatt.done)return false;
 const uint8_t enabled[2]={1,0};return knobGattWait(ble_gattc_write_flat(knobGatt.connection,cccd,enabled,2,knobGattValue,nullptr));
}
#endif
