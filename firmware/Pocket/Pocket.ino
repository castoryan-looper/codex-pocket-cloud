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

// All transport sends are serialized. The audio task never calls the WS client.
static SemaphoreHandle_t imageMutex;
static QueueHandle_t netQueue;
static WebSocketsClient socketClient;
static Preferences prefs;
static OneButton bootButton, powerButton;
static esp_io_expander_handle_t exio;
static String wifiSsid, wifiPass, remoteHost, remotePath, remoteToken, rootCa;
static bool tlsEnabled = true, wifiConfigured = false, bridgeConfigured = false, netConnected = false;
static uint16_t remotePort = 443;
static volatile bool recording = false, captureRequested = false;
static volatile bool imageDirty = false;
static uint8_t desiredImage[5000], lastImage[5000];
static String pendingHash;
static uint32_t lastContact = 0, lastTelemetry = 0;
static bool online = false;
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

void sendPacket(const JsonDocument &doc) {
  String out;
  serializeJson(doc, out);
  // Runtime commands, voice, frames and status use Wi-Fi only.
  // Device configuration uses HTTP; USB is only for flashing/startup logs.
  if (netConnected) {
    char *copy = strdup(out.c_str());
    if (copy && xQueueSend(netQueue, &copy, pdMS_TO_TICKS(20)) != pdTRUE) free(copy);
  }
}

void event(const char *action) {
  JsonDocument doc;
  doc["type"] = "event";
  doc["action"] = action;
  sendPacket(doc);
}

