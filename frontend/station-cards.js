/* Shared favorites grid, station editor, and hold-to-reorder interaction. */
window.StationCards = (() => {
 const grid=document.getElementById('favorites-grid');
 let handlers={},stations=[],signature='',gesture=null,drag=null,suppressClick=false;
 const node=(tag,cls,text)=>{const n=document.createElement(tag);n.className=cls||'';if(text!==undefined)n.textContent=text;return n;};
 const pencil='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m16 3 5 5M4 15 16 3a2 2 0 0 1 5 5L9 20l-6 1Z"/></svg>';
 const help=node('p','sr-only','Hold a station and drag to reorder. With a station focused, press Alt and an arrow key to move it.');
 help.id='station-reorder-help';grid.before(help);
 const announcement=node('p','sr-only');announcement.setAttribute('role','status');grid.after(announcement);
 const dialog=document.createElement('dialog');dialog.className='station-dialog';dialog.setAttribute('aria-labelledby','station-editor-title');
 dialog.innerHTML=`<form class="station-editor"><div class="dialog-heading"><h2 id="station-editor-title">Edit station</h2><button type="button" class="button secondary dialog-close" aria-label="Close station editor">×</button></div><label>Station name<input name="name" required maxlength="128"></label><label>Stream URL<input name="url" type="url" required maxlength="2048"></label><label>Logo URL <span class="muted">(optional)</span><input name="favicon" type="url" maxlength="2048"></label><label>Volume adjustment (dB)<input name="volume_delta_db" type="number" min="-24" max="12" step="0.5" required><span class="muted">Negative values make this station quieter.</span></label><p class="notice" role="alert" hidden></p><div class="dialog-actions"><button type="button" class="button danger-quiet station-remove">Remove station</button><button type="button" class="button secondary station-cancel">Cancel</button><button type="submit" class="button">Save changes</button></div></form>`;
 document.body.append(dialog);
 const form=dialog.querySelector('form'),error=dialog.querySelector('[role=alert]');let editing=null;
 const field=name=>form.elements.namedItem(name);
 function close(){if(!form.dataset.busy)dialog.close();}
 dialog.querySelector('.dialog-close').onclick=close;dialog.querySelector('.station-cancel').onclick=close;
 dialog.addEventListener('cancel',event=>{if(form.dataset.busy)event.preventDefault();});
 function edit(station){
  editing=station.id;for(const name of ['name','url','favicon'])field(name).value=station[name]||'';
  field('volume_delta_db').value=station.volume_delta_db??0;error.hidden=true;dialog.showModal();field('name').focus();
 }
 async function saveEditor(remove=false){
  if(form.dataset.busy)return;
  form.dataset.busy='true';error.hidden=true;
  for(const button of form.querySelectorAll('button'))button.disabled=true;
  try{
   if(remove)await handlers.remove(editing);
   else await handlers.save(editing,{name:field('name').value.trim(),url:field('url').value.trim(),favicon:field('favicon').value.trim(),volume_delta_db:Number(field('volume_delta_db').value)});
   dialog.close();
  }catch(exc){error.textContent=exc.message||'Could not save station.';error.hidden=false;}
  finally{delete form.dataset.busy;for(const button of form.querySelectorAll('button'))button.disabled=false;}
 }
 form.onsubmit=event=>{event.preventDefault();saveEditor();};
 dialog.querySelector('.station-remove').onclick=()=>saveEditor(true);
 function logo(station){
  const art=node('span','station-art',(station.name||'♪').slice(0,2));art.setAttribute('aria-hidden','true');
  try{const url=new URL(station.favicon);if(['http:','https:'].includes(url.protocol)){
   const image=node('img');image.src=url.href;image.alt='';image.draggable=false;image.referrerPolicy='no-referrer';
   image.onload=()=>{for(const child of [...art.childNodes])if(child!==image)child.remove();};image.onerror=()=>image.remove();art.append(image);
  }}catch{}
  return art;
 }
 function render(items,selected,canPlay,canEdit=true){
  stations=items;
  if(drag||gesture)return;
  const next=JSON.stringify([items,selected,canPlay,canEdit]);if(signature===next)return;signature=next;
  grid.replaceChildren(...items.map(station=>{
   const tile=node('div','favorite-tile');tile.dataset.stationId=station.id;
   const play=node('button','favorite-card'+(station.id===selected?' selected':''));play.type='button';
   // aria-disabled keeps offline cards focusable and reorderable.
   play.setAttribute('aria-disabled',String(!canPlay));play.setAttribute('aria-label','Play '+station.name);play.setAttribute('aria-pressed',String(station.id===selected));play.setAttribute('aria-describedby',help.id);
   const copy=node('span','card-copy');copy.append(node('strong','',station.name));
   play.append(logo(station),copy);
   play.onclick=()=>{if(!suppressClick&&!drag&&canPlay)handlers.play(station);};
   play.onkeydown=event=>{
    if(!event.altKey||!['ArrowUp','ArrowDown','ArrowLeft','ArrowRight'].includes(event.key))return;
    event.preventDefault();const ids=stations.map(s=>s.id),index=ids.indexOf(station.id),offset=['ArrowUp','ArrowLeft'].includes(event.key)?-1:1,target=index+offset;
    if(target<0||target>=ids.length)return;
    [ids[index],ids[target]]=[ids[target],ids[index]];
    persist(ids).then(()=>[...grid.children].find(t=>t.dataset.stationId===station.id)?.querySelector('.favorite-card').focus());
   };
   tile.append(play);
   if(canEdit){
   const button=node('button','station-edit');button.type='button';button.innerHTML=pencil;button.setAttribute('aria-label','Edit '+station.name);button.title='Edit station';button.onclick=()=>edit(station);
   tile.append(button);
   }
   return tile;
  }));
 }
 async function persist(ids){
  try{await handlers.reorder(ids);announcement.textContent='Station order saved.';}
  catch(exc){signature='';handlers.error?.(exc);}
  finally{signature='';handlers.refresh?.();}
 }
 function cancelGesture(){if(gesture)clearTimeout(gesture.timer);gesture=null;}
 function begin(target,x,y,pointerId){
  if(target.closest('.station-edit')||target.closest('dialog')||drag)return;
  const tile=target.closest('.favorite-tile');if(!tile)return;
  cancelGesture();gesture={tile,x,y,pointerId,timer:setTimeout(()=>{
   const g=gesture;if(!g)return;
   drag={tile:g.tile,original:[...grid.children].map(t=>t.dataset.stationId),x:g.x,y:g.y};
   tile.classList.add('dragging');grid.classList.add('reordering');suppressClick=true;
   announcement.textContent='Moving '+tile.querySelector('.card-copy strong').textContent;
   if(pointerId!==undefined)grid.setPointerCapture?.(pointerId);
   tick();
  },400)};
 }
 function move(x,y,event){
  if(!drag){if(gesture&&Math.hypot(x-gesture.x,y-gesture.y)>10)cancelGesture();return;}
  if(event.cancelable)event.preventDefault();drag.x=x;drag.y=y;
  const target=document.elementFromPoint(x,y)?.closest('.favorite-tile');
  if(!target||target===drag.tile||target.parentElement!==grid)return;
  const children=[...grid.children],from=children.indexOf(drag.tile),to=children.indexOf(target);
  grid.insertBefore(drag.tile,from<to?target.nextSibling:target);
 }
 function tick(){
  if(!drag)return;
  if(drag.y<70)window.scrollBy(0,-8);else if(drag.y>window.innerHeight-70)window.scrollBy(0,8);
  drag.frame=requestAnimationFrame(tick);
 }
 function finish(cancel=false){
  cancelGesture();if(!drag)return;
  const active=drag;cancelAnimationFrame(active.frame);
  active.tile.classList.remove('dragging');grid.classList.remove('reordering');drag=null;
  setTimeout(()=>{suppressClick=false;},350);
  const ids=[...grid.children].map(t=>t.dataset.stationId);
  if(cancel){for(const id of active.original){const tile=[...grid.children].find(t=>t.dataset.stationId===id);if(tile)grid.append(tile);}return;}
  if(JSON.stringify(ids)!==JSON.stringify(active.original))persist(ids);
 }
 grid.addEventListener('pointerdown',event=>{if(event.pointerType!=='touch'&&event.button===0)begin(event.target,event.clientX,event.clientY,event.pointerId);});
 grid.addEventListener('pointermove',event=>{if(event.pointerType!=='touch')move(event.clientX,event.clientY,event);});
 window.addEventListener('pointerup',event=>{if(event.pointerType!=='touch')finish();});
 window.addEventListener('pointercancel',event=>{if(event.pointerType!=='touch')finish(true);});
 grid.addEventListener('touchstart',event=>{if(event.touches.length===1){const touch=event.touches[0];begin(event.target,touch.clientX,touch.clientY);}else finish(true);},{passive:true});
 grid.addEventListener('touchmove',event=>{const touch=event.touches[0];if(touch)move(touch.clientX,touch.clientY,event);},{passive:false});
 grid.addEventListener('touchend',()=>finish());grid.addEventListener('touchcancel',()=>finish(true));
 grid.addEventListener('contextmenu',event=>{if(gesture||drag)event.preventDefault();});
 grid.addEventListener('dragstart',event=>event.preventDefault());
 window.addEventListener('keydown',event=>{if(event.key==='Escape'&&drag){event.preventDefault();finish(true);}});
 window.addEventListener('blur',()=>finish(true));
 return {bind:callbacks=>{handlers=callbacks;},render,edit};
})();
