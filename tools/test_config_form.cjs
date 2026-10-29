const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path'),assert=require('node:assert/strict');
(async()=>{for(const file of ['web/index.html','firmware/Pocket/config_portal.h']){
 const source=fs.readFileSync(path.join(__dirname,'..',file),'utf8');const els=new Map();function el(id){if(!els.has(id))els.set(id,{value:'',checked:false,disabled:false,options:[],replaceChildren(){},add(){}});return els.get(id)}
 const ctx=vm.createContext({document:{getElementById:el},location:{hash:'',pathname:'/'},sessionStorage:{getItem(){return ''}},history:{replaceState(){}},URLSearchParams,Option:function(){},Number,JSON,console});
 vm.runInContext(source.split('<script>')[1].split('</script>')[0],ctx);
 vm.runInContext(`renderSavedNetworks=()=>{};fill({holdMs:1800,recordSeconds:12,refreshMinutes:30,soundVolume:25,soundEnabled:true});ssidImage=()=>'';`,ctx);
 assert.equal(el('holdMs').value,1800);el('ssid').value='test-wifi';el('port').value=8448;el('host').value='example.com';
 vm.runInContext(`api=async(path,body)=>{if(!$('saveButton').disabled||!$('wifiSaveButton').disabled)throw Error('save buttons must both lock');globalThis.saved=body;return {message:'saved'}}`,ctx);
 await vm.runInContext('save({preventDefault(){},target:{reportValidity(){return true}}})',ctx);
 assert.equal(ctx.saved.holdMs,1800);assert.equal(ctx.saved.recordSeconds,12);assert.equal(ctx.saved.refreshMinutes,30);assert.equal(ctx.saved.soundVolume,25);assert.equal(ctx.saved.soundEnabled,true);assert.equal(el('wifiSaveButton').disabled,false);assert(source.includes('id="wifiSaveButton" type="submit"'));assert(!source.includes('id="holdMs" readonly'));
}console.log('Both configuration pages: load edited parameters, submit complete values, lock/unlock both save buttons passed')})().catch(e=>{console.error(e);process.exitCode=1});
