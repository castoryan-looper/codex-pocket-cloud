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
static volatile bool configModeActive=false;
static String portalSsid,portalKey;
static uint32_t portalRestartAt=0,portalCloseAt=0,portalBootAt=0;
static unsigned recordSeconds=20,holdMs=1000,refreshMinutes=15;

static const char CONFIG_PAGE[] PROGMEM=R"HTML(
<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Codex Pocket 设备配置</title>
<style>body{max-width:640px;margin:28px auto;padding:0 18px;background:#f3f4ee;color:#20251e;font:16px system-ui}section{background:white;border:1px solid #ddd;border-radius:12px;padding:20px;margin:18px 0}label{display:block;margin:14px 0}input,textarea,button,select{font:inherit;padding:10px;border:1px solid #aaa;border-radius:6px;box-sizing:border-box}input,textarea,select{width:100%;margin-top:6px}textarea{min-height:100px}button{background:#e1ebd8;cursor:pointer;margin:5px}small{color:#666}#message{white-space:pre-wrap}h1{font-size:26px}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.card{background:#f3f4ee;padding:12px;border-radius:8px;overflow-wrap:anywhere}.card strong{display:block;margin-top:6px}dl{display:grid;grid-template-columns:100px 1fr;gap:9px}dd{margin:0;overflow-wrap:anywhere}@media(max-width:450px){.grid{grid-template-columns:1fr}}</style>
<h1>Codex Pocket 设备配置</h1><p>支持 USB 调试配置和设备 Wi-Fi 网页。语音与任务通过 Wi-Fi 连接云端。</p><p id="message" role="status"></p>
<section><h2>如何打开配置网页</h2><p><strong>优先使用 USB：</strong>用 USB 数据线连接电脑与设备，在 Chrome 或 Edge 打开 <a href="https://43.160.215.253:8448/setup/">https://43.160.215.253:8448/setup/</a>，点击“连接 USB 设备”并选择 ESP32 串口。需使用受信任的 HTTPS；配置数据在浏览器与设备之间直接传输。</p><p>刷写固件或改用其他串口工具前，请先点击网页上的“断开连接”。仅充电的线缆或充电器不算主机数据连接。</p><p><strong>没有 USB 主机连接时使用配置热点：</strong>同时按住 BOOT + PWR 约 3 秒，连接设备屏幕显示的 CPOCKET-… 热点，打开 <code>http://192.168.4.1/</code>，使用屏幕 KEY 登录。检测到 USB 主机时配置热点会关闭；拔掉数据线后，可用双键长按打开热点。已连接局域网时也可通过设备 IP 访问。</p><small>USB 优先级仅针对设备配置。语音与任务仍通过 Wi-Fi 连接云端。</small></section>
<section id="login"><label>配置密码（显示在设备屏幕的 KEY 行）<input id="key" type="password" autocomplete="current-password"></label><button onclick="login()">打开设置</button><small>忘记密码时，同时按住 BOOT 和 PWR 约 3 秒查看。</small></section>
<section id="live" hidden><h2>设备实时状态 <small id="connection"></small></h2><div id="statusGrid" class="grid"></div><details open><summary>设备信息</summary><dl id="deviceInfo"></dl></details><small>每 2 秒更新。外网状态以设备实际连接云端为依据；Wi-Fi 连接成功不等于外网已经连通。</small></section>
<form id="settings" hidden novalidate onsubmit="save(event)"><section><h2>Wi-Fi</h2><label>选择附近的 2.4 GHz Wi-Fi<select id="wifiList" onchange="chooseWifi()"><option value="">先扫描，再选择网络</option></select></label><button id="scanButton" type="button" onclick="scanWifi()">扫描附近 Wi-Fi</button><p id="scanMessage" role="status"><small>扫描时设备 Wi-Fi 会短暂断开，完成后自动重连。</small></p><details id="manualWifi"><summary>手动输入隐藏网络</summary><label>2.4GHz Wi-Fi 名称<input id="ssid" maxlength="32" required></label></details><label>热点密码<input id="password" type="password" maxlength="63" autocomplete="new-password" placeholder="已记住的网络留空保留密码"></label><details><summary>已记住的 Wi-Fi（最多 8 个）</summary><div id="savedNetworks"></div><small>选择已记住的网络时密码留空会保留。第 9 个会替换最久未使用的网络，当前正在连接的网络会尽量保留。</small></details><label><input id="clearWifiPassword" type="checkbox" style="width:auto" onchange="$('password').disabled=this.checked;if(this.checked)$('password').value=''"> 开放网络（明确清除这个网络已保存的密码）</label><p id="network"></p><button id="wifiSaveButton" type="submit">保存配置并重启设备</button><small>保存当前表单中的全部配置，设备重启后生效。</small></section>
<section><h2>云端服务连接</h2><label>导入云端导出的连接配置<input id="importFile" type="file" accept=".json,application/json" onchange="importConfig(this)"></label><label>云端域名或 IP（不含协议）<input id="host" maxlength="253"></label><label>端口<input id="port" type="number" min="1" max="65535" value="443" required></label><label><input id="tls" type="checkbox" checked style="width:auto"> 使用 TLS 加密</label><label>设备连接密钥<input id="token" type="password" autocomplete="new-password" placeholder="已有密钥时留空保留"></label><label>根 CA 证书（PEM）<textarea id="ca"></textarea></label><small>设备通过 Wi-Fi 连接云端 WSS 服务。不要把配置网页端口开放到公网。</small></section>
<section><h2>蓝牙旋钮</h2><p id="bluetoothState">等待设备状态</p><p id="bluetoothDetail" role="status"></p><p id="bluetoothPaired">已配对设备：等待读取</p><button id="bluetoothScanButton" type="button" onclick="scanBluetooth()">搜索附近蓝牙</button><label>选择蓝牙旋钮<select id="bluetoothList" onchange="$('bluetoothPairButton').disabled=!this.value"><option value="">先搜索，再选择设备</option></select></label><button id="bluetoothPairButton" type="button" onclick="bluetoothCommand('pair')" disabled>配对所选设备</button><p id="bluetoothScanMessage" role="status"></p><button type="button" onclick="bluetoothCommand('forget')">忘记旋钮</button><p><small>按一下旋钮唤醒，断开其他主机后搜索，选择设备并配对。列表仅显示 BLE 广播，最多20个；HID标识不保证兼容，配对后会进一步检查。默认音量增减为旋转，静音为按下，按住旋转的亮度减/增为返回/换组。旋转选择，按下确认；执行任务须先选中“执行这条”。</small></p></section><section><h2>按键与屏幕</h2><label>BOOT 长按进入录音（毫秒）<input id="holdMs" value="1000" type="number" min="800" max="2500" step="100"></label><label>单次录音上限（秒）<input id="recordSeconds" type="number" min="2" max="20"></label><label><input id="soundEnabled" type="checkbox" checked style="width:auto"> 开启声音反馈</label><label>提示音音量（0–100）<input id="soundVolume" type="number" value="35" min="0" max="100" step="5" required></label><button type="button" onclick="testSound()">试听已保存的音量</button><p id="soundMessage" role="status"></p><small>旋转浏览不逐格响；录音期间不播放提示音。修改后保存并重启生效。</small><label>维护全刷最小间隔（分钟）<input id="refreshMinutes" type="number" min="5" max="60"></label></section>
<button id="saveButton" type="submit">保存并重启设备</button><button type="button" onclick="openPortal()">显示设备配置画面</button><button type="button" onclick="closePortal()">退出设备配置画面</button><p>USB 页面在设备重启后会自动恢复状态，无需切换电脑网络。没有 USB 主机连接时，双键长按可打开设备配置热点。</p></form>
<script>
const $=id=>document.getElementById(id);let auth=new URLSearchParams(location.hash.slice(1)).get('key')||sessionStorage.getItem('pocketSetupKey')||'';$('key').value=auth;history.replaceState(null,'',location.pathname);let polling=false,configLoaded=false,statusInFlight=false,saving=false;
async function api(path,body){const r=await fetch(path,{method:body?'POST':'GET',headers:{'X-Setup-Key':auth,...(body?{'Content-Type':'application/json'}:{})},body:body?JSON.stringify(body):undefined});const data=await r.json();if(!r.ok)throw Error(data.error||'请求失败');return data}
function fill(c){if(c.soundEnabled!==undefined)$('soundEnabled').checked=c.soundEnabled;renderSavedNetworks(c.wifiProfiles,c.wifiProfileLimit);$('clearWifiPassword').checked=false;$('password').disabled=false;for(const k of ['ssid','host','port','ca','holdMs','recordSeconds','refreshMinutes','soundVolume'])if(c[k]!==undefined)$(k).value=c[k];if(c.ssid){const option=new Option('已保存：'+c.ssid,c.ssid,true,true);$('wifiList').replaceChildren(option);}if(c.tls!==undefined)$('tls').checked=c.tls;if(c.token)$('token').value=c.token}
function text(tag,value){const e=document.createElement(tag);e.textContent=value;return e}
let savedNetworksKey='';
function chooseSavedWifi(ssid){$('ssid').value=ssid;$('password').value='';$('clearWifiPassword').checked=false;$('password').disabled=false;let option=[...$('wifiList').options].find(o=>o.value===ssid);if(!option){option=new Option('已记住：'+ssid,ssid);$('wifiList').add(option);}$('wifiList').value=ssid;$('message').textContent='已选中 '+ssid+'；密码留空可保留，保存后将设为首选。';}
function renderSavedNetworks(list,limit){const key=JSON.stringify([list,limit]);if(key===savedNetworksKey)return;savedNetworksKey=key;const box=$('savedNetworks');box.replaceChildren();$('clearWifiPassword').disabled=!Array.isArray(list);if(!Array.isArray(list)){box.append(text('p','多 Wi-Fi 记忆需要升级设备固件到 0.9.0。'));return;}if(!list.length){box.append(text('p','尚未保存 Wi-Fi。'));return;}for(const network of list){const row=text('p',network.ssid+(network.preferred?' · 首选':'')+(network.connected?' · 当前连接':''));const use=text('button','选择');use.type='button';use.onclick=()=>chooseSavedWifi(network.ssid);const forget=text('button','忘记');forget.type='button';forget.onclick=()=>forgetWifi(network.ssid,network.connected);row.append(use,forget);box.append(row);}}
async function forgetWifi(ssid,connected){if(!confirm('忘记 '+ssid+' 和保存在设备上的密码？'+(connected?'当前连接会断开。':'')))return;try{const result=await api('/api/wifi/forget',{ssid});$('message').textContent=result.message;const c=await api('/api/config');renderSavedNetworks(c.wifiProfiles,c.wifiProfileLimit);if($('ssid').value===ssid){$('ssid').value=c.ssid||'';$('password').value='';$('clearWifiPassword').checked=false;$('password').disabled=false;$('wifiList').replaceChildren(new Option(c.ssid?'首选：'+c.ssid:'请选择 Wi-Fi',c.ssid||''));}}catch(e){$('message').textContent='忘记网络失败：'+e.message;}}
const bytes=n=>n===undefined?'--':n>=1048576?(n/1048576).toFixed(1)+' MB':Math.round(n/1024)+' KB';
async function refreshStatus(){if(statusInFlight)return;statusInFlight=true;try{const s=await api('/api/status');$('live').hidden=false;$('statusGrid').replaceChildren();$('deviceInfo').replaceChildren();$('connection').textContent=s.usbProxy?'USB 配置连接':'设备 Wi-Fi 网页';
$('bluetoothState').textContent=({unpaired:'未配对',reconnecting:'等待旋钮重连',pairing:'正在配对',connected:'旋钮已连接',unsupported_or_failed:'连接失败或HID报告不支持'})[s.bluetoothState]||'蓝牙旋钮需升级固件';$('bluetoothDetail').textContent=s.bluetoothDetail||'';if(s.bluetoothConnected){$('bluetoothPairButton').disabled=true;$('bluetoothPairButton').textContent='已连接';}else if(s.bluetoothState==='pairing'){$('bluetoothPairButton').disabled=true;$('bluetoothPairButton').textContent='正在配对…';}else{$('bluetoothPairButton').disabled=!$('bluetoothList').value;$('bluetoothPairButton').textContent='配对所选设备';}$('bluetoothPaired').textContent=s.bluetoothAddress?'已配对旋钮：'+s.bluetoothAddress+(s.bluetoothConnected?' · 已连接':' · 未连接'):'已配对设备：无';renderSavedNetworks(s.wifiProfiles,s.wifiProfileLimit);$('wifiSaveButton').disabled=$('saveButton').disabled=saving||s.deviceConnected===false||s.recording||s.uploadPending;if(s.deviceConnected===false){$('statusGrid').append(text('p',s.message||'USB 未连接，请插入设备'));return;}if(!configLoaded){const c=await api('/api/config');if(c.deviceConnected!==false){fill(c);configLoaded=true;}}
const configModes={usb:'USB 优先',hotspot:'Wi-Fi 配置热点',lan:'局域网网页',none:'等待连接'};const cards=[['配置模式',configModes[s.configurationTransport]||'等待固件状态'],['Wi-Fi',s.wifiConnected?'已连接 · '+s.connectedSsid:'未连接'],['外网',s.internetReachable?'已验证连通':s.wifiConnected?'等待验证':'未连接 Wi-Fi'],['云端服务',s.cloudConnected?'在线':s.cloudTransportConnected?'通道已连接，等待确认':'未连接'],['USB',s.usbConnected?'已连接':'未连接'],['录音 / 上传',s.recording?'正在录音':s.uploadPending?'上传中 '+s.uploadProgress+'%':'空闲'],['设备电量（电压估算）',s.battery>=0?s.battery+'%':'暂未读到']];
for(const [label,value] of cards){const c=text('div',label);c.className='card';c.append(text('strong',value));$('statusGrid').append(c);}
const reasons={201:'未找到热点，请检查名称与 2.4GHz',202:'认证失败，请检查密码与安全方式',210:'热点安全方式不兼容',211:'热点认证方式不匹配',212:'信号太弱',15:'连接握手超时',204:'连接握手超时',200:'热点信号中断'};const info=[['定位城市',s.location?.city||'定位中/未知'],['定位方式','设备 Wi-Fi 出口 IP（大致位置）'],['Wi-Fi 诊断',s.wifiConnected?'已连接':s.wifiScanning?'正在扫描 Wi-Fi':(reasons[s.wifiReason]||('等待连接（代码 '+(s.wifiReason||'--')+'）'))],['已保存 Wi-Fi',s.configuredSsid||'未配置'],['设备地址',s.ip],['网关',s.gateway],['信号强度',s.rssi==null?'--':s.rssi+' dBm'],['云端地址',(s.host||'未配置')+':'+s.port],['外网判断',s.internetDetail],['固件版本',s.firmware],['芯片',s.chip+' / '+s.cores+' 核 / '+s.cpuMHz+' MHz'],['Flash',bytes(s.flashBytes)],['空闲内存',bytes(s.freeHeap)],['录音缓存',s.audioStorageReady?bytes(s.audioStorageFree)+' 可用':'不可用'],['最近音频',bytes(s.audioBytes)],['最近错误',s.audioError||'无'],['运行时间',Math.floor(s.uptimeSeconds/60)+' 分 '+s.uptimeSeconds%60+' 秒'],['MAC',s.mac]];
for(const [label,value] of info)$('deviceInfo').append(text('dt',label),text('dd',value??'--'));
$('network').textContent=s.wifiConnected?'当前连接：'+s.connectedSsid+' · '+s.ip:'当前未连接；已保存：'+(s.configuredSsid||'无');
}catch(e){$('connection').textContent='正在重新连接设备…';$('statusGrid').replaceChildren(text('p','未能读取设备状态，请检查 USB 或网络连接。'));$('wifiSaveButton').disabled=$('saveButton').disabled=true;}finally{statusInFlight=false;}}
async function login(){auth=$('key').value;try{const c=await api('/api/config');sessionStorage.setItem('pocketSetupKey',auth);if(c.deviceConnected!==false){fill(c);configLoaded=true;}$('login').hidden=true;$('settings').hidden=false;$('live').hidden=false;$('message').textContent='';await refreshStatus();if(!polling){polling=true;setInterval(refreshStatus,2000);}}catch(e){$('message').textContent=e.message}}

async function importConfig(input){try{const c=JSON.parse(await input.files[0].text());fill(c);$('message').textContent='连接配置已填入，保存后生效。'}catch(e){$('message').textContent='无法读取配置文件：'+e.message}}
function chooseWifi(){const value=$('wifiList').value;if(value){$('ssid').value=value;$('password').value='';$('clearWifiPassword').checked=false;$('password').disabled=false;}}
async function scanWifi(){
 $('scanButton').disabled=true;$('scanMessage').textContent='正在扫描附近 Wi-Fi，请稍候…';
 try{
  await api('/api/wifi/scan',{});let result;
  for(let i=0;i<35;i++){await new Promise(resolve=>setTimeout(resolve,800));result=await api('/api/wifi/scan');if(result.state==='done'||result.state==='error')break;}
  if(!result||result.state==='scanning')throw Error('扫描超时，请重试');
  if(result.state==='error')throw Error(result.error||'设备暂时无法扫描，请重试');
  const list=$('wifiList');list.replaceChildren(new Option('请选择 Wi-Fi',''));const seen=new Set();
  for(const network of result.networks||[]){if(seen.has(network.ssid))continue;seen.add(network.ssid);const label=network.ssid+(network.saved?' · 已记住':'')+' · '+network.rssi+' dBm'+(network.secure?' · 需要密码':' · 开放网络');list.add(new Option(label,network.ssid));}
  if(seen.has($('ssid').value))list.value=$('ssid').value;
  $('scanMessage').textContent=seen.size?'找到 '+seen.size+' 个 2.4 GHz 网络，请选择。':'未找到可见的 2.4 GHz 网络，可在下方手动输入隐藏网络。';
 }catch(e){$('scanMessage').textContent='扫描失败：'+e.message}finally{$('scanButton').disabled=false}
}
function ssidImage(ssid){
 const canvas=document.createElement('canvas');canvas.width=200;canvas.height=14;const ctx=canvas.getContext('2d');
 ctx.fillStyle='white';ctx.fillRect(0,0,200,14);ctx.fillStyle='black';ctx.font='12px "Microsoft YaHei","Noto Sans CJK SC",sans-serif';ctx.textBaseline='top';
 let label='WiFi: '+ssid;if(ctx.measureText(label).width>198){while(label.length&&ctx.measureText(label+'…').width>198)label=Array.from(label).slice(0,-1).join('');label+='…'}
 ctx.fillText(label,0,0);const px=ctx.getImageData(0,0,200,14).data,bytes=[];
 for(let y=0;y<14;y++)for(let x=0;x<200;x+=8){let b=0;for(let i=0;i<8;i++){const k=(y*200+x+i)*4;if(px[k]+px[k+1]+px[k+2]>=384)b|=128>>i}bytes.push(b)}
 return btoa(String.fromCharCode(...bytes));
}
async function save(e){e.preventDefault();if(!$('ssid').value){$('manualWifi').open=true;$('message').textContent='请先选择 Wi-Fi，或手动输入网络名称。';$('ssid').focus();return;}if(!e.target.reportValidity())return;saving=true;$('wifiSaveButton').disabled=$('saveButton').disabled=true;$('message').textContent='正在保存配置，请保持 USB 连接…';try{const c={};for(const k of ['ssid','password','host','token','ca'])c[k]=$(k).value;for(const k of ['port','holdMs','recordSeconds','refreshMinutes','soundVolume'])c[k]=Number($(k).value);c.soundEnabled=$('soundEnabled').checked;c.tls=$('tls').checked;c.clearWifiPassword=$('clearWifiPassword').checked;c.ssidBitmap=ssidImage(c.ssid);const r=await api('/api/config',c);$('password').value='';$('token').value='';$('message').textContent=r.message;}catch(e){$('message').textContent='保存失败：'+e.message}finally{saving=false;$('wifiSaveButton').disabled=$('saveButton').disabled=false}}
function renderBluetoothDevices(devices){
 const list=$('bluetoothList');list.replaceChildren(new Option('请选择蓝牙旋钮',''));const seen=new Set();
 for(const device of devices||[]){if(typeof device.address!=='string'||seen.has(device.address))continue;seen.add(device.address);list.add(new Option((device.name||'未命名设备')+' · '+device.address+' · '+device.rssi+' dBm'+(device.hid?' · HID':''),device.address));}
 $('bluetoothPairButton').disabled=!seen.size;
 $('bluetoothScanMessage').textContent=seen.size?'找到 '+seen.size+' 个 BLE 设备，请选择旋钮后配对。':'未找到 BLE 设备，请唤醒旋钮并断开它与其他主机的连接。';
}
async function scanBluetooth(){
 $('bluetoothScanButton').disabled=true;$('bluetoothPairButton').disabled=true;$('bluetoothScanMessage').textContent='正在搜索附近蓝牙…';
 try{await api('/api/bluetooth/scan',{});let result;
  for(let i=0;i<25;i++){await new Promise(resolve=>setTimeout(resolve,800));result=await api('/api/bluetooth/scan');if(result.state==='done'||result.state==='error')break;}
  if(!result||result.state==='scanning')throw Error('搜索超时，请稍后重试');if(result.state==='error')throw Error(result.error||'搜索失败');renderBluetoothDevices(result.devices);
 }catch(e){$('bluetoothScanMessage').textContent='蓝牙搜索失败：'+e.message;}finally{$('bluetoothScanButton').disabled=false;}
}
async function bluetoothCommand(command){
 if(command==='forget'&&!confirm('忘记已配对旋钮？'))return;
 const button=$('bluetoothPairButton');
 try{const address=$('bluetoothList').value;if(command==='pair'&&!address)throw Error('请先搜索并选择设备');
  button.disabled=true;button.textContent=command==='pair'?'正在配对…':'正在忘记…';$('bluetoothScanMessage').textContent=command==='pair'?'正在向设备发送配对请求…':'正在忘记旋钮…';
  const r=await api('/api/bluetooth/'+command,command==='pair'?{address}:{});
  $('bluetoothScanMessage').textContent=r.message;$('message').textContent=r.message;await refreshStatus();
 }catch(e){$('bluetoothScanMessage').textContent='蓝牙操作失败：'+e.message;$('message').textContent=e.message;button.disabled=!$('bluetoothList').value;button.textContent='配对所选设备';}
}

async function testSound(){try{const r=await api('/api/sound/test',{});$('soundMessage').textContent=r.message}catch(e){$('soundMessage').textContent=e.message}}
async function openPortal(){try{const r=await api('/api/open',{});$('message').textContent=r.message}catch(e){$('message').textContent=e.message}}
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
  if (origin.length() && origin!="http://"+configWeb.hostHeader()) {portalError(403,"请直接打开设备的配置网页");return false;}
  String key=configWeb.header("X-Setup-Key");
  unsigned difference=key.length()^portalKey.length();
  for(unsigned i=0;i<key.length() && i<portalKey.length();i++)difference|=(uint8_t)key[i]^(uint8_t)portalKey[i];
  if(difference){portalError(401,"配置密码不正确，请查看设备屏幕 KEY 行");return false;}
  return true;
}
static int configFailure(JsonDocument &out,int code,const char *error){out["error"]=error;return code;}
static void deviceConfig(JsonDocument &d){
 soundStatus(d);
 describeKnownWifi(d);
 d["firmware"]="codex-pocket-0.11.1";d["ssid"]=wifiSsid;d["host"]=remoteHost;d["port"]=remotePort;d["tls"]=tlsEnabled;d["ca"]=rootCa;
 d["tokenConfigured"]=remoteToken.length()>0;d["holdMs"]=holdMs;d["recordSeconds"]=recordSeconds;d["refreshMinutes"]=refreshMinutes;
}
// Scans run asynchronously. Disconnect STA briefly to avoid driver busy errors,
// retain credentials, then resume the saved network even if the page is closed.
static int wifiScanPhase=0,wifiScanAttempts=0;
static uint32_t wifiScanAt=0;
#include "wifi_reconnect.h"
static void resumeAfterScan(){wifiScanAt=millis();wifiResumeAfterManualScan();}
static int startWifiScan(JsonDocument &out){
 if(recording || uploadPending || syntheticRequested){out["error"]="请先结束录音或上传，再扫描 Wi-Fi";return 409;}
 if(wifiScanPhase==1 || wifiScanPhase==2){out["state"]="scanning";return 200;}
 if(wifiAutoPhase){out["error"]="正在自动搜索已保存网络，请稍后重试";return 409;}
 wifiPauseForManualScan();(void)esp_wifi_disconnect();wifiScanPhase=1;wifiScanAttempts=0;wifiScanAt=millis()+400;
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
 if(wifiScanPhase==4){out["state"]="error";out["error"]="设备未能启动 Wi-Fi 扫描";return;}
 if(wifiScanPhase!=3){out["state"]="idle";return;}
 out["state"]="done";JsonArray networks=out["networks"].to<JsonArray>();int count=WiFi.scanComplete();
 for(int i=0;i<count && i<60;i++){
   String ssid=WiFi.SSID(i);if(!ssid.length())continue;
   JsonObject network=networks.add<JsonObject>();network["ssid"]=ssid;network["rssi"]=WiFi.RSSI(i);
   network["saved"]=knownWifiFind(knownWifi,ssid.c_str())>=0;
   network["channel"]=WiFi.channel(i);network["secure"]=WiFi.encryptionType(i)!=WIFI_AUTH_OPEN;
 }
}
static void deviceStatus(JsonDocument &d){
 soundStatus(d);
 knobStatusJson(d);
 describeKnownWifi(d);
 bool wifi=WiFi.status()==WL_CONNECTED;
 d["recommendationView"]=remoteRecommendationReader?"reader":remoteRecommendationUi?"home":"";
 d["configurationTransport"]=HWCDC::isPlugged()?"usb":portalActive?"hotspot":wifi?"lan":"none";
 d["configurationHotspotActive"]=(bool)portalActive;d["configurationMode"]=(bool)configModeActive;
 deviceLocationStatus(d);
 d["firmware"]="codex-pocket-0.11.1";d["wifiScanning"]=(wifiScanPhase==1 || wifiScanPhase==2 || wifiAutoPhase!=0);d["usbConnected"]=HWCDC::isPlugged();d["wifiConnected"]=wifi;d["wifiReason"]=(int)lastWifiDisconnectReason;d["configuredSsid"]=wifiSsid;
 d["connectedSsid"]=wifi?WiFi.SSID():"";d["ssidImageReady"]=ssidBitmapValid;d["ip"]=WiFi.localIP().toString();d["gateway"]=WiFi.gatewayIP().toString();
 if(wifi)d["rssi"]=WiFi.RSSI();else d["rssi"]=nullptr;
 if(wifi && netConnected)d["internetReachable"]=true;else d["internetReachable"]=nullptr;
 d["internetDetail"]=wifi && netConnected?"设备已连通云端加密服务":wifi?"Wi-Fi 已连接，外网尚未验证":"尚未连接 Wi-Fi";
 d["cloudConnected"]=wifi && netConnected && online;d["cloudTransportConnected"]=wifi && netConnected;d["host"]=remoteHost;d["port"]=remotePort;
 d["chip"]=ESP.getChipModel();d["cores"]=ESP.getChipCores();d["cpuMHz"]=getCpuFrequencyMhz();d["flashBytes"]=ESP.getFlashChipSize();
 d["freeHeap"]=ESP.getFreeHeap();d["largestFreeHeap"]=ESP.getMaxAllocHeap();d["uptimeSeconds"]=millis()/1000;d["mac"]=WiFi.macAddress();d["battery"]=localBattery;
 d["audioStorageReady"]=audioStoreReady;d["audioStorageFree"]=audioStoreReady?FFat.freeBytes():0;
 d["recording"]=(bool)recording;d["uploadPending"]=(bool)uploadPending;d["uploadProgress"]=(unsigned)uploadProgress;
 d["audioBytes"]=(unsigned)audioBytes;d["audioError"]=audioFailure;d["uploadRetries"]=(unsigned)uploadRetries;d["audioEndSent"]=(bool)audioEndSent;d["uploadCodec"]=remoteMulaw8k?"mulaw8k":"pcm16";d["syntheticUpload"]=(bool)syntheticUpload;
}
static int saveDeviceConfig(const JsonDocument &doc,JsonDocument &result){
    if(wifiAutoPhase||wifiScanPhase==1||wifiScanPhase==2)return configFailure(result,409,"正在扫描，请等待完成后再保存配置");
    String ssid=doc["ssid"]|"",password=doc["password"]|"",host=doc["host"]|"",key=doc["token"]|"",ca=normalizeRootCa(String(doc["ca"]|""));
    if(doc["clearWifiPassword"]|false)password="";
    int volume=doc["soundVolume"]|soundVolume;bool enabled=doc["soundEnabled"]|soundEnabled;
    if(volume<0||volume>100)return configFailure(result,400,"提示音音量应为0到100");
    int port=doc["port"]|443,h=doc["holdMs"]|1000,r=doc["recordSeconds"]|20,m=doc["refreshMinutes"]|15;bool tls=doc["tls"]|true;
    bool validHost=host.length()<=253;
    for(unsigned i=0;i<host.length();i++)if(!isalnum((unsigned char)host[i]) && host[i]!='.' && host[i]!='-')validHost=false;
    if(!ssid.length()||ssid.length()>32||password.length()>63||!validHost||port<1||port>65535||h<800||h>2500||r<2||r>20||m<5||m>60||key.length()>256||ca.length()>8192){return configFailure(result,400,"请检查热点、主机、端口和功能参数的范围");}
    if(ssid.length()!=strlen(ssid.c_str())||password.length()!=strlen(password.c_str())||!knownWifiValid(ssid.c_str(),password.c_str()))return configFailure(result,400,"Wi-Fi 名称或密码无效");
    if(host.length() && tls && ca.indexOf("-----BEGIN CERTIFICATE-----")<0){return configFailure(result,400,"TLS 需要正确的 PEM 根 CA 证书");}
    if(host.length() && tls){
      mbedtls_x509_crt certificate;mbedtls_x509_crt_init(&certificate);
      int certificateResult=mbedtls_x509_crt_parse(&certificate,(const unsigned char*)ca.c_str(),ca.length()+1);
      mbedtls_x509_crt_free(&certificate);
      if(certificateResult!=0){return configFailure(result,400,"根 CA 证书无法解析，请重新导入原始连接 JSON 文件");}
    }
    if(host.length() && !key.length() && !remoteToken.length()){return configFailure(result,400,"请导入云端连接配置，或填写设备连接密钥");}
    uint8_t bitmap[350];size_t bitmapLength=0;bool hasBitmap=doc["ssidBitmap"].is<const char*>();
    if(hasBitmap){String encoded=doc["ssidBitmap"].as<String>();
      if(encoded.length()>500 || mbedtls_base64_decode(bitmap,sizeof(bitmap),&bitmapLength,(const unsigned char*)encoded.c_str(),encoded.length())!=0 || bitmapLength!=sizeof(bitmap))
        return configFailure(result,400,"Wi-Fi 名称显示图像格式错误");
    }
    KnownWifiSet updated=knownWifi;
    bool clearPassword=doc["clearWifiPassword"]|false;
    String protect=WiFi.status()==WL_CONNECTED?WiFi.SSID():"";
    if(!knownWifiUpsert(updated,ssid.c_str(),password.c_str(),password.length()>0||clearPassword,protect.c_str()))return configFailure(result,400,"无法保存 Wi-Fi 配置");
    if(!persistKnownWifi(updated))return configFailure(result,500,"Wi-Fi 记忆保存失败，请重试");
    knownWifi=updated;knownWifiPersisted=true;
    String savedPassword=knownWifi.profiles[knownWifiFind(knownWifi,ssid.c_str())].password;
    prefs.begin("pocket",false);
    if(hasBitmap){if(prefs.putBytes("ssid_img",bitmap,sizeof(bitmap))!=sizeof(bitmap)){prefs.end();return configFailure(result,500,"Wi-Fi 名称显示图像保存失败");}}
    else if(ssid!=wifiSsid)prefs.remove("ssid_img");
    prefs.putString("pass",savedPassword);
    prefs.putString("ssid",ssid);prefs.putString("host",host);prefs.putUInt("port",port);prefs.putString("path","/device");prefs.putBool("tls",tls);prefs.putString("ca",ca);
    if(key.length())prefs.putString("token",key);
    prefs.putBool("sound_on",enabled);prefs.putUInt("sound_vol",volume);
    prefs.putUInt("hold_ms",h);prefs.putUInt("rec_secs",r);prefs.putUInt("refresh_m",m);prefs.end();
    result["message"]="配置已保存，设备即将重启，USB 页面会自动重连。";portalRestartAt=millis()+1500;return 200;
 }
