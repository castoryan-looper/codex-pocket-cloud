#include <Arduino.h>
#include <ArduinoJson.h>
#include <OneButton.h>
#include <Preferences.h>
#include <WiFi.h>
#include <WebSocketsClient.h>
#include <mbedtls/base64.h>
#include <FFat.h>
#include <esp_partition.h>
#include <esp_random.h>
#include <esp_wifi.h>
#include "epaper_config.h"
#include "port_i2c.h"
#include "port_display.h"
#include "port_codec.h"
#include "port_adc.h"
#include "src/exio/esp_io_expander_tca9554.h"
#include "offline.h"
#include "connection_icons.h"
#include "ascii_font.h"
#include "local_ui.h"

// All transport sends are serialized. The audio task never calls the WS client.
static SemaphoreHandle_t imageMutex;
static QueueHandle_t netQueue;
class PocketSocket : public WebSocketsClient {
public:
  // One masked WebSocket frame with bounded TLS writes, including large payloads.
  const char *lastSendFailure="";
  size_t lastSendLength=0,lastSendOffset=0;
  bool sendPackedText(const String &text) {
    size_t length=text.length();lastSendFailure="";lastSendLength=length;lastSendOffset=0;
    uint8_t small[512];uint8_t *frame=length+14<=sizeof(small)?small:(uint8_t*)malloc(length+14);
    if(!frame){lastSendFailure="TX NO MEMORY";return false;}
    uint8_t mask[4];esp_fill_random(mask,sizeof(mask));
    uint8_t header=createHeader(frame,WSop_text,length,true,mask,true);
    for(size_t i=0;i<length;i++)frame[header+i]=(uint8_t)text[i]^mask[i%4];
    // Keep TLS writes small for hotspot paths that stall on full-size segments.
    // TCP preserves one continuous WebSocket frame across these writes.
    size_t total=header+length,offset=0;
    while(offset<total) {
      size_t count=min((size_t)512,total-offset);
      size_t written=write(&_client,frame+offset,count);
      if(written!=count)break;
      offset+=written;
    }
    lastSendOffset=offset;if(offset!=total)lastSendFailure="TLS SHORT WRITE";
    if(frame!=small)free(frame);return offset==total;
  }
  void logTransport(const char *stage) {
    char detail[160]={0};
    int code=_client.ssl ? _client.ssl->lastError(detail,sizeof(detail)) : 0;
    Serial.printf("NET %s tls=%d detail=%s heap=%u largest=%u wifi=%d rssi=%d\n",stage,code,detail,
      ESP.getFreeHeap(),ESP.getMaxAllocHeap(),(int)WiFi.status(),WiFi.RSSI());
    Serial.printf("NET options nodelay=%d ip=%s cpu=%u\n",_client.tcp ? _client.tcp->getNoDelay() : -1,WiFi.localIP().toString().c_str(),getCpuFrequencyMhz());
  }
};
static PocketSocket socketClient;
static Preferences prefs;
static OneButton bootButton, powerButton;
static esp_io_expander_handle_t exio;
static String wifiSsid, wifiPass, remoteHost, remotePath, remoteToken, rootCa;
static bool tlsEnabled = true, wifiConfigured = false, bridgeConfigured = false;
static volatile bool netConnected=false;
static volatile bool cloudReconnectRequested=false;
static volatile int lastWifiDisconnectReason=0;
static uint16_t remotePort = 443;
static volatile bool imageDirty = false;
static volatile bool recording = false, captureRequested = false, captureCancelled = false;
// Local states always take priority over delayed server bitmaps.
enum LocalUi { UiIdle=-1, UiHold=0, UiRecording=1, UiSending=2, UiRecognizing=3,
  UiTooShort=4, UiAudioError=5, UiCancelled=6, UiLimit=7 };
static volatile int localUi=UiIdle;
static volatile uint32_t localUiAt=0, pressStartedAt=0, recordingStartedAt=0;
static volatile uint32_t audioBytes=0;
static volatile bool audioEndSent=false, remoteVoiceBusy=false, remoteDraftReady=false;
static int localBattery=-1;
static const char * volatile audioFailure="";
static volatile int codecFailure=0;
static const char * volatile enqueueFailure="";
static volatile uint32_t networkProgressAt=0,networkPackets=0;
static bool audioStoreReady=false;
static volatile bool uploadPending=false;
static volatile bool remoteMulaw8k=false,remoteAutoVoice=false;
static volatile bool remoteRecommendationUi=false,remoteRecommendationReader=false;
static uint8_t ssidBitmap[350];
static bool ssidBitmapValid=false;
static volatile unsigned uploadRetries=0;
static volatile bool syntheticRequested=false,syntheticUpload=false;
static volatile unsigned uploadProgress=0;
static volatile bool transportProbeRequested=false;
static uint32_t uploadRequestedAt=0;
static const char *audioCachePath="/pocket-recording.pcm";
// Never format an existing filesystem. Only initialize an entirely erased FAT partition.
static bool initAudioStore() {
  const esp_partition_t *part=esp_partition_find_first(ESP_PARTITION_TYPE_DATA,ESP_PARTITION_SUBTYPE_DATA_FAT,"ffat");
  if(!part)return false;
  // wl_mount can initialize management sectors even when FAT mounting fails.
  // Inspect raw flash BEFORE any mount attempt, otherwise a virgin partition
  // is misclassified as nonblank by our own first mount attempt.
  bool erased=true;uint8_t block[1024];
  for(size_t offset=0;erased && offset<part->size;offset+=sizeof(block)) {
    size_t count=min(sizeof(block),(size_t)part->size-offset);
    if(esp_partition_read(part,offset,block,count)!=ESP_OK)return false;
    for(size_t i=0;i<count;i++)if(block[i]!=0xff) {erased=false;break;}
  }
  bool mounted=FFat.begin(erased);
  Serial.printf("AUDIO STORE partition=%s erased=%d mounted=%d\n",part->label,erased,mounted);
  if(!mounted)return false;
  const uint8_t pattern[]={0x50,0x54,0x54,0,0xff,0x55,0xaa,0x34};
  uint8_t verify[sizeof(pattern)]={0};
  File probe=FFat.open(audioCachePath,FILE_WRITE);
  bool writable=probe && probe.write(pattern,sizeof(pattern))==sizeof(pattern);
  if(probe)probe.close();
  probe=FFat.open(audioCachePath,FILE_READ);
  bool readable=probe && probe.read(verify,sizeof(verify))==sizeof(verify) && memcmp(pattern,verify,sizeof(pattern))==0;
  if(probe)probe.close();FFat.remove(audioCachePath);
  Serial.printf("AUDIO STORE CHECK write=%d read=%d free=%u\n",writable,readable,(unsigned)FFat.freeBytes());
  return writable && readable;
}
static volatile int lastLocalKey=0;
static volatile uint32_t lastLocalKeyAt=0;
static void soundEnqueue(uint8_t cue);
static void setLocalUi(int state) { if(state!=localUi){if(state==UiAudioError||state==UiTooShort)soundEnqueue(6);else if(state==UiCancelled)soundEnqueue(5);}localUi=state; localUiAt=millis(); imageDirty=true; }
static uint8_t desiredImage[5000], lastImage[5000];
static String pendingHash;
static uint32_t lastContact = 0, lastTelemetry = 0;
static volatile bool online = false;
static volatile int screenPhase = 0;
static volatile uint32_t fullRefreshes = 0, partialRefreshes = 0;
static uint32_t lastAudioEnd = 0;
static int offlineStage = -1;
#include "wifi_profiles.h"
#include "device_location.h"
#include "bluetooth_knob.h"
#include "sound_feedback.h"
#include "config_portal.h"
#include "config_screens.h"

