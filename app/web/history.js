/* Durable server records plus a small per-browser workbench snapshot. */
let sessionReady=false;
const sessionKey='dlss-session-v2';
function saveSession(){
  if(!sessionReady)return;
  sessions[state.mode]={asset:state.asset,output:state.output,framePreview:state.framePreview,selection:state.selection};
  try{localStorage.setItem(sessionKey,JSON.stringify({mode:state.mode,sessions,job:state.job,busy:state.busy,
    jobKind:state.jobKind,jobContext:state.jobContext,pendingPayload:state.pendingPayload,
    view:state.view,time:videoPlayer.pendingSourceTime??videoPlayer.source.currentTime,exportDefaultsVersion:1,settings:state.asset?settings():null}))}catch{}
}
window.addEventListener('pagehide',saveSession);
document.querySelector('.brand').addEventListener('click',event=>event.preventDefault());
form.addEventListener('change',saveSession);

function applySettings(values){
  if(!values)return;
  values={protect_region:[],protect_skin:0,protect_highlights:0,stability:0,render_scale:1,timing_mode:'preserve',preserve_media:true,...values};
  // Change the codec first, then restore its dependent container/quality fields.
  if(values.codec&&[...$('#codec').options].some(o=>o.value===values.codec&&!o.disabled))$('#codec').value=values.codec;
  updateCodec();
  for(const control of form.elements){
    if(!control.name||!(control.name in values)||control.name==='codec')continue;
    if(control.name==='protect_region')control.value=JSON.stringify(values.protect_region||[]);
    else if(control.type==='checkbox')control.checked=!!values[control.name];
    else if(control.tagName!=='SELECT'||[...control.options].some(o=>o.value===String(values[control.name])&&!o.disabled))control.value=values[control.name];
  }
  state.style=values.style||'默认';
  $$('[data-style]').forEach(button=>{const active=button.dataset.style===state.style;button.classList.toggle('active',active);button.setAttribute('aria-pressed',String(active))});
  $('#intensity-number').value=Math.round(Number($('#intensity').value)*100);
  if(values.codec||'cq' in values||'bitrate_mode' in values){
    const cq=String(values.cq??19);$('#quality').value=values.bitrate_mode==='auto'?'auto':values.bitrate||!['15','19','23','28'].includes(cq)?'custom':cq;
  }
  $$('.advanced-range').forEach(row=>row.querySelector('output').textContent=`${Math.round(Number(row.querySelector('input').value)*100)}%`);
  updateCodec();paintRanges();refreshProduct();
  window.StudioEnhancements?.refresh();
}

async function seekRestoredVideo(time){
  if(state.mode!=='video'||!Number.isFinite(time))return;
  const source=videoPlayer.source;
  if(source.readyState<1&&videoPlayer.sourcePreparing){videoPlayer.seek(time);return}
  const waitEvent=(name)=>new Promise(resolve=>{
    const done=()=>{clearTimeout(timer);source.removeEventListener(name,done);source.removeEventListener('error',done);resolve()};
    const timer=setTimeout(done,8000);source.addEventListener(name,done,{once:true});source.addEventListener('error',done,{once:true});
  });
  if(source.readyState<1)await waitEvent('loadedmetadata');
  if(!source.duration||source.error)return;
  videoPlayer.seek(time);
  if(source.seeking)await waitEvent('seeked');
}

