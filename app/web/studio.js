const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const form = $('#settings-form');
const state = {mode:'image', asset:null, output:null, framePreview:null, selection:null, jobKind:'full', jobContext:null, view:'original', style:'默认', busy:false, uploading:false, sourceWidth:0,sourceHeight:0, job:null, jobRecord:null, hardware:null, pendingPayload:null};
const sessions = {image:null,video:null};
let options = {containers:{hevc_nvenc:['mp4','mkv','mov']}};
let dragDepth=0, pollTimer=0, polling=false;
const videoFields=new Set(["motion_engine","motion","motion_vis","codec","container","bit_depth","cq","bitrate","bitrate_mode","audio","enc_preset","prores_profile","frames","stability","preserve_media","timing_mode"]);
const showNotice = (text,error=false) => {$('#notice-text').textContent=text;$('#notice').hidden=false;$('#notice').classList.toggle('error',error)};
const videoPlayer = new StudioVideoPlayer({onChange:refreshControls,onFrameCleared(){
  state.framePreview=null;
  if(state.mode==='video'){view(state.output?.preview?state.view:'original');status(state.output?'处理完成':'就绪');$('#notice').hidden=true;refreshControls()}
},onError:message=>showNotice(message,true),onViewChange:view,onSelectionChange(selection){state.selection=selection;settingsChanged();refreshControls()}});
const previewZoom = new StudioZoom(()=>{
  if(!state.asset)return null;
  if(state.mode==='image')return {stage:$('#image-stage'),area:$('#canvas'),width:state.view==='original'?state.sourceWidth:$('#result-image').naturalWidth||state.sourceWidth};
  return {stage:$('#video-viewport'),area:$('#video-picture'),width:state.framePreview&&state.view!=='original'?$('#video-frame-result').naturalWidth||state.sourceWidth:state.view==='original'?videoPlayer.source.videoWidth||state.sourceWidth:videoPlayer.result.videoWidth||state.sourceWidth};
});
videoPlayer.onGeometry=()=>previewZoom.fit();
function currentOutput(){return state.mode==='video'&&state.framePreview?state.framePreview.job:state.output}
const paintRanges=()=>$$('input[type=range][name]').forEach(el=>{el.style.setProperty('--range',`${100*(Number(el.value)-Number(el.min))/(Number(el.max)-Number(el.min))}%`);el.setAttribute('aria-valuetext',`${Math.round(Number(el.value)*100)}%`)});
function theme(value){document.documentElement.dataset.theme=value;try{localStorage.setItem('dlss-theme',value)}catch{}$$('.theme-switch button').forEach(b=>{const active=b.dataset.theme===value;b.classList.toggle('active',active);b.setAttribute('aria-pressed',active)})}
$$('.theme-switch button').forEach(b=>b.addEventListener('click',()=>theme(b.dataset.theme)));
theme(document.documentElement.dataset.theme);
function glass(enabled,persist=true){
  const maximized=document.documentElement.classList.contains('desktop-maximized');
  if(maximized)enabled=false;
  document.documentElement.dataset.glass=enabled?'on':'off';
  const button=$('#glass-toggle');
  button.classList.toggle('active',enabled);
  button.setAttribute('aria-pressed',String(enabled));
  button.disabled=maximized;
  button.title=maximized?'最大化时关闭玻璃，还原后恢复设置':enabled?'关闭面板玻璃':'开启面板玻璃';
  if(persist&&!maximized)try{localStorage.setItem('dlss-glass',enabled?'on':'off')}catch{}
}
$('#glass-toggle').addEventListener('click',()=>glass(document.documentElement.dataset.glass!=='on'));
glass(document.documentElement.dataset.glass==='on');