void imageAscii(uint8_t *image,int px,int py,const String &text) {
  for(unsigned n=0;n<text.length();n++) {
    unsigned char c=text[n];if(c<32||c>126)c='?';
    for(int y=0;y<12;y++)for(int x=0;x<7;x++) {
      int xx=px+n*7+x,yy=py+y;if(xx>=200||yy>=200)continue;
      if(asciiFont[c-32][y]&(0x40>>x))image[yy*25+xx/8]&=~(0x80>>(xx%8));
    }
  }
}


void imageAsciiLarge(uint8_t *image,int px,int py,const String &text) {
  for(unsigned n=0;n<text.length();n++) {
    unsigned char c=text[n];if(c<32||c>126)c='?';
    for(int y=0;y<12;y++) for(int x=0;x<7;x++) if(asciiFont[c-32][y]&(0x40>>x)) {
      for(int dy=0;dy<2;dy++) for(int dx=0;dx<2;dx++) {
        int xx=px+n*14+x*2+dx,yy=py+y*2+dy;
        if(xx>=0 && xx<200 && yy>=0 && yy<200) image[yy*25+xx/8]&=~(0x80>>(xx%8));
      }
    }
  }
}

bool sendPacket(const JsonDocument &doc) {
  String out;
  serializeJson(doc, out);
  // Runtime commands, voice, frames and status use Wi-Fi only.
  // USB carries configuration/status only; runtime traffic stays on Wi-Fi.
  if (netConnected) {
    char *copy = strdup(out.c_str());
    if (!copy) {enqueueFailure="TX NO MEMORY";return false;}
    if (xQueueSend(netQueue, &copy, pdMS_TO_TICKS(doc["type"]=="audio_end" ? 500 : 10)) == pdTRUE) return true;
    free(copy);
    enqueueFailure="TX QUEUE FULL";
  } else enqueueFailure="CLOUD DISCONNECTED";
  return false;
}

void event(const char *action) {
  if(!strcmp(action,"knob_click")||!strcmp(action,"b1_click"))soundEnqueue(SoundConfirm);
  JsonDocument doc;
  doc["type"] = "event";
  doc["action"] = action;
  if(sendPacket(doc)) {
    if(strcmp(action,"b1_click")==0)lastLocalKey=1;
    else if(strcmp(action,"b2_click")==0)lastLocalKey=2;
    lastLocalKeyAt=millis();imageDirty=true;
  }
}

void telemetry(bool hello = false) {
  JsonDocument doc;
  doc["type"] = hello ? "hello" : "telemetry";
  doc["firmware"] = "codex-pocket-0.11.1";
  doc["wifiConnected"] = WiFi.status()==WL_CONNECTED;
  doc["usbConnected"] = HWCDC::isPlugged();
  float volts = Get_VbatVoltage();
  doc["millivolts"] = (int)(volts * 1000);
  // Voltage estimate, not a calibrated fuel gauge; disconnected battery is unknown.
  if (volts > 2.8 && volts < 4.5) doc["battery"] = Get_Batterylevel();
  else doc["battery"] = nullptr;
  int battery=(volts>2.8 && volts<4.5) ? Get_Batterylevel() : -1;
  if(localBattery<0 || battery<0 || abs(localBattery-battery)>=2) {localBattery=battery;imageDirty=true;}
  doc["recording"]=(bool)recording;
  doc["localUi"]=(int)localUi;
  doc["audioBytes"]=(uint32_t)audioBytes;
  doc["holdMs"]=holdMs;
  knobStatusJson(doc);
  doc["rssi"] = WiFi.status() == WL_CONNECTED ? WiFi.RSSI() : 0;
  doc["displayBusy"] = gpio_get_level(EPD_BUSY_PIN);
  doc["displayTimeouts"] = EPD_GetBusyTimeouts();
  doc["screenPhase"] = screenPhase;
  doc["fullRefreshes"] = fullRefreshes;
  doc["partialRefreshes"] = partialRefreshes;
  doc["configurationMode"] = (bool)configModeActive;
  doc["configurationUrl"] = HWCDC::isPlugged()?"https://43.160.215.253:8448/setup/":portalActive?"http://"+WiFi.softAPIP().toString()+":80/":"http://"+WiFi.localIP().toString()+":80/";
  deviceLocationStatus(doc);
  doc["configurationSsid"] = portalSsid;
  sendPacket(doc);
}

