#pragma once
#include <esp_http_client.h>
#include <esp_crt_bundle.h>
#include <math.h>
#include <time.h>

// Resolve on the device's Wi-Fi, never on the cloud gateway's network.
static SemaphoreHandle_t locationMutex;
static String locationCity,locationZone;
static double locationLat=0,locationLon=0;
static int locationOffset=0;
static bool locationValid=false;
static uint32_t locationAt=0;
static void initDeviceLocation(){locationMutex=xSemaphoreCreateMutex();}
static void deviceLocationStatus(JsonDocument &doc){
  JsonObject loc=doc["location"].to<JsonObject>();
  loc["source"]="wifi-ip";
  xSemaphoreTake(locationMutex,portMAX_DELAY);
  bool valid=locationValid && WiFi.status()==WL_CONNECTED && millis()-locationAt<5400000UL;
  loc["status"]=valid?"ready":"unavailable";
  if(valid){
    loc["city"]=locationCity;loc["timezone"]=locationZone;
    loc["latitude"]=locationLat;loc["longitude"]=locationLon;
    loc["utcOffsetSeconds"]=locationOffset;loc["ageSeconds"]=(millis()-locationAt)/1000;
  }
  xSemaphoreGive(locationMutex);
}
struct LocationReply {String body;bool oversized=false;};
static esp_err_t locationHttpEvent(esp_http_client_event_t *event){
  auto reply=(LocationReply*)event->user_data;
  if(event->event_id==HTTP_EVENT_ON_DATA){
    if(reply->body.length()+event->data_len>8192){reply->oversized=true;return ESP_FAIL;}
    reply->body.concat((const char*)event->data,event->data_len);
  }
  return ESP_OK;
}
static void deviceLocationTask(void*){
  String network;
  uint32_t lastAttempt=0;
  bool attempted=false;
  for(;;){
    vTaskDelay(pdMS_TO_TICKS(2000));
    String current=WiFi.status()==WL_CONNECTED?WiFi.BSSIDstr()+"|"+WiFi.localIP().toString()+"|"+WiFi.gatewayIP().toString():"";
    if(current!=network){
      network=current;attempted=false;
      xSemaphoreTake(locationMutex,portMAX_DELAY);locationValid=false;xSemaphoreGive(locationMutex);
    }
    if(!network.length() || recording || uploadPending || captureRequested || time(nullptr)<1700000000 || ESP.getFreeHeap()<65000)continue;
    xSemaphoreTake(locationMutex,portMAX_DELAY);bool known=locationValid;xSemaphoreGive(locationMutex);
    if(attempted && millis()-lastAttempt<(known?1800000UL:120000UL))continue;
    attempted=true;lastAttempt=millis();
    LocationReply reply;
    esp_http_client_config_t config={};
    config.url="https://ipwho.is/?fields=success,city,latitude,longitude,timezone.id,timezone.offset";
    config.crt_bundle_attach=esp_crt_bundle_attach;
    config.timeout_ms=8000;config.buffer_size=1024;config.buffer_size_tx=512;
    config.disable_auto_redirect=true;config.event_handler=locationHttpEvent;config.user_data=&reply;
    esp_http_client_handle_t client=esp_http_client_init(&config);
    if(!client)continue;
    esp_err_t result=esp_http_client_perform(client);
    int status=esp_http_client_get_status_code(client);esp_http_client_cleanup(client);
    if(result!=ESP_OK || status!=200 || reply.oversized)continue;
    JsonDocument data;
    if(deserializeJson(data,reply.body) || !data["success"].as<bool>() || !data["latitude"].is<double>() || !data["longitude"].is<double>() || !data["timezone"]["offset"].is<int>())continue;
    double lat=data["latitude"],lon=data["longitude"];
    int offset=data["timezone"]["offset"];
    String city=data["city"]|"",zone=data["timezone"]["id"]|"";
    if(!isfinite(lat)||!isfinite(lon)||lat< -90||lat>90||lon< -180||lon>180||offset< -43200||offset>50400||city.length()==0||city.length()>80||zone.length()==0||zone.length()>80)continue;
    // Reject a reply if Wi-Fi changed during the HTTPS request.
    String after=WiFi.status()==WL_CONNECTED?WiFi.BSSIDstr()+"|"+WiFi.localIP().toString()+"|"+WiFi.gatewayIP().toString():"";
    if(after!=network)continue;
    xSemaphoreTake(locationMutex,portMAX_DELAY);
    locationCity=city;locationZone=zone;locationLat=lat;locationLon=lon;
    locationOffset=offset;locationAt=millis();locationValid=true;
    xSemaphoreGive(locationMutex);
  }
}