function status(text,kind=''){ $('#status-text').textContent=text;$('.status-dot').className=`status-dot ${kind}`; }
function fitStage(){
  if(!state.sourceWidth||!state.sourceHeight)return;
  if(state.mode==='video'){videoPlayer.fit();return}
  const el=$('#canvas'),ratio=state.sourceWidth/state.sourceHeight;
  $('#image-stage').style.width=`${Math.min(el.clientWidth,el.clientHeight*ratio)}px`;
  $('#image-stage').style.aspectRatio=String(ratio);previewZoom.fit();
}
new ResizeObserver(fitStage).observe($('#canvas'));
function sizeKey(){return $('#size-preset').value}
function view(value){if(value!=='original'&&!currentOutput()?.preview)return;state.view=value;$$('[data-view]').forEach(b=>{b.classList.toggle('active',b.dataset.view===value);b.setAttribute('aria-pressed',b.dataset.view===value)});const compare=value==='compare'&&state.mode==='image';$('#compare-line').hidden=!compare;$('#compare-slider').hidden=!compare;$('#result-label').hidden=!compare;$('#source-label').hidden=value==='result';$('#result-image').hidden=value==='original'||state.mode!=='image';$('#result-image').style.clipPath=value==='result'?'none':`inset(0 0 0 ${$('#compare-slider').value}%)`;if(state.mode==='video')videoPlayer.setView(value);fitStage();previewZoom.reset();saveSession()}
$$('[data-view]').forEach(b=>b.addEventListener('click',()=>view(b.dataset.view)));
$('#compare-slider').addEventListener('input',e=>{const value=e.target.value;$('#result-image').style.clipPath=`inset(0 0 0 ${value}%)`;$('#compare-line').style.left=`${value}%`});
function refreshControls(){
  const locked=state.busy||state.uploading,output=currentOutput();
  $('#start-button').disabled=locked||!state.asset||!state.hardware?.ready||(state.mode==='video'&&!videoPlayer.sourceDuration());
  $('#upload-button').disabled=locked;$('#empty-upload').disabled=locked;
  $$('[data-mode]').forEach(b=>b.disabled=locked);[...form.elements].forEach(e=>e.disabled=locked||e.dataset.codecDisabled==='true'||(state.mode==='image'&&(videoFields.has(e.name)||e.id==='quality')));$('#reset-settings').disabled=locked;
  form.elements.hdr.closest('label').hidden=state.mode==='video';form.elements.hdr.disabled=locked||state.mode==='video';
  $$('[data-view]').forEach(b=>b.disabled=b.dataset.view!=='original'&&(!output?.preview||state.mode==='video'&&b.dataset.view==='compare'&&!videoPlayer.canCompare()));
  videoPlayer.setLocked(state.uploading||state.busy&&state.jobKind==='frame',locked);
  const download=$('#download'),ready=!!output?.download;
  download.classList.toggle('disabled',!ready);download.setAttribute('aria-disabled',String(!ready));download.tabIndex=ready?0:-1;
  if(ready){download.href=output.download;download.setAttribute('download','')}else download.removeAttribute('href');
  $('#download-label').textContent=state.mode==='video'&&state.framePreview?'下载此帧':'下载结果';
  $('#job-progress').hidden=!state.busy;$('#cancel-job').hidden=!state.busy;download.hidden=state.busy;$('#cancel-job').disabled=!state.job;previewZoom.fit();
  $('#start-button span').textContent=state.busy?(state.jobKind==='frame'?'正在测试…':'正在处理…'):state.uploading?'正在上传…':'开始处理';
  if(typeof refreshProduct==='function')refreshProduct();
  window.StudioEnhancements?.refresh();
}
function showAsset(){
  const item=state.asset, imageMode=state.mode==='image';
  $('#file-name').textContent=item?item.name:imageMode?'尚未选择图片':'尚未选择视频';
  $('#file-meta').textContent=item?`${item.width} × ${item.height}`:'';
  $('#source-label').textContent='原图';
  if(item?.kind==='image')$('#source-image').src=item.url;
  else $('#source-image').removeAttribute('src');
  $('#source-image').alt=item?.name||'原图';
  $('#image-stage').hidden=!imageMode||!item;
  $('.canvas-panel').classList.toggle('video-mode',!imageMode);
  videoPlayer.setVisible(!imageMode&&!!item);
  if(!imageMode){videoPlayer.setSource(item);videoPlayer.setSelection(state.selection);videoPlayer.setResult(state.output?.preview||null,state.output?.preview_offset??state.output?.actual_start??state.output?.clip_start??0,state.output?.job_kind==='clip');videoPlayer.setFrame(state.framePreview)}
  $('[data-view=original]').textContent=imageMode?'原图':'原视频';
  $('#empty-state').hidden=!!item;
  $('#empty-state h2').textContent=imageMode?'把图片拖到这里':'把视频拖到这里';
  $('#empty-state p').textContent=imageMode?'支持 PNG、JPG、WebP 等常见格式':'支持 MP4、MOV、MKV 等常见格式';
  $('#empty-upload').textContent=imageMode?'选择图片':'选择视频';
  state.sourceWidth=item?.width||0;state.sourceHeight=item?.height||0;paintBitrate();
  if(state.output?.preview&&imageMode)$('#result-image').src=state.output.preview;
  else $('#result-image').removeAttribute('src');
  view('original');refreshControls();fitStage();previewZoom.reset();
  window.StudioEnhancements?.settingsChanged();
}