static void startConfigPortal(bool automatic);
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
  else if(method=="wifi_forget"){String ssid=req["body"]["ssid"]|"";out["status"]=forgetKnownWifi(ssid,payload);}
  else if(method=="bluetooth_scan_start")out["status"]=knobScanStart(payload);
  else if(method=="bluetooth_scan_results")knobScanResults(payload);
  else if(method=="bluetooth_pair"||method=="bluetooth_forget")out["status"]=knobCommand(method=="bluetooth_forget",payload,String(req["body"]["address"]|""));
  else if(method=="test_reconnect"){if(recording||uploadPending||captureRequested)out["status"]=configFailure(payload,409,"设备忙，请稍后重试");else{cloudReconnectRequested=true;payload["message"]="已请求重连云端";}}
  else if(method=="test_sound")out["status"]=soundTest(payload);
  else if(method=="test_upload"){
   if(recording || uploadPending || syntheticRequested || remoteVoiceBusy || !netConnected || !online)out["status"]=configFailure(payload,409,"请等待设备云端在线且空闲");
   else{syntheticRequested=true;payload["message"]="已开始无语音测试上传";}
  }
  else if(method=="open"){startConfigPortal(false);payload["message"]="设备已进入配置画面，PWR 长按可退出。";}
  else if(method=="close"){payload["message"]="设备即将退出配置画面，USB 网页仍可使用。";portalCloseAt=millis()+1000;}
  else if(method=="save"){
   if(recording || uploadPending)out["status"]=configFailure(payload,409,"请先结束录音或上传，再保存配置");
   else{JsonDocument config;config.set(req["body"]);out["status"]=saveDeviceConfig(config,payload);}
  }else out["status"]=configFailure(payload,400,"不支持的 USB 配置操作");
  out["data"]=payload;String json;serializeJson(out,json);Serial.println("POCKET_USB "+json);
 }
}

