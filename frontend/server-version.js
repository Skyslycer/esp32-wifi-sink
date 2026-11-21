(() => {
 'use strict';
 const labels=document.querySelectorAll('[data-server-version]');
 if(!labels.length)return;
 const endpoint=new URL('api/version',new URL('./',window.location.href));
 fetch(endpoint,{cache:'no-store'}).then(response=>{
  if(!response.ok)throw Error('Version unavailable');
  return response.json();
 }).then(data=>{
  if(typeof data.server_version!=='string'||!data.server_version)throw Error('Version unavailable');
  for(const label of labels)label.textContent=`v${data.server_version}`;
 }).catch(()=>{for(const label of labels)label.textContent='Version unavailable';});
})();
