/* One playback card, shared by the listening and administration pages. */
window.PlaybackCard = (() => {
 const icons={music:'<path d="M9 18V5l12-2v13M9 9l12-2"/><ellipse cx="6" cy="18" rx="3" ry="3"/><ellipse cx="18" cy="16" rx="3" ry="3"/>',offline:'<path d="M12 13v8m-4 0h8M8.5 9.5a4 4 0 0 0 5.7 5.7M15.5 8.5a4 4 0 0 1 .5 4M5.5 6.5a8 8 0 0 0 0 11M18.5 6.5a8 8 0 0 1 1.6 8.9M3 3l18 18"/>',play:'<path d="m9 5 11 7-11 7Z"/>',pause:'<path d="M8 5v14M16 5v14"/>',previous:'<path d="M5 5v14m14-14L8 12l11 7Z"/>',next:'<path d="M19 5v14M5 5l11 7-11 7Z"/>',heart:'<path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1.1-1.1a5.5 5.5 0 0 0-7.8 7.8L12 21l8.8-8.6a5.5 5.5 0 0 0 0-7.8Z"/>',device:'<rect x="6" y="2" width="12" height="20" rx="3"/><path d="M10 18h4"/>',airplay:'<rect x="2" y="3" width="20" height="14" rx="2"/><path d="m12 13 6 8H6Z"/>'};
 const svg=name=>`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name]}</svg>`;
 const get=id=>document.getElementById(id);
 let callbacks={},device=null,favorites=[];
 const saved=station=>station&&favorites.find(f=>f.id===station.id||(f.stationuuid&&f.stationuuid===station.stationuuid)||f.url===station.url);
 function mount(){
  document.querySelector('[data-playback-card]').innerHTML=`<section class="player playback-card" aria-label="Playback controls"><div id="player-art" class="player-art" aria-hidden="true"></div><div class="now-playing"><div class="state-line" role="status"><span id="playback-dot" class="dot"></span><span id="playback-state">Connecting</span></div><h2 id="station-title">Choose a station or other source</h2><p id="stream-title" class="stream-title" hidden></p></div><div class="transport"><button id="heart" class="button secondary icon-button" type="button" aria-label="Add to favorites">${svg('heart')}</button><button id="spotify-previous" class="button secondary icon-button" type="button" aria-label="Previous track">${svg('previous')}</button><button id="play-pause" class="button player-primary icon-button" type="button" aria-label="Resume">${svg('play')}</button><button id="spotify-next" class="button secondary icon-button" type="button" aria-label="Next track">${svg('next')}</button></div><div class="volume-control"><label for="volume">Volume <output id="volume-value" for="volume">—</output></label><div class="volume-track"><input id="volume" type="range" min="0" max="100" value="10" step="1" aria-label="Volume"></div></div></section>`;
  get('play-pause').onclick=()=>{
   const wifi=device?.player?.source==='wfas'||(!device?.player?.source&&device?.wfas?.enabled);
   if(wifi)callbacks.disconnect?.();
   else callbacks.transport?.(device?.player?.state==='playing'?'pause':'resume');
  };
  get('spotify-previous').onclick=()=>callbacks.transport?.('previous');
  get('spotify-next').onclick=()=>callbacks.transport?.('next');
  get('heart').onclick=()=>callbacks.favorite?.(device?.player?.station,saved(device?.player?.station));
  get('volume').oninput=()=>{get('volume-value').textContent=get('volume').value+'%';};
  get('volume').onchange=()=>callbacks.volume?.(Number(get('volume').value)/100);
 }
 function status(d,connected){
  if(!d)return 'No sink';
  if(!connected)return 'Reconnecting';
  if(!d.online)return 'Sink offline';
  if(d.player?.state==='paused')return 'Paused';
  if((!d.player?.source||d.player.source==='wfas')&&d.wfas?.enabled){
   if(d.wfas.error)return 'Connection failed';
   if(!d.wfas.ready)return 'Connecting device';
  }
  if(d.player?.state==='playing'||d.wfas?.ready){
   if(!d.diagnostics?.output_ready)return 'Waiting for speaker';
   if(d.player?.recovery_attempts)return 'Recovering source';
   if(d.player?.state!=='playing')return 'Waiting for audio';
   if(!d.diagnostics?.active||d.diagnostics?.playout_started===false)return 'Buffering';
   return 'Playing';
  }
  return 'Stopped';
 }
 function render(d,items,connected){
  device=d;favorites=items||[];
  const source=d?.player?.source||(d?.wfas?.enabled?'wfas':null),station=d?.player?.station;
  const radio=source==='radio'&&!!station,spotify=source==='spotify',airplay=source==='airplay',wifi=source==='wfas';
  const active=d?.player?.state==='playing',enabled=!!(connected&&d?.online);
  const track=d?.spotify?.track;
  const title=!d?'Select or pair a sink':airplay?'AirPlay':wifi?'Wi-Fi streaming':spotify?(track?.name||'Spotify'):saved(station)?.name||station?.name||({'test-tone':'Quiet test tone','bench-music':'Music check'}[source])||'Choose a station or other source';
  const subtitle=wifi?(d?.wfas_name||d?.wfas?.name||''):d?.player?.stream_title||'';
  const state=status(d,connected);
  get('playback-state').textContent=state;
  get('playback-dot').className='dot '+(state==='Playing'?'playing':state.startsWith('Waiting')||state==='Buffering'||state==='Recovering source'?'waiting':'');
  get('station-title').textContent=title;
  get('stream-title').textContent=subtitle;get('stream-title').hidden=!subtitle;
  const art=get('player-art'),url=spotify?track?.album_cover_url:station?.favicon;
  const offline=!d||!connected||!d.online;
  const signature=JSON.stringify([source,title,url,offline]);
  if(art.dataset.signature!==signature){
   art.replaceChildren();
   if(offline)art.innerHTML=svg('offline');
   else if(!source)art.innerHTML=svg('music');
   else if(airplay||wifi)art.innerHTML=svg(airplay?'airplay':'device');
   else {
    art.textContent=(title||'♪').slice(0,2);
    try{const parsed=new URL(url);if(['http:','https:'].includes(parsed.protocol)){const img=document.createElement('img');img.src=parsed.href;img.alt='';img.referrerPolicy='no-referrer';img.onload=()=>{for(const child of [...art.childNodes])if(child!==img)child.remove();};img.onerror=()=>img.remove();art.append(img);}}catch{}
   }
   art.dataset.signature=signature;
  }
  get('heart').hidden=!radio;get('heart').disabled=!radio||!enabled;
  get('heart').setAttribute('aria-pressed',Boolean(saved(station)));
  get('heart').setAttribute('aria-label',saved(station)?'Remove from favorites':'Add to favorites');
  get('heart').classList.toggle('is-saved',Boolean(saved(station)));
  for(const id of ['spotify-previous','spotify-next']){get(id).hidden=!spotify;get(id).disabled=!enabled;}
  get('play-pause').hidden=airplay||!source;
  get('play-pause').disabled=airplay||!source||!enabled;
  const label=wifi?'Disconnect device':active?'Pause':'Resume';
  const primary=get('play-pause');
  primary.classList.add('text-action');
  primary.classList.remove('icon-button');
  primary.closest('.transport').classList.add('wide-action');
  primary.textContent=label;
  primary.setAttribute('aria-label',label);
  primary.title=label;
  get('play-pause').closest('.transport').hidden=airplay||!source;
  get('volume').disabled=!enabled;
  if(document.activeElement!==get('volume')){get('volume').value=Math.round((d?.volume??.1)*100);get('volume-value').textContent=get('volume').value+'%';}
 }
 mount();
 return {render,bind:handlers=>{callbacks=handlers;}};
})();
