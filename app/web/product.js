/* Settings are drafts; every rendered version retains its own settings. */
const renderKeys=['size','style','intensity','local_structure','local_tone','skin','global_tone','detail','color','ui_correction','auto_mask','hdr','protect_region','protect_skin','protect_highlights','render_scale'];
function settingSummary(values={}){
  const changes=[['local_structure','结构'],['local_tone','局部色调'],['skin','皮肤'],['global_tone','全局色调'],['detail','效果融合'],['color','色彩融合']]
    .filter(([key])=>values[key]!==undefined&&Number(values[key])!==1).map(([key,label])=>`${label} ${Math.round(values[key]*100)}%`);
  for(const [key,label] of [['ui_correction','界面修正'],['auto_mask','自动遮罩'],['hdr','HDR']])if(values[key])changes.push(label);
  for(const [key,label] of [['stability','时序稳定'],['protect_skin','肤色保护'],['protect_highlights','高光保护']])if(values[key])changes.push(`${label} ${Math.round(values[key]*100)}%`);
  if(values.protect_region?.length===4)changes.push('区域保护');
  if(values.render_scale&&values.render_scale!==1)changes.push(`推理 ${Math.round(values.render_scale*100)}%`);
  return `${values.style||'默认'} · 强度 ${Math.round((values.intensity??1)*100)}%${changes.length?' · '+changes.join(' · '):''}`;
}
function resultDirty(){
  const output=currentOutput();if(!output?.settings||!state.asset)return false;
  const desired=settings(),keys=[...renderKeys];
  if(state.mode==='video'&&output.job_kind!=='frame'){
    const range=videoPlayer.getSelection(),clip=output.job_kind==='clip';
    if(clip===range.full||clip&&(Math.abs(range.start-output.settings.clip_start)>.000001||Math.abs(range.duration-output.settings.clip_duration)>.000001))return true;
  }
  if(state.mode==='video'&&output.job_kind!=='frame'){
    keys.push('motion','motion_vis','motion_engine','codec','container','audio','stability','timing_mode','preserve_media');
    const codec=desired.codec;
    if(codec==='prores')keys.push('prores_profile');
    else if(codec!=='ffv1'){keys.push('cq','bitrate');if(!codec.startsWith('h264_'))keys.push('bit_depth');if(codec.endsWith('_nvenc'))keys.push('enc_preset')}
  }
  const defaults={protect_region:[],protect_skin:0,protect_highlights:0,stability:0,render_scale:1,timing_mode:'preserve',preserve_media:true};
  return keys.some(key=>desired[key]!==undefined&&JSON.stringify(desired[key])!==JSON.stringify(output.settings[key]??defaults[key]));
}
function refreshProduct(){
  const output=currentOutput(),dirty=resultDirty(),video=state.mode==='video',locked=state.busy||state.uploading;
  const range=videoPlayer.getSelection();
  $('#apply-preset').disabled=locked||!$('#user-presets').value;$('#delete-preset').disabled=locked||!$('#user-presets').value;
  $('.view-switch').hidden=!state.asset;$('.zoom-controls').hidden=!state.asset;
  $('#upload-button span').textContent=state.asset?'更换素材':'选择素材';
  const kind=output?.job_kind==='frame'?`单帧 ${StudioVideoPlayer.formatTime(output.frame_time)}`:output?.job_kind==='clip'?`片段 ${StudioVideoPlayer.formatTime(output.clip_start)} 起`:'已生成结果';
  $('#download').title=output?`${kind} · ${output.width} × ${output.height} · ${settingSummary(output.settings)}`:'';
  if(dirty&&!locked&&$('#status-text').textContent.includes('完成'))status('设置待应用');
  if(!dirty&&!locked&&$('#status-text').textContent==='设置待应用')status('处理完成','done');
  if(!locked)$('#start-button span').textContent=video?dirty?'应用设置并处理':range.full?'处理整段视频':'处理选中片段':dirty?'应用设置并重新处理':'开始处理';
  $('#download-label').textContent=output?.job_kind==='frame'?'下载此帧':output?.job_kind==='clip'?'下载此片段':dirty?'下载已有结果':'下载结果';
  const values=settings();
  const codec=$('#codec');codec.title=($('#codec-note').textContent||'')+' '+(values.codec==='prores'?'适合后期剪辑，文件较大。':values.codec==='ffv1'?'适合无损归档，文件很大。':values.codec==='av1_svt'?'适合较小文件，CPU 编码耗时较长。':'适合较小文件与日常播放。')+(!options.nvenc_available?' 当前显卡编码不可用，使用 CPU 编码；影像增强仍由 GPU 完成。':'');
}
function settingsChanged(){
  if(state.busy)return;
  paintBitrate();refreshProduct();
  if(resultDirty()&&!$('#notice').classList.contains('error'))$('#notice').hidden=true;
  saveSession();
  window.StudioEnhancements?.settingsChanged();
}
document.addEventListener('input',event=>{if(event.target.form===form)settingsChanged()});
document.addEventListener('change',event=>{if(event.target.form===form)settingsChanged()});
$$('[data-style]').forEach(button=>button.addEventListener('click',settingsChanged));
const presetsKey='dlss-user-presets-v1';
let userPresets=[];
try{const saved=JSON.parse(localStorage.getItem(presetsKey)||'[]');if(Array.isArray(saved))userPresets=saved.filter(p=>p&&typeof p.id==='string'&&typeof p.name==='string'&&p.settings).slice(0,50)}catch{}
function paintPresets(selected=''){
  populateSelect($('#user-presets'),[['选择设置',''],...userPresets.map(p=>[p.name,p.id])],selected);
  $('#apply-preset').disabled=!selected||state.busy;$('#delete-preset').disabled=!selected||state.busy;
}
function storePresets(next,selected){
  try{localStorage.setItem(presetsKey,JSON.stringify(next));userPresets=next;paintPresets(selected);return true}
  catch{$('#preset-status').textContent='设置未能保存，请检查本机存储空间。';return false}
}
$('#user-presets').onchange=()=>{const selected=userPresets.find(p=>p.id===$('#user-presets').value);$('#preset-name').value=selected?.name||'';paintPresets(selected?.id||'')};
$('#save-preset').onclick=()=>{
  if(!form.reportValidity()||!$('#intensity-number').reportValidity())return;
  const name=$('#preset-name').value.trim();
  if(!name){$('#preset-status').textContent='请先填写设置名称。';$('#preset-name').focus();return}
  const duplicate=userPresets.find(p=>p.name===name);
  if(duplicate){$('#preset-status').textContent='名称已存在，请换一个名称以保留原设置。';return}
  if(userPresets.length>=50){$('#preset-status').textContent='最多保存 50 组设置，请先删除不需要的设置。';return}
  const values=Object.fromEntries(Object.entries(settings()).filter(([key])=>!['asset_id','frames'].includes(key)));
  const preset={id:crypto.randomUUID(),name,settings:values};
  if(storePresets([...userPresets,preset],preset.id))$('#preset-status').textContent='设置已保存在此工作台。';
};
$('#apply-preset').onclick=()=>{const preset=userPresets.find(p=>p.id===$('#user-presets').value);if(preset){applySettings(preset.settings);settingsChanged();$('#preset-status').textContent='设置已应用，重新处理后生效。'}};
$('#delete-preset').onclick=()=>{const id=$('#user-presets').value;if(storePresets(userPresets.filter(p=>p.id!==id),'')){$('#preset-name').value='';$('#preset-status').textContent='已删除保存的设置，当前参数不变。'}};
paintPresets();