void disconnectedImage() {
  int stage=!wifiConfigured ? 0 : WiFi.status()!=WL_CONNECTED ? 1 : !bridgeConfigured ? 5 : !netConnected ? 2 : 3;
  if (stage==offlineStage) return;
  offlineStage=stage;
  xSemaphoreTake(imageMutex, portMAX_DELAY);
  memcpy(desiredImage, offlineImages[stage], 5000);
  pendingHash = "offline";
  imageDirty = true;
  xSemaphoreGive(imageMutex);
}

void packet(const JsonDocument &doc) {
  String type = doc["type"] | "";
  if (type == "frame") {
    const char *data = doc["data"] | "";
    uint8_t decoded[5000];
    size_t length = 0;
    if (mbedtls_base64_decode(decoded, sizeof(decoded), &length,
        (const unsigned char *)data, strlen(data)) != 0 || length != 5000) return;
    xSemaphoreTake(imageMutex, portMAX_DELAY);
    memcpy(desiredImage, decoded, 5000);
    pendingHash = doc["hash"] | "";
    imageDirty = true;
    xSemaphoreGive(imageMutex);
    lastContact = millis();
    online = true;
    remoteVoiceBusy=doc["voiceBusy"]|false;
    remoteDraftReady=doc["draftReady"]|false;
    remoteAutoVoice=doc["voiceAutoSubmit"]|false;
    String recommendationView=doc["recommendationView"]|"";
    remoteRecommendationUi=recommendationView.length()>0;remoteRecommendationReader=recommendationView=="reader";
    remoteMulaw8k=false;
    for(JsonVariantConst codec : doc["audioCodecs"].as<JsonArrayConst>())
      if(String(codec.as<const char*>())=="mulaw8k")remoteMulaw8k=true;
    String state=doc["state"]|"";
    soundRemoteState(state);
    // Ignore delayed cloud "recording" screens until local capture/upload is over.
    if(audioEndSent && localUi==UiRecognizing) {
      bool finalState=remoteVoiceBusy || remoteDraftReady || (remoteAutoVoice && (state=="正在提交" || state=="工作中" || state=="已完成")) || state=="识别语音" || state=="待发送" || state=="语音失败" || state=="录音失败" || state=="录音太短";
      bool oldGateway=state.length()==0 && millis()-localUiAt>1500;
      if(finalState || oldGateway) setLocalUi(UiIdle);
    }
    offlineStage=-1;
  }
}