void telemetry(bool hello = false) {
  JsonDocument doc;
  doc["type"] = hello ? "hello" : "telemetry";
  doc["firmware"] = "codex-pocket-0.2.0";
  doc["wifiConnected"] = WiFi.status()==WL_CONNECTED;
  doc["usbConnected"] = HWCDC::isPlugged();
  float volts = Get_VbatVoltage();
  doc["millivolts"] = (int)(volts * 1000);
  // Voltage estimate, not a calibrated fuel gauge; disconnected battery is unknown.
  if (volts > 2.8 && volts < 4.5) doc["battery"] = Get_Batterylevel();
  else doc["battery"] = nullptr;
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
  int previousWifi=-1,previousUsb=-1,previousPortal=-1;
  for (;;) {
    bool wifiConnected=WiFi.status()==WL_CONNECTED;
    bool usbConnected=HWCDC::isPlugged();
    if (previousWifi!=wifiConnected || previousUsb!=usbConnected || previousPortal!=(int)portalActive) imageDirty=true;
    if (imageDirty) {
      xSemaphoreTake(imageMutex, portMAX_DELAY);
      memcpy(image, desiredImage, 5000);
      String hash = pendingHash;
      imageDirty = false;
      xSemaphoreGive(imageMutex);
      previousWifi=wifiConnected; previousUsb=usbConnected;
      previousPortal=portalActive;
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
        if (refreshCount >= 80 && millis()-lastFullRefresh >= refreshMinutes*60*1000UL && !recording && millis()-lastAudioEnd > 30000) {
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
      ack["hash"] = hash;
      ack["wifiConnected"]=wifiConnected;
      ack["usbConnected"]=usbConnected;
      ack["configurationMode"]=(bool)portalActive;
      sendPacket(ack);
    }
    vTaskDelay(pdMS_TO_TICKS(100));
  }
}

void audioTask(void *) {
  int16_t stereo[1024];
  int16_t mono[512];
  unsigned char b64[1400];
  for (;;) {
    if (!captureRequested) { vTaskDelay(pdMS_TO_TICKS(10)); continue; }
    captureRequested = false;
    recording = true;
    JsonDocument start; start["type"] = "audio_start"; start["maxSeconds"]=recordSeconds; sendPacket(start);
    uint32_t began = millis();
    uint32_t sentBytes=0;
    bool captureError=false;
    while (recording && millis()-began < recordSeconds*1000UL && sentBytes+sizeof(mono)<=recordSeconds*32000UL) {
      if (Codec_RecordData((uint8_t *)stereo, sizeof(stereo)) != ESP_OK) { captureError=true; break; }
      // ES8311 mono microphone is present on the left slot of the stereo I2S bus.
      for (int i=0; i<512; i++) mono[i] = stereo[2*i];
      size_t size=0;
      mbedtls_base64_encode(b64, sizeof(b64), &size, (uint8_t *)mono, sizeof(mono));
      b64[size] = 0;
      JsonDocument chunk;
      chunk["type"] = "audio_chunk";
      chunk["data"] = (char *)b64;
      sendPacket(chunk);
      sentBytes+=sizeof(mono);
    }
    recording = false;
    lastAudioEnd=millis();
    JsonDocument end; end["type"] = "audio_end";
    end["bytes"]=sentBytes;
    end["error"]=captureError ? "codec_read_failed" : "";
    sendPacket(end);
  }
}

void setupNetwork() {
  prefs.begin("pocket", true);
  wifiSsid=prefs.getString("ssid", ""); wifiPass=prefs.getString("pass", "");
  remoteHost=prefs.getString("host", ""); remotePath=prefs.getString("path", "/device");
  remoteToken=prefs.getString("token", ""); rootCa=prefs.getString("ca", "");
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
      hello["firmware"]="codex-pocket-0.2.0";
      hello["wifiConnected"]=WiFi.status()==WL_CONNECTED;
      hello["usbConnected"]=HWCDC::isPlugged();
      String out; serializeJson(hello, out); socketClient.sendTXT(out);
    } else if (type == WStype_DISCONNECTED) {
      netConnected = false;
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
  bootButton.setPressMs(holdMs);
  powerButton.setPressMs(1200);
  bootButton.attachClick([](){ if (!portalActive && !recording) event("b1_click"); });
  bootButton.attachLongPressStart([](){ if(portalActive)return; if (netConnected) captureRequested=true; else disconnectedImage(); });
  bootButton.attachLongPressStop([](){ recording=false; captureRequested=false; });
  powerButton.attachClick([](){ if(!portalActive)event("b2_click"); });
  powerButton.attachLongPressStart([](){ if(portalActive){portalCloseAt=millis()+100;return;}recording=false; event("b2_long"); });
  // Do not interpret a held power-on button as a stop command.
  while (digitalRead(PWR_BUTTON_PIN)==LOW) delay(10);
  disconnectedImage();
  xTaskCreate(screenTask,"screen",8192,nullptr,1,nullptr);
  xTaskCreate(audioTask,"audio",8192,nullptr,2,nullptr);
  telemetry(true);
}

void loop() {
  static uint32_t chordStart=0;
  static bool buttonGuard=false,chordDone=false;
  bool bootDown=digitalRead(BOOT_BUTTON_PIN)==LOW,powerDown=digitalRead(PWR_BUTTON_PIN)==LOW;
  if(bootDown && powerDown) {
    if(!buttonGuard){buttonGuard=true;chordStart=millis();chordDone=false;recording=false;captureRequested=false;}
    if(!chordDone && millis()-chordStart>=3000){startConfigPortal();chordDone=true;}
  }
  if(buttonGuard){if(!bootDown&&!powerDown){buttonGuard=false;bootButton.reset();powerButton.reset();}}
  else {bootButton.tick();powerButton.tick();}
  loopConfigPortal();
  if (bridgeConfigured && wifiConfigured && WiFi.status()==WL_CONNECTED && (!portalActive||netConnected)) socketClient.loop();
  char *queued=nullptr;
  for (int i=0; i<4 && xQueueReceive(netQueue,&queued,0)==pdTRUE; i++) {
    if (netConnected) socketClient.sendTXT(queued);
    free(queued);
  }
  if (millis()-lastTelemetry > 5000) { lastTelemetry=millis(); telemetry(); }
  if (online && millis()-lastContact > 20000) { online=false; disconnectedImage(); }
  if (!online) disconnectedImage();
  delay(2);
}
