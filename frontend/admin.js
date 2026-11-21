const ingressUrl=path=>new URL(path.replace(/^\//,''),new URL('./',window.location.href));
'use strict';

const $ = (id) => document.getElementById(id);
const storage = {
  get(store, key) { try { return window[store].getItem(key); } catch { return null; } },
  set(store, key, value) { try { window[store].setItem(key, value); } catch { /* Browser may disable storage. */ } },
  remove(store, key) { try { window[store].removeItem(key); } catch { /* No stored value to clear. */ } }
};
const state = {
  devices: [], favorites: [], server: {}, selected: storage.get('localStorage', 'radio-sink') || '',

  syncDevice: null, syncSaving: false, connected: false, signatures: {}, refreshPending: null, noticeTimer: null
};

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}
function replace(id, ...nodes) { $(id).replaceChildren(...nodes); }
function setDisabled(id, disabled) { $(id).disabled = Boolean(disabled) || $(id).hasAttribute('aria-busy'); }
function current() { return state.devices.find((d) => d.device_id === state.selected); }
function available() { const d = current(); return Boolean(state.connected && d?.online); }
function devicePath(suffix = '') {
  if (!state.selected) throw new Error('Choose an audio sink first.');
  return `/api/devices/${encodeURIComponent(state.selected)}${suffix}`;
}
function showNotice(message, success = false) {
  clearTimeout(state.noticeTimer);
  const target = $('notice'); target.textContent = message;
  target.classList.toggle('success', success); target.hidden = false;
  if (success) state.noticeTimer = setTimeout(() => { target.hidden = true; }, 6500);
}
function requireKey() {}
function errorText(data, fallback) {
  const detail = data?.error || data?.detail;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) return detail.map((item) => item.msg || 'Invalid input').join('; ');
  if(detail?.code==='BT_CONNECTION_LIMIT')return 'The ESP32 Bluetooth controller rejected the connection: connection limit exceeded.';
  return detail?.message || detail?.code || fallback;
}
async function api(path, options = {}) {
  const headers = { ...(options.body !== undefined ? { 'Content-Type': 'application/json' } : {}), ...options.headers };

  const response = await fetch(ingressUrl(path), { ...options, headers, body: options.body !== undefined ? JSON.stringify(options.body) : undefined });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {

    throw new Error(errorText(data, `Request failed (${response.status}).`));
  }
  return data;
}
async function mutate(path, body, method = 'POST') {
  requireKey();
  return api(path, { method, ...(body === undefined ? {} : { body }) });
}
async function action(button, work, success) {
  if (button) { button.disabled = true; button.setAttribute('aria-busy', 'true'); }
  try {
    const result = await work();
    if (success) showNotice(typeof success === 'function' ? success(result) : success, true);
    await refresh(); return result;
  } catch (error) { showNotice(error.message || 'That action could not be completed.'); }
  finally {
    if (button) { button.disabled = false; button.removeAttribute('aria-busy'); }
    render();
  }
}
async function command(name, args = {}) {
  return mutate(devicePath('/command'), { command: name, args });
}
function outputKind(d) { return d?.hello?.output?.kind || d?.diagnostics?.output?.kind || ''; }
function bluetooth(d) { return d?.diagnostics?.bluetooth || {}; }
function logo(station, className = 'station-art') {
  const wrap = element('span', className); wrap.setAttribute('aria-hidden', 'true');
  wrap.append(element('span', '', (station?.name || '♪').trim().slice(0, 2).toUpperCase()));
  try {
    const url = new URL(station?.favicon || '');
    if (['http:', 'https:'].includes(url.protocol)) {
      const img = document.createElement('img'); img.src = url.href; img.alt = '';
      img.loading = 'lazy'; img.referrerPolicy = 'no-referrer';
      img.addEventListener('load', () => { wrap.firstElementChild.hidden = true; });
      img.addEventListener('error', () => img.remove()); wrap.append(img);
    }
  } catch { /* A missing logo keeps its station initials. */ }
  return wrap;
}
function changed(section, value) {
  const signature = JSON.stringify(value);
  if (state.signatures[section] === signature) return false;
  state.signatures[section] = signature; return true;
}
function setInputUnlessFocused(id, value) { if (document.activeElement !== $(id)) $(id).value = value ?? ''; }
function renderDevices() {
  if (!state.devices.some((d) => d.device_id === state.selected)) state.selected = state.devices.find((d) => d.online)?.device_id || state.devices[0]?.device_id || '';
  if (changed('devices', state.devices.map((d) => [d.device_id, d.display_name, d.online]))) {
    const options = state.devices.map((d) => {
      const option = element('option', '', `${d.display_name || d.device_id}${d.online ? '' : ' · offline'}`);
      option.value = d.device_id; return option;
    });
    if (!options.length) { const option = element('option', '', 'No sinks found yet'); option.value = ''; options.push(option); }
    replace('sink-select', ...options);
  }
  $('sink-select').value = state.selected;
  const d = current();
  setInputUnlessFocused('device-delta',d?.volume_delta_db||0);
  for(const k of ['radio','airplay','spotify','wfas'])setInputUnlessFocused('delta-'+k,d?.source_deltas_db?.[k]||0);
  setInputUnlessFocused('device-name', d?.display_name || '');
  for(const component of ['radio','spotify','airplay','wfas']){
   const input=$('device-'+component),key=component==='wfas'?'wfas_allowed':component+'_enabled';
   const available=Boolean(state.connected&&d&&state.server.features?.[component]);
   if(input.dataset.device!==(d?.device_id||'')){delete input.dataset.dirty;input.dataset.device=d?.device_id||'';}
   if(!input.dataset.dirty)input.checked=d?.[key]??true;
   input.disabled=!available;
   input.title=!state.server.features?.[component]?'Not installed on this server.':'';
  }
  $('device-identity').textContent = d ? `Sink ID: ${d.device_id}` : 'A sink will appear after it connects to this server.';
}
function syncLabel() {
  const value = Number($('airplay-sync').value);
  const text = value === 0 ? '0 ms' : `${Math.abs(value)} ms ${value < 0 ? 'earlier' : 'later'}`;
  $('airplay-sync-value').textContent = text;
  $('airplay-sync').setAttribute('aria-valuetext', text);
}
function renderAirPlaySync() {
  const d = current(), changedSink = state.syncDevice !== d?.device_id;
  if (changedSink || (!state.syncSaving && document.activeElement !== $('airplay-sync'))) {
    $('airplay-sync').value = d?.airplay_sync_offset_ms ?? 0;
    state.syncDevice = d?.device_id;
  }
  syncLabel();
  const disabled = !state.connected || !d || !state.server.features?.airplay || state.syncSaving;
  $('airplay-sync').disabled = disabled;
  $('airplay-sync-reset').disabled = disabled || !Number(d?.airplay_sync_offset_ms ?? 0);
  $('airplay-sync-help').textContent = !state.server.features?.airplay ? 'AirPlay is not configured on this server.' :
    !d ? 'Select a sink to adjust AirPlay sync.' : 'Saved for this sink. Allow a few seconds to settle after adjusting.';
}
async function saveAirPlaySync(value) {
  const path = devicePath('/airplay/timing');
  $('airplay-sync').value = value;
  syncLabel();
  state.syncSaving = true;
  renderAirPlaySync();
  try { await action(null, () => mutate(path, { sync_offset_ms: value }), 'AirPlay timing adjustment saved.'); }
  finally { state.syncSaving = false; state.syncDevice = null; renderAirPlaySync(); }
}
function renderPlayer() {
  const d = current();
  PlaybackCard.render(d,state.favorites,state.connected);
  if (d?.last_error?.message && d?.player?.state !== 'playing' && changed('last-error', [d.device_id, d.last_error])) showNotice(d.last_error.message);
  $('test-music').hidden = !state.server.features?.bench_music;
}
function makeButton(label, className, work) {
  const button = element('button', `button ${className || ''}`, label); button.type = 'button';
  button.addEventListener('click', () => work(button)); return button;
}
function renderFavorites() {
  StationCards.render(state.favorites,current()?.player?.station?.id,available());
  $('favorites-empty').hidden=state.favorites.length>0;
}
async function testStream(button, url) {
  await action(button, () => mutate('/api/radio/test', { url }), (data) => data.working ? 'Stream opened and decoded successfully.' : 'This stream did not produce audio.');
}
function renderBluetooth() {
  const d = current(), bt = bluetooth(d), wired = outputKind(d) === 'i2s_pcm5102';
  $('bt-wired').hidden = !wired; $('bt-controls').hidden = wired;
  if (wired) return;
  const connected = Boolean(bt.connected || d?.diagnostics?.output_ready);
  $('bt-state').textContent = bt.scanning ? 'Looking for speakers…' : bt.connecting ? 'Connecting…' : bt.state === 'reconnecting' ? 'Reconnecting…' : connected ? 'Connected' : 'Not connected';
  const names={...(d?.bluetooth_names||{})};
  for(const item of d?.bluetooth_devices||[])if(item.name)names[item.address.toLowerCase()]=item.name;
  for(const info of [d?.hello?.output,bt]){
   const address=info?.connected_address||info?.active_address;
   if(address&&info.connected_name)names[address.toLowerCase()]=info.connected_name;
  }
  const speaker=address=>names[address?.toLowerCase()]?`${names[address.toLowerCase()]} (${address})`:address;
  const target = bt.connected_name || speaker(bt.connected_address || bt.active_address);
  $('bt-description').textContent = target ? `${target}${d?.diagnostics?.output_ready ? ' (audio ready)' : ''}` : bt.preferred_address ? `Preferred speaker: ${speaker(bt.preferred_address)}` : 'Find a speaker and choose Pair & connect.';
  if (document.activeElement !== $('bt-auto')) $('bt-auto').checked = bt.autoreconnect === true;
  setDisabled('bt-auto', !available() || typeof bt.autoreconnect !== 'boolean');
  setDisabled('scan-start', !available() || Boolean(bt.scanning));
  setDisabled('scan-stop', !available() || !bt.scanning);
  setDisabled('bt-disconnect', !available() || !connected);
  $('bt-preferred-label').textContent = bt.preferred_address ? `Preferred: ${speaker(bt.preferred_address)}` : 'No preferred speaker saved.';
  const devices = d?.bluetooth_devices || [], bonded = bt.bonded_addresses || [];
  if (changed('bt-devices', [d?.device_id, devices, bonded, names, available()])) {
    const all = [...devices];
    for (const address of bonded) if (!all.some((v) => v.address === address)) all.push({ address, name: names[address.toLowerCase()]||'Unknown speaker' });
    const rows = all.map((item) => {
      const row = element('div', 'station-row'), info = element('div', 'row-info');
      info.append(element('strong', '', names[item.address.toLowerCase()] || item.name || 'Unknown speaker'), element('p', '', `${item.address}${bonded.includes(item.address) ? ' (paired)' : ''}`));
      const button = makeButton('Select', 'secondary', () => { $('bt-address').value = item.address; $('bt-address').focus(); });
      row.append(info, button); return row;
    });
    if (!rows.length) rows.push(element('p', 'muted', bt.scanning ? 'Scanning. Results will appear here.' : 'No devices found yet. Put your speaker in pairing mode and start a scan.'));
    replace('bt-devices', ...rows);
  }
  const event = [...(d?.events || [])].reverse().find((e) => e.event?.startsWith('BT_') || e.event === 'OUTPUT_READY');
  $('bt-progress').textContent = event ? `${eventLabel(event.event)}${event.detail ? `: ${event.detail}` : ''}` : '';
}
function count(value) {
  if (value === null || value === undefined) return '—';
  try { return BigInt(value).toLocaleString(); } catch { return String(value); }
}
function bytes(value) { return value === null || value === undefined ? '—' : `${(Number(value) / 1024).toFixed(1)} KiB`; }
function eventLabel(name) { return String(name || 'Event').replaceAll('_', ' ').toLowerCase().replace(/^./, (v) => v.toUpperCase()); }
function usefulTelemetry(device){
  const s=device?.diagnostics||{},bt=s.bluetooth||{};
  const ms=value=>`${(Number(value)/1000).toFixed(1)} ms`;
  const frames=value=>`${count(value)} frames (${(Number(value)/44.1).toFixed(1)} ms)`;
  const fields=[
    ['Output ready',s.output_ready],['Audio playing',s.playout_started],
    ['IP address',s.ip],['Wi-Fi signal',s.wifi_rssi,v=>`${v} dBm`],['Wi-Fi channel',s.wifi_channel],['Wi-Fi reconnects',s.wifi_reconnects],
    ['Uptime',s.uptime_ms,v=>`${Math.floor(Number(v)/60000)} min`],
    ['Current audio buffer',s.buffer_fill_frames,frames],['Audio buffer target',s.buffer_target_frames,frames],['Audio buffer capacity',s.buffer_capacity_frames,frames],
    ['Buffer underruns',s.underruns],['Buffer overruns',s.overruns],['Audio packets received',s.sbc_packets],
    ['Longest audio packet gap',s.largest_sbc_gap_us,ms],
    ['Free memory',s.free_heap,bytes],['Lowest free memory',s.minimum_free_heap,bytes],['Largest free memory block',s.largest_free_internal_block,bytes],['Memory allocation failures',s.allocation_failures],
    ['Bluetooth state',bt.state],['Speaker name',bt.connected_name],['Speaker connected',bt.connected],['Automatic reconnect',bt.autoreconnect],['Next speaker retry',bt.next_retry_ms,v=>`${(Number(v)/1000).toFixed(1)} s`],
    ['Speaker-reported delay',bt.speaker_delay_valid?bt.speaker_delay_ms:undefined,v=>`${v} ms`]
  ];
  return Object.fromEntries(fields.filter(([,value])=>value!==undefined&&value!==null).map(([label,value,format])=>[label,format?format(value):value]));
}
function usefulFirmware(device){
  const hello=device?.hello||{},caps=hello.capabilities||{};
  const fields=[['Firmware version',hello.firmware],['Protocol version',hello.protocol],
    ['Audio output',hello.output?.kind==='bluetooth_a2dp'?'Bluetooth':hello.output?.kind==='i2s_pcm5102'?'PCM5102 / AUX':hello.output?.kind],
    ['Audio format',hello.output?.audio_codec||caps.sbc_audio],['Adaptive buffering supported',caps.adaptive_buffer],
    ['Output timing feedback supported',caps.output_timing===undefined?undefined:Boolean(caps.output_timing)],
    ['Bluetooth controls supported',caps.bluetooth_management],['Hardware volume supported',caps.hardware_volume],
    ['Volume control method',caps.device_volume_method==='digital_attenuation'?'Digital volume':caps.device_volume_method],
    ['Audio buffer capacity',caps.buffer_capacity_frames??hello.buffer_capacity_frames,v=>`${count(v)} frames (${(Number(v)/44.1).toFixed(1)} ms)`]];
  return Object.fromEntries(fields.filter(([,value])=>value!==undefined&&value!==null).map(([label,value,format])=>[label,format?format(value):value]));
}
function renderDiagnosticDetails(device){
  let container=$('diagnostic-details');
  if(!container){container=element('div','diagnostic-details');container.id='diagnostic-details';$('diagnostic-grid').after(container);}
  const grouped=['player','diagnostics','hello','spotify','airplay','wfas','events','bluetooth_devices'];
  const prefs=Object.fromEntries(Object.entries(device||{}).filter(([key])=>!grouped.includes(key)));
  const groups=[['Playback and buffering',device?.player],['Device telemetry',usefulTelemetry(device)],['Firmware and capabilities',usefulFirmware(device)],['Spotify',device?.spotify],['AirPlay',device?.airplay],['Wi-Fi streaming',device?.wfas],['Discovered speakers',device?.bluetooth_devices],['Identity and preferences',prefs]];
  function flatten(value,path='',rows=[]){
    if(value&&typeof value==='object'&&Object.keys(value).length){
      for(const [key,item] of Object.entries(value))flatten(item,path?path+' / '+key:key,rows);
    }else rows.push([path||'Value',value===null||value===undefined?'Unavailable':typeof value==='boolean'?(value?'Yes':'No'):typeof value==='object'?JSON.stringify(value):String(value)]);
    return rows;
  }
  groups.forEach(([title,value],index)=>{
    let details=container.children[index];
    if(!details){details=element('details','diagnostic-group');const summary=element('summary','',title),grid=element('div','diagnostic-grid');details.append(summary,grid);container.append(details);}
    if(!changed('diagnostic-detail-'+index,value))return;
    const rows=flatten(value);
    details.lastElementChild.replaceChildren(...rows.map(([label,text])=>{
      const dl=element('dl','metric');
      dl.append(element('dt','',label.replaceAll('_',' ')),element('dd','',text));return dl;
    }));
  });
}
function renderDiagnostics() {
  const d = current(), s = d?.diagnostics || {}, p = d?.player || {};
  const buffer = s.buffer_capacity_frames ? `${Math.round(Number(s.buffer_fill_frames) / Number(s.buffer_capacity_frames) * 100)}% (${Math.round(Number(s.buffer_fill_frames) / 44.1)} ms)` : '—';
  const buffering=p.buffering||{};
  const sourceNames={radio:'Internet radio',airplay:'AirPlay',spotify:'Spotify',wfas:'Wi-Fi streaming','test-tone':'Test tone','bench-music':'Test music'};
  const values = [
    ['Playback source',sourceNames[p.source]||p.source||'None'],
    ['Playback state',p.state||'Stopped'],
    ['Buffer group',buffering.group??'—'],
    ['Requested buffer group',buffering.requested_group??'—'],
    ['Calculated buffer target',buffering.target_ms===undefined?'—':`${buffering.target_ms} ms (${count(buffering.target_frames)} frames)`],
    ['Applied buffer target',s.buffer_target_frames===undefined?'—':`${(Number(s.buffer_target_frames)/44.1).toFixed(1)} ms (${count(s.buffer_target_frames)} frames)`],
    ['Buffer capacity',s.buffer_capacity_frames===undefined?'—':`${count(s.buffer_capacity_frames)} frames`],
    ['Playback tempo',buffering.tempo===undefined?'—':`${buffering.tempo}×`],
    ['Output', outputKind(d) === 'i2s_pcm5102' ? 'PCM5102 / AUX' : outputKind(d) === 'bluetooth_a2dp' ? 'Bluetooth' : '—'],
    ['Firmware', d?.hello?.firmware || '—'], ['Protocol', d?.hello?.versions?.join(', ') || '—'],
    ['Wi-Fi signal', s.wifi_rssi === undefined ? '—' : `${s.wifi_rssi} dBm`], ['IP address', s.ip || '—'],
    ['Uptime', s.uptime_ms === undefined ? '—' : `${Math.floor(Number(s.uptime_ms) / 60000)} min`],
    ['PCM buffer', buffer], ['Output format', d?.hello ? '44.1 kHz / stereo / 16-bit' : '—'],
    ['Packets sent', count(p.packets_sent)], ['Packets received', count(s.sbc_packets)],
    ['Underruns', count(s.underruns)], ['Overruns', count(s.overruns)],
    ['Free heap', bytes(s.free_heap)], ['Minimum heap', bytes(s.minimum_free_heap)],
    ['Allocation failures', count(s.allocation_failures)], ['Largest internal block', bytes(s.largest_free_internal_block)],
    ['Wi-Fi reconnects', count(s.wifi_reconnects)], ['Bluetooth reconnects', count(s.bluetooth_reconnects ?? 0)],
    ['Submitted position', count(s.submitted_position)], ['Presented position', count(s.presented_position)]
  ];
  replace('diagnostic-grid', ...values.map(([label, value]) => { const dl = element('dl', 'metric'); dl.append(element('dt', '', label), element('dd', '', value)); return dl; }));
  renderDiagnosticDetails(d);
  const events = [...(d?.events || [])].filter(event=>event.event!=='COMMAND_COMPLETED').reverse();
  if (changed('events', [d?.device_id, events])) {
    replace('event-list', ...events.map((event) => {
      const li = element('li'), time = element('time', '', event.received_at ? new Date(event.received_at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '—');
      const info = element('span'); info.append(element('strong', '', eventLabel(event.event || event.code)), document.createTextNode(event.detail ? `: ${event.detail}` : event.error?.message ? `: ${event.error.message}` : ''));
      li.append(time, info); return li;
    }));
  }
  $('no-events').hidden = events.length > 0;
  for (const id of ['test-tone', 'test-music', 'reset-statistics', 'reboot']) setDisabled(id, !available());
  $('export-diagnostics').disabled = !d;
}
function sinkStatus(d){
 if(!state.connected||!d.online)return {label:'Not connected',color:''};
 const telemetry=d.diagnostics||{},bt=telemetry.bluetooth||{};
 if(!telemetry.output_ready)return {label:bt.connecting?'Connecting to speaker':outputKind(d)==='bluetooth_a2dp'?'Waiting for speaker':'Waiting for output',color:'waiting'};
 if(d.player?.recovery_attempts)return {label:'Recovering source',color:'waiting'};
 if(d.player?.state==='playing'&&telemetry.playout_started===false)return {label:'Buffering',color:'waiting'};
 return {label:'Online',color:'online'};
}
function renderServerSinks(){
 const sinks=state.devices.map((d,index)=>({d,index})).sort((a,b)=>(b.d.created_at||0)-(a.d.created_at||0)||b.index-a.index).map(({d})=>d);
 const seen=d=>d.last_seen_at?d.online&&Date.now()/1000-d.last_seen_at<10?'Just now':new Date(d.last_seen_at*1000).toLocaleString():'Unknown';
 if(!changed('server-sinks',[state.selected,sinks.map(d=>[d.device_id,d.display_name,outputKind(d)||d.output_type,seen(d),sinkStatus(d)])]))return;
 const rows=sinks.map(d=>{
  const row=element('tr');row.classList.toggle('is-selected',d.device_id===state.selected);
  const type=outputKind(d)||d.output_type;
  const name=element('th','',d.display_name||d.device_id);name.scope='row';
  const identifier=element('td','sink-list-id',d.device_id);
  const last=element('td','',seen(d));
  if(d.last_seen_at)last.title=new Date(d.last_seen_at*1000).toLocaleString();
  const status=sinkStatus(d),cell=element('td'),label=element('span','sink-list-status');
  const dot=element('span','dot '+status.color);dot.setAttribute('aria-hidden','true');label.append(dot,document.createTextNode(status.label));cell.append(label);
  const action=element('td'),button=makeButton(d.device_id===state.selected?'Selected':'Select','secondary',()=>{
   state.selected=d.device_id;storage.set('localStorage','radio-sink',d.device_id);state.signatures={};render();
  });
  button.disabled=d.device_id===state.selected;button.setAttribute('aria-label',`Select ${d.display_name||d.device_id}`);action.append(button);
  row.append(name,element('td','',type==='bluetooth_a2dp'?'Bluetooth':type==='i2s_pcm5102'?'PCM5102 / I2S':'Unknown'),identifier,last,cell,action);return row;
 });
 replace('server-sinks',...rows);$('server-sinks-empty').hidden=rows.length>0;
}
function renderServer(){
 renderServerSinks();
 const runtime=state.server.runtime||{};
 const values=[['Uptime',runtime.uptime_seconds===undefined?'—':`${Math.floor(runtime.uptime_seconds/60)} min`],['Memory used',runtime.memory_bytes == null ? '—' : `${(Number(runtime.memory_bytes) / 1048576).toFixed(1)} MiB`],['Connected sinks',runtime.connected_sinks??'—'],['Process ID',runtime.process_id??'—'],['Python',runtime.python_version||'—'],['Known sinks',runtime.known_sinks??'—'],['Pending approvals',runtime.pending_approvals??'—'],['Playing sinks',runtime.playing_sinks??'—']];
 replace('server-stats',...values.map(([label,value])=>{const tile=element('dl','metric');tile.append(element('dt','',label),element('dd','',value));return tile;}));
 for(const [component,name] of [['spotify','Spotify'],['airplay','AirPlay'],['wfas','Wi-Fi streaming']]){
  const d=current(),enabled=component==='wfas'?d?.wfas_allowed!==false:d?.[component+'_enabled'];
  const reason=!state.connected?'Server is unavailable.':!d?'Select a sink first.':!d.online?'This sink is offline.':!state.server.features?.[component]?`${name} is not configured on this server.`:!enabled?`${name} is disabled for this sink.`:component==='wfas'&&!d.wfas_enabled?'Connect a Wi-Fi streaming sender first.':runtime.restart_pending?'Server is restarting.':'';
  setDisabled('restart-'+component,Boolean(reason));$('restart-'+component).title=reason||`Restart ${name} for this sink`;
 }
 setDisabled('restart-server',!state.connected||!runtime.restart_available||runtime.restart_pending);
}
function render() {
  setDisabled('unpair-sink', !state.connected || !current());
  renderDevices(); renderAirPlaySync(); renderPlayer(); renderFavorites(); renderBluetooth(); renderDiagnostics(); renderServer();
  $('connection-label').textContent = state.connected ? 'Server online' : 'Reconnecting';
  $('connection-badge').querySelector('.dot').classList.toggle('online', state.connected);
}
function applyState(data) {
  if (Array.isArray(data.devices)) state.devices = data.devices;
  if (Array.isArray(data.favorites) && JSON.stringify(data.favorites) !== JSON.stringify(state.favorites)) { state.favorites = data.favorites; }
  if (data.server) state.server = data.server;
  state.connected = true; render();
}
async function refresh() {
  if (state.refreshPending) return state.refreshPending;
  state.refreshPending = (async () => {
    try {
      const [server, devices, favorites] = await Promise.all([api('/api/health'), api('/api/devices'), api('/api/radio/favorites')]);
      applyState({ server, devices: devices.devices, favorites: favorites.favorites });
    } catch { state.connected = false; render(); }
    finally { state.refreshPending = null; }
  })();
  return state.refreshPending;
}

$('sink-select').addEventListener('change', (event) => {
  state.selected = event.target.value; storage.set('localStorage', 'radio-sink', state.selected);
  state.signatures = {}; render();
});
StationCards.bind({
 play:station=>action(null,()=>mutate(devicePath('/player/play'),{station_id:station.id})),
 save:async(id,values)=>{await mutate('/api/radio/favorites/'+encodeURIComponent(id),values,'PUT');await refresh();},
 remove:async id=>{await mutate('/api/radio/favorites/'+encodeURIComponent(id),undefined,'DELETE');await refresh();},
 reorder:async ids=>{await mutate('/api/radio/favorites/reorder',{ids});await refresh();},
 refresh,
 error:error=>showNotice(error.message)
});
PlaybackCard.bind({
 disconnect:()=>action(null,()=>mutate(devicePath('/wfas'),{enabled:false})),
 transport:transport=>action(null,()=>mutate(devicePath('/player/'+transport))),
 volume:volume=>action(null,()=>mutate(devicePath('/player/volume'),{volume})),
 favorite:(station,favorite)=>action(null,()=>{
  if(favorite)return mutate('/api/radio/favorites/'+encodeURIComponent(favorite.id),undefined,'DELETE');
  const allowed=['name','url','stationuuid','favicon','codec','bitrate','countrycode','language','lastcheckok'];
  return mutate('/api/radio/favorites',Object.fromEntries(Object.entries(station).filter(([k])=>allowed.includes(k))));
 })
});
for(const component of ['spotify','airplay','wfas']){
 $('restart-'+component).onclick=event=>action(event.currentTarget,()=>mutate(devicePath('/receivers/'+component+'/restart')), 'Component restarted.');
}
$('restart-server').onclick=event=>action(event.currentTarget,async()=>{
 await mutate('/api/server/restart',{});
 state.connected=false;state.server.runtime={...state.server.runtime,restart_pending:true};
},'Server restart requested. Reconnecting…');
setInterval(async()=>{
 if(!$('server-panel').open)return;
 try{state.server=await api('/api/health');renderServer();}catch{state.connected=false;renderServer();}
},5000);
$('airplay-sync').addEventListener('input', syncLabel);
$('airplay-sync').addEventListener('change', () => saveAirPlaySync(Number($('airplay-sync').value)));
$('airplay-sync-reset').addEventListener('click', () => saveAirPlaySync(0));
$('custom-form').addEventListener('submit', (event) => { event.preventDefault(); action(event.submitter, () => mutate('/api/radio/favorites', { name: $('custom-name').value.trim(), url: $('custom-url').value.trim() }), 'Station saved.'); });
$('custom-test').addEventListener('click', (event) => { if ($('custom-url').reportValidity()) testStream(event.currentTarget, $('custom-url').value.trim()); });
for(const component of ['radio','spotify','airplay','wfas'])$('device-'+component).addEventListener('change',event=>{event.target.dataset.dirty='true';});
$('device-form').addEventListener('submit',event=>{
 event.preventDefault();
 const payload={display_name:$('device-name').value.trim(),volume_delta_db:Number($('device-delta').value),
  source_deltas_db:Object.fromEntries(['radio','airplay','spotify','wfas'].map(k=>[k,Number($('delta-'+k).value)])),
  ...Object.fromEntries(['radio','spotify','airplay','wfas'].filter(component=>state.server.features?.[component]).map(component=>[component==='wfas'?'wfas_allowed':component+'_enabled',$('device-'+component).checked]))};
 action(event.submitter,async()=>{await mutate(devicePath(),payload,'PATCH');for(const component of ['radio','spotify','airplay','wfas'])delete $('device-'+component).dataset.dirty;},'Sink settings saved.');
});
$('scan-start').addEventListener('click', (event) => action(event.currentTarget, () => command('BLUETOOTH_SCAN_START')));
$('scan-stop').addEventListener('click', (event) => action(event.currentTarget, () => command('BLUETOOTH_SCAN_STOP')));
$('bt-disconnect').addEventListener('click', (event) => action(event.currentTarget, () => command('BLUETOOTH_DISCONNECT')));
$('bt-auto').addEventListener('change', (event) => action(null, () => command('BLUETOOTH_SET_AUTORECONNECT', { enabled: event.target.checked })));
function selectedAddress() {
  const address = $('bt-address').value.trim().toLowerCase();
  if (!/^[0-9a-f]{2}(:[0-9a-f]{2}){5}$/.test(address)) throw new Error('Enter or select a complete Bluetooth address.');
  return address;
}
$('bt-address-form').addEventListener('submit', (event) => { event.preventDefault(); action(event.submitter, () => command('BLUETOOTH_PAIR', { address: selectedAddress() })); });
$('bt-preferred').addEventListener('click', (event) => action(event.currentTarget, () => command('BLUETOOTH_SET_PREFERRED', { address: selectedAddress() }), 'Preferred speaker saved.'));
$('bt-forget').addEventListener('click', (event) => {
  action(event.currentTarget, async () => {
    const address = selectedAddress();
    if (!window.confirm(`Forget the saved pairing for ${address}?`)) return;
    return command('BLUETOOTH_FORGET', { address });
  });
});
$('test-tone').addEventListener('click', (event) => action(event.currentTarget, () => mutate(devicePath('/diagnostics/test-tone'), { frequency: 440, duration_seconds: 10 }), 'Quiet tone requested for 10 seconds.'));
$('test-music').addEventListener('click', (event) => action(event.currentTarget, () => mutate(devicePath('/diagnostics/test-music'), { duration_seconds: 10 }), 'Music check requested for 10 seconds.'));
$('reset-statistics').addEventListener('click', (event) => action(event.currentTarget, () => command('RESET_STATISTICS'), 'Diagnostic counters reset.'));
$('reboot').addEventListener('click', (event) => {
  if (window.confirm(`Reboot ${current()?.display_name || 'this sink'}? Listening will stop temporarily.`)) action(event.currentTarget, () => command('REBOOT'), 'Reboot requested. Waiting for the sink to reconnect.');
});
$('export-diagnostics').addEventListener('click', (event) => action(event.currentTarget, async () => {
  const data = await api(devicePath('/diagnostics/export'));
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }));
  const anchor = document.createElement('a'); anchor.href = url; anchor.download = `radio-diagnostics-${state.selected}.json`; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}));

render(); refresh();
if ('EventSource' in window) {
  const source = new EventSource(ingressUrl('/api/events'));
  source.addEventListener('state', (event) => { try { applyState(JSON.parse(event.data)); } catch { /* A later valid update can recover. */ } });
  source.onerror = () => { state.connected = false; render(); };
}
setInterval(() => { if (!state.connected) refresh(); }, 3500);


$('unpair-sink').addEventListener('click', event => {
  const d=current();if(!d)return;
  if(!window.confirm(`Unpair ${d.display_name}? It will stop playing and require approval in Setup.`))return;
  action(event.currentTarget,()=>mutate(devicePath('/unpair'),{}),'Sink unpaired. Approve it again in Setup when ready.');
});
