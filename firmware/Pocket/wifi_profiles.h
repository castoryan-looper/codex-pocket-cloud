#pragma once
#include "wifi_profile_policy.h"

static KnownWifiSet knownWifi={};
static bool knownWifiPersisted=false;
static bool persistKnownWifi(const KnownWifiSet &set){
  JsonDocument doc;doc["v"]=1;doc["preferred"]=set.preferred;doc["clock"]=set.clock;
  JsonArray list=doc["networks"].to<JsonArray>();
  for(unsigned i=0;i<set.count;i++){
    JsonObject item=list.add<JsonObject>();item["s"]=set.profiles[i].ssid;item["p"]=set.profiles[i].password;item["u"]=set.profiles[i].used;
  }
  String wire;serializeJson(doc,wire);
  Preferences storage;if(!storage.begin("pocket",false))return false;
  bool saved=storage.putString("wifi_known",wire)==wire.length();storage.end();return saved;
}
static void syncPreferredWifi(){
  int index=knownWifiFind(knownWifi,knownWifi.preferred);
  wifiConfigured=knownWifi.count>0;
  wifiSsid=index>=0?knownWifi.profiles[index].ssid:"";
  wifiPass=index>=0?knownWifi.profiles[index].password:"";
}
static void loadKnownWifi(){
  knownWifi={};Preferences storage;String wire;
  if(storage.begin("pocket",true)){wire=storage.getString("wifi_known","");storage.end();}
  JsonDocument doc;
  bool valid=wire.length() && !deserializeJson(doc,wire) && doc["v"]==1 && doc["networks"].is<JsonArray>() && doc["networks"].size()<=KNOWN_WIFI_LIMIT;
  if(valid){
    for(JsonObject item:doc["networks"].as<JsonArray>()){
      String ssid=item["s"]|"",password=item["p"]|"";
      if(ssid.length()!=strlen(ssid.c_str())||password.length()!=strlen(password.c_str())||!knownWifiValid(ssid.c_str(),password.c_str())){valid=false;break;}
      if(knownWifiFind(knownWifi,ssid.c_str())>=0){valid=false;break;}
      KnownWifiProfile &p=knownWifi.profiles[knownWifi.count++];
      strcpy(p.ssid,ssid.c_str());strcpy(p.password,password.c_str());p.used=item["u"]|0U;
      if(p.used>knownWifi.clock)knownWifi.clock=p.used;
    }
    String preferred=doc["preferred"]|"";
    if(knownWifiFind(knownWifi,preferred.c_str())>=0)strcpy(knownWifi.preferred,preferred.c_str());
    else if(knownWifi.count)strcpy(knownWifi.preferred,knownWifi.profiles[0].ssid);
  }
  if(!valid){
    knownWifi={};
    if(knownWifiValid(wifiSsid.c_str(),wifiPass.c_str()))knownWifiUpsert(knownWifi,wifiSsid.c_str(),wifiPass.c_str(),true);
    knownWifiPersisted=persistKnownWifi(knownWifi);
  }else knownWifiPersisted=true;
  syncPreferredWifi();
}
static void describeKnownWifi(JsonDocument &doc){
  doc["wifiProfileLimit"]=KNOWN_WIFI_LIMIT;doc["wifiProfilesPersisted"]=knownWifiPersisted;
  JsonArray list=doc["wifiProfiles"].to<JsonArray>();String current=WiFi.status()==WL_CONNECTED?WiFi.SSID():"";
  for(unsigned i=0;i<knownWifi.count;i++){
    JsonObject item=list.add<JsonObject>();item["ssid"]=knownWifi.profiles[i].ssid;
    item["preferred"]=strcmp(knownWifi.preferred,knownWifi.profiles[i].ssid)==0;
    item["connected"]=current==knownWifi.profiles[i].ssid;
  }
}
