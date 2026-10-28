#pragma once
#include <WebServer.h>
#include <DNSServer.h>
#include <mbedtls/x509_crt.h>

static String normalizeRootCa(String ca) {
  ca.replace("\\r\\n", "\n");
  ca.replace("\\n", "\n");
  ca.replace("\\r", "\n");
  ca.replace("\r\n", "\n");
  ca.trim();
  if (ca.length()) ca += '\n';
  return ca;
}

// Included after Pocket.ino's shared state. HTTP is local/AP only; every config
// operation requires the random key shown physically on the device.
static WebServer configWeb(80);
static DNSServer configDns;
static volatile bool portalActive=false;
static bool portalAutoOpened=false;
static String portalSsid,portalKey;
static uint32_t portalRestartAt=0,portalCloseAt=0,portalBootAt=0;
static unsigned recordSeconds=20,holdMs=1000,refreshMinutes=15;

static const char CONFIG_PAGE[] PROGMEM=R"HTML(
<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Codex Pocket �豸����</title>
<style>body{max-width:640px;margin:28px auto;padding:0 18px;background:#f3f4ee;color:#20251e;font:16px system-ui}section{background:white;border:1px solid #ddd;border-radius:12px;padding:20px;margin:18px 0}label{display:block;margin:14px 0}input,textarea,button,select{font:inherit;padding:10px;border:1px solid #aaa;border-radius:6px;box-sizing:border-box}input,textarea,select{width:100%;margin-top:6px}textarea{min-height:100px}button{background:#e1ebd8;cursor:pointer;margin:5px}small{color:#666}#message{white-space:pre-wrap}h1{font-size:26px}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.card{background:#f3f4ee;padding:12px;border-radius:8px;overflow-wrap:anywhere}.card strong{display:block;margin-top:6px}dl{display:grid;grid-template-columns:100px 1fr;gap:9px}dd{margin:0;overflow-wrap:anywhere}@media(max-width:450px){.grid{grid-template-columns:1fr}}</style>
<h1>Codex Pocket �豸����</h1><p>֧�� USB �������ú��豸 Wi-Fi ��ҳ������������ͨ�� Wi-Fi �����ƶˡ�</p><p id="message" role="status"></p>
<section id="login"><label>�������루��ʾ���豸��Ļ�� KEY �У�<input id="key" type="password" autocomplete="current-password"></label><button onclick="login()">������</button><small>��������ʱ��ͬʱ��ס BOOT �� PWR Լ 3 ��鿴��</small></section>
<section id="live" hidden><h2>�豸ʵʱ״̬ <small id="connection"></small></h2><div id="statusGrid" class="grid"></div><details open><summary>�豸��Ϣ</summary><dl id="deviceInfo"></dl></details><small>ÿ 2 ����¡�����״̬���豸ʵ�������ƶ�Ϊ���ݣ�Wi-Fi ���ӳɹ������������Ѿ���ͨ��</small></section>
<form id="settings" hidden novalidate onsubmit="save(event)"><section><h2>Wi-Fi</h2><label>ѡ�񸽽��� 2.4 GHz Wi-Fi<select id="wifiList" onchange="chooseWifi()"><option value="">��ɨ�裬��ѡ������</option></select></label><button id="scanButton" type="button" onclick="scanWifi()">ɨ�踽�� Wi-Fi</button><p id="scanMessage" role="status"><small>ɨ��ʱ�豸 Wi-Fi ����ݶϿ�����ɺ��Զ�������</small></p><details id="manualWifi"><summary>�ֶ�������������</summary><label>2.4GHz Wi-Fi ����<input id="ssid" maxlength="32" required></label></details><label>�ȵ�����<input id="password" type="password" maxlength="63" autocomplete="new-password" placeholder="ͬһ�ȵ���������ԭ����"></label><p id="network"></p></section>
<section><h2>�ƶ˷�������</h2><label>�����ƶ˵�������������<input id="importFile" type="file" accept=".json,application/json" onchange="importConfig(this)"></label><label>�ƶ������� IP������Э�飩<input id="host" maxlength="253"></label><label>�˿�<input id="port" type="number" min="1" max="65535" value="443" required></label><label><input id="tls" type="checkbox" checked style="width:auto"> ʹ�� TLS ����</label><label>�豸������Կ<input id="token" type="password" autocomplete="new-password" placeholder="������Կʱ���ձ���"></label><label>�� CA ֤�飨PEM��<textarea id="ca"></textarea></label><small>�豸ͨ�� Wi-Fi �����ƶ� WSS ���񡣲�Ҫ��������ҳ�˿ڿ��ŵ�������</small></section>
<section><h2>��������Ļ</h2><label>BOOT ��������¼�������룩<input id="holdMs" readonly value="1000" type="number" min="800" max="2500" step="100"></label><label>����¼�����ޣ��룩<input id="recordSeconds" type="number" min="2" max="20"></label><label>ά��ȫˢ��С��������ӣ�<input id="refreshMinutes" type="number" min="5" max="60"></label></section>
<button id="saveButton" type="submit">���沢�����豸</button><button type="button" onclick="closePortal()">�ر������ȵ�</button><p>USB ҳ�����豸��������Զ��ָ�״̬�������л��������硣˫�������Կɴ��豸�����ȵ㡣</p></form>
<script>
const $=id=>document.getElementById(id);let auth=new URLSearchParams(location.hash.slice(1)).get('key')||sessionStorage.getItem('pocketSetupKey')||'';$('key').value=auth;history.replaceState(null,'',location.pathname);let polling=false,configLoaded=false,statusInFlight=false,saving=false;
async function api(path,body){const r=await fetch(path,{method:body?'POST':'GET',headers:{'X-Setup-Key':auth,...(body?{'Content-Type':'application/json'}:{})},body:body?JSON.stringify(body):undefined});const data=await r.json();if(!r.ok)throw Error(data.error||'����ʧ��');return data}
function fill(c){for(const k of ['ssid','host','port','ca','recordSeconds','refreshMinutes'])if(c[k]!==undefined)$(k).value=c[k];if(c.ssid){const option=new Option('�ѱ��棺'+c.ssid,c.ssid,true,true);$('wifiList').replaceChildren(option);}if(c.tls!==undefined)$('tls').checked=c.tls;if(c.token)$('token').value=c.token}
function text(tag,value){const e=document.createElement(tag);e.textContent=value;return e}
const bytes=n=>n===undefined?'--':n>=1048576?(n/1048576).toFixed(1)+' MB':Math.round(n/1024)+' KB';
async function refreshStatus(){if(statusInFlight)return;statusInFlight=true;try{const s=await api('/api/status');$('live').hidden=false;$('statusGrid').replaceChildren();$('deviceInfo').replaceChildren();$('connection').textContent=s.usbProxy?'USB ��������':'�豸 Wi-Fi ��ҳ';
$('saveButton').disabled=saving||s.deviceConnected===false||s.recording||s.uploadPending;if(s.deviceConnected===false){$('statusGrid').append(text('p',s.message||'USB δ���ӣ�������豸'));return;}if(!configLoaded){const c=await api('/api/config');if(c.deviceConnected!==false){fill(c);configLoaded=true;}}
const cards=[['Wi-Fi',s.wifiConnected?'������ �� '+s.connectedSsid:'δ����'],['����',s.internetReachable?'����֤��ͨ':s.wifiConnected?'�ȴ���֤':'δ���� Wi-Fi'],['�ƶ˷���',s.cloudConnected?'����':s.cloudTransportConnected?'ͨ�������ӣ��ȴ�ȷ��':'δ����'],['USB',s.usbConnected?'������':'δ����'],['¼�� / �ϴ�',s.recording?'����¼��':s.uploadPending?'�ϴ��� '+s.uploadProgress+'%':'����'],['�豸��������ѹ���㣩',s.battery>=0?s.battery+'%':'��δ����']];
for(const [label,value] of cards){const c=text('div',label);c.className='card';c.append(text('strong',value));$('statusGrid').append(c);}
const reasons={201:'δ�ҵ��ȵ㣬���������� 2.4GHz',202:'��֤ʧ�ܣ����������밲ȫ��ʽ',210:'�ȵ㰲ȫ��ʽ������',211:'�ȵ���֤��ʽ��ƥ��',212:'�ź�̫��',15:'�������ֳ�ʱ',204:'�������ֳ�ʱ',200:'�ȵ��ź��ж�'};const info=[['Wi-Fi ���',s.wifiConnected?'������':s.wifiScanning?'����ɨ�� Wi-Fi':(reasons[s.wifiReason]||('�ȴ����ӣ����� '+(s.wifiReason||'--')+'��'))],['�ѱ��� Wi-Fi',s.configuredSsid||'δ����'],['�豸��ַ',s.ip],['����',s.gateway],['�ź�ǿ��',s.rssi==null?'--':s.rssi+' dBm'],['�ƶ˵�ַ',(s.host||'δ����')+':'+s.port],['�����ж�',s.internetDetail],['�̼��汾',s.firmware],['оƬ',s.chip+' / '+s.cores+' �� / '+s.cpuMHz+' MHz'],['Flash',bytes(s.flashBytes)],['�����ڴ�',bytes(s.freeHeap)],['¼������',s.audioStorageReady?bytes(s.audioStorageFree)+' ����':'������'],['�����Ƶ',bytes(s.audioBytes)],['�������',s.audioError||'��'],['����ʱ��',Math.floor(s.uptimeSeconds/60)+' �� '+s.uptimeSeconds%60+' ��'],['MAC',s.mac]];
for(const [label,value] of info)$('deviceInfo').append(text('dt',label),text('dd',value??'--'));
$('network').textContent=s.wifiConnected?'��ǰ���ӣ�'+s.connectedSsid+' �� '+s.ip:'��ǰδ���ӣ��ѱ��棺'+(s.configuredSsid||'��');
}catch(e){$('connection').textContent='�������������豸��';$('statusGrid').replaceChildren(text('p','δ�ܶ�ȡ�豸״̬������ USB ���������ӡ�'));$('saveButton').disabled=true;}finally{statusInFlight=false;}}
async function login(){auth=$('key').value;try{const c=await api('/api/config');sessionStorage.setItem('pocketSetupKey',auth);if(c.deviceConnected!==false){fill(c);configLoaded=true;}$('login').hidden=true;$('settings').hidden=false;$('live').hidden=false;$('message').textContent='';await refreshStatus();if(!polling){polling=true;setInterval(refreshStatus,2000);}}catch(e){$('message').textContent=e.message}}

async function importConfig(input){try{const c=JSON.parse(await input.files[0].text());fill(c);$('message').textContent='�������������룬�������Ч��'}catch(e){$('message').textContent='�޷���ȡ�����ļ���'+e.message}}
function chooseWifi(){const value=$('wifiList').value;if(value)$('ssid').value=value;}
async function scanWifi(){
 $('scanButton').disabled=true;$('scanMessage').textContent='����ɨ�踽�� Wi-Fi�����Ժ�';
 try{
  await api('/api/wifi/scan',{});let result;
  for(let i=0;i<35;i++){await new Promise(resolve=>setTimeout(resolve,800));result=await api('/api/wifi/scan');if(result.state==='done'||result.state==='error')break;}
  if(!result||result.state==='scanning')throw Error('ɨ�賬ʱ��������');
  if(result.state==='error')throw Error(result.error||'�豸��ʱ�޷�ɨ�裬������');
  const list=$('wifiList');list.replaceChildren(new Option('��ѡ�� Wi-Fi',''));const seen=new Set();
  for(const network of result.networks||[]){if(seen.has(network.ssid))continue;seen.add(network.ssid);const label=network.ssid+' �� '+network.rssi+' dBm'+(network.secure?' �� ��Ҫ����':' �� ��������');list.add(new Option(label,network.ssid));}
  if(seen.has($('ssid').value))list.value=$('ssid').value;
  $('scanMessage').textContent=seen.size?'�ҵ� '+seen.size+' �� 2.4 GHz ���磬��ѡ��':'δ�ҵ��ɼ��� 2.4 GHz ���磬�����·��ֶ������������硣';
 }catch(e){$('scanMessage').textContent='ɨ��ʧ�ܣ�'+e.message}finally{$('scanButton').disabled=false}
}
function ssidImage(ssid){
 const canvas=document.createElement('canvas');canvas.width=200;canvas.height=14;const ctx=canvas.getContext('2d');
 ctx.fillStyle='white';ctx.fillRect(0,0,200,14);ctx.fillStyle='black';ctx.font='12px "Microsoft YaHei","Noto Sans CJK SC",sans-serif';ctx.textBaseline='top';
 let label='WiFi: '+ssid;if(ctx.measureText(label).width>198){while(label.length&&ctx.measureText(label+'��').width>198)label=Array.from(label).slice(0,-1).join('');label+='��'}
 ctx.fillText(label,0,0);const px=ctx.getImageData(0,0,200,14).data,bytes=[];
 for(let y=0;y<14;y++)for(let x=0;x<200;x+=8){let b=0;for(let i=0;i<8;i++){const k=(y*200+x+i)*4;if(px[k]+px[k+1]+px[k+2]>=384)b|=128>>i}bytes.push(b)}
 return btoa(String.fromCharCode(...bytes));
}
async function save(e){e.preventDefault();if(!$('ssid').value){$('manualWifi').open=true;$('message').textContent='����ѡ�� Wi-Fi�����ֶ������������ơ�';$('ssid').focus();return;}if(!e.target.reportValidity())return;saving=true;$('saveButton').disabled=true;$('message').textContent='���ڱ������ã��뱣�� USB ���ӡ�';try{const c={};for(const k of ['ssid','password','host','token','ca'])c[k]=$(k).value;for(const k of ['port','holdMs','recordSeconds','refreshMinutes'])c[k]=Number($(k).value);c.tls=$('tls').checked;c.ssidBitmap=ssidImage(c.ssid);const r=await api('/api/config',c);$('password').value='';$('token').value='';$('message').textContent=r.message;}catch(e){$('message').textContent='����ʧ�ܣ�'+e.message}finally{saving=false;$('saveButton').disabled=false}}
async function closePortal(){try{const r=await api('/api/close',{});$('message').textContent=r.message}catch(e){$('message').textContent=e.message}}
if(auth)login();
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
  if (origin.length() && origin!="http://"+configWeb.hostHeader()) {portalError(403,"��ֱ�Ӵ��豸��������ҳ");return false;}
  String key=configWeb.header("X-Setup-Key");
  unsigned difference=key.length()^portalKey.length();
  for(unsigned i=0;i<key.length() && i<portalKey.length();i++)difference|=(uint8_t)key[i]^(uint8_t)portalKey[i];
  if(difference){portalError(401,"�������벻��ȷ����鿴�豸��Ļ KEY ��");return false;}
  return true;
}
static int configFailure(JsonDocument &out,int code,const char *error){out["error"]=error;return code;}
static void deviceConfig(JsonDocument &d){
 d["firmware"]="codex-pocket-0.5.2";d["ssid"]=wifiSsid;d["host"]=remoteHost;d["port"]=remotePort;d["tls"]=tlsEnabled;d["ca"]=rootCa;
 d["tokenConfigured"]=remoteToken.length()>0;d["holdMs"]=holdMs;d["recordSeconds"]=recordSeconds;d["refreshMinutes"]=refreshMinutes;
}
// Scans run asynchronously. Disconnect STA briefly to avoid driver busy errors,
// retain credentials, then resume the saved network even if the page is closed.
static int wifiScanPhase=0,wifiScanAttempts=0;
static uint32_t wifiScanAt=0;
static void resumeAfterScan(){WiFi.setAutoReconnect(true);if(wifiConfigured)WiFi.begin(wifiSsid.c_str(),wifiPass.c_str());}
static int startWifiScan(JsonDocument &out){
 if(recording || uploadPending || syntheticRequested){out["error"]="���Ƚ���¼�����ϴ�����ɨ�� Wi-Fi";return 409;}
 if(wifiScanPhase==1 || wifiScanPhase==2){out["state"]="scanning";return 200;}
 WiFi.setAutoReconnect(false);(void)esp_wifi_disconnect();wifiScanPhase=1;wifiScanAttempts=0;wifiScanAt=millis()+400;
 out["state"]="scanning";return 200;
}
static void loopWifiScan(){
 if(wifiScanPhase==1 && (int32_t)(millis()-wifiScanAt)>=0){
   int result=WiFi.scanNetworks(true,false,false,150);wifiScanAttempts++;
   if(result==WIFI_SCAN_RUNNING)wifiScanPhase=2;
   else if(result==WIFI_SCAN_FAILED && wifiScanAttempts<8)wifiScanAt=millis()+500;
   else if(result==WIFI_SCAN_FAILED){wifiScanPhase=4;resumeAfterScan();}
   else{wifiScanPhase=3;resumeAfterScan();}
 } else if(wifiScanPhase==2){
   int result=WiFi.scanComplete();
   if(result!=WIFI_SCAN_RUNNING){wifiScanPhase=result==WIFI_SCAN_FAILED?4:3;resumeAfterScan();}
 }
}
static void wifiScanResult(JsonDocument &out){
 if(wifiScanPhase==1 || wifiScanPhase==2){out["state"]="scanning";return;}
 if(wifiScanPhase==4){out["state"]="error";out["error"]="�豸δ������ Wi-Fi ɨ��";return;}
 if(wifiScanPhase!=3){out["state"]="idle";return;}
 out["state"]="done";JsonArray networks=out["networks"].to<JsonArray>();int count=WiFi.scanComplete();
 for(int i=0;i<count && i<60;i++){
   String ssid=WiFi.SSID(i);if(!ssid.length())continue;
   JsonObject network=networks.add<JsonObject>();network["ssid"]=ssid;network["rssi"]=WiFi.RSSI(i);
   network["channel"]=WiFi.channel(i);network["secure"]=WiFi.encryptionType(i)!=WIFI_AUTH_OPEN;
 }
}
static void deviceStatus(JsonDocument &d){
 bool wifi=WiFi.status()==WL_CONNECTED;
 d["firmware"]="codex-pocket-0.5.2";d["wifiScanning"]=(wifiScanPhase==1 || wifiScanPhase==2);d["usbConnected"]=HWCDC::isPlugged();d["wifiConnected"]=wifi;d["wifiReason"]=(int)lastWifiDisconnectReason;d["configuredSsid"]=wifiSsid;
 d["connectedSsid"]=wifi?WiFi.SSID():"";d["ssidImageReady"]=ssidBitmapValid;d["ip"]=WiFi.localIP().toString();d["gateway"]=WiFi.gatewayIP().toString();
 if(wifi)d["rssi"]=WiFi.RSSI();else d["rssi"]=nullptr;
 if(wifi && netConnected)d["internetReachable"]=true;else d["internetReachable"]=nullptr;
 d["internetDetail"]=wifi && netConnected?"�豸����ͨ�ƶ˼��ܷ���":wifi?"Wi-Fi �����ӣ�������δ��֤":"��δ���� Wi-Fi";
 d["cloudConnected"]=wifi && netConnected && online;d["cloudTransportConnected"]=wifi && netConnected;d["host"]=remoteHost;d["port"]=remotePort;
 d["chip"]=ESP.getChipModel();d["cores"]=ESP.getChipCores();d["cpuMHz"]=getCpuFrequencyMhz();d["flashBytes"]=ESP.getFlashChipSize();
 d["freeHeap"]=ESP.getFreeHeap();d["uptimeSeconds"]=millis()/1000;d["mac"]=WiFi.macAddress();d["battery"]=localBattery;
 d["audioStorageReady"]=audioStoreReady;d["audioStorageFree"]=audioStoreReady?FFat.freeBytes():0;
 d["recording"]=(bool)recording;d["uploadPending"]=(bool)uploadPending;d["uploadProgress"]=(unsigned)uploadProgress;
 d["audioBytes"]=(unsigned)audioBytes;d["audioError"]=audioFailure;d["uploadRetries"]=(unsigned)uploadRetries;d["audioEndSent"]=(bool)audioEndSent;d["uploadCodec"]=remoteMulaw8k?"mulaw8k":"pcm16";d["syntheticUpload"]=(bool)syntheticUpload;
}
static int saveDeviceConfig(const JsonDocument &doc,JsonDocument &result){
    String ssid=doc["ssid"]|"",password=doc["password"]|"",host=doc["host"]|"",key=doc["token"]|"",ca=normalizeRootCa(String(doc["ca"]|""));
    int port=doc["port"]|443,h=1000,r=doc["recordSeconds"]|20,m=doc["refreshMinutes"]|15;bool tls=doc["tls"]|true;
    bool validHost=host.length()<=253;
    for(unsigned i=0;i<host.length();i++)if(!isalnum((unsigned char)host[i]) && host[i]!='.' && host[i]!='-')validHost=false;
    if(!ssid.length()||ssid.length()>32||password.length()>63||!validHost||port<1||port>65535||h<800||h>2500||r<2||r>20||m<5||m>60||key.length()>256||ca.length()>8192){return configFailure(result,400,"�����ȵ㡢�������˿ں͹��ܲ����ķ�Χ");}
    if(host.length() && tls && ca.indexOf("-----BEGIN CERTIFICATE-----")<0){return configFailure(result,400,"TLS ��Ҫ��ȷ�� PEM �� CA ֤��");}
    if(host.length() && tls){
      mbedtls_x509_crt certificate;mbedtls_x509_crt_init(&certificate);
      int certificateResult=mbedtls_x509_crt_parse(&certificate,(const unsigned char*)ca.c_str(),ca.length()+1);
      mbedtls_x509_crt_free(&certificate);
      if(certificateResult!=0){return configFailure(result,400,"�� CA ֤���޷������������µ���ԭʼ���� JSON �ļ�");}
    }
    if(host.length() && !key.length() && !remoteToken.length()){return configFailure(result,400,"�뵼���ƶ��������ã�����д�豸������Կ");}
    uint8_t bitmap[350];size_t bitmapLength=0;bool hasBitmap=doc["ssidBitmap"].is<const char*>();
    if(hasBitmap){String encoded=doc["ssidBitmap"].as<String>();
      if(encoded.length()>500 || mbedtls_base64_decode(bitmap,sizeof(bitmap),&bitmapLength,(const unsigned char*)encoded.c_str(),encoded.length())!=0 || bitmapLength!=sizeof(bitmap))
        return configFailure(result,400,"Wi-Fi ������ʾͼ���ʽ����");
    }
    prefs.begin("pocket",false);
    if(hasBitmap){if(prefs.putBytes("ssid_img",bitmap,sizeof(bitmap))!=sizeof(bitmap)){prefs.end();return configFailure(result,500,"Wi-Fi ������ʾͼ�񱣴�ʧ��");}}
    else if(ssid!=wifiSsid)prefs.remove("ssid_img");
    if(password.length()||ssid!=wifiSsid)prefs.putString("pass",password);
    prefs.putString("ssid",ssid);prefs.putString("host",host);prefs.putUInt("port",port);prefs.putString("path","/device");prefs.putBool("tls",tls);prefs.putString("ca",ca);
    if(key.length())prefs.putString("token",key);
    prefs.putUInt("hold_ms",h);prefs.putUInt("rec_secs",r);prefs.putUInt("refresh_m",m);prefs.end();
    result["message"]="�����ѱ��棬�豸����������USB ҳ����Զ�������";portalRestartAt=millis()+1500;return 200;
 }