function switchMode(mode){if(state.busy||state.uploading||state.mode===mode)return;videoPlayer.pause();sessions[state.mode]={asset:state.asset,output:state.output,framePreview:state.framePreview,selection:state.selection};state.mode=mode;const saved=sessions[mode];state.asset=saved?.asset||null;state.output=saved?.output||null;state.framePreview=saved?.framePreview||null;state.selection=saved?.selection||null;$$('[data-mode]').forEach(b=>{b.classList.toggle('active',b.dataset.mode===mode);b.setAttribute('aria-pressed',b.dataset.mode===mode)});$('#video-settings').hidden=mode!=='video';$('#file-input').accept=mode==='image'?'image/png,image/jpeg,image/webp,image/bmp,image/tiff':'.mp4,.mov,.mkv,.webm,.avi,.m4v';$('#notice').hidden=true;$('#job-log').textContent='尚未开始处理。';status(currentOutput()?'处理完成':state.asset?'就绪':'等待素材');showAsset();saveSession()}
$$('[data-mode]').forEach(b=>b.addEventListener('click',()=>switchMode(b.dataset.mode)));
async function readResponse(response){
  let value;
  try{value=await response.json()}catch{throw new Error('服务响应异常，正在等待恢复。')}
  if(!response.ok){const error=new Error(typeof value.detail==='string'?value.detail:'参数无效，请检查输入数值。');error.status=response.status;throw error}
  return value;
}
async function apiRequest(url,options={},timeout=15000){
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),timeout);
  try{return await readResponse(await fetch(url,{...options,signal:controller.signal}))}finally{clearTimeout(timer)}
}
async function upload(file){if(!file||state.busy||state.uploading)return;const isImage=/\.(png|jpe?g|webp|bmp|tiff?)$/i.test(file.name);const isVideo=/\.(mp4|mov|mkv|webm|avi|m4v)$/i.test(file.name);if(state.mode==='image'&&!isImage||state.mode==='video'&&!isVideo){showNotice(`请在${state.mode==='image'?'图片':'视频'}增强中上传对应格式的文件。`,true);return}videoPlayer.pause();$('#notice').hidden=true;state.uploading=true;refreshControls();status('正在上传…','running');const body=new FormData();body.append('file',file);try{state.asset=await readResponse(await fetch('/api/upload',{method:'POST',body}));state.output=null;state.framePreview=null;state.selection=null;$('#notice').hidden=true;$('#job-log').textContent='素材已准备好。';status('就绪');showAsset();saveSession()}catch(e){showNotice(e.message,true);status('上传失败','error')}finally{state.uploading=false;refreshControls();saveSession();$('#file-input').value=''}}
$('#upload-button').addEventListener('click',()=>$('#file-input').click());$('#empty-upload').addEventListener('click',()=>$('#file-input').click());$('#file-input').addEventListener('change',e=>upload(e.target.files[0]));
const panel=$('.canvas-panel');panel.addEventListener('dragenter',e=>{e.preventDefault();if(state.busy||state.uploading)return;dragDepth++;$('#drop-overlay').hidden=false});panel.addEventListener('dragover',e=>e.preventDefault());panel.addEventListener('dragleave',()=>{dragDepth--;if(dragDepth<=0)$('#drop-overlay').hidden=true});panel.addEventListener('drop',e=>{e.preventDefault();dragDepth=0;$('#drop-overlay').hidden=true;upload(e.dataTransfer.files[0])});document.addEventListener('dragover',e=>e.preventDefault());document.addEventListener('drop',e=>e.preventDefault());
$$('[data-style]').forEach(b=>b.addEventListener('click',()=>{state.style=b.dataset.style;$$('[data-style]').forEach(e=>{e.classList.toggle('active',e===b);e.setAttribute('aria-pressed',e===b)})}));
$('#intensity').addEventListener('input',e=>{$('#intensity-number').value=Math.round(Number(e.target.value)*100);paintRanges()});$('#intensity-number').addEventListener('input',e=>{if(e.target.value!==''&&e.target.validity.valid){$('#intensity').value=Number(e.target.value)/100;paintRanges()}});
const advanced=[['local_structure','局部结构',0,2,1],['local_tone','局部色调',0,2,1],['skin','皮肤细节',0,2,1],['global_tone','全局色调',0,2,1],['detail','效果融合',0,2,1],['color','色彩融合',0,1,1]];
for(const [name,label,min,max,val] of advanced){const div=document.createElement('div');div.className='advanced-range';div.innerHTML=`<label for="${name}">${label}<output>${Math.round(val*100)}%</output></label><input id="${name}" name="${name}" type="range" min="${min}" max="${max}" value="${val}" step="0.05">`;div.querySelector('input').addEventListener('input',e=>{div.querySelector('output').textContent=`${Math.round(Number(e.target.value)*100)}%`;paintRanges()});$('#advanced-sliders').append(div)}
function populateSelect(el,entries,selected){el.replaceChildren();entries.forEach(([text,value])=>{const option=document.createElement('option');option.textContent=text;option.value=value;el.append(option)});if(selected&&entries.some(([,v])=>v===selected))el.value=selected}
function recommendedBitrate(){
  if(sizeKey()!=='原始尺寸（不放大）')return options.video_defaults?.bitrates[sizeKey()]||6000;
  const longEdge=Math.max(state.sourceWidth,state.sourceHeight);
  return longEdge<=1920?6000:longEdge<=2560?9000:12000;
}
function paintBitrate(){
  $('#quality option[value=auto]').textContent=`推荐码率 · ${recommendedBitrate()/1000} Mbps`;
}
function updateCodec(){
  const codec=$('#codec').value,nvenc=codec.endsWith('_nvenc'),cpuH264=codec==='h264_cpu',quality=nvenc||codec==='av1_svt'||cpuH264;
  for(const option of $('#quality').options)option.disabled=cpuH264&&!['auto','custom'].includes(option.value);
  if($('#quality').selectedOptions[0]?.disabled)$('#quality').value='auto';
  const cq=form.elements.cq; cq.closest('label').hidden=cpuH264;
  form.elements.bitrate.min=cpuH264?'1':'0';
  if(cpuH264&&!Number(form.elements.bitrate.value))form.elements.bitrate.value=recommendedBitrate();
  const allowed=options.containers[codec]||['mp4'];populateSelect($('#container'),allowed.map(v=>[v,v]),$('#container').value);
  const flags={'prores-field':codec==='prores','bit-depth-field':quality&&!codec.startsWith('h264_'),'quality-field':quality,'enc-preset-field':nvenc,'custom-quality':quality&&$('#quality').value==='custom'};
  for(const [id,visible] of Object.entries(flags)){
    const field=$('#'+id);field.hidden=!visible;
    field.querySelectorAll('input,select').forEach(el=>el.dataset.codecDisabled=String(!visible));
  }
  if(cpuH264)cq.dataset.codecDisabled='true';
  $('#codec-note').textContent=codec==='prores'?'ProRes 使用固定 10 bit，画质与体积由 ProRes 规格决定。':codec==='ffv1'?'FFV1 保存无损 RGB，无需选择压缩画质和编码预设。':codec.startsWith('h264_')?'H.264 使用 8 bit；推荐码率随分辨率自动调整，也可自定义。':'推荐码率随分辨率自动调整；选择恒定画质时，数值越低，质量越高。';
  paintBitrate();
  refreshControls();
}
$('#codec').addEventListener('change',updateCodec);$('#quality').addEventListener('change',updateCodec);
$('#reset-settings').addEventListener('click',()=>{form.reset();form.elements.protect_region.value='[]';state.style='默认';$('#intensity-number').value=100;$$('[data-style]').forEach(b=>{b.classList.toggle('active',b.dataset.style==='默认');b.setAttribute('aria-pressed',b.dataset.style==='默认')});$$('details').forEach(d=>d.open=false);$('#custom-quality').hidden=true;$$('.advanced-range').forEach(div=>div.querySelector('output').textContent=`${Math.round(Number(div.querySelector('input').value)*100)}%`);updateCodec();$('#container').value=options.video_defaults.container;paintRanges();settingsChanged()});
function settings(){const result={asset_id:state.asset?.id,size:sizeKey(),style:state.style};for(const el of form.elements){if(!el.name||el.dataset.codecDisabled==='true'||(state.mode==='image'&&videoFields.has(el.name))||(state.mode==='video'&&el.name==='hdr'))continue;result[el.name]=el.type==='checkbox'?el.checked:el.type==='number'||el.type==='range'?Number(el.value):el.value}result.size=sizeKey();if(result.bit_depth!==undefined)result.bit_depth=Number(result.bit_depth);result.render_scale=Number(result.render_scale??1);try{result.protect_region=JSON.parse(result.protect_region||'[]')}catch{result.protect_region=[]}if(result.codec?.startsWith('h264_'))result.bit_depth=8;if(state.mode==='video'&&$('#quality').dataset.codecDisabled!=='true'){const quality=$('#quality').value;result.bitrate_mode=quality==='auto'?'auto':'manual';if(quality==='auto'){result.cq=19;result.bitrate=recommendedBitrate()}else if(quality!=='custom'){result.cq=Number(quality);result.bitrate=0}}return result}
function showProgress(job){
  state.jobRecord=job;
  const progress=$('#progress-bar');
  if(Number.isFinite(job.progress))progress.value=job.progress;else progress.removeAttribute('value');
  let text=job.message||'准备处理…';
  if(job.phase==='render'&&job.status!=='cancelling')text=`正在增强 · ${job.progress}% · ${job.frame}/${job.total_frames} 帧${job.eta&&job.eta!=='--:--'?` · 预计剩余 ${job.eta}`:''}`;
  $('#progress-text').textContent=text;
  $('#cancel-job').textContent=job.status==='cancelling'?'取消中…':'取消任务';
  $('#cancel-job').disabled=!state.job||job.status==='cancelling';
  status(job.phase==='preview'?'正在生成预览':job.status==='cancelling'?'正在取消':Number.isFinite(job.progress)?`正在处理 ${job.progress}%`:'正在处理…','running');
  if(job.status==='queued')status(job.queue_position?`排队中 · 第 ${job.queue_position} 个`:'排队中','running');
  if(job.status==='pausing')status('正在保存暂停点…','running');
  window.StudioEnhancements?.refresh();
}
function schedulePoll(id,delay){clearTimeout(pollTimer);pollTimer=setTimeout(()=>poll(id),delay)}
async function poll(id,attempt=0){
  if(id!==state.job)return;
  if(polling){schedulePoll(id,150);return}
  polling=true;
  try{
    const job=await apiRequest(`/api/jobs/${id}`,{},8000);if(id!==state.job)return;
    $('#job-log').textContent=job.log||job.message;
    state.jobRecord=job;
    if(['queued','running','pausing','cancelling'].includes(job.status)){
      state.busy=true;refreshControls();showProgress(job);schedulePoll(id,1200);return;
    }
    state.busy=false;state.pendingPayload=null;
    if(job.status==='done'){
      if(state.jobKind==='frame'){
        state.framePreview={job,time:job.frame_time??state.jobContext.time,source:job.asset?.url||state.jobContext.source};
        videoPlayer.setFrame(state.framePreview);paintHardware(job.hardware);view('compare');
        status('单帧测试完成','done');showNotice(`已增强 ${StudioVideoPlayer.formatTime(state.framePreview.time)} 的当前帧。播放或移动时间轴会返回视频。`);
      }else{
        state.output=job;paintHardware(job.hardware);status('处理完成','done');
        if(state.mode==='image'&&job.preview)$('#result-image').src=job.preview;
        if(state.mode==='video')videoPlayer.setResult(job.preview||null,job.preview_offset??job.actual_start??job.clip_start??0,job.job_kind==='clip');
        view(job.preview?'result':'original');
        showNotice(job.cache_hit?'已复用相同设置的结果，可以直接查看或下载。':job.stage_cache_hit?'已复用增强阶段，完成合成与导出。':job.preview?'处理完成，可以切换预览或下载结果。':'处理完成，此格式无法直接预览，请下载结果。');
      }
      if(job.storage_warning)showNotice(job.storage_warning,true);
    }else if(job.status==='paused'){status('已暂停');showNotice('已保存处理进度，可以继续；关闭程序后也会保留。')}
    else{status(job.status==='cancelled'?'已取消':'处理失败',job.status==='cancelled'?'':'error');showNotice(job.message,job.status!=='cancelled')}
    refreshControls();saveSession();window.StudioEnhancements?.lookup();
  }catch(error){
    if(id!==state.job)return;
    if(error.status===404){state.busy=false;status('任务已失效','error');showNotice(error.message,true);refreshControls();saveSession();return}
    state.busy=true;refreshControls();status('正在重连…','error');$('#progress-text').textContent='连接暂时中断，正在自动重连；当前任务仍保留。';
    clearTimeout(pollTimer);pollTimer=setTimeout(()=>poll(id,attempt+1),Math.min(10000,1200*2**Math.min(attempt,3)));
  }finally{polling=false}
}
async function submitJob(payload){
  payload.request_id ||= crypto.randomUUID();state.pendingPayload=payload;saveSession();
  while(state.busy&&state.pendingPayload===payload){
    try{
      const job=await apiRequest('/api/jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      state.job=job.id;state.pendingPayload=null;saveSession();poll(job.id);return;
    }catch(error){
      if(error.status&&![408,502,504].includes(error.status)){state.pendingPayload=null;saveSession();throw error}
      status('正在重连…','error');$('#progress-text').textContent='正在确认任务是否已提交，恢复连接后会继续，不会重复处理。';
      await new Promise(resolve=>setTimeout(resolve,3000));
    }
  }
}
$('#cancel-job').addEventListener('click',async()=>{
  if(!state.job||!state.busy)return;
  $('#cancel-job').disabled=true;
  try{const job=await apiRequest(`/api/jobs/${state.job}/cancel`,{method:'POST'});showProgress(job);schedulePoll(state.job,200)}
  catch(error){showNotice('取消请求未确认，请恢复连接后重试。',true);$('#cancel-job').disabled=false}
});
$('#start-button').addEventListener('click',async()=>{
  if(!state.asset||state.busy||state.uploading||!state.hardware?.ready)return;
  if(!form.reportValidity()||!$('#intensity-number').reportValidity())return;
  const payload=settings();
  // Timeline handles own the range; legacy history frame caps never limit exports.
  if(state.mode==='video'){
    const range=videoPlayer.getSelection();
    if(!(range.duration>0)){showNotice('视频尚未准备好，请稍后重试。',true);return}
    payload.frames=0;
    if(!range.full)Object.assign(payload,{job_kind:'clip',clip_start:range.start,clip_duration:range.duration});
  }videoPlayer.pause();videoPlayer.clearFrame(false);
  state.busy=true;state.jobKind=payload.job_kind||'full';state.job=null;state.framePreview=null;
  view('original');refreshControls();$('#notice').hidden=true;showProgress({message:'准备处理…'});
  try{await submitJob(payload)}catch(e){state.busy=false;showNotice(e.message,true);status('无法开始','error');refreshControls();saveSession()}
});
const frameFields=['size','style','intensity','local_structure','local_tone','skin','global_tone','detail','color','ui_correction','auto_mask','hdr','protect_region','protect_skin','protect_highlights','render_scale'];
// Retained for legacy frame-history validation; the workspace exports timeline selections.
async function renderFrame(){
  if(state.mode!=='video'||!state.asset||state.busy||state.uploading||!state.hardware?.ready)return;
  videoPlayer.pause();
  if(!$('#intensity-number').reportValidity()||frameFields.some(name=>{const el=form.elements.namedItem(name);return el?.reportValidity&&!el.reportValidity()}))return;
  const all=settings(),payload=Object.fromEntries(frameFields.filter(name=>name in all).map(name=>[name,all[name]]));
  state.busy=true;state.jobKind='frame';state.job=null;refreshControls();$('#notice').hidden=true;showProgress({message:'读取当前帧…'});
  try{
    const frame=await videoPlayer.captureFrame(),body=new FormData();
    body.append('file',frame.blob,`当前帧-${Math.round(frame.time*1000)}ms.png`);
    const asset=await readResponse(await fetch('/api/upload',{method:'POST',body}));
    state.jobContext={time:frame.time,source:asset.url};payload.asset_id=asset.id;payload.job_kind='frame';payload.source_asset_id=state.asset.id;payload.frame_time=frame.time;
    status('正在增强当前帧…','running');await submitJob(payload);
  }catch(e){state.busy=false;showNotice(e.message,true);status('单帧测试失败','error');refreshControls();saveSession()}
}
$('#close-notice').addEventListener('click',()=>$('#notice').hidden=true);$('#status-button').addEventListener('click',()=>$('#log-dialog').showModal());$('#close-log').addEventListener('click',()=>$('#log-dialog').close());$('#log-dialog').addEventListener('click',e=>{if(e.target===$('#log-dialog')){const r=e.target.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)e.target.close()}});
form.addEventListener('submit',e=>e.preventDefault());
async function init(){
  try{
    options=await apiRequest('/api/options');paintHardware(options.hardware);const codecLabels={hevc_nvenc:'HEVC · 日常播放 / GPU',h264_nvenc:'H.264 · 通用播放 / GPU',h264_cpu:'H.264 · 通用播放 / CPU',av1_nvenc:'AV1 · 较小文件 / GPU',av1_svt:'AV1 · 较小文件 / CPU',prores:'ProRes · 后期剪辑 / CPU',ffv1:'FFV1 · 无损归档 / CPU'};populateSelect($('#codec'),Object.entries(options.codecs).map(([label,value])=>[codecLabels[value]||label,value]));
    const select=$('#codec'),defaults=options.video_defaults;
    for(const option of select.options){
      if(option.value.endsWith('_nvenc')&&!(options.encoder_available?.[option.value]??options.nvenc_available)){option.disabled=true;option.textContent+='（当前不可用）'}
      option.defaultSelected=option.value===defaults.codec;
    }
    select.value=defaults.codec;
    updateCodec();paintRanges();showAsset();await restoreSession();
  }catch(error){$('#gpu-status').textContent='服务离线';$('.engine-status').dataset.ready='false';showNotice('无法连接处理服务，正在重试…',true);setTimeout(init,5000)}
}
// history.js supplies recovery before the deferred initialization starts.
window.addEventListener('DOMContentLoaded',init,{once:true});

