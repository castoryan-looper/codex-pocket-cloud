const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path'),assert=require('node:assert/strict');
(async()=>{for(const file of ['web/index.html','firmware/Pocket/config_portal.h']){
 const elements=new Map();const el=id=>{if(!elements.has(id))elements.set(id,{value:'',disabled:false,textContent:'',options:[],replaceChildren(...v){this.options=v},add(v){this.options.push(v)}});return elements.get(id)};
 const calls=[];const ctx=vm.createContext({document:{getElementById:el},location:{hash:'',pathname:'/'},sessionStorage:{getItem(){return ''}},history:{replaceState(){}},URLSearchParams,Option:function(text,value){this.text=text;this.value=value},Number,JSON,confirm:()=>true,setTimeout:fn=>fn()});
 const s=fs.readFileSync(path.join(__dirname,'..',file),'utf8');vm.runInContext(s.split('<script>')[1].split('</script>')[0],ctx);
 ctx.transport=async(route,body)=>{calls.push({route,body});return route.endsWith('/scan')&&body===undefined?{state:'done',devices:[{address:'aa:bb:cc:dd:ee:ff',name:'<unsafe>',rssi:-40,hid:true},{address:'aa:bb:cc:dd:ee:ff',name:'duplicate',rssi:-41}]}:{state:'scanning',message:'ok'}};
 vm.runInContext('api=transport;refreshStatus=async()=>{}',ctx);await vm.runInContext('scanBluetooth()',ctx);
 assert.equal(el('bluetoothList').options.length,2);assert(el('bluetoothList').options[1].text.includes('<unsafe>'));assert.equal(el('bluetoothPairButton').disabled,false);assert.equal(el('bluetoothScanButton').disabled,false);
 el('bluetoothList').value='aa:bb:cc:dd:ee:ff';await vm.runInContext("bluetoothCommand('pair')",ctx);assert.equal(calls.at(-1).body.address,'aa:bb:cc:dd:ee:ff');assert.equal(el('bluetoothScanMessage').textContent,'ok');
 const count=calls.length;el('bluetoothList').value='';await vm.runInContext("bluetoothCommand('pair')",ctx);assert.equal(calls.length,count);
 ctx.transport=async()=>{throw Error('offline')};vm.runInContext('api=transport;refreshStatus=async()=>{}',ctx);await vm.runInContext('scanBluetooth()',ctx);assert.equal(el('bluetoothPairButton').disabled,true);assert.equal(el('bluetoothScanButton').disabled,false);
}console.log('Both pages: BLE scan polling, duplicate labels, selected address pairing, empty selection and offline recovery passed')})().catch(e=>{console.error(e);process.exitCode=1});