static void loopUsbConfig(){
 static String line;static uint32_t lastByte=0;
 if(line.length() && millis()-lastByte>15000)line="";
 unsigned budget=256;
 while(budget-- && Serial.available()){
  char c=Serial.read();lastByte=millis();if(c=='\r')continue;
  if(c!='\n'){if(line.length()<14000)line+=c;else line="";continue;}
  if(!line.length())continue;
  JsonDocument req;bool parsed=deserializeJson(req,line)==DeserializationError::Ok;line="";
  if(!parsed || String(req["type"]|"")!="usb_config")continue;
  JsonDocument out,payload;out["id"]=req["id"];out["status"]=200;String method=req["method"]|"";
  if(method=="status")deviceStatus(payload);
  else if(method=="get")deviceConfig(payload);
  else if(method=="scan_start")out["status"]=startWifiScan(payload);
  else if(method=="scan_results")wifiScanResult(payload);
  else if(method=="test_upload"){
   if(recording || uploadPending || syntheticRequested || remoteVoiceBusy || !netConnected || !online)out["status"]=configFailure(payload,409,"��ȴ��豸�ƶ������ҿ���");
   else{syntheticRequested=true;payload["message"]="�ѿ�ʼ�����������ϴ�";}
  }
  else if(method=="close"){payload["message"]="�����ȵ㼴���رգ�USB ���������Կ�ʹ�á�";portalCloseAt=millis()+1000;}
  else if(method=="save"){
   if(recording || uploadPending)out["status"]=configFailure(payload,409,"���Ƚ���¼�����ϴ����ٱ�������");
   else{JsonDocument config;config.set(req["body"]);out["status"]=saveDeviceConfig(config,payload);}
  }else out["status"]=configFailure(payload,400,"��֧�ֵ� USB ���ò���");
  out["data"]=payload;String json;serializeJson(out,json);Serial.println("POCKET_USB "+json);
 }
}

