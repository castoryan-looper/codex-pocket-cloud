#pragma once
#include <WebServer.h>
#include <DNSServer.h>

// Included after Pocket.ino's shared state. HTTP is local/AP only; every config
// operation requires the random key shown physically on the device.
static WebServer configWeb(80);
static DNSServer configDns;
static volatile bool portalActive=false;
static String portalSsid,portalKey;
static uint32_t portalRestartAt=0,portalCloseAt=0,portalBootAt=0;
static unsigned recordSeconds=20,holdMs=1200,refreshMinutes=15;

static const char CONFIG_PAGE[] PROGMEM=R"HTML(
<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Codex Pocket 设备配置</title>
<style>body{max-width:640px;margin:28px auto;padding:0 18px;background:#f3f4ee;color:#20251e;font:16px system-ui}section{background:white;border:1px solid #ddd;border-radius:12px;padding:20px;margin:18px 0}label{display:block;margin:14px 0}input,textarea,button,select{font:inherit;padding:10px;border:1px solid #aaa;border-radius:6px;box-sizing:border-box}input,textarea{width:100%;margin-top:6px}textarea{min-height:100px}button{background:#e1ebd8;cursor:pointer;margin:5px}small{color:#666}#message{white-space:pre-wrap}h1{font-size:26px}</style>
<h1>Codex Pocket 设备配置</h1><p>配置直接保存在设备上，不需要 USB 桥接。</p><p id="message" role="status"></p>
<section id="login"><label>配置密码（显示在设备屏幕的 KEY 行）<input id="key" type="password" autocomplete="current-password"></label><button onclick="login()">打开设置</button><small>忘记密码时，同时按住 BOOT 和 PWR 约 3 秒查看。</small></section>
<form id="settings" hidden onsubmit="save(event)"><section><h2>Wi-Fi</h2><label>2.4GHz 热点名称<input id="ssid" maxlength="32" required></label><label>热点密码<input id="password" type="password" maxlength="63" autocomplete="new-password" placeholder="同一热点留空则保留原密码"></label><p id="network"></p></section>
<section><h2>电脑连接（可稍后配置）</h2><label>导入电脑控制台下载的连接配置<input id="importFile" type="file" accept=".json,application/json" onchange="importConfig(this)"></label><label>桥接域名或 IP（不含协议）<input id="host" maxlength="253"></label><label>端口<input id="port" type="number" min="1" max="65535" value="443" required></label><label><input id="tls" type="checkbox" checked style="width:auto"> 使用 TLS 加密</label><label>设备连接密钥<input id="token" type="password" autocomplete="new-password" placeholder="已有密钥时留空保留"></label><label>根 CA 证书（PEM）<textarea id="ca"></textarea></label><small>跨网络需要可用的固定 WSS 入口；电脑保持在线。不要把配置网页端口开放到公网。</small></section>
<section><h2>按键与屏幕</h2><label>BOOT 长按进入录音（毫秒）<input id="holdMs" type="number" min="800" max="2500" step="100"></label><label>单次录音上限（秒）<input id="recordSeconds" type="number" min="2" max="20"></label><label>维护全刷最小间隔（分钟）<input id="refreshMinutes" type="number" min="5" max="60"></label></section>
<button type="submit">保存并重启设备</button><button type="button" onclick="closePortal()">关闭配置热点</button><p>修改配置后设备会自动重连。以后仍可通过双键长按重新打开配置热点。</p></form>
<script>
const $=id=>document.getElementById(id);let auth=sessionStorage.getItem('pocketSetupKey')||'';$('key').value=auth;
async function api(path,body){const r=await fetch(path,{method:body?'POST':'GET',headers:{'X-Setup-Key':auth,...(body?{'Content-Type':'application/json'}:{})},body:body?JSON.stringify(body):undefined});const data=await r.json();if(!r.ok)throw Error(data.error||'请求失败');return data}
function fill(c){for(const k of ['ssid','host','port','ca','holdMs','recordSeconds','refreshMinutes'])if(c[k]!==undefined)$(k).value=c[k];if(c.tls!==undefined)$('tls').checked=c.tls;if(c.token)$('token').value=c.token}
async function login(){auth=$('key').value;try{const c=await api('/api/config');sessionStorage.setItem('pocketSetupKey',auth);fill(c);$('login').hidden=true;$('settings').hidden=false;$('network').textContent=(c.wifiConnected?'Wi-Fi 已连接':'Wi-Fi 未连接')+' · '+(c.bridgeConnected?'电脑桥接在线':'电脑桥接未连接');$('message').textContent='';}catch(e){$('message').textContent=e.message}}
async function importConfig(input){try{const c=JSON.parse(await input.files[0].text());fill(c);$('message').textContent='连接配置已填入，保存后生效。'}catch(e){$('message').textContent='无法读取配置文件：'+e.message}}
async function save(e){e.preventDefault();try{const c={};for(const k of ['ssid','password','host','token','ca'])c[k]=$(k).value;for(const k of ['port','holdMs','recordSeconds','refreshMinutes'])c[k]=Number($(k).value);c.tls=$('tls').checked;const r=await api('/api/config',c);$('password').value='';$('token').value='';$('message').textContent=r.message;}catch(e){$('message').textContent=e.message}}
async function closePortal(){try{const r=await api('/api/close',{});$('message').textContent=r.message}catch(e){$('message').textContent=e.message}}
</script></html>
)HTML";