void screenTask(void *) {
  screenPhase = 1;
  PortLvgl_Start_Init();
  screenPhase = 2;
  memset(lastImage, 0, sizeof(lastImage));
  uint8_t image[5000];
  unsigned refreshCount = 0;
  uint32_t lastFullRefresh = millis();
  int previousWifi=-1,previousUsb=-1,previousPortal=-1,previousCloud=-1,previousUi=-99,previousSecond=-1,previousProgress=-1,previousKey=0;
  String previousSsid;
  for (;;) {
    bool wifiConnected=WiFi.status()==WL_CONNECTED;
    String connectedSsid=wifiConnected?WiFi.SSID():"";
    bool usbConnected=HWCDC::isPlugged();
    int cloud=wifiConnected && netConnected && online;
    int ui=localUi;
    if(!configModeActive && !recording && !uploadPending) {
      if(!wifiConnected) ui=8;
      else if(!bridgeConfigured) ui=10;
      else if(!netConnected) ui=9;
      else if(!online) ui=11;
    }
    int key=(millis()-lastLocalKeyAt<500) ? lastLocalKey : 0;
    int second=recording ? (millis()-recordingStartedAt)/1000 : -1;
    int progress=ui==UiHold ? min(4,(int)((millis()-pressStartedAt)/250)) : ui==UiSending ? (int)(uploadProgress/10) : -1;
    if (previousWifi!=wifiConnected || previousUsb!=usbConnected || previousPortal!=(int)configModeActive || previousCloud!=cloud || previousUi!=ui || previousSecond!=second || previousProgress!=progress || previousKey!=key || previousSsid!=connectedSsid) imageDirty=true;
    if (imageDirty) {
      xSemaphoreTake(imageMutex, portMAX_DELAY);
      memcpy(image, desiredImage, 5000);
      String hash = pendingHash;
      imageDirty = false;
      xSemaphoreGive(imageMutex);
      previousWifi=wifiConnected; previousUsb=usbConnected;
      previousPortal=configModeActive;previousCloud=cloud;previousUi=ui;previousSecond=second;previousProgress=progress;previousKey=key;previousSsid=connectedSsid;
      if(!configModeActive) {
        if(ui>=0 && ui<12) memcpy(image,ui==UiRecognizing && remoteAutoVoice ? localAutoRecognizing : localScreens[ui],5000);
        else if(!remoteRecommendationUi) memcpy(image+186*25,key==1?localFooterBoot:key==2?localFooterPower:localFooter,sizeof(localFooter));
        if(ui==UiHold) {
          for(int y=148;y<154;y++) for(int x=14;x<14+progress*43;x++) image[y*25+x/8]&=~(0x80>>(x%8));
        }
        if(ui==UiRecording || ui==UiLimit) {
          char timer[24];unsigned seconds=ui==UiLimit ? recordSeconds : (millis()-recordingStartedAt)/1000;
          snprintf(timer,sizeof(timer),"%02u:%02u / %us",seconds/60,seconds%60,recordSeconds);
          imageAsciiLarge(image,23,103,timer);
        }
        if(ui==UiSending) {
          imageAsciiLarge(image,65,110,String(uploadProgress)+"%");
          if(uploadRetries)imageAscii(image,52,143,"RETRY "+String(uploadRetries)+"/3");
        }
        if(ui==UiAudioError) {
          imageAscii(image,8,108,audioFailure);
          char diagnostic[28];snprintf(diagnostic,sizeof(diagnostic),"MIC=%d Q=%u",codecFailure,(unsigned)uxQueueMessagesWaiting(netQueue));
          imageAscii(image,8,129,diagnostic);
        }
        if(ui==9 || ui==11) imageAscii(image,8,118,remoteHost+":"+String(remotePort));
        const uint8_t *header=cloud ? localHeaderOnline : netConnected ? localHeaderConnecting : localHeaderOffline;
        for(int y=0;y<25;y++) memcpy(image+y*25,header+y*10,10);
        for(int y=0;y<24;y++) for(int x=139;x<200;x++) image[y*25+x/8]|=0x80>>(x%8);
        imageAscii(image,145,5,localBattery<0 ? "--%" : String(localBattery)+"%");
      }
      if(!configModeActive && wifiConnected && ((ui==UiIdle && !remoteRecommendationUi) || ui==9 || ui==11)) {
        // Show the actual SSID even while waiting for the cloud to reconnect.
        int top=ui==UiIdle?186:145;
        for(int y=top;y<top+14;y++)memset(image+y*25,0xff,25);
        if(ssidBitmapValid && connectedSsid==wifiSsid)memcpy(image+top*25,ssidBitmap,sizeof(ssidBitmap));
        else {
          String name=connectedSsid;
          if(name.length()>22)name=name.substring(0,21)+"~";
          imageAscii(image,4,top+1,"WiFi:"+name);
        }
      }
      if(configModeActive) {
        memcpy(image,usbConnected?configUsbScreen:configApScreen,5000);
        if(usbConnected){
          imageAscii(image,8,94,"https://43.160.215.253");
          imageAscii(image,8,110,":8448/setup/");
        }
        else if(portalActive){
          imageAscii(image,8,92,portalSsid);
          imageAscii(image,8,113,"KEY: "+portalKey);
          imageAscii(image,8,137,"http://"+WiFi.softAPIP().toString()+":80/");
        }else imageAscii(image,8,99,"AP STARTING...");
      }
      // This strip is owned by the MCU so it remains current without a bridge.
      for (int y=1;y<24;y++) for (int x=76;x<142;x++) image[y*25+x/8]|=0x80>>(x%8);
      const uint16_t *icons[3]={wifiConnected ? icon_wifi_on : icon_wifi_off,usbConnected ? icon_usb_on : icon_usb_off,knobConnected ? icon_bt_on : icon_bt_off};
      for (int i=0;i<3;i++) for (int y=0;y<16;y++) for (int x=0;x<16;x++) {
        int px=80+i*22+x,py=4+y;
        if (icons[i][y] & (0x8000>>x)) image[py*25+px/8]&=~(0x80>>(px%8));
      }
      if (memcmp(lastImage, image, 5000) != 0) {
        screenPhase = 3;
        for (int y=0; y<200; y++) for (int x=0; x<200; x++)
          EPD_DrawColorPixel(x, y, (image[y*25+x/8] & (0x80>>(x%8))) ? 0xff : 0);
        ++refreshCount;
        if (refreshCount >= 80 && millis()-lastFullRefresh >= refreshMinutes*60*1000UL && localUi==UiIdle && !captureRequested && !recording && millis()-lastAudioEnd > 30000) {
          EPD_Init();
          EPD_DisplayPartBaseImage();
          EPD_Init_Partial();
          fullRefreshes++;
          refreshCount=0;
          lastFullRefresh=millis();
        } else { EPD_DisplayPart(); partialRefreshes++; }
        memcpy(lastImage, image, 5000);
      }
      screenPhase = 4;
      if (EPD_GetBusyTimeouts()) continue;
      JsonDocument ack;
      ack["type"] = "frame_ack";
      ack["hash"] = ui>=0 ? "local-"+String(ui) : hash;
      ack["localUi"]=ui;ack["recording"]=(bool)recording;
      ack["wifiConnected"]=wifiConnected;
      ack["usbConnected"]=usbConnected;
      ack["configurationMode"]=(bool)configModeActive;
      sendPacket(ack);
    }
    vTaskDelay(pdMS_TO_TICKS(20));
  }
}

