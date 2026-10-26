/* Reuse the existing workbench for protection, accurate inspection and durable jobs. */
(() => {
  const regionControl=form.elements.namedItem('protect_region');
  const layers=new Map();
  let drawing=false,drag=null,difference=false,lookupTimer,lookupRevision=0,batchBusy=false;
  let alignRevision=0,stepRevision=0,lookupAsset=null,lookupRanges=[];
  let previewWatchId=null,previewWatchTimer=0,previewWatchRevision=0;
  const pendingKey='dlss-batch-pending-v1';
  let pendingBatch=[];
  try{const saved=JSON.parse(localStorage.getItem(pendingKey)||'[]');if(Array.isArray(saved))pendingBatch=saved.filter(item=>item&&typeof item.asset_id==='string'&&typeof item.request_id==='string').slice(0,200)}catch{}
  function persistPending(){try{localStorage.setItem(pendingKey,JSON.stringify(pendingBatch))}catch{}}
  async function enqueueConfirmed(payload){
    if(!pendingBatch.some(item=>item.request_id===payload.request_id)){pendingBatch.push(payload);persistPending()}
    let attempt=0;
    while(true){
      try{
        const result=await apiRequest('/api/jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
        pendingBatch=pendingBatch.filter(item=>item.request_id!==payload.request_id);persistPending();return result;
      }catch(error){
        if(error.status&&![408,502,504].includes(error.status)){
          pendingBatch=pendingBatch.filter(item=>item.request_id!==payload.request_id);persistPending();throw error;
        }
        showNotice('正在确认队列提交，连接恢复后会继续；相同请求不会重复创建任务。');
        await new Promise(resolve=>setTimeout(resolve,Math.min(5000,500*2**Math.min(attempt++,4))));
      }
    }
  }
  const clamp=value=>Math.max(0,Math.min(1,value));
  const region=()=>{try{const value=JSON.parse(regionControl.value);return Array.isArray(value)&&value.length===4?value:[]}catch{return []}};
  const selectedStage=()=>state.mode==='video'?$('#video-viewport'):$('#image-stage');
  function setRegion(value){
    regionControl.value=JSON.stringify(value.map(n=>Math.round(n*100000)/100000));
    regionControl.dispatchEvent(new Event('change',{bubbles:true}));
    settingsChanged();refresh();
  }
  function finishDrawing(){
    drawing=false;drag=null;
    for(const layer of layers.values())layer.classList.remove('selecting');
    $('#protect-select').setAttribute('aria-pressed','false');
  }
  for(const stage of [$('#image-stage'),$('#video-viewport')]){
    const layer=document.createElement('div');layer.className='protection-layer';layer.hidden=true;
    const box=document.createElement('div');box.className='protection-box';box.innerHTML='<span>保留原画</span>';
    layer.append(box);stage.append(layer);layers.set(stage,layer);
    const point=event=>{const rect=stage.getBoundingClientRect();return [clamp((event.clientX-rect.left)/rect.width),clamp((event.clientY-rect.top)/rect.height)]};
    layer.addEventListener('pointerdown',event=>{
      if(!drawing||event.button!==0)return;
      event.preventDefault();event.stopPropagation();layer.setPointerCapture(event.pointerId);
      drag={id:event.pointerId,start:point(event),latest:point(event)};
    });
    layer.addEventListener('pointermove',event=>{
      if(!drag||drag.id!==event.pointerId)return;
      drag.latest=point(event);paintBox(box,dragRegion());
    });
    const done=event=>{
      if(!drag||drag.id!==event.pointerId)return;
      event.preventDefault();event.stopPropagation();
      const value=dragRegion();finishDrawing();
      if(value[2]>.005&&value[3]>.005)setRegion(value);else refresh();
    };
    layer.addEventListener('pointerup',done);
    layer.addEventListener('pointercancel',()=>{finishDrawing();refresh()});
  }
  function dragRegion(){const [a,b]=drag.start,[c,d]=drag.latest;return [Math.min(a,c),Math.min(b,d),Math.abs(c-a),Math.abs(d-b)]}
  function paintBox(box,value){
    box.hidden=value.length!==4;
    if(value.length===4){box.style.left=`${value[0]*100}%`;box.style.top=`${value[1]*100}%`;box.style.width=`${value[2]*100}%`;box.style.height=`${value[3]*100}%`}
  }
  $('#protect-select').onclick=()=>{
    if(!state.asset||state.busy||state.uploading)return;
    videoPlayer.pause();
    for(const dialog of document.querySelectorAll('dialog[open]'))dialog.close();
    if($('.inspector').classList.contains('mobile-open'))$('.inspector-close').click();
    drawing=!drawing;drag=null;
    if(drawing){previewZoom.reset();view('original');showNotice('在画面上拖出需要保留原画的区域；按 Escape 取消。')}
    refresh();
  };
  $('#protect-clear').onclick=()=>{finishDrawing();setRegion([])};
  document.addEventListener('keydown',event=>{if(event.key==='Escape'&&drawing){finishDrawing();refresh()}});
  function setDifference(value){
    difference=!!value;
    $('.canvas-panel').classList.toggle('difference-on',difference);
    $('#difference-view').setAttribute('aria-pressed',String(difference));
  }
  $('#difference-view').onclick=()=>{
    if(!currentOutput()?.preview)return;
    videoPlayer.pause();if(!difference)view('result');setDifference(!difference);
    if(state.mode==='video')alignPaused();
  };
  for(const button of $$('[data-view]'))button.addEventListener('click',()=>setDifference(false));
  async function alignPaused(){
    const job=state.output;
    if(state.mode!=='video'||state.framePreview||!job?.preview||!videoPlayer.source.paused||videoPlayer.source.seeking)return;
    const sourceTime=videoPlayer.source.currentTime,revision=++alignRevision;
    try{
      const mapping=await apiRequest(`/api/jobs/${job.id}/timeline?time=${encodeURIComponent(sourceTime)}&count=5`);
      if(revision!==alignRevision||state.output?.id!==job.id||!videoPlayer.source.paused||Math.abs(sourceTime-videoPlayer.source.currentTime)>.002)return;
      // A paused browser presents the preceding source PTS, not a rounded nominal frame.
      const frames=mapping.frames||[];
      const chosen=[...frames].reverse().find(f=>f.source_time<=sourceTime+.0005)||frames[0];
      if(!chosen)return;
      videoPlayer.exactMapping={assetId:state.asset.id,resultUrl:videoPlayer.resultUrl,sourceClock:sourceTime,resultTime:chosen.result_time};
      videoPlayer.sync(true);
      $('#video-time').title=`原片 ${StudioVideoPlayer.formatTime(chosen.source_time)} · 结果 ${StudioVideoPlayer.formatTime(chosen.result_time)} · 第 ${chosen.index+1} 帧`;
    }catch(error){if(error.status!==404)$('#video-time').title='使用媒体时间对齐；当前帧映射暂不可用'}
  }
  videoPlayer.source.addEventListener('seeking',()=>{videoPlayer.exactMapping=null;alignRevision++});
  for(const name of ['pause','seeked'])videoPlayer.source.addEventListener(name,alignPaused);
  videoPlayer.result.addEventListener('loadedmetadata',alignPaused);
  document.addEventListener('keydown',async event=>{
    if(!event.altKey||!['ArrowLeft','ArrowRight'].includes(event.key)||state.mode!=='video'||!state.asset||event.target.closest('input,select,textarea,dialog'))return;
    event.preventDefault();videoPlayer.pause();const time=videoPlayer.source.currentTime,forward=event.key==='ArrowRight';
    const assetId=state.asset.id,jobId=state.output?.id,revision=++stepRevision;
    const current=()=>revision===stepRevision&&state.mode==='video'&&state.asset?.id===assetId&&state.output?.id===jobId&&Math.abs(videoPlayer.source.currentTime-time)<.002;
    try{
      if(!state.output)throw Error('source-only');
      const map=await apiRequest(`/api/jobs/${state.output.id}/timeline?time=${time}&count=21`);
      if(!current())return;
      const frames=map.frames||[],frame=forward?frames.find(f=>f.source_time>time+.0005):[...frames].reverse().find(f=>f.source_time<time-.0005);
      if(frame){videoPlayer.seek(frame.source_time+.00001);return}
    }catch{}
    if(current())videoPlayer.seek(time+(forward?1:-1)/(videoPlayer.fps||30));
  });
  const coverage=document.createElement('div');coverage.className='cached-ranges';coverage.setAttribute('aria-hidden','true');$('.video-track').prepend(coverage);
  function paintCoverage(ranges){
    coverage.replaceChildren();const duration=videoPlayer.sourceDuration();if(!duration)return;
    if(!Array.isArray(ranges))return;
    for(const value of ranges||[]){
      const start=Number(value.source_start??value.start??0),end=Number(value.source_end??value.end??0);
      if(end<=start)continue;
      const span=document.createElement('span');span.style.left=`${clamp(start/duration)*100}%`;span.style.width=`${Math.max(0,clamp(end/duration)-clamp(start/duration))*100}%`;coverage.append(span);
    }
  }
  async function lookup(){
    if(!state.asset||state.busy||state.uploading)return;
    const revision=++lookupRevision,payload=settings();
    if(state.mode==='video'){
      const range=videoPlayer.getSelection();
      if(!range.full)Object.assign(payload,{job_kind:'clip',clip_start:range.start,clip_duration:range.duration});
    }
    try{
      const result=await apiRequest('/api/cache/lookup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      if(revision!==lookupRevision)return;
      lookupAsset=state.asset.id;lookupRanges=result.ranges||result.coverage?.ranges||result.coverage||[];
      paintCoverage(lookupRanges);
      $('#start-button').title=result.exact||result.cache_hit?'相同设置已有结果，将直接复用':result.stage_reusable?'可复用增强阶段，仅更新合成或编码':'按当前设置处理；已完成片段会保留';
    }catch{if(revision===lookupRevision)paintCoverage([])}
  }
  function settingsChangedHook(){lookupRevision++;lookupAsset=null;lookupRanges=[];clearTimeout(lookupTimer);lookupTimer=setTimeout(lookup,400);refresh()}
  $('#batch-add').onclick=()=>{const input=$('#batch-files');input.accept=$('#file-input').accept;input.click()};
  $('#batch-files').addEventListener('change',async event=>{
    const files=[...event.target.files];event.target.value='';if(!files.length||batchBusy)return;
    const values=settings(),mode=state.mode;
    batchBusy=true;refresh();let added=0;
    try{
      for(const file of files){
        const isImage=/\.(png|jpe?g|webp|bmp|tiff?)$/i.test(file.name);
        const isVideo=/\.(mp4|mov|mkv|webm|avi|m4v)$/i.test(file.name);
        if(mode==='image'?!isImage:!isVideo)throw Error(`素材类型与当前模式不符：${file.name}`);
        status(`正在加入队列 · ${added+1}/${files.length}`,'running');
        const body=new FormData();body.append('file',file);
        const asset=await readResponse(await fetch('/api/upload',{method:'POST',body}));
        await enqueueConfirmed({...values,asset_id:asset.id,job_kind:'full',request_id:crypto.randomUUID()});
        added++;
      }
      showNotice(`已将 ${added} 个素材加入队列，可在最近结果中查看进度。`);
    }catch(error){showNotice(`${added?`已加入 ${added} 个。`:''}${error.message}`,true)}
    finally{batchBusy=false;refresh();if(!state.busy)status(added?'任务已加入队列':'就绪');window.dispatchEvent(new Event('studio-jobs-changed'))}
  });
  $('#pause-job').onclick=async()=>{
    const job=state.jobRecord;if(!job||job.id!==state.job)return;
    const paused=job.status==='paused';$('#pause-job').disabled=true;
    try{
      const saved=await apiRequest(`/api/jobs/${job.id}/${paused?'resume':'pause'}`,{method:'POST'});
      state.jobRecord=saved;state.busy=saved.status!=='paused';refreshControls();showProgress(saved);schedulePoll(job.id,100);
    }catch(error){showNotice(error.message,true)}finally{refresh()}
  };
  $('#retry-source-preview').onclick=async()=>{
    if(state.mode!=='video'||!state.asset)return;
    const id=state.asset.id;$('#retry-source-preview').disabled=true;
    try{
      await apiRequest(`/api/assets/${id}/preview`,{method:'POST'});
      const next=await apiRequest(`/api/assets/${id}/info`);
      if(state.asset?.id===id){Object.assign(state.asset,next);videoPlayer.setSource(state.asset);refreshControls();saveSession()}
    }catch(error){showNotice(error.message,true)}finally{refresh()}
  };
  $('#copy-diagnostics').onclick=async()=>{
    try{const info=await apiRequest('/api/diagnostics');await navigator.clipboard.writeText(JSON.stringify(info,null,2));$('#diagnostic-status').textContent='诊断已复制，包含组件身份、运行结果与性能记录。'}
    catch(error){$('#diagnostic-status').textContent=`无法复制诊断：${error.message}`}
  };
  $('#continuous-self-test').onclick=async()=>{
    const button=$('#continuous-self-test');button.disabled=true;
    try{
      $('#diagnostic-status').textContent='正在准备连续视频自检…';
      const value=await apiRequest('/api/diagnostics/self-test',{method:'POST'},60000);
      $('#diagnostic-status').textContent='自检任务已加入队列，可在最近结果中查看。';
      if(!state.busy&&!state.uploading){const job=await apiRequest(`/api/jobs/${value.id}`);await restoreJob(job);$('#log-dialog').close()}
    }catch(error){$('#diagnostic-status').textContent=error.message}finally{button.disabled=false}
  };
  for(const rangeInput of $$('input[name=stability],input[name=protect_skin],input[name=protect_highlights]')){
    rangeInput.addEventListener('input',()=>{rangeInput.closest('.advanced-range').querySelector('output').textContent=`${Math.round(Number(rangeInput.value)*100)}%`});
  }
  function refresh(){
    const stage=selectedStage(),value=region();
    for(const [host,layer] of layers){layer.hidden=!state.asset||host!==stage||(!drawing&&!value.length);layer.classList.toggle('selecting',drawing&&host===stage);if(!drag)paintBox(layer.firstElementChild,value)}
    $('#protect-select').disabled=!state.asset||state.busy||state.uploading;
    $('#protect-select').textContent=drawing?'取消选择':value.length?'重新选择':'选择区域';
    $('#protect-select').setAttribute('aria-pressed',String(drawing));
    $('#protect-clear').disabled=!value.length||state.busy||state.uploading;
    $('#difference-view').disabled=!currentOutput()?.preview;
    if(!currentOutput()?.preview||state.view==='original')setDifference(false);
    $('#batch-add').disabled=batchBusy||!state.hardware?.ready;
    $('#batch-add').textContent=batchBusy?'正在加入…':'批量添加';
    $('#retry-source-preview').hidden=state.mode!=='video'||state.asset?.preview_status!=='failed';
    $('#retry-source-preview').disabled=state.uploading;
    const job=state.jobRecord,current=job?.id===state.job&&(job.asset_id===state.asset?.id||job.source_asset_id===state.asset?.id);
    const canPause=current&&job.asset?.kind==='video'&&['queued','running','pausing','paused'].includes(job.status);
    $('#pause-job').hidden=!canPause;$('#pause-job').disabled=job?.status==='pausing'||job?.status==='paused'&&job.resumable===false;$('#pause-job').textContent=job?.status==='paused'?'继续':'暂停';
    if(job?.metrics)$('#status-button').title=`查看处理日志与性能记录${job.metrics.total_seconds?` · 总耗时 ${job.metrics.total_seconds.toFixed(2)} 秒`:''}`;
    if(!state.busy&&state.mode==='video')paintCoverage(lookupAsset===state.asset?.id?lookupRanges:!resultDirty()?(state.output?.coverage?.ranges||state.output?.coverage||[]):[]);
    watchSourcePreview();
  }
  function watchSourcePreview(){
    const asset=state.mode==='video'?state.asset:null;
    if(!asset||asset.preview_status!=='preparing'){
      clearTimeout(previewWatchTimer);previewWatchTimer=0;previewWatchId=null;previewWatchRevision++;return;
    }
    if(previewWatchId===asset.id)return;
    previewWatchId=asset.id;const revision=++previewWatchRevision,id=asset.id;
    const pollPreview=async()=>{
      if(revision!==previewWatchRevision||state.mode!=='video'||state.asset?.id!==id)return;
      try{
        const next=await apiRequest(`/api/assets/${id}/info`);
        if(revision!==previewWatchRevision||state.mode!=='video'||state.asset?.id!==id)return;
        Object.assign(state.asset,next);videoPlayer.setSource(state.asset);refreshControls();saveSession();
        if(next.preview_status==='failed')showNotice('原片显示预览未能生成，仍可处理并下载增强结果。',true);
        if(next.preview_status!=='preparing')return;
      }catch{}
      if(revision===previewWatchRevision)previewWatchTimer=setTimeout(pollPreview,1000);
    };
    previewWatchTimer=setTimeout(pollPreview,250);
  }
  window.StudioEnhancements={refresh,settingsChanged:settingsChangedHook,alignPaused,lookup};
  window.addEventListener('studio-jobs-changed',()=>lookup());
  $('#reset-settings').addEventListener('click',()=>{finishDrawing();setTimeout(settingsChangedHook,0)});
  videoPlayer.source.addEventListener('loadedmetadata',settingsChangedHook);
  if(pendingBatch.length)setTimeout(async()=>{
    batchBusy=true;refresh();let confirmed=0;
    try{for(const payload of [...pendingBatch]){await enqueueConfirmed(payload);confirmed++}showNotice(`已恢复确认 ${confirmed} 个队列任务。`)}
    catch(error){showNotice(error.message,true)}finally{batchBusy=false;refresh();window.dispatchEvent(new Event('studio-jobs-changed'))}
  },0);
  refresh();
})();