static void portalJson(int status,JsonDocument &doc) {
  String body;serializeJson(doc,body);
  configWeb.sendHeader("Cache-Control","no-store");
  configWeb.sendHeader("X-Frame-Options","DENY");
  configWeb.send(status,"application/json; charset=utf-8",body);
}
static void portalError(int status,const char *message) {
  JsonDocument doc;doc["error"]=message;portalJson(status,doc);
}
static bool portalAuthorized() {
  String origin=configWeb.header("Origin");
  if (origin.length() && origin!="http://"+configWeb.hostHeader()) {portalError(403,"请直接打开设备的配置网页");return false;}
  String key=configWeb.header("X-Setup-Key");
  unsigned difference=key.length()^portalKey.length();
  for(unsigned i=0;i<key.length() && i<portalKey.length();i++)difference|=(uint8_t)key[i]^(uint8_t)portalKey[i];
  if(difference){portalError(401,"配置密码不正确，请查看设备屏幕 KEY 行");return false;}
  return true;
}
static void startConfigPortal() {
  if(portalActive)return;
  WiFi.mode(WIFI_AP_STA);
  if(!WiFi.softAP(portalSsid.c_str(),portalKey.c_str()))return;
  configDns.start(53,"*",WiFi.softAPIP());
  portalActive=true;
  imageDirty=true;
}
static void initConfigPortal() {
  prefs.begin("pocket",false);
  portalKey=prefs.getString("ap_key","");
  if(portalKey.length()<8){char key[13];snprintf(key,sizeof(key),"%08lX%04lX",(unsigned long)esp_random(),(unsigned long)(esp_random()&0xffff));portalKey=key;prefs.putString("ap_key",portalKey);}
  uint64_t mac=ESP.getEfuseMac();char name[24];snprintf(name,sizeof(name),"CPOCKET-%06lX",(unsigned long)((mac>>24)&0xffffff));portalSsid=name;
  holdMs=constrain(prefs.getUInt("hold_ms",1200),800U,2500U);
  recordSeconds=constrain(prefs.getUInt("rec_secs",20),2U,20U);
  refreshMinutes=constrain(prefs.getUInt("refresh_m",15),5U,60U);
  bool firstWebSetup=!prefs.getBool("web_ready",false);prefs.putBool("web_ready",true);prefs.end();
  const char *headers[]={"X-Setup-Key","Origin"};configWeb.collectHeaders(headers,2);
  configWeb.on("/",HTTP_GET,[](){configWeb.sendHeader("Cache-Control","no-store");configWeb.send_P(200,"text/html; charset=utf-8",CONFIG_PAGE);});
  configWeb.on("/api/config",HTTP_GET,[](){
    if(!portalAuthorized())return;
    JsonDocument doc;doc["ssid"]=wifiSsid;doc["host"]=remoteHost;doc["port"]=remotePort;doc["tls"]=tlsEnabled;doc["ca"]=rootCa;
    doc["tokenConfigured"]=remoteToken.length()>0;doc["holdMs"]=holdMs;doc["recordSeconds"]=recordSeconds;doc["refreshMinutes"]=refreshMinutes;
    doc["wifiConnected"]=WiFi.status()==WL_CONNECTED;doc["bridgeConnected"]=netConnected;doc["ip"]=WiFi.localIP().toString();portalJson(200,doc);
  });
  configWeb.on("/api/config",HTTP_POST,[](){
    if(!portalAuthorized())return;
    String body=configWeb.arg("plain");if(body.length()>12000){portalError(413,"配置内容过大");return;}
    JsonDocument doc;if(deserializeJson(doc,body)){portalError(400,"配置格式错误");return;}
    String ssid=doc["ssid"]|"",password=doc["password"]|"",host=doc["host"]|"",key=doc["token"]|"",ca=doc["ca"]|"";
    int port=doc["port"]|443,h=doc["holdMs"]|1200,r=doc["recordSeconds"]|20,m=doc["refreshMinutes"]|15;bool tls=doc["tls"]|true;
    bool validHost=host.length()<=253;
    for(unsigned i=0;i<host.length();i++)if(!isalnum((unsigned char)host[i]) && host[i]!='.' && host[i]!='-')validHost=false;
    if(!ssid.length()||ssid.length()>32||password.length()>63||!validHost||port<1||port>65535||h<800||h>2500||r<2||r>20||m<5||m>60||key.length()>256||ca.length()>8192){portalError(400,"请检查热点、主机、端口和功能参数的范围");return;}
    if(host.length() && tls && ca.indexOf("-----BEGIN CERTIFICATE-----")<0){portalError(400,"TLS 需要正确的 PEM 根 CA 证书");return;}
    if(host.length() && !key.length() && !remoteToken.length()){portalError(400,"请导入电脑连接配置，或填写设备连接密钥");return;}
    prefs.begin("pocket",false);
    if(password.length()||ssid!=wifiSsid)prefs.putString("pass",password);
    prefs.putString("ssid",ssid);prefs.putString("host",host);prefs.putUInt("port",port);prefs.putString("path","/device");prefs.putBool("tls",tls);prefs.putString("ca",ca);
    if(key.length())prefs.putString("token",key);
    prefs.putUInt("hold_ms",h);prefs.putUInt("rec_secs",r);prefs.putUInt("refresh_m",m);prefs.end();
    JsonDocument result;result["message"]="配置已保存，设备即将重启。若网页断开，请重新连接热点；双键长按可再次打开配置。";portalJson(200,result);portalRestartAt=millis()+1500;
  });
  configWeb.on("/api/close",HTTP_POST,[](){if(!portalAuthorized())return;JsonDocument result;result["message"]="配置热点即将关闭，设备继续通过 Wi-Fi 工作。";portalJson(200,result);portalCloseAt=millis()+1000;});
  configWeb.onNotFound([](){configWeb.sendHeader("Location","http://"+WiFi.softAPIP().toString()+"/",true);configWeb.send(302,"text/plain","");});
  configWeb.begin();portalBootAt=millis();
  if(firstWebSetup||!wifiConfigured)startConfigPortal();
}
static void loopConfigPortal() {
  configWeb.handleClient();
  if(portalActive)configDns.processNextRequest();
  if(portalRestartAt && (int32_t)(millis()-portalRestartAt)>=0)ESP.restart();
  if(portalCloseAt && (int32_t)(millis()-portalCloseAt)>=0){portalCloseAt=0;portalActive=false;configDns.stop();WiFi.softAPdisconnect(true);offlineStage=-1;imageDirty=true;}
  if(!portalActive && WiFi.status()!=WL_CONNECTED && millis()-portalBootAt>30000)startConfigPortal();
}

