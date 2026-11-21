import {ESPLoader,Transport,SparkMD5} from './vendor/esptool-0.7.1.js';
import {SESSION_KEY,selectedBoard,validateSettings,configurationImage,usableServerIPv4,suggestedServerIPv4} from './setup-config.js';
const $=id=>document.getElementById(id);
const url=path=>new URL(path.replace(/^\//,''),new URL('./',location.href));
const fields=['board','output','ssid','password','server','port','bclk','lrclk','dout','baud'];
let step=1,busy=false,expected=null,pendingSignature='',polling=false,catalog=null,savedBoard='',savedOutput='',localHttpsUrl=null,setupConfig=null,serverEdited=false,automaticServer='',portEdited=false;
function notice(message,error=true){$('setup-notice').textContent=message;$('setup-notice').hidden=!message;$('setup-notice').classList.toggle('success',!error);}
async function api(path,body){const response=await fetch(url(path),body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const data=await response.json();if(!response.ok)throw Error(data.error?.message||data.detail||'Could not complete setup.');return data;}
function settings(){return Object.fromEntries(fields.map(name=>[name,['port','bclk','lrclk','dout','baud'].includes(name)?Number($('setup-'+name).value):$('setup-'+name).value]));}
function remember(){try{sessionStorage.setItem(SESSION_KEY,JSON.stringify(settings()));}catch{notice('Session storage is unavailable. Settings will remain only while this page is open.');}}
function board(){return selectedBoard(catalog,$('setup-board').value);}
function updateBoard(resetPins=false,preferredOutput=''){
 const selected=board();const current=preferredOutput||$('setup-output').value;
 $('setup-output').replaceChildren(...Object.entries(selected.outputs).filter(([id])=>selected.images[id]).map(([id,label])=>{const option=document.createElement('option');option.value=id;option.textContent=label;return option;}));
 if(Object.hasOwn(selected.outputs,current)&&selected.images[current])$('setup-output').value=current;
 if(resetPins)for(const [key,value] of Object.entries(selected.i2s.default_pins))$('setup-'+key).value=value;
 pins();
}
function pins(){ $('setup-pins').hidden=$('setup-output').value!=='i2s'; }
function show(number){step=number;for(let i=1;i<=4;i++){$('step-'+i).hidden=i!==number;const item=document.querySelector(`[data-step="${i}"]`);if(i===number)item.setAttribute('aria-current','step');else item.removeAttribute('aria-current');}document.querySelector('.setup-steps').dataset.currentLabel=`Step ${number} of 4: ${document.querySelector(`[data-step="${number}"] .setup-step-label`).textContent}`;notice('');if(number===3){pendingSignature='';$('pending-clients').replaceChildren();$('approval-help').textContent='Waiting for approval requests.';poll();}$('step-'+number).querySelector('h2').setAttribute('tabindex','-1');$('step-'+number).querySelector('h2').focus();}
function prefillServer(){
 const field=$('setup-server');
 if(serverEdited||(field.value&&field.value!==automaticServer))return;
 const candidate=suggestedServerIPv4(setupConfig?.audio_server?.advertised_ipv4,location.hostname);
 if(candidate){
  field.value=candidate;automaticServer=candidate;
  try{if(sessionStorage.getItem(SESSION_KEY)!==null)remember();}catch{}
 }
}
function support(){
 let reason='';
 if(!window.isSecureContext)reason='USB flashing requires a secure browser origin. Open this page through an HTTPS reverse proxy or the optional built-in setup HTTPS listener with its certificate trusted.';
 else if(!navigator.serial)reason='Use a desktop browser with Web Serial support, such as Chrome or Edge.';
 const message=$('usb-support');message.textContent=reason;
 if(!window.isSecureContext&&localHttpsUrl){
  const link=document.createElement('a');link.href=localHttpsUrl;link.textContent=localHttpsUrl;
  message.append(document.createTextNode(' Open '),link,document.createTextNode(' and trust its certificate.'));
 }
 message.hidden=!reason;$('flash-device').disabled=busy||!!reason;
}
api('/api/setup/config').then(config=>{
 setupConfig=config;prefillServer();
 if(!portEdited)$('setup-port').value=config.audio_server?.tcp_port||8765;
 if(config.https?.enabled&&config.https.url){
  const target=new URL(config.https.url);
  if(target.protocol==='https:')localHttpsUrl=config.https.url;
 }
 support();
}).catch(()=>{});

try{const saved=JSON.parse(sessionStorage.getItem(SESSION_KEY)||'null');if(saved){savedBoard=saved.board||'';savedOutput=saved.output||'';for(const name of fields)if(name!=='board'&&saved[name]!==undefined)$('setup-'+name).value=saved[name];}}catch{}
try{portEdited=JSON.parse(sessionStorage.getItem(SESSION_KEY)||'null')?.port!==undefined;}catch{}
serverEdited=!!$('setup-server').value;prefillServer();
$('setup-port').addEventListener('input',()=>{portEdited=true;});
$('setup-server').addEventListener('input',()=>{serverEdited=true;});
pins();support();
api('/api/setup/firmware').then(manifest=>{
 catalog=manifest;
 if(manifest.schema_version!==1||!manifest.firmware_version||!Object.keys(manifest.boards||{}).length)throw Error('No board firmware is available.');
 $('setup-board').replaceChildren(...Object.entries(manifest.boards).map(([id,value])=>{const option=document.createElement('option');option.value=id;option.textContent=value.name;return option;}));
 if(Object.hasOwn(manifest.boards,savedBoard))$('setup-board').value=savedBoard;
 $('setup-board').disabled=false;updateBoard(!Object.hasOwn(manifest.boards,savedBoard),savedOutput);
}).catch(error=>{notice(error.message);});
$('setup-board').addEventListener('change',()=>{updateBoard(true);remember();});
$('setup-form').addEventListener('input',()=>{pins();remember();});
$('setup-output').addEventListener('change',()=>{pins();remember();});
$('setup-baud').addEventListener('change',remember);
$('setup-form').onsubmit=event=>{event.preventDefault();try{const s=validateSettings(settings(),board());remember();$('flash-summary').replaceChildren();for(const [label,value] of [['Board',board().name],['Firmware',`v${catalog.firmware_version}`],['Output',board().outputs[s.output]],['Wi-Fi network',s.ssid],['Server',`${s.server}:${s.port}`]]){const term=document.createElement('dt'),description=document.createElement('dd');term.textContent=label;description.textContent=value;$('flash-summary').append(term,description);}support();show(2);}catch(error){notice(error.message);}};
$('forget-settings').onclick=()=>{try{sessionStorage.removeItem(SESSION_KEY);}catch{}$('setup-form').reset();serverEdited=false;automaticServer='';portEdited=false;$('setup-port').value=setupConfig?.audio_server?.tcp_port||8765;prefillServer();$('setup-baud').value='460800';if(catalog)updateBoard(true);pins();notice('Saved setup settings cleared.',false);};
$('review-pending').onclick=()=>{expected=null;remember();show(3);};
$('back-settings').onclick=()=>show(1);$('back-flash').onclick=()=>show(expected?2:1);$('retry-approval').onclick=()=>show(3);
$('setup-another').onclick=()=>{expected=null;remember();$('flash-progress').value=0;$('flash-status').textContent='Ready to flash.';show(1);};
window.addEventListener('beforeunload',event=>{if(busy){event.preventDefault();event.returnValue='';}});
$('flash-device').onclick=async()=>{
 if(busy)return;
 let transport;
 try{
  const s=validateSettings(settings(),board());
  if(![115200,230400,460800,921600].includes(s.baud))throw Error('Choose a flash baud rate.');
  remember();
  // Port selection occurs directly from the user's click.
  const port=await navigator.serial.requestPort();
  busy=true;for(const id of ['flash-device','back-settings','retry-approval','setup-baud'])$(id).disabled=true;
  $('flash-progress').value=0;$('flash-status').textContent='Preparing firmware…';notice('');
  const selected=board();const image=selected.images[s.output];
  if(image?.url!==`assets/firmware/${s.board}/${s.output}.bin`)throw Error('Unexpected firmware path in manifest.');
  if(!image)throw Error('This output’s firmware has not been published.');
  const response=await fetch(url(image.url));if(!response.ok)throw Error('Could not download firmware.');
  const data=new Uint8Array(await response.arrayBuffer());
  const digest=[...new Uint8Array(await crypto.subtle.digest('SHA-256',data))].map(v=>v.toString(16).padStart(2,'0')).join('');
  if(digest!==image.sha256||data.length!==image.size)throw Error('Firmware verification failed. Reload and try again.');
  transport=new Transport(port,true);
  const terminal={clean(){},writeLine(){},write(){}};
  const loader=new ESPLoader({transport,baudrate:s.baud,romBaudrate:115200,terminal});
  $('flash-status').textContent='Connecting to the board…';await loader.main();
  if(loader.chip.CHIP_NAME!==selected.chip)throw Error(`The connected chip does not match ${selected.name} (${selected.chip}).`);
  const mac=await loader.chip.readMac(loader);expected='esp32-'+mac.replaceAll(':','').toLowerCase();
  if(selected.flash.offset+data.length>selected.config_offset)throw Error('Firmware overlaps the configuration partition.');
  const total=data.length+selected.config_size;let written=[0,0];
  await loader.writeFlash({fileArray:[{data,address:selected.flash.offset},{data:configurationImage(s,selected),address:selected.config_offset}],flashSize:selected.flash.size,flashMode:selected.flash.mode,flashFreq:selected.flash.frequency,eraseAll:false,compress:true,
   calculateMD5Hash:image=>SparkMD5.ArrayBuffer.hash(image.buffer.slice(image.byteOffset,image.byteOffset+image.byteLength)),
   reportProgress(index,bytes){written[index]=bytes;$('flash-progress').value=Math.min(100,Math.round((written[0]+written[1])/total*100));$('flash-status').textContent=`Writing firmware: ${$('flash-progress').value}%`;}});
  $('flash-status').textContent='Flash complete. Restarting…';
  try {
   const known=await api(`/api/setup/devices/${encodeURIComponent(expected)}/status`);
   if(known.approved)await api(`/api/devices/${encodeURIComponent(expected)}/unpair`,{});
  } finally {
   // Release GPIO0 and assert EN before the library releases reset.
   await transport.setDTR(false);await transport.setRTS(true);
   await loader.after('hard_reset',false);
  }
  await transport.disconnect();transport=null;$('flash-progress').value=100;
  show(3);
 }catch(error){notice(error.name==='NotFoundError'?'No USB port selected.':error.message||'Flashing failed. Reconnect the board and try again.');$('flash-status').textContent='Not completed. You can retry.';}
 finally{if(transport)try{await transport.disconnect();}catch{}busy=false;for(const id of ['back-settings','retry-approval','setup-baud'])$(id).disabled=false;support();}
};
function renderPending(devices){
 devices=devices.filter(d=>d.online);
 const signature=JSON.stringify(devices.map(d=>[d.device_id,d.online]));if(signature===pendingSignature)return;pendingSignature=signature;
 $('pending-clients').replaceChildren();
 $('approval-help').textContent='Waiting for approval requests.';
 for(const d of devices){
  const form=document.createElement('form');form.className='setup-client'+(d.device_id===expected?' is-current':'');
  const title=document.createElement('strong');title.textContent=d.device_id;
  const status=document.createElement('p');status.className='muted small';status.textContent='Awaiting approval';
  const label=document.createElement('label');label.textContent='Room or sink name';
  const input=document.createElement('input');input.required=true;input.maxLength=64;input.value='';input.placeholder='Living room';label.append(input);
  const button=document.createElement('button');button.className='button';button.textContent='Approve & finish';button.disabled=!d.online;button.type='submit';
  form.append(title,status,label,button);$('pending-clients').append(form);
  form.onsubmit=async event=>{event.preventDefault();button.disabled=true;try{const name=input.value.trim();if(!name)throw Error('Enter a name for this sink.');const result=await api(`/api/setup/devices/${encodeURIComponent(d.device_id)}/approve`,{display_name:name});try{localStorage.setItem('radio-sink',result.device_id);}catch{}$('setup-done').textContent=`${result.display_name} is approved and ready. Choose its speaker and source in Administration.`;show(4);}catch(error){notice(error.message);button.disabled=!d.online;}};
 }
}
async function poll(){if(step!==3||polling)return;polling=true;try{const result=await api('/api/setup/devices');if(step===3)renderPending(result.devices);}catch(error){if(step===3){renderPending([]);notice(error.message);}}finally{polling=false;}}
setInterval(poll,2000);
