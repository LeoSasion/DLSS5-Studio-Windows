/* One source clock drives both videos. Frame previews never replace the source media. */
window.StudioVideoPlayer = class StudioVideoPlayer {
  constructor({onChange,onFrameCleared,onError,onSelectionChange,onViewChange}) {
    this.source=document.querySelector('#video-preview');
    this.result=document.querySelector('#video-result');
    this.stage=document.querySelector('#video-stage');
    this.picture=document.querySelector('#video-picture');
    this.viewport=document.querySelector('#video-viewport');
    this.frameOriginal=document.querySelector('#video-frame-original');
    this.frameResult=document.querySelector('#video-frame-result');
    this.slider=document.querySelector('#video-compare-slider');
    this.line=document.querySelector('#video-compare-line');
    this.timeline=document.querySelector('#video-timeline');
    this.track=document.querySelector('.video-track');
    this.trimStart=document.querySelector('#trim-start');
    this.trimEnd=document.querySelector('#trim-end');
    this.selection={start:0,end:null};this.sourceLength=0;this.fps=30;
    this.playButton=document.querySelector('#video-play');
    this.muteButton=document.querySelector('#video-mute');
    this.loading=document.querySelector('#video-loading');
    this.timeLabel=document.querySelector('#video-time');
    this.controls=this.stage.querySelector('.video-transport');
    this.playIcon=this.playButton.querySelector('img');
    this.transportCache={};
    this.onChange=onChange;this.onFrameCleared=onFrameCleared;this.onError=onError;
    this.onSelectionChange=onSelectionChange;this.onViewChange=onViewChange;
    this.view='original';this.frame=null;this.locked=false;this.assetId=null;this.resultUrl=null;
    this.resultOffset=0;this.resultFailed=false;this.sourceFailed=false;this.sourcePreparing=false;this.displayUrl=null;this.pendingSourceTime=null;this.wantPlaying=false;this.buffering=false;this.raf=0;
    this.result.muted=true;
    this.playButton.addEventListener('click',()=>this.source.paused?this.play():this.pause());
    this.timeline.addEventListener('input',()=>this.seek(Number(this.timeline.value)));
    this.bindSelection(this.trimStart,'start');this.bindSelection(this.trimEnd,'end');
    this.bindTransportVisibility();
    this.muteButton.addEventListener('click',()=>{this.source.muted=!this.source.muted;this.paintTransport()});
    this.slider.addEventListener('input',()=>this.paintView());
    for(const event of ['loadedmetadata','loadeddata','canplay','durationchange']){
      this.source.addEventListener(event,()=>{if(event==='loadedmetadata'&&this.pendingSourceTime!==null){const target=this.pendingSourceTime;this.pendingSourceTime=null;this.source.currentTime=Math.max(0,Math.min(target,Math.max(0,this.source.duration-.0001)))}this.fit();this.paint();this.resumeBuffered();this.onChange()});
      this.result.addEventListener(event,()=>{if(this.usingResult()&&this.source.currentTime<this.startTime())this.seek(this.startTime());this.sync(this.source.paused);this.paint();this.resumeBuffered();this.onChange()});
    }
    this.source.addEventListener('play',()=>{
      if(this.locked){this.pause();return}
      this.clearFrame();this.wantPlaying=true;this.sync(true);this.tick();this.paintTransport();
    });
    this.source.addEventListener('playing',()=>{this.buffering=false;this.sync(true);this.paint()});
    this.source.addEventListener('pause',()=>{this.result.pause();cancelAnimationFrame(this.raf);this.raf=0;this.sync(true);this.paintTransport()});
    this.source.addEventListener('timeupdate',()=>{this.checkEnd();this.sync();this.paintTransport()});
    this.source.addEventListener('seeking',()=>{this.clearFrame();this.sync(true);this.paintTransport();this.onChange()});
    this.source.addEventListener('seeked',()=>{this.sync(true);this.paint();this.onChange()});
    this.result.addEventListener('seeked',()=>{this.sync(this.source.paused);this.paintView()});
    this.source.addEventListener('ratechange',()=>this.sync(true));
    this.source.addEventListener('volumechange',()=>this.paintTransport());
    this.source.addEventListener('ended',()=>this.pause());
    for(const video of [this.source,this.result])video.addEventListener('waiting',()=>{
      if(this.wantPlaying&&this.usingResult()){
        this.buffering=true;this.source.pause();this.result.pause();this.paint();
      }
    });
    this.source.addEventListener('error',()=>{
      if(!this.assetId)return;
      if(this.sourcePreparing)return;
      this.sourceFailed=true;this.pause();this.paint();this.onChange();
      this.onError(this.sourcePreviewFailed?'原片显示预览暂不可用，仍可处理并下载结果。':'这个视频暂时无法在浏览器中播放，请使用 H.264 MP4 或 WebM 视频。');
    });
    this.result.addEventListener('error',()=>{
      if(!this.resultUrl)return;
      this.resultFailed=true;this.pause();this.paint();this.onChange();
      this.onError('增强结果的预览加载失败，可以下载视频后查看。');
    });
    for(const image of [this.frameOriginal,this.frameResult]){
      image.addEventListener('load',()=>{this.paint();this.onChange()});
      image.addEventListener('error',()=>{if(this.frame){this.onError('单帧预览加载失败，请重试或下载此帧。');this.clearFrame()}});
    }
    new ResizeObserver(()=>this.fit()).observe(this.picture);
  }
  static formatTime(value){
    value=Number.isFinite(value)?Math.max(0,value):0;
    const ms=Math.floor(value*1000),seconds=Math.floor(ms/1000),minutes=Math.floor(seconds/60);
    return `${String(minutes).padStart(2,'0')}:${String(seconds%60).padStart(2,'0')}.${String(ms%1000).padStart(3,'0')}`;
  }
  static playbackTime(value){
    const total=Math.floor(Number.isFinite(value)?Math.max(0,value):0),hours=Math.floor(total/3600);
    return `${hours?String(hours).padStart(2,'0')+':':''}${String(Math.floor(total/60)%60).padStart(2,'0')}:${String(total%60).padStart(2,'0')}`;
  }
  paintControlsVisibility(){
    this.stage.classList.toggle('controls-visible',!!(this.controlsNearBottom||this.controlsDragging||this.controlsKeyboard||this.controlsTouch));
  }
  clearControlsVisibility(){
    clearTimeout(this.controlsTimer);
    this.controlsNearBottom=this.controlsDragging=this.controlsKeyboard=this.controlsTouch=false;
    this.paintControlsVisibility();
  }
  showTouchControls(){
    clearTimeout(this.controlsTimer);this.controlsTouch=true;this.paintControlsVisibility();
    this.controlsTimer=setTimeout(()=>{this.controlsTouch=false;this.paintControlsVisibility()},2800);
  }
  bindTransportVisibility(){
    let touchOrigin=null;
    this.stage.addEventListener('pointermove',event=>{
      if(event.pointerType==='touch')return;
      this.controlsKeyboard=false;
      const rect=this.stage.getBoundingClientRect();
      this.controlsNearBottom=event.clientY>=rect.bottom-72&&event.clientY<=rect.bottom&&event.clientX>=rect.left&&event.clientX<=rect.right;
      this.paintControlsVisibility();
    });
    this.stage.addEventListener('pointerleave',()=>{this.controlsNearBottom=false;this.paintControlsVisibility()});
    this.stage.addEventListener('pointerdown',event=>{
      this.controlsKeyboard=false;
      if(event.pointerType==='touch')touchOrigin={id:event.pointerId,x:event.clientX,y:event.clientY};
      if(this.controls.contains(event.target)){
        this.controlsDragging=!!event.target.closest('input[type=range],.trim-handle');
        if(event.pointerType==='touch')this.showTouchControls();
      }
      this.paintControlsVisibility();
    });
    this.stage.addEventListener('pointerup',event=>{
      if(event.pointerType==='touch'&&touchOrigin?.id===event.pointerId&&!this.controls.contains(event.target)&&Math.hypot(event.clientX-touchOrigin.x,event.clientY-touchOrigin.y)<8){
        if(this.controlsTouch){this.controlsTouch=false;clearTimeout(this.controlsTimer)}else this.showTouchControls();
        this.paintControlsVisibility();
      }
      touchOrigin=null;
    });
    const release=event=>{
      if(this.controlsDragging&&event.pointerType==='touch')this.showTouchControls();
      this.controlsDragging=false;this.paintControlsVisibility();
    };
    window.addEventListener('pointerup',release);window.addEventListener('pointercancel',release);
    this.controls.addEventListener('focusin',event=>{
      if(event.target.matches(':focus-visible')){this.controlsKeyboard=true;this.paintControlsVisibility()}
    });
    this.controls.addEventListener('focusout',()=>queueMicrotask(()=>{
      if(!this.controls.contains(document.activeElement)){this.controlsKeyboard=false;this.paintControlsVisibility()}
    }));
    this.controls.addEventListener('keydown',event=>{
      if(event.key==='Escape'){
        event.preventDefault();event.stopPropagation();document.activeElement.blur();this.clearControlsVisibility();
      }else{this.controlsKeyboard=true;this.paintControlsVisibility()}
    },true);
    window.addEventListener('blur',()=>this.clearControlsVisibility());
    this.paintControlsVisibility();
  }
  setSource(asset){
    const url=asset?.preview_url||(asset?.preview_status==='preparing'?null:asset?.url)||null;
    this.sourcePreparing=asset?.preview_status==='preparing';this.sourcePreviewFailed=asset?.preview_status==='failed';
    if(this.assetId===asset?.id){
      if(this.displayUrl===url)return;
      this.pendingSourceTime=this.pendingSourceTime??this.source.currentTime??0;
      this.pause();this.displayUrl=url;this.sourceFailed=false;
      if(url)this.source.src=url;else this.source.removeAttribute('src');
      this.source.load();this.paint();return;
    }
    this.clearControlsVisibility();
    this.pause();this.assetId=asset?.id||null;this.sourceFailed=false;
    this.sourceLength=Number(asset?.duration)||0;this.fps=Number(asset?.fps)>0?Number(asset.fps):30;
    this.displayUrl=url;this.pendingSourceTime=null;
    this.selection={start:0,end:null};
    this.clearFrame(false);this.setResult(null);
    if(url)this.source.src=url;else this.source.removeAttribute('src');
    this.source.load();this.paint();
  }
  setVisible(visible){this.visible=visible;this.stage.hidden=!visible;if(!visible){this.pause();this.loading.hidden=true;this.clearControlsVisibility()}else{this.fit();this.paintView()}}
  setResult(url,offset=0,isClip=false){
    this.resultClip=isClip;
    if(this.resultUrl===url&&this.resultOffset===offset)return;
    this.exactMapping=null;
    this.resultOffset=offset;
    this.result.pause();this.resultUrl=url||null;this.resultFailed=false;
    if(url)this.result.src=url;else this.result.removeAttribute('src');
    this.result.load();this.paint();
  }
  setFrame(frame){
    if(this.frame?.job.id===frame?.job.id)return;
    this.pause();this.frame=frame;
    if(frame){this.frameOriginal.src=frame.source;this.frameResult.src=frame.job.preview}
    else{this.frameOriginal.removeAttribute('src');this.frameResult.removeAttribute('src')}
    this.paint();
  }
  clearFrame(notify=true){
    if(!this.frame)return;
    this.frame=null;this.frameOriginal.removeAttribute('src');this.frameResult.removeAttribute('src');
    if(notify)this.onFrameCleared();
    this.paint();
  }
  canCapture(){return !!this.assetId&&!this.sourceFailed&&this.source.readyState>=2&&!this.source.seeking&&this.source.videoWidth>0}
  sourceDuration(){return this.assetId?(Number.isFinite(this.source.duration)?this.source.duration:this.sourceLength):0}
  getSelection(){
    const duration=this.sourceDuration(),gap=Math.min(1/this.fps,duration);
    const start=Math.max(0,Math.min(Number(this.selection.start)||0,Math.max(0,duration-gap)));
    const requestedEnd=this.selection.end===null?duration:Number(this.selection.end);
    const end=Math.max(start+gap,Math.min(Number.isFinite(requestedEnd)?requestedEnd:duration,duration));
    return {start,end,duration:Math.max(0,end-start),full:start===0&&Math.abs(end-duration)<.000001};
  }
  setSelection(selection){
    this.selection={start:Number(selection?.start)||0,end:selection?.end==null?null:Number(selection.end)};
    this.paintSelection();
  }
  editSelection(edge,value){
    if(this.selectionLocked||!this.sourceDuration())return;
    const range=this.getSelection(),duration=this.sourceDuration(),gap=Math.min(1/this.fps,duration);
    // The source frame grid determines both endpoints; the end is exclusive.
    const snapped=value>=duration?duration:Math.round(value*this.fps)/this.fps;
    if(edge==='start')range.start=Math.max(0,Math.min(snapped,range.end-gap));
    else range.end=Math.min(duration,Math.max(snapped,range.start+gap));
    this.setSelection({start:range.start,end:range.end>=duration?null:range.end});
    this.onViewChange?.('original');
    this.seek(edge==='start'?range.start:Math.max(range.start,range.end-gap));
    this.onSelectionChange?.({...this.selection});
  }
  bindSelection(handle,edge){
    let drag=null;
    handle.addEventListener('pointerdown',event=>{
      if(event.button!==0||handle.disabled)return;
      event.preventDefault();event.stopPropagation();handle.focus();
      drag={id:event.pointerId,x:event.clientX,value:this.getSelection()[edge],width:this.track.getBoundingClientRect().width};
      handle.setPointerCapture(event.pointerId);handle.classList.add('dragging');this.pause();
    });
    handle.addEventListener('pointermove',event=>{
      if(drag?.id===event.pointerId&&drag.width>0)this.editSelection(edge,drag.value+(event.clientX-drag.x)/drag.width*this.sourceDuration());
    });
    const release=event=>{
      if(drag?.id!==event.pointerId)return;
      drag=null;handle.classList.remove('dragging');
      if(handle.hasPointerCapture(event.pointerId))handle.releasePointerCapture(event.pointerId);
    };
    handle.addEventListener('pointerup',release);handle.addEventListener('pointercancel',release);handle.addEventListener('lostpointercapture',release);
    handle.addEventListener('keydown',event=>{
      if(handle.disabled)return;
      const range=this.getSelection(),step=event.shiftKey?1:1/this.fps;
      const values={ArrowLeft:range[edge]-step,ArrowDown:range[edge]-step,ArrowRight:range[edge]+step,ArrowUp:range[edge]+step,Home:0,End:this.sourceDuration()};
      if(!(event.key in values))return;
      event.preventDefault();event.stopPropagation();this.editSelection(edge,values[event.key]);
    });
  }
  paintSelection(){
    const range=this.getSelection(),duration=this.sourceDuration(),gap=Math.min(1/this.fps,duration),disabled=this.selectionLocked||!duration;
    const key=[range.start,range.end,duration,disabled].join(':');if(this.selectionPaintKey===key)return;this.selectionPaintKey=key;
    this.track.style.setProperty('--trim-start',`${duration?range.start/duration*100:0}%`);
    this.track.style.setProperty('--trim-end',`${duration?range.end/duration*100:100}%`);
    for(const [handle,edge,label,min,max] of [[this.trimStart,'start','片段起点',0,Math.max(0,range.end-gap)],[this.trimEnd,'end','片段终点',Math.min(duration,range.start+gap),duration]]){
      const stamp=StudioVideoPlayer.formatTime(range[edge]);
      handle.disabled=disabled;handle.setAttribute('aria-valuemin',String(min));handle.setAttribute('aria-valuemax',String(max));
      handle.setAttribute('aria-valuenow',String(range[edge]));handle.setAttribute('aria-valuetext',stamp);
      handle.title=`${label} ${stamp}；拖动调整，方向键逐帧微调，Shift + 方向键调整一秒`;
      handle.querySelector('.trim-time').textContent=stamp;
    }
  }
  canCompare(){return this.frame?this.frameOriginal.complete&&this.frameOriginal.naturalWidth>0&&this.frameResult.complete&&this.frameResult.naturalWidth>0:!!this.resultUrl&&!this.resultFailed&&this.result.readyState>=2&&this.source.readyState>=2}
  usingResult(){return !this.frame&&this.view!=='original'&&!!this.resultUrl&&!this.resultFailed}
  duration(){
    let duration=this.sourceDuration();
    if(this.usingResult()&&Number.isFinite(this.result.duration))duration=Math.min(duration,this.resultOffset+this.result.duration);
    return duration;
  }
  startTime(){return this.usingResult()?this.resultOffset:0}
  setLocked(locked,selectionLocked=locked){this.locked=locked;this.selectionLocked=selectionLocked;this.paintTransport()}
  setView(value){
    this.view=value;
    const end=this.duration();
    if(end>0&&(this.source.currentTime>end||this.source.currentTime<this.startTime()))this.seek(Math.max(this.startTime(),Math.min(this.source.currentTime,end-.001)));
    this.sync(true);this.paint();
  }
  async play(){
    if(this.locked||!this.canCapture())return;
    this.clearFrame();this.wantPlaying=true;this.buffering=false;
    if(this.source.ended||this.source.currentTime>=this.duration()-.01||this.source.currentTime<this.startTime())this.source.currentTime=this.startTime();
    try{await this.source.play();this.sync(true)}
    catch(error){this.wantPlaying=false;if(error.name!=='AbortError')this.onError('视频暂时无法播放，请稍后重试。')}
    this.paintTransport();
  }
  pause(){this.wantPlaying=false;this.buffering=false;this.source.pause();this.result.pause();this.paintTransport()}
  seek(time){
    if(this.locked||!Number.isFinite(time)||!this.assetId)return;
    this.pause();this.clearFrame();
    if(this.usingResult()&&(time<this.startTime()||time>this.duration()))this.onViewChange?.('original');
    const duration=this.duration();
    const target=Math.max(this.startTime(),Math.min(time,Math.max(this.startTime(),duration-.0001)));
    if(this.source.readyState<1)this.pendingSourceTime=target;else this.source.currentTime=target;
    this.sync(true);this.paintTransport();
  }
  sync(force=false){
    if(!this.usingResult()||this.result.readyState<1){this.result.pause();return}
    const exact=this.source.paused&&this.exactMapping?.assetId===this.assetId&&this.exactMapping.resultUrl===this.resultUrl&&Math.abs(this.exactMapping.sourceClock-this.source.currentTime)<.002?this.exactMapping.resultTime:null;
    const target=Math.max(0,Math.min(exact??this.source.currentTime-this.resultOffset,Math.max(0,this.result.duration-.0001)));
    const drift=target-this.result.currentTime;
    if(!this.result.seeking&&(Math.abs(drift)>(force ? .002 : .10)))this.result.currentTime=target;
    this.result.muted=true;
    this.result.playbackRate=this.source.playbackRate*(Math.abs(drift)>.025&&Math.abs(drift)<.10?(drift>0?1.03:.97):1);
    if(this.source.paused||this.buffering)this.result.pause();
    else if(this.result.paused)this.result.play().catch(error=>{if(error.name!=='AbortError'){this.pause();this.onError('视频对比播放暂时不可用，请暂停后拖动时间轴查看。')}});
  }
  resumeBuffered(){
    if(this.buffering&&this.wantPlaying&&this.source.readyState>=3&&this.result.readyState>=3){
      this.buffering=false;this.source.play().then(()=>this.sync(true)).catch(()=>this.pause());
    }
  }
  checkEnd(){if(this.usingResult()&&!this.source.paused&&this.source.currentTime>=this.duration()-.005)this.pause()}
  tick(){
    cancelAnimationFrame(this.raf);
    if(this.source.paused){this.raf=0;return}
    this.checkEnd();this.sync();this.paintTransport();
    this.raf=requestAnimationFrame(()=>this.tick());
  }
  fit(){
    const media=this.view==='result'?(this.frame?this.frameResult:this.result):this.source;
    const ratio=(media.videoWidth||media.naturalWidth)/(media.videoHeight||media.naturalHeight)||this.source.videoWidth/this.source.videoHeight;
    if(!Number.isFinite(ratio)||ratio<=0)return;
    const width=Math.min(this.picture.clientWidth,this.picture.clientHeight*ratio);
    this.viewport.style.width=`${width}px`;this.viewport.style.height=`${width/ratio}px`;
    this.onGeometry?.();
  }
  paintView(){
    const frame=!!this.frame,compare=this.view==='compare'&&this.canCompare(),enhanced=this.view!=='original';
    this.result.hidden=frame||!enhanced||!this.resultUrl||this.resultFailed;
    this.frameOriginal.hidden=!frame;
    this.frameResult.hidden=!frame||!enhanced;
    const clip=compare?`inset(0 0 0 ${this.slider.value}%)`:'none';
    this.result.style.clipPath=clip;this.frameResult.style.clipPath=clip;
    this.slider.hidden=!compare;this.line.hidden=!compare;this.line.style.left=`${this.slider.value}%`;
    const sourceLabel=document.querySelector('#video-source-label'),resultLabel=document.querySelector('#video-result-label');
    sourceLabel.hidden=enhanced&&!compare;sourceLabel.textContent=frame?`当前帧 · ${StudioVideoPlayer.formatTime(this.frame.time)}`:'原视频';
    resultLabel.hidden=!enhanced;resultLabel.textContent=frame?'单帧增强':this.resultClip?'片段增强':'增强后';
    let loading='';
    if(this.assetId&&!this.sourceFailed&&this.source.readyState<2&&(!enhanced||!this.resultUrl))loading=this.sourcePreparing?'正在准备 HDR 原片显示预览…':'正在加载视频…';
    else if(this.buffering)loading='正在同步视频…';
    else if(enhanced&&(frame||this.resultUrl)&&!this.canCompare()&&!this.resultFailed)loading='正在加载增强预览…';
    this.loading.textContent=loading;this.loading.hidden=!this.visible||!loading;
  }
  paintTransport(){
    const duration=this.sourceDuration(),time=Math.min(this.pendingSourceTime??this.source.currentTime??0,duration),paused=this.source.paused;
    const cache=this.transportCache;
    this.timeline.min='0';this.paintSelection();
    if(cache.duration!==duration){this.timeline.max=String(duration);cache.duration=duration}
    this.timeline.disabled=this.locked||!duration;this.playButton.disabled=this.locked||!this.canCapture();
    // Synchronization remains per frame; text and timeline paint at most 20 times/s.
    const bucket=Math.floor(time*20);
    if(paused||cache.bucket!==bucket){
      cache.bucket=bucket;this.timeline.value=String(time);
      const stamp=StudioVideoPlayer.formatTime(time),label=`${StudioVideoPlayer.playbackTime(time)} / ${StudioVideoPlayer.playbackTime(duration)}`;
      if(this.timeLabel.textContent!==label)this.timeLabel.textContent=label;
      if(cache.stamp!==stamp||cache.timeDuration!==duration){this.timeline.setAttribute('aria-valuetext',stamp);this.timeLabel.title=`${stamp} / ${StudioVideoPlayer.formatTime(duration)}`;cache.stamp=stamp;cache.timeDuration=duration}
    }
    if(cache.paused!==paused){
      cache.paused=paused;this.playButton.title=paused?'播放视频':'暂停视频';
      this.playButton.setAttribute('aria-label',this.playButton.title);this.playIcon.src=`/assets/icons/${paused?'play':'pause'}.svg`;
    }
    if(cache.muted!==this.source.muted){
      cache.muted=this.source.muted;this.muteButton.title=this.source.muted?'取消静音':'静音';this.muteButton.setAttribute('aria-label',this.muteButton.title);
      this.muteButton.setAttribute('aria-pressed',String(this.source.muted));
    }
  }
  paint(){this.paintView();this.paintTransport();this.fit()}
  async captureFrame(){
    this.pause();
    if(this.source.seeking)await new Promise((resolve,reject)=>{
      const timeout=setTimeout(()=>finish(new Error('跳转尚未完成，请稍后重试。')),10000);
      const done=()=>finish(),fail=()=>finish(new Error('无法读取当前视频帧。'));
      const finish=error=>{clearTimeout(timeout);this.source.removeEventListener('seeked',done);this.source.removeEventListener('error',fail);error?reject(error):resolve()};
      this.source.addEventListener('seeked',done,{once:true});this.source.addEventListener('error',fail,{once:true});
    });
    if(!this.canCapture())throw new Error('当前视频帧还未准备好，请稍后重试。');
    await new Promise(requestAnimationFrame);
    const canvas=document.createElement('canvas');canvas.width=this.source.videoWidth;canvas.height=this.source.videoHeight;
    canvas.getContext('2d').drawImage(this.source,0,0);
    const time=this.source.currentTime;
    const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/png'));
    if(!blob)throw new Error('无法读取当前视频帧，请换一个时间点重试。');
    return {blob,time,width:canvas.width,height:canvas.height};
  }
};