static void startConfigPortal(bool automatic=false) {
  configModeActive=true;imageDirty=true;
  if(!automatic || !portalActive)portalAutoOpened=automatic;
  // Configuration mode stays visible while USB replaces the AP transport.
  if(HWCDC::isPlugged() || portalActive)return;
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
  soundEnabled=prefs.getBool("sound_on",true);soundVolume=constrain(prefs.getUInt("sound_vol",35),0U,100U);
  holdMs=constrain(prefs.getUInt("hold_ms",1000),800U,2500U);
  recordSeconds=constrain(prefs.getUInt("rec_secs",20),2U,20U);
  refreshMinutes=constrain(prefs.getUInt("refresh_m",15),5U,60U);
  bool firstWebSetup=!prefs.getBool("web_ready",false);prefs.putBool("web_ready",true);prefs.end();
  const char *headers[]={"X-Setup-Key","Origin"};configWeb.collectHeaders(headers,2);
  configWeb.on("/",HTTP_GET,[](){configWeb.sendHeader("Cache-Control","no-store");configWeb.send_P(200,"text/html; charset=utf-8",CONFIG_PAGE);});
  configWeb.on("/api/bluetooth/scan",HTTP_POST,[](){if(!portalAuthorized())return;JsonDocument out;int code=knobScanStart(out);portalJson(code,out);});
  configWeb.on("/api/bluetooth/scan",HTTP_GET,[](){if(!portalAuthorized())return;JsonDocument out;knobScanResults(out);portalJson(200,out);});
  configWeb.on("/api/sound/test",HTTP_POST,[](){if(!portalAuthorized())return;JsonDocument out;int code=soundTest(out);portalJson(code,out);});
  configWeb.on("/api/bluetooth/pair",HTTP_POST,[](){if(!portalAuthorized())return;JsonDocument out;JsonDocument body;if(deserializeJson(body,configWeb.arg("plain"))){portalError(400,"配对请求格式错误");return;}int code=knobCommand(false,out,String(body["address"]|""));portalJson(code,out);});
  configWeb.on("/api/bluetooth/forget",HTTP_POST,[](){if(!portalAuthorized())return;JsonDocument out;int code=knobCommand(true,out);portalJson(code,out);});
  configWeb.on("/api/config",HTTP_GET,[](){
    if(!portalAuthorized())return;
    JsonDocument doc;deviceConfig(doc);portalJson(200,doc);
  });
  configWeb.on("/api/status",HTTP_GET,[](){if(!portalAuthorized())return;JsonDocument doc;deviceStatus(doc);portalJson(200,doc);});
  configWeb.on("/api/wifi/scan",HTTP_POST,[](){if(!portalAuthorized())return;JsonDocument d;int status=startWifiScan(d);portalJson(status,d);});
  configWeb.on("/api/wifi/forget",HTTP_POST,[](){if(!portalAuthorized())return;JsonDocument body,result;if(deserializeJson(body,configWeb.arg("plain"))){portalError(400,"配置格式错误");return;}String ssid=body["ssid"]|"";int status=forgetKnownWifi(ssid,result);portalJson(status,result);});
  configWeb.on("/api/wifi/scan",HTTP_GET,[](){if(!portalAuthorized())return;JsonDocument d;wifiScanResult(d);portalJson(200,d);});
  configWeb.on("/api/config",HTTP_POST,[](){
    if(!portalAuthorized())return;
    String body=configWeb.arg("plain");if(body.length()>12000){portalError(413,"配置内容过大");return;}
    JsonDocument doc;if(deserializeJson(doc,body)){portalError(400,"配置格式错误");return;}
    if(recording || uploadPending){portalError(409,"请先结束录音或上传，再保存配置");return;}
    JsonDocument result;int status=saveDeviceConfig(doc,result);portalJson(status,result);
  });
  configWeb.on("/api/open",HTTP_POST,[](){if(!portalAuthorized())return;startConfigPortal(false);JsonDocument result;result["message"]="设备已进入配置画面";portalJson(200,result);});
  configWeb.on("/api/close",HTTP_POST,[](){if(!portalAuthorized())return;JsonDocument result;result["message"]="设备即将退出配置画面，继续通过 Wi-Fi 工作。";portalJson(200,result);portalCloseAt=millis()+1000;});
  configWeb.onNotFound([](){configWeb.sendHeader("Location","http://"+WiFi.softAPIP().toString()+"/",true);configWeb.send(302,"text/plain","");});
  configWeb.begin();portalBootAt=millis();
  if(firstWebSetup||!wifiConfigured)startConfigPortal(true);
}
static void loopConfigPortal() {
  configWeb.handleClient();
  if(portalActive)configDns.processNextRequest();
  if(portalRestartAt && (int32_t)(millis()-portalRestartAt)>=0)ESP.restart();
  if(portalCloseAt && (int32_t)(millis()-portalCloseAt)>=0){portalCloseAt=0;configModeActive=false;portalActive=false;configDns.stop();WiFi.softAPdisconnect(true);offlineStage=-1;imageDirty=true;}
  if(configModeActive && portalAutoOpened && WiFi.status()==WL_CONNECTED && (!portalActive||WiFi.softAPgetStationNum()==0) && !portalCloseAt)portalCloseAt=millis()+500;
  if(HWCDC::isPlugged()){
    if(portalActive){portalActive=false;configDns.stop();WiFi.softAPdisconnect(true);offlineStage=-1;imageDirty=true;}
    return;
  }
  if(configModeActive && !portalActive)startConfigPortal(portalAutoOpened);
  if(!configModeActive && WiFi.status()!=WL_CONNECTED && millis()-portalBootAt>30000)startConfigPortal(true);
}

