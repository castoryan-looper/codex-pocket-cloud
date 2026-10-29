#pragma once
#include <stdint.h>
#include <string.h>

static const unsigned KNOWN_WIFI_LIMIT=8;
struct KnownWifiProfile {char ssid[33];char password[64];uint32_t used;};
struct KnownWifiSet {KnownWifiProfile profiles[KNOWN_WIFI_LIMIT];unsigned count;uint32_t clock;char preferred[33];};
struct KnownWifiVisible {const char *ssid;int rssi;bool secure;};

static int knownWifiFind(const KnownWifiSet &set,const char *ssid){
  for(unsigned i=0;i<set.count;i++)if(strcmp(set.profiles[i].ssid,ssid)==0)return (int)i;
  return -1;
}
static bool knownWifiValid(const char *ssid,const char *password){
  if(!ssid||!password||!ssid[0]||strlen(ssid)>32||strlen(password)>63)return false;
  for(const unsigned char *p=(const unsigned char*)ssid;*p;p++)if(*p<32||*p==127)return false;
  return true;
}
static uint32_t knownWifiTick(KnownWifiSet &set){
  if(set.clock==UINT32_MAX){
    // Preserve relative recency when the counter wraps.
    uint32_t old[KNOWN_WIFI_LIMIT];for(unsigned i=0;i<set.count;i++)old[i]=set.profiles[i].used;
    for(unsigned i=0;i<set.count;i++){
      uint32_t rank=1;for(unsigned j=0;j<set.count;j++)if(old[j]<old[i]||(old[j]==old[i]&&j<i))rank++;
      set.profiles[i].used=rank;
    }
    set.clock=set.count;
  }
  return ++set.clock;
}
static bool knownWifiUpsert(KnownWifiSet &set,const char *ssid,const char *password,bool replacePassword,const char *protect=""){
  if(!knownWifiValid(ssid,password))return false;
  int index=knownWifiFind(set,ssid);
  if(index<0){
    if(set.count<KNOWN_WIFI_LIMIT)index=(int)set.count++;
    else {
      for(unsigned i=0;i<set.count;i++)if(strcmp(set.profiles[i].ssid,protect)!=0 && (index<0||set.profiles[i].used<set.profiles[index].used))index=i;
      if(index<0)return false;
    }
    memset(&set.profiles[index],0,sizeof(set.profiles[index]));
    strcpy(set.profiles[index].ssid,ssid);replacePassword=true;
  }
  if(replacePassword)strcpy(set.profiles[index].password,password);
  set.profiles[index].used=knownWifiTick(set);
  strcpy(set.preferred,ssid);
  return true;
}
static bool knownWifiTouch(KnownWifiSet &set,const char *ssid){
  int index=knownWifiFind(set,ssid);
  if(index<0||set.profiles[index].used==set.clock)return false;
  set.profiles[index].used=knownWifiTick(set);return true;
}
static bool knownWifiForget(KnownWifiSet &set,const char *ssid){
  int index=knownWifiFind(set,ssid);if(index<0)return false;
  for(unsigned i=index+1;i<set.count;i++)set.profiles[i-1]=set.profiles[i];
  memset(&set.profiles[--set.count],0,sizeof(KnownWifiProfile));
  if(strcmp(set.preferred,ssid)==0){
    set.preferred[0]=0;int newest=-1;
    for(unsigned i=0;i<set.count;i++)if(newest<0||set.profiles[i].used>set.profiles[newest].used)newest=i;
    if(newest>=0)strcpy(set.preferred,set.profiles[newest].ssid);
  }
  return true;
}
static bool knownWifiBlocked(const uint32_t *blocked,unsigned index,uint32_t now){
  return blocked[index]!=0 && (int32_t)(now-blocked[index])<0;
}
static int knownWifiChoose(const KnownWifiSet &set,const KnownWifiVisible *visible,unsigned count,const uint32_t *blocked,uint32_t now){
  int best=-1,bestRssi=-1000;
  for(unsigned n=0;n<count;n++){
    int index=knownWifiFind(set,visible[n].ssid);
    if(index<0||knownWifiBlocked(blocked,index,now)||((set.profiles[index].password[0]!=0)!=visible[n].secure))continue;
    bool preferred=strcmp(set.preferred,set.profiles[index].ssid)==0;
    bool bestPreferred=best>=0&&strcmp(set.preferred,set.profiles[best].ssid)==0;
    if(best<0 || (preferred&&!bestPreferred) || (preferred==bestPreferred &&
       (visible[n].rssi>bestRssi || (visible[n].rssi==bestRssi&&set.profiles[index].used>set.profiles[best].used)))){
      best=index;bestRssi=visible[n].rssi;
    }
  }
  return best;
}
static int knownWifiFallback(const KnownWifiSet &set,const uint32_t *blocked,uint32_t now){
  int preferred=knownWifiFind(set,set.preferred);
  if(preferred>=0&&!knownWifiBlocked(blocked,preferred,now))return preferred;
  int best=-1;for(unsigned i=0;i<set.count;i++)if(!knownWifiBlocked(blocked,i,now)&&(best<0||set.profiles[i].used>set.profiles[best].used))best=i;
  return best;
}