async function restoreJob(job){
  const frame=job.job_kind==='frame',asset=frame?job.source_asset:job.asset;
  if(!asset)throw Error('原素材已被清理，仍可下载已完成的结果。');
  const saved=sessions[asset.kind];
  const prior=state.mode===asset.kind&&state.asset?.id===asset.id?state.output:saved?.asset?.id===asset.id?saved.output:null;
  state.busy=false;switchMode(asset.kind);state.asset=asset;state.framePreview=null;state.output=frame?prior:job.status==='done'?job:null;
  state.job=job.id;state.jobRecord=job;state.jobKind=job.job_kind||'full';state.jobContext=frame?{source:job.asset.url,time:job.frame_time}:null;
  state.pendingPayload=null;state.selection=job.job_kind==='clip'?{start:job.settings.clip_start,end:job.settings.clip_start+job.settings.clip_duration}:null;applySettings(job.settings);showAsset();
  if(frame){await seekRestoredVideo(job.frame_time);state.framePreview=job.status==='done'?{job,time:job.frame_time,source:job.asset.url}:null;videoPlayer.setFrame(state.framePreview)}
  if(['queued','running','pausing','cancelling'].includes(job.status)){
    state.busy=true;refreshControls();showProgress(job);schedulePoll(job.id,100);
  }else{
    view(job.status==='done'&&job.preview?(frame?'compare':'result'):'original');
    status(job.status==='done'?'处理完成':job.status==='paused'?'已暂停':job.status==='cancelled'?'已取消':'处理失败',job.status==='error'?'error':'');
    if(job.status==='error')showNotice(job.message,true);refreshControls();
  }
  saveSession();
}

async function restoreSession(){
  let saved=null;
  try{saved=JSON.parse(localStorage.getItem(sessionKey)||'null')}catch{}
  try{
    if(saved?.sessions){
      for(const mode of ['image','video']){
        const record=saved.sessions[mode];if(!record?.asset)continue;
        try{
          const asset=await apiRequest(`/api/assets/${record.asset.id}/info`);
          const result=record.output?await apiRequest(`/api/jobs/${record.output.id}`).catch(()=>null):null;
          const selection=record.selection??(mode==='video'&&saved.scope==='clip'?{start:Number(saved.clipStart)||0,end:(Number(saved.clipStart)||0)+(Number(saved.clipDuration)||3)}:null);
          sessions[mode]={asset,output:result?.status==='done'?result:null,framePreview:null,selection};
        }catch(error){if(!error.status)throw error}
      }
      state.mode=saved.mode==='video'?'video':'image';
      const record=sessions[state.mode];state.asset=record?.asset||null;state.output=record?.output||null;state.selection=record?.selection||null;
      $$('[data-mode]').forEach(button=>{const active=button.dataset.mode===state.mode;button.classList.toggle('active',active);button.setAttribute('aria-pressed',String(active))});
      $('#video-settings').hidden=state.mode!=='video';$('#file-input').accept=state.mode==='image'?'image/png,image/jpeg,image/webp,image/bmp,image/tiff':'.mp4,.mov,.mkv,.webm,.avi,.m4v';
      // Migrate the old automatic ProRes fallback once; saved presets and versions stay intact.
      let restored=saved.settings;
      if(!saved.exportDefaultsVersion&&!saved.busy&&!saved.pendingPayload&&restored?.codec==='prores'&&(!restored.prores_profile||restored.prores_profile==='hq')&&!restored.bitrate){
        restored={...restored,codec:options.video_defaults.codec,container:'mp4',bitrate_mode:'auto'};
      }
      applySettings(restored);showAsset();
    }
    const recent=await apiRequest('/api/jobs?limit=1');
    if(recent.active.length){await restoreJob(recent.active[0]);return}
    if(saved?.busy&&saved.job){
      try{await restoreJob(await apiRequest(`/api/jobs/${saved.job}`));return}catch(error){if(error.status!==404)throw error}
    }
    if(saved?.job&&!saved.busy){
      try{
        const previous=await apiRequest(`/api/jobs/${saved.job}`),source=previous.source_asset||previous.asset;
        if(previous.status==='paused'&&source?.id===state.asset?.id){await restoreJob(previous);return}
      }catch(error){if(error.status!==404)throw error}
    }
    if(saved?.pendingPayload&&state.asset){
      state.busy=true;state.jobKind=saved.jobKind;state.jobContext=saved.jobContext;refreshControls();
      sessionReady=true;submitJob(saved.pendingPayload).catch(error=>{state.busy=false;showNotice(error.message,true);refreshControls();saveSession()});return;
    }
    const frame=saved?.sessions?.[state.mode]?.framePreview;
    if(frame?.job&&state.mode==='video'){
      try{await restoreJob(await apiRequest(`/api/jobs/${frame.job.id}`));return}catch(error){if(!error.status)throw error}
    }
    await seekRestoredVideo(saved?.time);
    view(state.output?.preview?(saved?.view||'result'):'original');
    status(state.output?'处理完成':state.asset?'就绪':'等待素材');refreshProduct();
  }finally{sessionReady=true;saveSession()}
}