function paintHardware(hardware){
  if(!hardware){
    state.hardware={ready:false};
    $('#gpu-name').textContent='处理服务版本不匹配';
    $('#gpu-status').textContent='服务不匹配';
    $('.engine-status').dataset.ready='false';
    $('.engine-status').title='当前处理服务版本不匹配，请使用新版启动器打开页面。';
    showNotice('当前页面连接的是旧版处理服务，无法识别显卡。请关闭此旧页面，通过 DLSS Studio 启动器打开当前服务，再上传素材。',true);
    refreshControls();
    return;
  }
  state.hardware=hardware;
  const name=$('#gpu-name'), status=$('#gpu-status');
  name.textContent=hardware.gpu_name||'未找到可用显卡';name.title=name.textContent;
  const series={rtx30:'30 系组件',rtx40:'40 系组件',rtx50:'50 系组件'}[hardware.profile];
  status.textContent=hardware.gpu_name?hardware.gpu_name.replace(/^NVIDIA\s+/i,'').replace(/\s+GeForce\s*/i,' ').replace(/^GeForce\s+/i,'').replace(/\s+Blackwell Workstation Edition$/i,'').replace(/\s+Laptop GPU$/i,' Laptop'):hardware.ready?'显卡已就绪':'未找到可用显卡';
  const badge=$('.engine-status');
  badge.dataset.ready=String(!!hardware.ready);
  badge.title=`DLSS 5 · 自动适配\n${name.textContent}\n${series||'未匹配组件'} · ${hardware.message||''}`;
  badge.setAttribute('aria-label',badge.title);
  status.title=hardware.message||'';
  if(!hardware.ready)showNotice(hardware.message,true);
  refreshControls();
}
