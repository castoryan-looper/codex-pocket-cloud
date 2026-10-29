/* USB data stays in the browser. Only explicitly supported configuration RPCs. */
let usbPort, usbWriter, usbReader, usbPending = new Map(), usbQueue = Promise.resolve();
let usbBuffer = '', usbSequence = 0;
const usbMethods = {
  'GET /api/status': 'status', 'GET /api/config': 'get',
  'POST /api/config': 'save', 'POST /api/wifi/scan': 'scan_start',
  'GET /api/wifi/scan': 'scan_results', 'POST /api/open': 'open',
  'POST /api/close': 'close', 'POST /api/wifi/forget': 'wifi_forget'
};
function failPending(error) {
  for (const item of usbPending.values()) { clearTimeout(item.timer); item.reject(error); }
  usbPending.clear();
}
async function readUsb() {
  const decoder = new TextDecoder();
  try {
    while (usbPort && usbPort.readable) {
      usbReader = usbPort.readable.getReader();
      try {
        while (true) {
          const {value, done} = await usbReader.read();
          if (done) break;
          usbBuffer += decoder.decode(value, {stream: true});
          if (usbBuffer.length > 40000) usbBuffer = usbBuffer.slice(-18000);
          let end;
          while ((end = usbBuffer.indexOf('\n')) >= 0) {
            const line = usbBuffer.slice(0, end); usbBuffer = usbBuffer.slice(end + 1);
            const marker = line.indexOf('POCKET_USB ');
            if (marker < 0) continue;
            try {
              const response = JSON.parse(line.slice(marker + 11));
              const item = usbPending.get(response.id);
              if (!item) continue;
              usbPending.delete(response.id); clearTimeout(item.timer);
              if (response.status !== 200) item.reject(new Error(response.data?.error || '设备请求失败'));
              else item.resolve({...response.data, usbProxy: true, deviceConnected: true});
            } catch (_) { /* Ignore unrelated serial output. */ }
          }
        }
      } finally { usbReader.releaseLock(); usbReader = null; }
    }
  } catch (_) { /* Disconnect is reported below. */ }
  failPending(new Error('USB 已断开，请重新连接设备'));
  usbPort = null; usbWriter = null;
  document.getElementById('usbMessage').textContent = 'USB 已断开，请重新连接设备';
  document.getElementById('connect').disabled = false;
}
async function usbRequest(path, body) {
  const method = usbMethods[(body === undefined ? 'GET ' : 'POST ') + path];
  if (!method) throw new Error('不支持此配置操作');
  const run = async () => {
    if (!usbWriter) throw new Error('请先连接 USB 设备');
    const id = String(++usbSequence), message = {type:'usb_config', id, method};
    if (body !== undefined) message.body = body;
    const wire = new TextEncoder().encode(JSON.stringify(message) + '\n');
    const response = new Promise((resolve, reject) => {
      const timer = setTimeout(() => {usbPending.delete(id); reject(new Error('设备未响应，请检查 USB 连接'));}, 12000);
      usbPending.set(id, {resolve, reject, timer});
    });
    try {
      for (let i=0; i<wire.length; i+=64) {
        await usbWriter.write(wire.slice(i, i+64));
        await new Promise(resolve => setTimeout(resolve, 10));
      }
    } catch (error) { failPending(error); }
    return response;
  };
  const result = usbQueue.then(run, run);
  usbQueue = result.catch(() => {});
  return result;
}
document.getElementById('connect').addEventListener('click', async () => {
  const message = document.getElementById('usbMessage');
  if (!('serial' in navigator)) { message.textContent = '请使用支持 Web Serial 的 Chrome 或 Edge，并通过受信任的 HTTPS 访问。'; return; }
  try {
    usbPort = await navigator.serial.requestPort({filters:[{usbVendorId:0x303a}]});
    await usbPort.open({baudRate:115200, bufferSize:32768});
    await usbPort.setSignals({dataTerminalReady:true, requestToSend:false});
    usbWriter = usbPort.writable.getWriter(); readUsb();
    const status = await usbRequest('/api/status');
    if (!String(status.firmware || '').startsWith('codex-pocket-')) throw new Error('此设备不是 Codex Pocket');
    message.textContent = 'USB 已连接'; document.getElementById('connect').disabled = true;
    await login();
  } catch (error) {
    message.textContent = '连接失败：' + error.message;
    if (usbReader) await usbReader.cancel().catch(() => {});
    if (usbWriter) { usbWriter.releaseLock(); usbWriter = null; }
    if (usbPort) await usbPort.close().catch(() => {});
    usbPort = null; document.getElementById('connect').disabled = false;
  }
});