(() => {
  const dialog=document.createElement('dialog');dialog.className='history-dialog';dialog.id='history-dialog';dialog.setAttribute('aria-labelledby','history-title');
  dialog.innerHTML=`<div class="history-heading"><h2 id="history-title">最近结果</h2><button type="button" class="icon-button history-close" aria-label="关闭最近结果"><img class="icon" src="/assets/icons/x.svg" alt=""></button></div><div class="history-tabs"><button type="button" class="glass-button" data-history-tab="recent" aria-pressed="true">处理记录</button><button type="button" class="glass-button" data-history-tab="cache" aria-pressed="false">缓存管理</button></div><label class="history-filter">按素材查看<select id="history-source"><option value="">全部素材</option></select></label><p class="history-message" id="history-message" role="status"></p><ul class="history-list"></ul><div class="cache-options" hidden><p class="history-message">保留当前结果、满意版本、正在使用的素材和最近一小时上传的素材；默认也保留所有已完成结果。</p><label class="check-row"><input type="checkbox" id="clear-results">同时清理未标记满意的历史结果及其素材</label><p class="history-message" id="cleanup-warning" hidden>清理后无法恢复这些历史结果，请先下载需要保留的文件。</p><p class="history-message" id="cleanup-summary" role="status"></p><ul class="cache-preview" aria-label="待清理项目"></ul><button type="button" class="glass-button" id="cleanup-cache" disabled>清理预览中的文件</button></div><div class="history-footer"><button type="button" class="glass-button" id="history-prev">上一页</button><span id="history-page"></span><button type="button" class="glass-button" id="history-next">下一页</button><button type="button" class="glass-button" id="history-refresh">刷新</button></div>`;
  document.body.append(dialog);
  let tab='recent',page=0,total=0,revision=0,plan=null,cleanupBusy=false;
  const perPage=()=>innerHeight<600?2:3;
  const q=selector=>dialog.querySelector(selector),message=q('#history-message'),list=q('.history-list');
  const size=bytes=>bytes>=1024**3?`${(bytes/1024**3).toFixed(2)} GiB`:bytes>=1024**2?`${(bytes/1024**2).toFixed(1)} MiB`:`${(bytes/1024).toFixed(1)} KiB`;
  const showError=error=>{message.textContent=error.message||'暂时无法读取，请点击刷新重试。';message.classList.add('error')};
  const button=(label,action)=>{const el=document.createElement('button');el.type='button';el.className='glass-button';el.textContent=label;el.onclick=action;return el};
  function cleanupPayload(){
    const records=[...Object.values(sessions),{asset:state.asset,output:state.output,framePreview:state.framePreview,selection:state.selection}].filter(Boolean);
    return {include_results:q('#clear-results').checked,
      keep_asset_ids:records.flatMap(r=>[r.asset?.id,r.framePreview?.job.asset_id]).filter(Boolean),
      keep_job_ids:records.flatMap(r=>[r.output?.id,r.framePreview?.job.id]).filter(Boolean)};
  }
  async function label(job,values){
    const saved=await apiRequest(`/api/jobs/${job.id}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:job.name||'',favorite:!!job.favorite,...values})});
    Object.assign(job,saved);
  }
  function row(job){
    const item=document.createElement('li');item.className='history-item';
    const image=document.createElement('img');image.className='history-thumb';image.loading='lazy';image.alt='';
    image.src=job.thumbnail||(job.asset?.kind==='image'?(job.preview||job.asset.url):'/assets/icons/image.svg');
    image.onerror=()=>{image.onerror=null;image.src='/assets/icons/image.svg'};
    const content=document.createElement('div'),name=document.createElement('strong'),meta=document.createElement('p'),summary=document.createElement('p');
    const source=(job.source_asset||job.asset)?.name||'素材已清理';
    name.textContent=job.name||source;name.title=name.textContent;
    const labels={done:'已完成',error:'失败',cancelled:'已取消',running:'处理中',queued:'等待处理',pausing:'保存暂停点',paused:'已暂停',cancelling:'取消中'};
    const kind=job.job_kind==='frame'?`单帧 ${StudioVideoPlayer.formatTime(job.frame_time)}`:job.job_kind==='clip'?`片段 ${StudioVideoPlayer.formatTime(job.clip_start)} · ${job.settings.clip_duration} 秒`:job.asset?.kind==='video'?'视频':'图片';
    meta.textContent=`${job.name?source+' · ':''}${labels[job.status]||job.status}${job.cache_hit?' · 已复用结果':''}${job.queue_position?` · 第 ${job.queue_position} 个`:''} · ${kind} · ${new Date(job.created_at*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})}${job.width?` · ${job.width} × ${job.height}`:''}`;
    summary.textContent=settingSummary(job.settings);content.append(name,meta,summary);item.append(image,content);
    const actions=document.createElement('div');actions.className='history-actions';
    const open=button(job.status==='done'?'查看':'恢复',async()=>{open.disabled=true;try{await restoreJob(job);dialog.close()}catch(error){showError(error);open.disabled=false}});
    open.disabled=state.busy&&state.job!==job.id||state.uploading;actions.append(open);
    if(['queued','running','pausing','paused'].includes(job.status)){
      const paused=job.status==='paused';
      if(paused||job.asset?.kind==='video'){
        const toggle=button(paused?'继续':'暂停',async()=>{toggle.disabled=true;try{const saved=await apiRequest(`/api/jobs/${job.id}/${paused?'resume':'pause'}`,{method:'POST'});if(state.job===job.id){state.jobRecord=saved;if(paused){state.busy=true;refreshControls();schedulePoll(job.id,100)}else showProgress(saved)}await render()}catch(error){showError(error)}finally{toggle.disabled=false}});
        toggle.disabled=job.status==='pausing'||paused&&job.resumable===false;
        if(paused&&job.resumable===false)toggle.title='运行环境已变化，请创建新的处理任务';actions.append(toggle);
      }
      actions.append(button('取消',async()=>{try{await apiRequest(`/api/jobs/${job.id}/cancel`,{method:'POST'});if(state.job===job.id)schedulePoll(job.id,100);await render()}catch(error){showError(error)}}));
    }
    if(job.download){const download=document.createElement('a');download.className='glass-button';download.textContent='下载';download.href=job.download;download.setAttribute('download','');actions.append(download)}
    const reuse=button('复用设置',()=>{applySettings(job.settings);settingsChanged();dialog.close();showNotice('已载入此版本的设置，重新处理后生效。')});reuse.disabled=state.busy||state.uploading;actions.append(reuse);
    const favorite=button(job.favorite?'已标满意':'标为满意',async()=>{favorite.disabled=true;try{await label(job,{favorite:!job.favorite});favorite.textContent=job.favorite?'已标满意':'标为满意';favorite.setAttribute('aria-pressed',String(job.favorite))}catch(error){showError(error)}finally{favorite.disabled=false}});
    favorite.setAttribute('aria-pressed',String(!!job.favorite));favorite.title='满意版本及其素材不会被缓存清理删除';actions.append(favorite);
    const edit=document.createElement('div');edit.className='history-name';edit.hidden=true;
    const input=document.createElement('input');input.type='text';input.maxLength=80;input.value=job.name||'';input.placeholder='填写版本名称';input.setAttribute('aria-label','版本名称');
    const save=button('保存名称',async()=>{save.disabled=true;try{await label(job,{name:input.value});name.textContent=job.name||source;name.title=name.textContent;edit.hidden=true}catch(error){showError(error)}finally{save.disabled=false}});
    const cancel=button('取消',()=>{edit.hidden=true});edit.append(input,save,cancel);
    actions.append(button('命名',()=>{edit.hidden=!edit.hidden;if(!edit.hidden)input.focus()}));item.append(actions,edit);return item;
  }
  async function render(){
    const current=++revision;plan=null;message.classList.remove('error');message.textContent='正在读取…';
    list.replaceChildren();q('.cache-preview').replaceChildren();q('#cleanup-cache').disabled=true;
    q('.cache-options').hidden=tab!=='cache';q('.history-filter').hidden=tab!=='recent';
    q('#cleanup-summary').textContent='正在计算清理范围…';
    for(const id of ['history-prev','history-next','history-page'])q('#'+id).hidden=tab==='cache';
    try{
      if(tab==='cache'){
        const payload=cleanupPayload();
        const [stats,preview]=await Promise.all([apiRequest('/api/cache'),apiRequest('/api/cache/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)})]);
        if(current!==revision)return;
        plan={...payload,preview_token:preview.preview_token};message.textContent=`素材与结果共占用 ${size(stats.bytes)}。`;
        const c=preview.counts;q('#cleanup-summary').textContent=`预计释放 ${size(preview.bytes)} · 临时目录 ${c.temporary} 个 · 源素材 ${c.asset} 份 · 已完成结果 ${c.result} 份`;
        for(const entry of preview.items){const li=document.createElement('li'),text=document.createElement('span'),bytes=document.createElement('span');text.textContent=`${{temporary:'临时文件',asset:'源素材',result:'处理结果'}[entry.kind]} · ${entry.name}`;text.title=entry.id;bytes.textContent=size(entry.bytes);li.append(text,bytes);q('.cache-preview').append(li)}
        q('#cleanup-cache').disabled=cleanupBusy||state.busy||state.uploading||!preview.items.length;
      }else{
        const source=q('#history-source').value;
        const [value,groups]=await Promise.all([apiRequest(`/api/jobs?offset=${page*perPage()}&limit=${perPage()}&source=${encodeURIComponent(source)}`),apiRequest('/api/history/sources')]);
        if(current!==revision)return;total=value.total;
        populateSelect(q('#history-source'),[['全部素材',''],...groups.sources.map(s=>[`${s.name} · ${s.count} 个版本`,s.id])],source);
        message.textContent=total?`共 ${total} 个版本。设置可复用，满意版本会保留。`:'还没有处理记录。上传素材并开始处理后，结果会出现在这里。';
        for(const job of value.jobs)list.append(row(job));
        const pages=Math.max(1,Math.ceil(total/perPage()));
        q('#history-page').textContent=`${page+1} / ${pages}`;q('#history-prev').disabled=page===0;q('#history-next').disabled=page+1>=pages;
      }
    }catch(error){if(current===revision)showError(error)}
  }
  $('#history-button').onclick=()=>{page=0;dialog.showModal();render()};
  q('.history-close').onclick=()=>dialog.close();q('#history-refresh').onclick=render;
  q('#history-source').onchange=()=>{page=0;render()};
  for(const control of dialog.querySelectorAll('[data-history-tab]'))control.onclick=()=>{tab=control.dataset.historyTab;page=0;dialog.querySelectorAll('[data-history-tab]').forEach(b=>b.setAttribute('aria-pressed',String(b===control)));render()};
  q('#history-prev').onclick=()=>{page--;render()};q('#history-next').onclick=()=>{page++;render()};
  q('#clear-results').onchange=event=>{q('#cleanup-warning').hidden=!event.target.checked;render()};
  q('#cleanup-cache').onclick=async()=>{
    if(!plan||cleanupBusy||state.busy||state.uploading)return;
    cleanupBusy=true;q('#cleanup-cache').disabled=true;q('#clear-results').disabled=true;
    try{const result=await apiRequest('/api/cache/cleanup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(plan)},60000);await render();message.textContent+=` 已释放 ${size(result.freed_bytes)}${result.skipped?`，${result.skipped} 个占用中的目录未清理`:''}。`}
    catch(error){await render();showError(error)}finally{cleanupBusy=false;q('#clear-results').disabled=false}
  };
  let timer;window.addEventListener('resize',()=>{clearTimeout(timer);timer=setTimeout(()=>{if(dialog.open){page=0;render()}},150)});
})();
