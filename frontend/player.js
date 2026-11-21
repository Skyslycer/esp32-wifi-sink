const ingressUrl=path=>new URL(path.replace(/^\//,''),new URL('./',window.location.href));
'use strict';
const $=id=>document.getElementById(id);
let devices=[],favorites=[],selected=localStorage.getItem('radio-sink')||'',connected=false;
let favoriteSignature='',resultSignature='';
const current=()=>devices.find(d=>d.device_id===selected);
const path=s=>`/api/devices/${encodeURIComponent(selected)}${s}`;
function el(tag,cls,text){const n=document.createElement(tag);n.className=cls||'';if(text!==undefined)n.textContent=text;return n;}
async function api(url,body,method='POST'){const r=await fetch(ingressUrl(url),body===undefined?{}:{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const d=await r.json();if(!r.ok)throw Error(d.error?.message||d.detail?.message||d.error?.code||'Could not complete request');return d;}
async function act(work){try{$('notice').hidden=true;await work();await refresh();}catch(e){$('notice').textContent=e.message;$('notice').hidden=false;}}
function art(station,cls){const n=el('span',cls,(station?.name||'♪').slice(0,2));try{const u=new URL(station?.favicon);if(['http:','https:'].includes(u.protocol)){const img=el('img');img.src=u.href;img.alt='';img.referrerPolicy='no-referrer';img.onload=()=>n.firstChild?.nodeType===3&&n.firstChild.remove();img.onerror=()=>img.remove();n.append(img);}}catch{}return n;}
function saved(station){return favorites.find(f=>f.id===station?.id||(f.stationuuid&&f.stationuuid===station?.stationuuid)||f.url===station?.url);}
function play(station){return act(()=>api(path('/player/play'),station.id&&!station.id.startsWith('preview-')&&favorites.some(f=>f.id===station.id)?{station_id:station.id}:{station:Object.fromEntries(Object.entries(station).filter(([k])=>['name','url','stationuuid','favicon','codec','bitrate','countrycode','language','lastcheckok'].includes(k)))}));}
function render(){const d=current();const station=['spotify','airplay','wfas'].includes(d?.player?.source)?null:d?.player?.station;const active=d?.player?.state==='playing';
 const ids=devices.map(v=>`${v.device_id}:${v.display_name}`).join();if($('sink-select').dataset.ids!==ids){const options=devices.map(v=>{const o=el('option','',v.display_name);o.value=v.device_id;return o;});if(!options.length){const option=el('option','','No sinks found yet');option.value='';options.push(option);}$('sink-select').replaceChildren(...options);$('sink-select').dataset.ids=ids;}$('sink-select').value=selected;
 PlaybackCard.render(d,favorites,connected);
 $('radio-sections').hidden=d?.radio_enabled===false;
 document.querySelector('.wifi-streaming').hidden=!d||d.wfas_allowed===false;
 renderWFASAction();
 StationCards.render(favorites,saved(station)?.id,!!(connected&&d?.online),false);
 $('favorites-empty').hidden=!!favorites.length;
}
function apply(data){devices=data.devices||devices;favorites=data.favorites||favorites;if(!devices.some(d=>d.device_id===selected))selected=devices.find(d=>d.online)?.device_id||devices[0]?.device_id||'';connected=true;render();}
async function refresh(){try{const [d,f]=await Promise.all([api('/api/devices'),api('/api/radio/favorites')]);apply({devices:d.devices,favorites:f.favorites});}catch{connected=false;render();}}
$('sink-select').onchange=e=>{selected=e.target.value;localStorage.setItem('radio-sink',selected);render();};
StationCards.bind({
 play:station=>play(station),
 save:async(id,values)=>{await api('/api/radio/favorites/'+encodeURIComponent(id),values,'PUT');await refresh();},
 remove:async id=>{await api('/api/radio/favorites/'+encodeURIComponent(id),{},'DELETE');await refresh();},
 reorder:async ids=>{await api('/api/radio/favorites/reorder',{ids});await refresh();},
 refresh,
 error:error=>{$('notice').textContent=error.message;$('notice').hidden=false;}
});
PlaybackCard.bind({
 disconnect:()=>act(()=>api(path('/wfas'),{enabled:false})),
 transport:action=>act(()=>api(path('/player/'+action),{})),
 volume:volume=>act(()=>api(path('/player/volume'),{volume})),
 favorite:(station,favorite)=>act(async()=>{
  if(favorite)return api('/api/radio/favorites/'+encodeURIComponent(favorite.id),{},'DELETE');
  const allowed=['name','url','stationuuid','favicon','codec','bitrate','countrycode','language','lastcheckok'];
  await api('/api/radio/favorites',Object.fromEntries(Object.entries(station).filter(([k])=>allowed.includes(k))));
 })
});
const regions=new Intl.DisplayNames(['en'],{type:'region'});
$('search-form').onsubmit=e=>{e.preventDefault();act(async()=>{$('search-status').textContent='Searching…';const q=new URLSearchParams({name:$('search-name').value.trim()});const r=await api('/api/radio/search?'+q);$('search-status').textContent=`${r.stations.length} ${r.stations.length===1?'station':'stations'} found`;$('search-results').replaceChildren(...r.stations.map(s=>{const row=el('div','station-row station-search-row');const info=el('div','row-info');info.append(el('strong','',s.name),el('p','',[regions.of(s.countrycode||'ZZ')].filter(Boolean).join(' · ')));const b=el('button','button station-play-button','▶');b.setAttribute('aria-label',`Play ${s.name}`);b.title=`Play ${s.name}`;b.disabled=!current()?.online;b.onclick=()=>play(s);row.append(art(s,'station-art'),info,b);return row;}));});};
refresh();const events=new EventSource(ingressUrl('/api/events'));events.addEventListener('state',e=>{try{apply(JSON.parse(e.data));}catch{}});events.onerror=()=>{connected=false;render();};setInterval(()=>{if(!connected)refresh();},3500);





let wfasServers=[];
function selectedWFASConnected(){
 const device=current();
 return !!(device?.wfas?.ready&&$('wfas-server').value===device.wfas_host+':'+(device.wfas_port||9090));
}
function renderWFASAction(){
 const button=$('wfas-connect');
 button.textContent=selectedWFASConnected()?'Disconnect':'Connect';
 button.disabled=!current()?.online||current()?.wfas_allowed===false||(!selectedWFASConnected()&&!wfasServers.some(s=>s.id===$('wfas-server').value&&s.compatible));
}

async function discoverWFAS(){
 try{
  const data=await api('/api/wfas/servers');
  wfasServers=[...(data.servers||[])];
  const device=current();
  if(device?.online&&device?.wfas?.ready&&device.wfas_host&&!wfasServers.some(s=>s.host===device.wfas_host&&s.port===(device.wfas_port||9090))){
   wfasServers.unshift({id:device.wfas_host+':'+(device.wfas_port||9090),host:device.wfas_host,port:device.wfas_port||9090,name:device.wfas_name||device.wfas.name||'Selected sender',sample_rate:device.wfas_sample_rate||44100,compatible:true});
  }
  const select=$('wfas-server'), previous=select.value;
  const signature=JSON.stringify([wfasServers,device?.device_id,device?.wfas?.ready]);
  if(select.dataset.signature!==signature){
   select.replaceChildren(...wfasServers.map(server=>{
    const connected=device?.wfas?.ready&&server.host===device.wfas_host&&server.port===(device.wfas_port||9090);
    const option=el('option','',server.name+(connected?' [connected]':'')+(server.reason?' ('+server.reason+')':''));
    option.value=server.id;option.disabled=!server.compatible;return option;
   }));
   if(!wfasServers.length){const option=el('option','','No senders found');option.value='';select.append(option);}
   if(wfasServers.some(s=>s.id===previous&&s.compatible))select.value=previous;
   select.dataset.signature=signature;
  }
 }catch{
  delete $('wfas-server').dataset.signature;
  $('wfas-server').replaceChildren(el('option','','Discovery unavailable'));
  wfasServers=[];
 }
 renderWFASAction();
}
$('wfas-form').onsubmit=event=>{
 event.preventDefault();
 if(current()?.wfas_allowed===false)return;
 if(selectedWFASConnected()){act(()=>api(path('/wfas'),{enabled:false}));return;}
 const server=wfasServers.find(s=>s.id===$('wfas-server').value&&s.compatible);
 if(server)act(()=>api(path('/wfas'),{enabled:true,host:server.host,port:server.port,sample_rate:server.sample_rate}));
};
$('wfas-server').onchange=renderWFASAction;

discoverWFAS();setInterval(discoverWFAS,3000);

$('search-name').value='';
window.addEventListener('pageshow',()=>{$('search-name').value='';});