static void startConfigPortal(bool automatic=false) {
  if(portalActive){if(!automatic)portalAutoOpened=false;return;}
  portalAutoOpened=automatic;
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
  ssidBitmapValid=prefs.getBytesLength("ssid_img")==sizeof(ssidBitmap);
  if(ssidBitmapValid)ssidBitmapValid=prefs.getBytes("ssid_img",ssidBitmap,sizeof(ssidBitmap))==sizeof(ssidBitmap);
  holdMs=1000;
  recordSeconds=constrain(prefs.getUInt("rec_secs",20),2U,20U);
  refreshMinutes=constrain(prefs.getUInt("refresh_m",15),5U,60U);
  bool firstWebSetup=!prefs.getBool("web_ready",false);prefs.putBool("web_ready",true);prefs.end();
  const char *headers[]={"X-Setup-Key","Origin"};configWeb.collectHeaders(headers,2);
  configWeb.on("/",HTTP_GET,[](){configWeb.sendHeader("Cache-Control","no-store");configWeb.send_P(200,"text/html; charset=utf-8",CONFIG_PAGE);});
  configWeb.on("/api/config",HTTP_GET,[](){
    if(!portalAuthorized())return;
    JsonDocument doc;deviceConfig(doc);portalJson(200,doc);
  });
  configWeb.on("/api/status",HTTP_GET,[](){if(!portalAuthorized())return;JsonDocument doc;deviceStatus(doc);portalJson(200,doc);});
  configWeb.on("/api/wifi/scan",HTTP_POST,[](){if(!portalAuthorized())return;JsonDocument d;int status=startWifiScan(d);portalJson(status,d);});
  configWeb.on("/api/wifi/scan",HTTP_GET,[](){if(!portalAuthorized())return;JsonDocument d;wifiScanResult(d);portalJson(200,d);});
  configWeb.on("/api/config",HTTP_POST,[](){
    if(!portalAuthorized())return;
    String body=configWeb.arg("plain");if(body.length()>12000){portalError(413,"�������ݹ���");return;}
    JsonDocument doc;if(deserializeJson(doc,body)){portalError(400,"���ø�ʽ����");return;}
    if(recording || uploadPending){portalError(409,"���Ƚ���¼�����ϴ����ٱ�������");return;}
    JsonDocument result;int status=saveDeviceConfig(doc,result);portalJson(status,result);
  });
  configWeb.on("/api/close",HTTP_POST,[](){if(!portalAuthorized())return;JsonDocument result;result["message"]="�����ȵ㼴���رգ��豸����ͨ�� Wi-Fi ������";portalJson(200,result);portalCloseAt=millis()+1000;});
  configWeb.onNotFound([](){configWeb.sendHeader("Location","http://"+WiFi.softAPIP().toString()+"/",true);configWeb.send(302,"text/plain","");});
  configWeb.begin();portalBootAt=millis();
  if(firstWebSetup||!wifiConfigured)startConfigPortal(true);
}
static void loopConfigPortal() {
  configWeb.handleClient();
  if(portalActive)configDns.processNextRequest();
  if(portalRestartAt && (int32_t)(millis()-portalRestartAt)>=0)ESP.restart();
  if(portalCloseAt && (int32_t)(millis()-portalCloseAt)>=0){portalCloseAt=0;portalActive=false;configDns.stop();WiFi.softAPdisconnect(true);offlineStage=-1;imageDirty=true;}
  if(!portalActive && WiFi.status()!=WL_CONNECTED && millis()-portalBootAt>30000)startConfigPortal(true);
  if(portalActive && portalAutoOpened && WiFi.status()==WL_CONNECTED && WiFi.softAPgetStationNum()==0 && !portalCloseAt)portalCloseAt=millis()+500;
}