// Capture never waits for TLS or the upload queue. The reserved FAT partition
// holds at most one 20-second PCM clip; remove it after upload/cancel and on boot.
void audioTask(void *) {
  int16_t stereo[960],mono[480];
  for (;;) {
    if(syntheticRequested) {
      syntheticRequested=false;
      if(!audioStoreReady || uploadPending || recording || !netConnected || !online)continue;
      File test=FFat.open(audioCachePath,FILE_WRITE);
      uint8_t silence[512]={0};bool ok=(bool)test;
      for(int n=0;ok && n<250;n++) {ok=test.write(silence,sizeof(silence))==sizeof(silence);vTaskDelay(1);}
      if(test)test.close();
      if(!ok){FFat.remove(audioCachePath);audioFailure="AUDIO WRITE FAILED";setLocalUi(UiAudioError);continue;}
      captureCancelled=false;audioEndSent=false;audioBytes=128000;uploadProgress=0;uploadRetries=0;audioFailure="";syntheticUpload=true;
      uploadRequestedAt=millis();setLocalUi(UiSending);uploadPending=true;continue;
    }
    if(!captureRequested) {soundIdleTick();vTaskDelay(pdMS_TO_TICKS(5));continue;}
    captureRequested=false;
    if(uploadPending || digitalRead(BOOT_BUTTON_PIN)!=LOW || digitalRead(PWR_BUTTON_PIN)==LOW || configModeActive || !netConnected || !online)continue;
    captureCancelled=false;audioEndSent=false;audioBytes=0;uploadProgress=0;uploadRetries=0;syntheticUpload=false;audioFailure="";codecFailure=0;
    File clip=audioStoreReady ? FFat.open(audioCachePath,FILE_WRITE) : File();
    if(!clip || FFat.freeBytes()<recordSeconds*32000UL) {
      if(clip)clip.close();audioFailure="AUDIO STORAGE FAILED";setLocalUi(UiAudioError);continue;
    }
    if(soundQueue)xQueueReset(soundQueue);
    // Play before capture, then discard buffered microphone audio containing it.
    if(soundEnabled&&soundVolume){soundPlay(SoundRecordStart);for(unsigned flush=0;flush<3;flush++)Codec_RecordData((uint8_t*)stereo,sizeof(stereo));}
    if(captureCancelled||digitalRead(BOOT_BUTTON_PIN)!=LOW||digitalRead(PWR_BUTTON_PIN)==LOW||configModeActive){clip.close();FFat.remove(audioCachePath);continue;}
    recording=true;recordingStartedAt=millis();setLocalUi(UiRecording);
    Serial.printf("PTT START %lu mode=local-cache\n",(unsigned long)recordingStartedAt);
    uint32_t began=millis(),captured=0;bool failed=false;
    while(recording && digitalRead(BOOT_BUTTON_PIN)==LOW && digitalRead(PWR_BUTTON_PIN)!=LOW && millis()-began<recordSeconds*1000UL && captured+sizeof(mono)<=recordSeconds*32000UL) {
      int result=Codec_RecordData((uint8_t*)stereo,sizeof(stereo));
      if(result!=ESP_OK) {codecFailure=result;audioFailure="MIC READ FAILED";failed=true;break;}
      if(!recording || digitalRead(BOOT_BUTTON_PIN)!=LOW || digitalRead(PWR_BUTTON_PIN)==LOW)break;
      for(int i=0;i<480;i++)mono[i]=stereo[2*i];
      if(clip.write((uint8_t*)mono,sizeof(mono))!=sizeof(mono)) {audioFailure="AUDIO WRITE FAILED";failed=true;break;}
      captured+=sizeof(mono);audioBytes=captured;
    }
    bool limit=millis()-began>=recordSeconds*1000UL || captured+sizeof(mono)>recordSeconds*32000UL;
    if(digitalRead(PWR_BUTTON_PIN)==LOW || configModeActive)captureCancelled=true;
    clip.close();recording=false;lastAudioEnd=millis();
    Serial.printf("PTT STOP %lu bytes=%lu error=%d cancelled=%d reason=%s heap=%u\n",(unsigned long)lastAudioEnd,(unsigned long)captured,failed,(int)captureCancelled,audioFailure,ESP.getFreeHeap());
    if(captureCancelled || failed || captured<6400) {
      FFat.remove(audioCachePath);
      setLocalUi(captureCancelled?UiCancelled:failed?UiAudioError:UiTooShort);
    } else {
      uploadRequestedAt=millis();setLocalUi(limit && digitalRead(BOOT_BUTTON_PIN)==LOW ? UiLimit : UiSending);
      uploadPending=true;soundEnqueue(SoundRecordEnd);
    }
  }
}

// 16-bit linear PCM to 8-bit G.711 mu-law, used after averaging sample pairs.
static uint8_t muLawEncode(int32_t sample) {
  int mask=0xff;
  if(sample<0){sample=132-sample;mask=0x7f;}else sample+=132;
  sample=min(sample,(int32_t)32767);
  int segment=0;while(segment<7 && sample>((0x100<<segment)-1))segment++;
  return ((segment<<4)|((sample>>(segment+3))&15))^mask;
}

// A failed partial frame is discarded by the gateway when this connection
// closes. Keep the cached clip and start it from the beginning on reconnect.
void retryUpload(File &clip,bool &started,const char *reason) {
  if(clip)clip.close();started=false;
  if(netConnected)socketClient.disconnect();
  netConnected=false;online=false;remoteMulaw8k=false;
  if(++uploadRetries<=3 && millis()-uploadRequestedAt<300000UL && !captureCancelled) {
    uploadProgress=0;audioFailure="";setLocalUi(UiSending);
    Serial.printf("UPLOAD RETRY attempt=%u cause=%s\n",uploadRetries,reason);
  } else {
    FFat.remove(audioCachePath);uploadPending=false;audioFailure=reason;
    setLocalUi(UiAudioError);Serial.printf("UPLOAD FAILED retries=%u cause=%s\n",uploadRetries,reason);
  }
}

