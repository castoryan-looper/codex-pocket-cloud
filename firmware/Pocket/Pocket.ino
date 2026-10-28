#include <Arduino.h>
#include <ArduinoJson.h>
#include <OneButton.h>
#include <Preferences.h>
#include <WiFi.h>
#include <WebSocketsClient.h>
#include <mbedtls/base64.h>
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
static WebSocketsClient socketClient;
static Preferences prefs;
static OneButton bootButton, powerButton;
static esp_io_expander_handle_t exio;
static String wifiSsid, wifiPass, remoteHost, remotePath, remoteToken, rootCa;
static bool tlsEnabled = true, wifiConfigured = false, bridgeConfigured = false;
static volatile bool netConnected=false;
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
static volatile int lastLocalKey=0;
static volatile uint32_t lastLocalKeyAt=0;
static void setLocalUi(int state) { localUi=state; localUiAt=millis(); imageDirty=true; }
static uint8_t desiredImage[5000], lastImage[5000];
static String pendingHash;
static uint32_t lastContact = 0, lastTelemetry = 0;
static volatile bool online = false;
static volatile int screenPhase = 0;
static volatile uint32_t fullRefreshes = 0, partialRefreshes = 0;
static uint32_t lastAudioEnd = 0;
static int offlineStage = -1;
#include "config_portal.h"

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
  // Device configuration uses HTTP; USB is only for flashing/startup logs.
  if (netConnected) {
    char *copy = strdup(out.c_str());
    if (!copy) return false;
    if (xQueueSend(netQueue, &copy, pdMS_TO_TICKS(doc["type"]=="audio_end" ? 500 : 10)) == pdTRUE) return true;
    free(copy);
  }
  return false;
}

