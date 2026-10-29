#pragma once
// One owner for automatic association and scanning; the UI scanner pauses it.
static uint32_t wifiBlockedUntil[KNOWN_WIFI_LIMIT]={};
static uint32_t wifiAttemptAt=0,wifiNextScanAt=0,wifiAutoScanAt=0;
static int wifiAttemptIndex=-1,wifiAutoPhase=0;
static bool wifiManualScan=false;
static String wifiAttemptSsid,wifiResumeSsid,wifiLastRecordedSsid;

static bool wifiDeadlineReached(uint32_t now,uint32_t deadline){return deadline==0 || (int32_t)(now-deadline)>=0;}
static void wifiStartKnown(int index,bool clearScan=true){
  if(index<0 || (unsigned)index>=knownWifi.count)return;
  if(clearScan)WiFi.scanDelete();
  WiFi.setAutoReconnect(false);
  wifiAttemptIndex=index;wifiAttemptSsid=knownWifi.profiles[index].ssid;wifiAttemptAt=millis();wifiAutoPhase=0;
  lastWifiDisconnectReason=0;
  WiFi.setMinSecurity(knownWifi.profiles[index].password[0]?WIFI_AUTH_WPA_PSK:WIFI_AUTH_OPEN);
  WiFi.begin(knownWifi.profiles[index].ssid,knownWifi.profiles[index].password);
}
static void wifiPauseForManualScan(){
  wifiResumeSsid=WiFi.status()==WL_CONNECTED?WiFi.SSID():wifiAttemptSsid;
  wifiManualScan=true;wifiAttemptIndex=-1;WiFi.setAutoReconnect(false);
}
static void wifiResumeAfterManualScan(){
  wifiManualScan=false;
  int index=knownWifiFind(knownWifi,wifiResumeSsid.c_str());
  if(index<0)index=knownWifiFallback(knownWifi,wifiBlockedUntil,millis());
  if(index>=0)wifiStartKnown(index,false);
  else wifiNextScanAt=millis()+1000;
}
static void loopKnownWifi(){
  uint32_t now=millis();
  if(wifiManualScan || wifiScanPhase==1 || wifiScanPhase==2)return;
  if(WiFi.status()==WL_CONNECTED){
    wifiAttemptIndex=-1;wifiAutoPhase=0;
    if(!recording&&!uploadPending&&!captureRequested){
      String connected=WiFi.SSID();
      if(connected!=wifiLastRecordedSsid){
        KnownWifiSet updated=knownWifi;
        if(knownWifiTouch(updated,connected.c_str()) && persistKnownWifi(updated)){knownWifi=updated;knownWifiPersisted=true;}
        wifiLastRecordedSsid=connected;
      }
    }
    return; // A working network is never displaced just because another AP is stronger.
  }
  // Connected uploads returned above. If Wi-Fi is already lost, searching is
  // necessary to resume a cached upload/recognition; waiting for it would deadlock.
  if(!knownWifi.count || recording || captureRequested)return;
  if(wifiAttemptIndex>=0){
    bool authFailure=lastWifiDisconnectReason==202 || lastWifiDisconnectReason==15 || lastWifiDisconnectReason==204;
    bool missing=lastWifiDisconnectReason==201;
    if(now-wifiAttemptAt<15000 && (!(authFailure||missing)||now-wifiAttemptAt<1500))return;
    wifiBlockedUntil[wifiAttemptIndex]=now+(authFailure?300000UL:120000UL);
    wifiAttemptIndex=-1;WiFi.setAutoReconnect(false);(void)esp_wifi_disconnect();wifiNextScanAt=now+500;
  }
  if(wifiAutoPhase==1){
    if(!wifiDeadlineReached(now,wifiAutoScanAt))return;
    WiFi.scanDelete();int result=WiFi.scanNetworks(true,false,false,150);
    if(result==WIFI_SCAN_RUNNING)wifiAutoPhase=2;
    else if(result>=0)wifiAutoPhase=2;
    else{wifiAutoPhase=0;wifiNextScanAt=now+30000;}
    return;
  }
  if(wifiAutoPhase==2){
    int count=WiFi.scanComplete();if(count==WIFI_SCAN_RUNNING){if(now-wifiAutoScanAt<15000)return;esp_wifi_scan_stop();WiFi.scanDelete();wifiAutoPhase=0;wifiNextScanAt=now+30000;return;}
    int best=-1;uint32_t hiddenBlocked[KNOWN_WIFI_LIMIT];memcpy(hiddenBlocked,wifiBlockedUntil,sizeof(hiddenBlocked));
    if(count>=0){
      String names[60];KnownWifiVisible visible[60];unsigned used=0;
      for(int i=0;i<count && i<60;i++){
        names[used]=WiFi.SSID(i);int known=knownWifiFind(knownWifi,names[used].c_str());if(known>=0)hiddenBlocked[known]=now+30000;visible[used]={names[used].c_str(),WiFi.RSSI(i),WiFi.encryptionType(i)!=WIFI_AUTH_OPEN};used++;
      }
      best=knownWifiChoose(knownWifi,visible,used,wifiBlockedUntil,now);
    }
    wifiAutoPhase=0;WiFi.scanDelete();
    // A saved hidden network has no scan-visible SSID; allow a direct attempt too.
    if(best<0)best=knownWifiFallback(knownWifi,hiddenBlocked,now);
    if(best>=0)wifiStartKnown(best);
    else wifiNextScanAt=now+30000;
    return;
  }
  if(!wifiDeadlineReached(now,wifiNextScanAt))return;
  WiFi.setAutoReconnect(false);(void)esp_wifi_disconnect();wifiAutoPhase=1;wifiAutoScanAt=now+400;
}
static int forgetKnownWifi(const String &ssid,JsonDocument &result){
  if(recording||uploadPending||captureRequested||syntheticRequested){result["error"]="请先结束录音或上传，再忘记网络";return 409;}
  if(ssid.length()==0||ssid.length()>32||ssid.length()!=strlen(ssid.c_str())){result["error"]="网络名称无效";return 400;}
  if(wifiManualScan||wifiAutoPhase||wifiScanPhase==1||wifiScanPhase==2){result["error"]="正在扫描，请稍后再忘记网络";return 409;}
  KnownWifiSet updated=knownWifi;
  if(!knownWifiForget(updated,ssid.c_str())){result["error"]="此网络未保存在设备中";return 404;}
  if(!persistKnownWifi(updated)){result["error"]="网络列表保存失败，原配置未更改";return 500;}
  bool disconnect=(WiFi.status()==WL_CONNECTED && WiFi.SSID()==ssid)||wifiAttemptSsid==ssid;
  bool changedPreferred=strcmp(knownWifi.preferred,updated.preferred)!=0;
  knownWifi=updated;knownWifiPersisted=true;syncPreferredWifi();
  Preferences legacy;if(legacy.begin("pocket",false)){
    legacy.putString("ssid",wifiSsid);legacy.putString("pass",wifiPass);
    if(changedPreferred){legacy.remove("ssid_img");ssidBitmapValid=false;}
    legacy.end();
  }
  memset(wifiBlockedUntil,0,sizeof(wifiBlockedUntil));wifiAttemptIndex=-1;
  if(disconnect||WiFi.status()!=WL_CONNECTED){wifiAttemptSsid="";(void)esp_wifi_disconnect();wifiNextScanAt=millis()+500;}
  result["message"]=disconnect?"已忘记此网络，设备会尝试其他已保存网络。":"已从设备中忘记此网络。";
  return 200;
}