void uploadTick() {
  static File clip;static bool started=false;static bool compressed=false;
  static uint32_t uploaded=0,wireBytes=0;
  if(!uploadPending)return;
  bool expired=millis()-uploadRequestedAt>300000UL;
  if(captureCancelled || expired) {
    if(started && netConnected)socketClient.sendTXT("{\"type\":\"audio_end\",\"cancelled\":true,\"error\":\"upload_cancelled\"}");
    if(clip)clip.close();FFat.remove(audioCachePath);started=false;uploadPending=false;
    if(!captureCancelled)audioFailure="UPLOAD TIMEOUT";
    setLocalUi(captureCancelled?UiCancelled:UiAudioError);return;
  }
  if(started && !netConnected){retryUpload(clip,started,"CLOUD DISCONNECTED");return;}
  if(!netConnected || !online)return;
  bool sent=false;
  if(!started) {
    clip=FFat.open(audioCachePath,FILE_READ);uploaded=0;wireBytes=0;
    if(!clip || clip.size()!=audioBytes) {if(clip)clip.close();FFat.remove(audioCachePath);audioFailure="AUDIO SIZE MISMATCH";uploadPending=false;setLocalUi(UiAudioError);return;}
    compressed=remoteMulaw8k;
    JsonDocument start;start["type"]="audio_start";start["maxSeconds"]=recordSeconds;
    if(compressed)start["codec"]="mulaw8k";
    String json;serializeJson(start,json);sent=socketClient.sendTXT(json);started=sent;
    Serial.printf("UPLOAD START at=%lu pcm=%lu codec=%s retry=%u test=%d\n",(unsigned long)millis(),(unsigned long)audioBytes,compressed?"mulaw8k":"pcm16",uploadRetries,(int)syntheticUpload);
  } else if(clip.available()) {
    // Keep each complete masked WebSocket frame below one 512-byte TLS write.
    // Smaller static buffers also leave heap headroom for BLE + TLS + display frames.
    static uint8_t pcm[960],encodedAudio[240];static unsigned char encoded[385];
    int count=clip.read(pcm,compressed?sizeof(pcm):288);size_t length=0;
    if(count<=0 || (compressed && count%4)) {audioFailure="AUDIO READ FAILED";}
    else {
      size_t wireSize=count;const uint8_t *wire=pcm;
      if(compressed) {
        wireSize=count/4;wire=encodedAudio;
        for(size_t i=0;i<wireSize;i++) {
          int16_t first=(int16_t)((uint16_t)pcm[i*4]|((uint16_t)pcm[i*4+1]<<8));
          int16_t second=(int16_t)((uint16_t)pcm[i*4+2]|((uint16_t)pcm[i*4+3]<<8));
          encodedAudio[i]=muLawEncode(((int32_t)first+(int32_t)second)/2);
        }
      }
      if(mbedtls_base64_encode(encoded,sizeof(encoded),&length,wire,wireSize)!=0){audioFailure="AUDIO ENCODE FAILED";}
      else {
        encoded[length]=0;JsonDocument chunk;chunk["type"]="audio_chunk";chunk["data"]=(char*)encoded;
        String json;serializeJson(chunk,json);uint32_t before=millis();sent=socketClient.sendPackedText(json);
        if(millis()-before>500)Serial.printf("UPLOAD SLOW ms=%lu offset=%lu\n",(unsigned long)(millis()-before),(unsigned long)uploaded);
        if(sent){uploaded+=count;wireBytes+=wireSize;uploadProgress=audioBytes?min(100UL,uploaded*100UL/audioBytes):0;}
      }
    }
  } else {
    JsonDocument end;end["type"]="audio_end";end["bytes"]=uploaded;
    if(syntheticUpload){end["cancelled"]=true;end["error"]="diagnostic_cancelled";}
    String json;serializeJson(end,json);sent=socketClient.sendTXT(json);
    if(sent){
      clip.close();FFat.remove(audioCachePath);started=false;uploadPending=false;
      Serial.printf("UPLOAD END pcm=%lu encoded=%lu ms=%lu retry=%u test=%d\n",(unsigned long)uploaded,(unsigned long)wireBytes,(unsigned long)(millis()-uploadRequestedAt),uploadRetries,(int)syntheticUpload);
      audioEndSent=true;if(localUi==UiSending)setLocalUi(syntheticUpload?UiIdle:UiRecognizing);
    }
  }
  if(!sent){
    socketClient.logTransport("upload-failed");
    if(audioFailure[0]){if(clip)clip.close();FFat.remove(audioCachePath);started=false;uploadPending=false;setLocalUi(UiAudioError);socketClient.disconnect();}
    else{Serial.printf("UPLOAD TX FAILED cause=%s length=%u offset=%u\n",socketClient.lastSendFailure,(unsigned)socketClient.lastSendLength,(unsigned)socketClient.lastSendOffset);retryUpload(clip,started,"CLOUD SEND FAILED");}
  }
}

// Diagnostic traffic has no audio_start/end and cannot create an AI task.
void transportProbe() {
  transportProbeRequested=false;
  if(recording || uploadPending || !netConnected) {Serial.println("PROBE BUSY/OFFLINE");return;}
  socketClient.logTransport("probe-start");
  const unsigned sizes[]={512,1312,8192};
  for(unsigned size:sizes) {
    String message="{\"type\":\"transport_probe\",\"pad\":\"";
    message.reserve(size+64);while(message.length()<size-2)message+='x';message+="\"}";
    uint32_t began=millis();unsigned sent=0;
    for(int i=0;i<4 && netConnected;i++) {
      if(!socketClient.sendPackedText(message))break;
      sent+=message.length();socketClient.loop();vTaskDelay(pdMS_TO_TICKS(2));
    }
    Serial.printf("PROBE size=%u bytes=%u ms=%lu connected=%d\n",size,sent,(unsigned long)(millis()-began),(int)netConnected);
  }
  socketClient.logTransport("probe-end");
}

// TLS handshakes and Wi-Fi socket writes must not block physical button sampling.
void networkTask(void *) {
  for(;;) {
    if(cloudReconnectRequested&&!recording&&!uploadPending&&!captureRequested){cloudReconnectRequested=false;socketClient.disconnect();}
    if(bridgeConfigured && wifiConfigured && WiFi.status()==WL_CONNECTED) socketClient.loop();
    networkProgressAt=millis();
    char *queued=nullptr;
    for(int i=0;i<8 && xQueueReceive(netQueue,&queued,0)==pdTRUE;i++) {
      if(netConnected) {
        bool isEnd=strstr(queued,"\"audio_end\"")!=nullptr;
        bool sent=socketClient.sendTXT(queued);
        if(sent)networkPackets++;
        networkProgressAt=millis();
        if(isEnd && sent) {
          audioEndSent=true;
          if(localUi==UiSending) setLocalUi(UiRecognizing);
        } else if(!sent) {
          // Telemetry/ack failure cannot cancel microphone capture or a cached clip.
          socketClient.logTransport("control-send-failed");
        }
      }
      free(queued);
    }
    if(transportProbeRequested)transportProbe();
    uploadTick();
    if(millis()-lastTelemetry>5000) {lastTelemetry=millis();telemetry();}
    vTaskDelay(pdMS_TO_TICKS(2));
  }
}