void event(const char *action) {
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
  doc["firmware"] = "codex-pocket-0.3.0";
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
  doc["holdMs"]=1000;
  doc["rssi"] = WiFi.status() == WL_CONNECTED ? WiFi.RSSI() : 0;
  doc["displayBusy"] = gpio_get_level(EPD_BUSY_PIN);
  doc["displayTimeouts"] = EPD_GetBusyTimeouts();
  doc["screenPhase"] = screenPhase;
  doc["fullRefreshes"] = fullRefreshes;
  doc["partialRefreshes"] = partialRefreshes;
  doc["configurationMode"] = (bool)portalActive;
  doc["configurationUrl"] = "http://"+WiFi.localIP().toString()+"/";
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

void packet(const String &line) {
  if (line.length() > 15000) return;
  JsonDocument doc;
  if (deserializeJson(doc, line)) return;
  String type = doc["type"] | "";
  if (type == "frame") {
    String data = doc["data"] | "";
    uint8_t decoded[5000];
    size_t length = 0;
    if (mbedtls_base64_decode(decoded, sizeof(decoded), &length,
        (const unsigned char *)data.c_str(), data.length()) != 0 || length != 5000) return;
    xSemaphoreTake(imageMutex, portMAX_DELAY);
    memcpy(desiredImage, decoded, 5000);
    pendingHash = doc["hash"] | "";
    imageDirty = true;
    xSemaphoreGive(imageMutex);
    lastContact = millis();
    online = true;
    remoteVoiceBusy=doc["voiceBusy"]|false;
    remoteDraftReady=doc["draftReady"]|false;
    String state=doc["state"]|"";
    // Ignore delayed cloud "recording" screens until local capture/upload is over.
    if(audioEndSent && localUi==UiRecognizing) {
      bool finalState=remoteVoiceBusy || remoteDraftReady || state=="识别语音" || state=="待发送" || state=="语音失败" || state=="录音失败" || state=="录音太短";
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
  for (;;) {
    bool wifiConnected=WiFi.status()==WL_CONNECTED;
    bool usbConnected=HWCDC::isPlugged();
    int cloud=netConnected && online;
    int ui=localUi;
    if(!portalActive) {
      if(!wifiConnected) ui=8;
      else if(!bridgeConfigured) ui=10;
      else if(!netConnected) ui=9;
      else if(!online) ui=11;
    }
    int key=(millis()-lastLocalKeyAt<500) ? lastLocalKey : 0;
    int second=recording ? (millis()-recordingStartedAt)/1000 : -1;
    int progress=ui==UiHold ? min(4,(int)((millis()-pressStartedAt)/250)) : -1;
    if (previousWifi!=wifiConnected || previousUsb!=usbConnected || previousPortal!=(int)portalActive || previousCloud!=cloud || previousUi!=ui || previousSecond!=second || previousProgress!=progress || previousKey!=key) imageDirty=true;
    if (imageDirty) {
      xSemaphoreTake(imageMutex, portMAX_DELAY);
      memcpy(image, desiredImage, 5000);
      String hash = pendingHash;
      imageDirty = false;
      xSemaphoreGive(imageMutex);
      previousWifi=wifiConnected; previousUsb=usbConnected;
      previousPortal=portalActive;previousCloud=cloud;previousUi=ui;previousSecond=second;previousProgress=progress;previousKey=key;
      if(!portalActive) {
        if(ui>=0 && ui<12) memcpy(image,localScreens[ui],5000);
        else memcpy(image+186*25,key==1?localFooterBoot:key==2?localFooterPower:localFooter,sizeof(localFooter));
        if(ui==UiHold) {
          for(int y=148;y<154;y++) for(int x=14;x<14+progress*43;x++) image[y*25+x/8]&=~(0x80>>(x%8));
        }
        if(ui==UiRecording || ui==UiLimit) {
          char timer[24];unsigned seconds=ui==UiLimit ? recordSeconds : (millis()-recordingStartedAt)/1000;
          snprintf(timer,sizeof(timer),"%02u:%02u / %us",seconds/60,seconds%60,recordSeconds);
          imageAsciiLarge(image,23,103,timer);
        }
        if(ui==9 || ui==11) imageAscii(image,8,118,remoteHost+":"+String(remotePort));
        const uint8_t *header=cloud ? localHeaderOnline : netConnected ? localHeaderConnecting : localHeaderOffline;
        for(int y=0;y<25;y++) memcpy(image+y*25,header+y*10,10);
        for(int y=0;y<24;y++) for(int x=139;x<200;x++) image[y*25+x/8]|=0x80>>(x%8);
        imageAscii(image,145,5,localBattery<0 ? "--%" : String(localBattery)+"%");
      }
      if(portalActive) {
        memcpy(image,offlineImages[4],5000);
        imageAscii(image,10,94,portalSsid);
        imageAscii(image,10,113,"KEY: "+portalKey);
        imageAscii(image,10,135,"192.168.4.1");
        imageAscii(image,10,169,"PWR HOLD: CLOSE");
      }
      // This strip is owned by the MCU so it remains current without a bridge.
      for (int y=1;y<24;y++) for (int x=76;x<132;x++) image[y*25+x/8]|=0x80>>(x%8);
      const uint16_t *icons[2]={wifiConnected ? icon_wifi_on : icon_wifi_off,usbConnected ? icon_usb_on : icon_usb_off};
      for (int i=0;i<2;i++) for (int y=0;y<16;y++) for (int x=0;x<16;x++) {
        int px=80+i*28+x,py=4+y;
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
      ack["configurationMode"]=(bool)portalActive;
      sendPacket(ack);
    }
    vTaskDelay(pdMS_TO_TICKS(20));
  }
}

void audioTask(void *) {
  int16_t stereo[1024];
  int16_t mono[512];
  unsigned char b64[1400];
  for (;;) {
    if (!captureRequested) { vTaskDelay(pdMS_TO_TICKS(5)); continue; }
    captureRequested=false;
    // Release just after the threshold must never start an unattended recording.
    if(digitalRead(BOOT_BUTTON_PIN)!=LOW || digitalRead(PWR_BUTTON_PIN)==LOW || portalActive || !netConnected || !online) continue;
    captureCancelled=false;audioEndSent=false;audioBytes=0;
    recording=true;recordingStartedAt=millis();setLocalUi(UiRecording);
    Serial.printf("PTT START %lu\n",(unsigned long)recordingStartedAt);
    JsonDocument start;start["type"]="audio_start";start["maxSeconds"]=recordSeconds;
    bool captureError=!sendPacket(start);
    uint32_t began=millis(),sentBytes=0;
    while (!captureError && recording && digitalRead(BOOT_BUTTON_PIN)==LOW && digitalRead(PWR_BUTTON_PIN)!=LOW && netConnected && millis()-began<recordSeconds*1000UL && sentBytes+sizeof(mono)<=recordSeconds*32000UL) {
      if(Codec_RecordData((uint8_t*)stereo,sizeof(stereo))!=ESP_OK) {captureError=true;break;}
      if(!recording || digitalRead(BOOT_BUTTON_PIN)!=LOW || digitalRead(PWR_BUTTON_PIN)==LOW) break;
      for(int i=0;i<512;i++) mono[i]=stereo[2*i];
      size_t size=0;mbedtls_base64_encode(b64,sizeof(b64),&size,(uint8_t*)mono,sizeof(mono));b64[size]=0;
      JsonDocument chunk;chunk["type"]="audio_chunk";chunk["data"]=(char*)b64;
      if(!sendPacket(chunk)) {captureError=true;break;}
      sentBytes+=sizeof(mono);audioBytes=sentBytes;
    }
    bool limit=millis()-began>=recordSeconds*1000UL || sentBytes+sizeof(mono)>recordSeconds*32000UL;
    if(digitalRead(PWR_BUTTON_PIN)==LOW || portalActive) captureCancelled=true;
    recording=false;lastAudioEnd=millis();
    Serial.printf("PTT STOP %lu bytes=%lu error=%d cancelled=%d\n",(unsigned long)lastAudioEnd,(unsigned long)sentBytes,captureError,(int)captureCancelled);
    if(captureCancelled) setLocalUi(UiCancelled);
    else if(captureError || !netConnected) setLocalUi(UiAudioError);
    else if(sentBytes<6400) setLocalUi(UiTooShort);
    else if(limit && digitalRead(BOOT_BUTTON_PIN)==LOW) setLocalUi(UiLimit);
    else setLocalUi(UiSending);
    JsonDocument end;end["type"]="audio_end";end["bytes"]=sentBytes;
    end["cancelled"]=(bool)captureCancelled;
    end["error"]=captureCancelled ? "cancelled" : captureError ? "capture_or_queue_failed" : "";
    if(!sendPacket(end) && !captureCancelled) setLocalUi(UiAudioError);
  }
}

// TLS handshakes and Wi-Fi socket writes must not block physical button sampling.
void networkTask(void *) {
  for(;;) {
    if(bridgeConfigured && wifiConfigured && WiFi.status()==WL_CONNECTED && (!portalActive||netConnected)) socketClient.loop();
    char *queued=nullptr;
    for(int i=0;i<8 && xQueueReceive(netQueue,&queued,0)==pdTRUE;i++) {
      if(netConnected) {
        bool isEnd=strstr(queued,"\"audio_end\"")!=nullptr;
        bool sent=socketClient.sendTXT(queued);
        if(isEnd && sent) {
          audioEndSent=true;
          if(localUi==UiSending) setLocalUi(UiRecognizing);
        } else if(!sent && (recording || localUi==UiSending)) {
          captureCancelled=true;recording=false;captureRequested=false;setLocalUi(UiAudioError);
        }
      }
      free(queued);
    }
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
  wifiConfigured = wifiSsid.length()>0;
  bridgeConfigured = remoteHost.length() && remoteToken.length() && (!tlsEnabled || rootCa.length());
  if (!wifiConfigured) return;
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(wifiSsid.c_str(), wifiPass.c_str());
  configTime(0, 0, "pool.ntp.org", "time.cloudflare.com");
  if(!bridgeConfigured)return;
  socketClient.onEvent([](WStype_t type, uint8_t *payload, size_t length) {
    if (type == WStype_CONNECTED) {
      netConnected = true;
      JsonDocument hello;
      hello["type"]="hello"; hello["token"]=remoteToken;
      hello["firmware"]="codex-pocket-0.3.0";
      hello["wifiConnected"]=WiFi.status()==WL_CONNECTED;
      hello["usbConnected"]=HWCDC::isPlugged();
      String out; serializeJson(hello, out); socketClient.sendTXT(out);
    } else if (type == WStype_DISCONNECTED) {
      netConnected = false;
      online = false;
      recording = false;
      captureRequested = false;
    } else if (type == WStype_TEXT && length < 15000) {
      JsonDocument msg;
      if (!deserializeJson(msg, payload, length) && String(msg["type"] | "") == "frame")
        packet(String((char *)payload).substring(0, length));
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
  bootButton.setup(BOOT_BUTTON_PIN, INPUT_PULLUP, true);
  powerButton.setup(PWR_BUTTON_PIN, INPUT_PULLUP, true);
  setupNetwork();
  initConfigPortal();
  holdMs=1000;
  bootButton.setDebounceMs(20);powerButton.setDebounceMs(20);
  bootButton.setClickMs(180);powerButton.setClickMs(180);
  bootButton.setPressMs(1000);
  powerButton.setPressMs(1200);
  bootButton.attachPress([](){if(!portalActive && !remoteVoiceBusy && localUi!=UiRecognizing && localUi!=UiSending){pressStartedAt=millis();if(netConnected&&online)setLocalUi(UiHold);}});
  bootButton.attachClick([](){ if(!portalActive && !recording){if(localUi==UiHold)setLocalUi(UiIdle);event("b1_click");} });
  bootButton.attachLongPressStart([](){
    if(portalActive || remoteVoiceBusy || localUi==UiRecognizing || localUi==UiSending)return;
    if(netConnected && online) captureRequested=true; else disconnectedImage();
  });
  bootButton.attachLongPressStop([](){
    bool wasRecording=recording;recording=false;captureRequested=false;
    Serial.printf("PTT RELEASE %lu\n",(unsigned long)millis());
    if(wasRecording)setLocalUi(UiSending);
    else if(localUi==UiHold)setLocalUi(UiIdle);
    else if(localUi==UiLimit)setLocalUi((remoteDraftReady||remoteVoiceBusy)?UiIdle:(audioEndSent?UiRecognizing:UiSending));
  });
  powerButton.attachClick([](){ if(!portalActive)event("b2_click"); });
  powerButton.attachLongPressStart([](){ if(portalActive){portalCloseAt=millis()+100;return;}captureCancelled=true;recording=false;captureRequested=false;setLocalUi(UiCancelled);event("b2_long"); });
  // Do not interpret a held power-on button as a stop command.
  while (digitalRead(PWR_BUTTON_PIN)==LOW) delay(10);
  disconnectedImage();
  xTaskCreate(screenTask,"screen",8192,nullptr,1,nullptr);
  xTaskCreate(audioTask,"audio",8192,nullptr,2,nullptr);
  xTaskCreate(networkTask,"network",12288,nullptr,1,nullptr);
}

void loop() {
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
  // Short feedback expires locally even when the server is slow.
  if((localUi==UiTooShort || localUi==UiCancelled) && millis()-localUiAt>3000) setLocalUi(UiIdle);
  if(localUi==UiRecognizing && millis()-localUiAt>95000) setLocalUi(UiAudioError);
  if (online && millis()-lastContact > 20000) { online=false; disconnectedImage(); }
  if (!online) disconnectedImage();
  delay(2);
}