void setupNetwork() {
  prefs.begin("pocket", true);
  wifiSsid=prefs.getString("ssid", ""); wifiPass=prefs.getString("pass", "");
  remoteHost=prefs.getString("host", ""); remotePath=prefs.getString("path", "/device");
  remoteToken=prefs.getString("token", ""); rootCa=normalizeRootCa(prefs.getString("ca", ""));
  tlsEnabled=prefs.getBool("tls", true); remotePort=prefs.getUInt("port", 443);
  prefs.end();
  loadKnownWifi();
  bridgeConfigured = remoteHost.length() && remoteToken.length() && (!tlsEnabled || rootCa.length());
  if (!wifiConfigured) return;
  WiFi.onEvent([](WiFiEvent_t event,WiFiEventInfo_t info){
    if(event==ARDUINO_EVENT_WIFI_STA_DISCONNECTED){lastWifiDisconnectReason=info.wifi_sta_disconnected.reason;netConnected=false;online=false;remoteMulaw8k=false;Serial.printf("WIFI DISCONNECT reason=%d\n",(int)lastWifiDisconnectReason);imageDirty=true;}
    else if(event==ARDUINO_EVENT_WIFI_STA_GOT_IP)lastWifiDisconnectReason=0;
  });
  WiFi.mode(WIFI_STA);
  WiFi.persistent(false);
  WiFi.setAutoReconnect(false);
  WiFi.setSleep(false);
  wifiStartKnown(knownWifiFind(knownWifi,knownWifi.preferred));
  configTime(0, 0, "pool.ntp.org", "time.cloudflare.com");
  if(!bridgeConfigured)return;
  socketClient.onEvent([](WStype_t type, uint8_t *payload, size_t length) {
    if (type == WStype_CONNECTED) {
      netConnected = true;
      JsonDocument hello;
      hello["type"]="hello"; hello["token"]=remoteToken;
      hello["firmware"]="codex-pocket-0.11.1";
      hello["wifiConnected"]=WiFi.status()==WL_CONNECTED;
      hello["usbConnected"]=HWCDC::isPlugged();
      deviceLocationStatus(hello);
      String out; serializeJson(hello, out); socketClient.sendTXT(out);
    } else if (type == WStype_DISCONNECTED) {
      Serial.printf("NET DISCONNECT reason=%.*s recording=%d bytes=%lu queue=%u\n",(int)length,payload ? (char*)payload : "",(int)recording,(unsigned long)audioBytes,(unsigned)uxQueueMessagesWaiting(netQueue));
      socketClient.logTransport("disconnected");
      // A transient cloud disconnect must not stop local microphone capture.
      netConnected = false;
      online = false;remoteMulaw8k=false;remoteAutoVoice=false;
    } else if (type == WStype_ERROR) {
      Serial.printf("NET ERROR %.*s\n",(int)length,payload ? (char*)payload : "");
      socketClient.logTransport("error");
    } else if (type == WStype_TEXT && length < 15000) {
      JsonDocument msg;
      if (!deserializeJson(msg, payload, length) && String(msg["type"] | "") == "frame")
        packet(msg);
    }
  });
  if (tlsEnabled) socketClient.beginSslWithCA(remoteHost.c_str(), remotePort, remotePath.c_str(), rootCa.c_str());
  else socketClient.begin(remoteHost.c_str(), remotePort, remotePath.c_str());
  socketClient.setReconnectInterval(5000);
  socketClient.enableHeartbeat(15000, 3000, 2);
}

void setup() {
  Serial.begin(115200);
  Serial.setTxTimeoutMs(100);
  uint32_t serialWait=millis();
  while (!Serial && millis()-serialWait<3000) delay(10);
  Serial.println("POCKET_BOOT: initializing hardware");
  imageMutex=xSemaphoreCreateMutex();
  netQueue=xQueueCreate(64, sizeof(char *));
  initDeviceLocation();
  for (gpio_num_t pin : {EPD_DC_PIN,EPD_CS_PIN,EPD_SCK_PIN,EPD_MOSI_PIN,EPD_RST_PIN,EPD_BUSY_PIN,BOOT_BUTTON_PIN,PWR_BUTTON_PIN})
    gpio_hold_dis(pin);
  gpio_hold_dis(ESP32_I2C_SDA_PIN);
  gpio_hold_dis(ESP32_I2C_SCL_PIN);
  gpio_reset_pin(ESP32_I2C_SDA_PIN);
  gpio_reset_pin(ESP32_I2C_SCL_PIN);
  // Resetting the CPU during a slave transfer can leave SDA held low. Recover
  // the bus with clocks and a STOP before handing pins to the I2C peripheral.
  pinMode(ESP32_I2C_SDA_PIN,OUTPUT_OPEN_DRAIN | PULLUP);
  pinMode(ESP32_I2C_SCL_PIN,OUTPUT_OPEN_DRAIN | PULLUP);
  digitalWrite(ESP32_I2C_SDA_PIN,HIGH);
  digitalWrite(ESP32_I2C_SCL_PIN,HIGH);
  delay(5);
  Serial.printf("I2C before recovery: SDA=%d SCL=%d\n",digitalRead(ESP32_I2C_SDA_PIN),digitalRead(ESP32_I2C_SCL_PIN));
  for (int pulse=0;pulse<18;pulse++) {
    digitalWrite(ESP32_I2C_SCL_PIN,LOW); delayMicroseconds(10);
    digitalWrite(ESP32_I2C_SCL_PIN,HIGH); delayMicroseconds(10);
  }
  digitalWrite(ESP32_I2C_SDA_PIN,LOW); delayMicroseconds(10);
  digitalWrite(ESP32_I2C_SCL_PIN,HIGH); delayMicroseconds(10);
  digitalWrite(ESP32_I2C_SDA_PIN,HIGH); delayMicroseconds(10);
  Serial.printf("I2C after recovery: SDA=%d SCL=%d\n",digitalRead(ESP32_I2C_SDA_PIN),digitalRead(ESP32_I2C_SCL_PIN));
  pinMode(ESP32_I2C_SDA_PIN,INPUT_PULLUP);
  pinMode(ESP32_I2C_SCL_PIN,INPUT_PULLUP);
  Serial.printf("I2C input levels: SDA=%d SCL=%d\n",gpio_get_level(ESP32_I2C_SDA_PIN),gpio_get_level(ESP32_I2C_SCL_PIN));
  auto bus=I2cMasterBus::requestInstance(ESP32_I2C_SCL_PIN, ESP32_I2C_SDA_PIN, ESP32_I2C_DEV_NUM);
  delay(250);
  uint8_t expanderAddress=0;
  for (int address=8; address<120; address++) {
    if (i2c_master_probe(bus->Get_I2cBusHandle(),address,40)==ESP_OK) {
      Serial.printf("I2C device: 0x%02x\n",address);
      if (address==0x20 || address==0x38) expanderAddress=address;
    }
  }
  if (!expanderAddress) {
    while (true) {
      Serial.println("POCKET_HARDWARE_ERROR: no power expander on SDA18/SCL8");
      delay(2000);
    }
  }
  ESP_ERROR_CHECK(esp_io_expander_new_i2c_tca9554(bus->Get_I2cBusHandle(), expanderAddress, &exio));
  // E-paper power, codec power, speaker enable, battery latch.
  auto pins=IO_EXPANDER_PIN_NUM_0|IO_EXPANDER_PIN_NUM_1|IO_EXPANDER_PIN_NUM_3|IO_EXPANDER_PIN_NUM_5;
  ESP_ERROR_CHECK(esp_io_expander_set_dir(exio,pins,IO_EXPANDER_OUTPUT));
  ESP_ERROR_CHECK(esp_io_expander_set_level(exio,pins,1));
  BoardAdc_Init();
  Codec_StartInit();
  audioStoreReady=initAudioStore();
  if(audioStoreReady)FFat.remove(audioCachePath);
  Serial.printf("AUDIO STORE ready=%d free=%u\n",audioStoreReady,audioStoreReady ? (unsigned)FFat.freeBytes() : 0);
  bootButton.setup(BOOT_BUTTON_PIN, INPUT_PULLUP, true);
  powerButton.setup(PWR_BUTTON_PIN, INPUT_PULLUP, true);
  setupNetwork();
  initConfigPortal();
  soundQueue=xQueueCreate(6,sizeof(SoundRequest));
  // holdMs is loaded from persisted configuration by initConfigPortal().
  bootButton.setDebounceMs(20);powerButton.setDebounceMs(20);
  bootButton.setClickMs(180);powerButton.setClickMs(180);
  bootButton.setPressMs(holdMs);
  powerButton.setPressMs(1200);
  bootButton.attachPress([](){if(!configModeActive && !remoteVoiceBusy && localUi!=UiRecognizing && localUi!=UiSending){pressStartedAt=millis();if(netConnected&&online&&!remoteRecommendationUi)setLocalUi(UiHold);}});
  bootButton.attachClick([](){ if(!configModeActive && !recording){if(localUi==UiHold)setLocalUi(UiIdle);event("b1_click");} });
  bootButton.attachLongPressStart([](){
    if(configModeActive || uploadPending || remoteVoiceBusy || localUi==UiRecognizing || localUi==UiSending)return;
    if(netConnected && online) captureRequested=true; else disconnectedImage();
  });
  bootButton.attachLongPressStop([](){
    bool wasRecording=recording;recording=false;captureRequested=false;
    Serial.printf("PTT RELEASE %lu\n",(unsigned long)millis());
    if(wasRecording)setLocalUi(UiSending);
    else if(localUi==UiHold)setLocalUi(UiIdle);
    else if(localUi==UiLimit)setLocalUi((remoteDraftReady||remoteVoiceBusy)?UiIdle:(audioEndSent?UiRecognizing:UiSending));
  });
  powerButton.attachClick([](){ if(!configModeActive)event("b2_click"); });
  powerButton.attachLongPressStart([](){ if(configModeActive){portalCloseAt=millis()+100;return;}if(remoteRecommendationReader&&!recording&&!uploadPending){event("b2_long");return;}captureCancelled=true;recording=false;captureRequested=false;setLocalUi(UiCancelled);event("b2_long"); });
  // Do not interpret a held power-on button as a stop command.
  while (digitalRead(PWR_BUTTON_PIN)==LOW) delay(10);
  initKnob();
  disconnectedImage();
  xTaskCreate(screenTask,"screen",8192,nullptr,1,nullptr);
  xTaskCreate(audioTask,"audio",8192,nullptr,2,nullptr);
  xTaskCreate(networkTask,"network",12288,nullptr,3,nullptr);
  xTaskCreate(deviceLocationTask,"location",8192,nullptr,1,nullptr);
}

void loop() {
  loopUsbConfig();
  soundObserve();
  if(knobQueue){KnobInput input;unsigned budget=8;while(budget--&&xQueueReceive(knobQueue,&input,0)==pdTRUE){
    if(!knobConnected||input.epoch!=knobEpoch||millis()-input.at>800||configModeActive||recording||uploadPending||captureRequested||!online||!netConnected)continue;
    const char *action=input.action==1?"knob_right":input.action==2?"knob_left":input.action==4?"knob_click":input.action==8?"knob_back":"knob_feed";event(action);
  }}
  loopWifiScan();
  loopKnownWifi();
  static uint32_t chordStart=0;
  static bool buttonGuard=false,chordDone=false;
  bool bootDown=digitalRead(BOOT_BUTTON_PIN)==LOW,powerDown=digitalRead(PWR_BUTTON_PIN)==LOW;
  if(bootDown && powerDown) {
    if(!buttonGuard){bool wasCapturing=recording||captureRequested||localUi==UiCancelled;buttonGuard=true;chordStart=millis();chordDone=false;captureCancelled=true;recording=false;captureRequested=false;setLocalUi(wasCapturing?UiCancelled:UiIdle);}
    if(!chordDone && millis()-chordStart>=3000){startConfigPortal();chordDone=true;}
  }
  if(buttonGuard){if(!bootDown&&!powerDown){buttonGuard=false;bootButton.reset();powerButton.reset();}}
  else {bootButton.tick();powerButton.tick();}
  loopConfigPortal();
  if(bootDown&&!powerDown&&!configModeActive&&remoteRecommendationUi&&localUi==UiIdle&&!recording&&!uploadPending&&netConnected&&online&&millis()-pressStartedAt>=250)setLocalUi(UiHold);
  // Short feedback expires locally even when the server is slow.
  if((localUi==UiTooShort || localUi==UiCancelled) && millis()-localUiAt>3000) setLocalUi(UiIdle);
  if(localUi==UiRecognizing && millis()-localUiAt>95000) setLocalUi(UiAudioError);
  if (online && millis()-lastContact > 20000) { online=false; disconnectedImage(); }
  if (!online) disconnectedImage();
  delay(2);
}
