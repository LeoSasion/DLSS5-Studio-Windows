// Included by nr.cpp: access to the same NGX shim, SR cache and bounded queues.
#include "nr_video_cache.h"
#include "nr_video_passthrough.h"
namespace {
int RunVideoQuality(const std::string& dllDir,int adapter,unsigned inW,unsigned inH,
                    const NrModelParams& model,float detail,float colour,bool hdr,float scale,
                    unsigned outWReq,unsigned outHReq,bool motionOn,bool motionVis,int motionEngine,
                    unsigned srPreset,bool verbose,const NrPostSettings& post) {
    using Clock=std::chrono::steady_clock;
    const auto start=Clock::now();
    auto elapsed=[](Clock::time_point a){return std::chrono::duration<double,std::milli>(Clock::now()-a).count();};
    SetLogToStderr(true);
    _setmode(_fileno(stdin),_O_BINARY);_setmode(_fileno(stdout),_O_BINARY);
    const bool signalHdr=post.transfer!="sdr";
    const bool wide=signalHdr&&post.primaries!="bt709";
    const bool p3=signalHdr&&post.primaries=="smpte432";
    unsigned tW=outWReq,tH=outHReq;
    if(tW&&!tH)tH=static_cast<unsigned>(std::llround(static_cast<double>(inH)*tW/inW));
    else if(tH&&!tW)tW=static_cast<unsigned>(std::llround(static_cast<double>(inW)*tH/inH));
    else if(!tW&&!tH){tW=static_cast<unsigned>(std::llround(inW*scale));tH=static_cast<unsigned>(std::llround(inH*scale));}
    tW=std::max(1u,tW);tH=std::max(1u,tH);
    if(tW>16384 || tH>16384 || static_cast<uint64_t>(tW)*tH>100000000)
        throw ToolError("video output exceeds the supported resource budget");
    const unsigned nW=std::max(1u,static_cast<unsigned>(std::lround(tW*post.renderScale)));
    const unsigned nH=std::max(1u,static_cast<unsigned>(std::lround(tH*post.renderScale)));
    GpuContext gpu;gpu.Initialize(false,adapter);
    NgxSession ngx;
    // Upscaling needs an NGX session even on a pure model-cache replay.
    if(tW!=inW||tH!=inH)ngx.Init(gpu.Device(),DefaultDllSearchPaths(dllDir),verbose);
    auto vsr=std::make_unique<VideoVsr>(gpu,ngx,inW,inH,tW,tH,
        post.pixelFormat=="rgba"&&!signalHdr&&!hdr);
    const bool flowEnabled=motionOn||motionVis||post.stability>0;
    OpticalFlow flow;NvofFlow nvof;bool nvofActive=false;
    if(flowEnabled){
        if(motionEngine!=2)nvofActive=nvof.Init(gpu,nW,nH,nW,nH);
        if(!nvofActive)flow.Init(gpu,nW,nH);
    }
    uint64_t signature=NrVideoCache::Hash(post.cacheKey.data(),post.cacheKey.size());
    const unsigned identity[]={inW,inH,tW,tH,nW,nH,model.preset,model.style,model.autoMask,
        model.uiCorrection,static_cast<unsigned>(motionOn),static_cast<unsigned>(motionEngine),srPreset,static_cast<unsigned>(hdr)};
    const float controls[]={model.intensity,model.localStructure,model.localTone,model.skinStructure,model.globalTone};
    signature=NrVideoCache::Hash(identity,sizeof(identity),signature);
    const uint32_t algorithm[]={7,static_cast<uint32_t>(vsr->Available()),motionOn?(nvofActive?1u:2u):0u};
    signature=NrVideoCache::Hash(algorithm,sizeof(algorithm),signature);
    signature=NrVideoCache::Hash(controls,sizeof(controls),signature);
    signature=NrVideoCache::Hash(&post.fps,sizeof(post.fps),signature);
    signature=NrVideoCache::Hash(post.transfer.data(),post.transfer.size(),signature);
    signature=NrVideoCache::Hash(post.primaries.data(),post.primaries.size(),signature);
    signature=NrVideoCache::Hash(post.pixelFormat.data(),post.pixelFormat.size(),signature);
    std::unique_ptr<NrVideoCache::Store> cache;
    if(!motionVis&&!post.cacheDir.empty()&&post.cacheFrames){
        try{cache=std::make_unique<NrVideoCache::Store>(Narrow2Widen(post.cacheDir),nW,nH,post.cacheFrames,signature);}
        catch(const std::exception& e){LogWarn("NR cache unavailable: %s",e.what());}
    }
    const bool replay=cache&&cache->Replay();
    if(replay)LogInfo("NR cache: complete model stage hit (no NR initialization/evaluation)");
    LogInfo("video quality: %ux%u -> %ux%u, NR %ux%u, raw %s, transfer %s",inW,inH,tW,tH,nW,nH,post.pixelFormat.c_str(),post.transfer.c_str());
    if(!replay&&!motionVis&&!ngx.Initialized())ngx.Init(gpu.Device(),DefaultDllSearchPaths(dllDir),verbose);
    auto* caps=ngx.CapabilityParams();
    ForwarderApi fwd;
    auto releaseFeature=[&](void* p){if(p)fwd.release(p);};
    std::unique_ptr<void,decltype(releaseFeature)> featureOwner(nullptr,releaseFeature);
    const auto snippet=std::filesystem::absolute(Narrow2Widen(dllDir+"/nvngx_dlssnr.dll")).wstring();
    wchar_t appData[MAX_PATH]{};
    const auto appLen=GetEnvironmentVariableW(L"LOCALAPPDATA",appData,MAX_PATH);
    const std::wstring dataPath=(appLen>0&&appLen<MAX_PATH)?std::wstring(appData)+L"\\video2dlssnr":L".";
    GpuTexture color,depth,motion,zeroMotion,output;
    const bool needsModel=!replay&&!motionVis;
    if(!motionVis)output=gpu.CreateTexture(nW,nH,DXGI_FORMAT_R16G16B16A16_FLOAT,true,L"nrOutput");
    if(needsModel) {
        color=gpu.CreateTexture(nW,nH,DXGI_FORMAT_R16G16B16A16_FLOAT,true,L"nrProxy");
        depth=gpu.CreateTexture(nW,nH,DXGI_FORMAT_R32_FLOAT,true,L"nrDepth");
        gpu.UploadR32Float(depth,std::vector<float>(static_cast<size_t>(nW)*nH,0));
    }
    if(flowEnabled)motion=gpu.CreateTexture(nW,nH,DXGI_FORMAT_R16G16_FLOAT,true,L"nrMotion");
    if(needsModel&&!motionOn)zeroMotion=gpu.CreateTexture(nW,nH,DXGI_FORMAT_R16G16_FLOAT,true,L"nrZeroMotion");
    gpu.Begin();
    if(needsModel)gpu.Transition(depth,D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
    if(motion.res)gpu.RecordClearUav(motion);
    if(zeroMotion.res)gpu.RecordClearUav(zeroMotion);
    gpu.EndAndWait();
    void* feature=nullptr;
    if(needsModel){
        if(!caps)throw ToolError("no capability parameters from NGX");
        ValidateCapabilityParams(caps);
        if(!fwd.Load())throw ToolError("could not load forwarder nvngx.dll_dlssnr.dll");
        NrFeatureDesc d;d.inputW=nW;d.inputH=nH;d.outputW=nW;d.outputH=nH;d.preset=model.preset;
        NrFeature prime;
        auto* list=gpu.Begin();prime.TryCreate(ngx,list,d);gpu.EndAndWait();prime.Release();
        list=gpu.Begin();
        feature=fwd.create(snippet.c_str(),dataPath.c_str(),gpu.Device(),list,caps,nW,nH,nW,nH,&model);
        gpu.EndAndWait();
        if(!feature)throw ToolError("CreateFeature 18 failed for high-precision video");
        featureOwner.reset(feature);
    }
    std::vector<std::unique_ptr<SrStage>> srCache;
    NrGpuPost gpuPost(gpu,tW,tH,nW,nH,post);
    LogInfo("post backend: GPU FP32 residual, cubic history, gain-compensated trust");
    const bool sixteen=post.pixelFormat=="rgba64le";
    const size_t inPixels=static_cast<size_t>(inW)*inH;
    const size_t inputBytes=inPixels*4*(sixteen?2:1);
    const size_t maxQueued=std::clamp<size_t>((128ull*1024*1024)/std::max<size_t>(1,inPixels*16),1,4);
    struct Frame {ImageF image;bool reset;};
    FrameQueue<Frame> inputQueue(maxQueued);
    FrameQueue<std::vector<uint8_t>> outputQueue(std::clamp<size_t>((64ull*1024*1024)/std::max<size_t>(1,static_cast<size_t>(tW)*tH*4*(sixteen?2:1)),1,4));
    std::atomic<bool> ioFailed{false};
    std::atomic<uint64_t> unpackNs{0},writeNs{0};
    const double initialization=elapsed(start);
    std::thread reader([&]{
        try {
            const int levels=sixteen?65536:256;
            std::vector<float> decode(levels);
            for(int i=0;i<levels;++i)decode[i]=NrPost::Decode(static_cast<float>(i)/(levels-1),post.transfer);
            std::vector<uint8_t> bytes(inputBytes);
            SceneHistory scenes;
            const int gw=std::max(1,static_cast<int>(64ull*inW/std::max(inW,inH))),gh=std::max(1,static_cast<int>(64ull*inH/std::max(inW,inH)));
            while(true){
                size_t got=0;
                while(got<inputBytes){const size_t n=std::fread(bytes.data()+got,1,inputBytes-got,stdin);if(!n)break;got+=n;}
                if(got==0)break;
                if(got!=inputBytes){ioFailed=true;LogErr("truncated raw video frame (%zu/%zu bytes)",got,inputBytes);break;}
                const auto t=Clock::now();
                Frame frame;frame.image.w=inW;frame.image.h=inH;frame.image.px.resize(inPixels*4);
                for(size_t i=0;i<inPixels*4;++i){
                    const unsigned v=sixteen?(static_cast<unsigned>(bytes[2*i])|(static_cast<unsigned>(bytes[2*i+1])<<8)):bytes[i];
                    frame.image.px[i]=(i%4==3)?static_cast<float>(v)/(levels-1):decode[v];
                }
                std::vector<float> gray(static_cast<size_t>(gw)*gh);
                for(int y=0;y<gh;++y)for(int x=0;x<gw;++x){
                    const size_t p=(static_cast<size_t>(y*inH/gh)*inW+x*inW/gw)*4;
                    const auto rgb=NrPost::Proxy({{frame.image.px[p],frame.image.px[p+1],frame.image.px[p+2]}},signalHdr,wide,p3);
                    gray[static_cast<size_t>(y)*gw+x]=NrPost::SrgbEncode(std::max(0.0f,NrPost::Luma(rgb)));
                }
                frame.reset=scenes.Update(gray,gw,gh,post.fps);
                unpackNs.fetch_add(static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now()-t).count()));
                if(!inputQueue.push(std::move(frame)))break;
            }
        }catch(const std::exception& e){ioFailed=true;LogErr("video reader: %s",e.what());}
        inputQueue.close();
    });
    std::thread writer([&]{
        std::vector<uint8_t> bytes;
        while(outputQueue.pop(bytes)){
            const auto t=Clock::now();
            if(std::fwrite(bytes.data(),1,bytes.size(),stdout)!=bytes.size()){ioFailed=true;inputQueue.close();outputQueue.close();break;}
            writeNs.fetch_add(static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now()-t).count()));
        }
        if(std::fflush(stdout)!=0)ioFailed=true;
    });
    double uploadMs=0,flowMs=0,nrMs=0,composeMs=0,readbackMs=0,gpuNrMs=0,gpuPostMs=0;
    uint64_t frames=0,resets=0,cachedFrames=0,evaluatedFrames=0;

    bool failed=false;
    Frame frame;
    try {
        while(inputQueue.pop(frame)){
            if(ioFailed.load()){failed=true;break;}
            auto t=Clock::now();
            ImageF anchor=vsr->Available()?vsr->Upscale(frame.image):((tW!=inW||tH!=inH)?UpscaleTo(gpu,ngx,srCache,frame.image,tW,tH,signalHdr||hdr,srPreset):std::move(frame.image));
            const uint64_t sourceHash=(cache&&cache->Enabled())?NrVideoCache::Hash(anchor.px.data(),anchor.px.size()*sizeof(float)):0;
            gpuPost.Prepare(anchor.px,(!replay&&!motionVis)?&color:nullptr);
            uploadMs+=elapsed(t);
            t=Clock::now();
            if(flowEnabled){
                if(nvofActive)nvof.Compute(gpu,gpuPost.Proxy(),motion,frame.reset);
                else {auto* cmd=gpu.Begin();flow.Record(cmd,gpuPost.Proxy(),motion,frame.reset);gpu.EndAndWait();}
            }
            flowMs+=elapsed(t);
            std::vector<uint16_t> nrPixels;
            if(!motionVis&&replay){
                t=Clock::now();nrPixels=cache->ReadHalf(sourceHash);readbackMs+=elapsed(t);++cachedFrames;
            }else if(!motionVis){
                t=Clock::now();
                auto* cmd=gpu.Begin();
                gpu.Transition(color,D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
                gpu.Transition(output,D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
                gpu.RecordTimestampBegin();
                const int result=fwd.evaluate(cmd,feature,caps,color.res.Get(),depth.res.Get(),
                    (motionOn?motion:zeroMotion).res.Get(),output.res.Get(),nW,nH,nW,nH,frame.reset?1:0);
                gpu.RecordTimestampEnd();gpu.EndAndWait();
                nrMs+=elapsed(t);gpuNrMs+=gpu.LastGpuMs();
                if(NVSDK_NGX_FAILED(static_cast<NVSDK_NGX_Result>(result))){failed=true;LogErr("evaluate failed on frame %llu: 0x%08X",frames,static_cast<unsigned>(result));break;}
                ++evaluatedFrames;
                if(cache&&cache->Enabled()){t=Clock::now();nrPixels=gpu.ReadbackRgbaHalf(output);readbackMs+=elapsed(t);cache->WriteHalf(sourceHash,nrPixels);}
            }
            t=Clock::now();
            std::vector<uint8_t> bytes;
            if(motionVis) {
                const auto vectors=gpu.ReadbackRg16Float(motion);
                auto result=anchor.px;
                const float maxMag=std::max(8.0f,nW/120.0f);
                for(unsigned y=0;y<tH;++y)for(unsigned x=0;x<tW;++x) {
                    const size_t i=(static_cast<size_t>(y)*tW+x)*4;
                    const float mx=NrPost::Sample(vectors,nW,nH,2,(x+.5f)*nW/tW-.5f,(y+.5f)*nH/tH-.5f,0);
                    const float my=NrPost::Sample(vectors,nW,nH,2,(x+.5f)*nW/tW-.5f,(y+.5f)*nH/tH-.5f,1);
                    result[i]=NrPost::Clamp01(.5f+mx/maxMag);result[i+1]=NrPost::Clamp01(.5f+my/maxMag);result[i+2]=NrPost::Clamp01(std::sqrt(mx*mx+my*my)/maxMag);
                }
                bytes=NrPost::Pack(result,post);
            } else {
                if(replay)gpu.UploadRgbaHalf(output,nrPixels);
                bytes=gpuPost.Process(anchor.px,output,motion,frame.reset,detail,colour);
                gpuPostMs+=gpuPost.LastGpuMs();
            }
            composeMs+=elapsed(t);
            if(!outputQueue.push(std::move(bytes))){failed=true;break;}
            ++frames;if(frame.reset)++resets;
            if(frames%5==0){std::fprintf(stderr,"NRPROG %llu %.3f\n",frames,frames/std::max(.001,(elapsed(start)-initialization)/1000));std::fflush(stderr);}
        }
    }catch(const std::exception& e){failed=true;LogErr("video pipeline: %s",e.what());}
    inputQueue.close();outputQueue.close();
    if(failed||ioFailed.load()){
        CancelSynchronousIo(reader.native_handle());CancelSynchronousIo(writer.native_handle());
    }
    reader.join();writer.join();
    failed=failed||ioFailed.load();
    if(cache){try{cache->Finish(!failed);}catch(const std::exception& e){LogWarn("NR cache commit: %s",e.what());}}
    const double wall=elapsed(start);
    std::fprintf(stderr,"NRTRANSFER {\"upload_allocations\":%llu,\"readback_allocations\":%llu,\"fence_waits\":%llu}\n",
        gpu.UploadAllocationCount(),gpu.ReadbackAllocationCount(),gpu.FenceWaitCount());
    std::fprintf(stderr,"NRMETRIC {\"version\":1,\"timing_kind\":\"synchronized_wall\",\"frames\":%llu,\"cached_nr_frames\":%llu,\"nr_evaluated_frames\":%llu,\"reset_count\":%llu,\"initialization_ms\":%.3f,\"wall_ms\":%.3f,\"nr_width\":%u,\"nr_height\":%u,\"stage_ms\":{\"upload_upscale\":%.3f,\"flow\":%.3f,\"nr\":%.3f,\"composite\":%.3f,\"readback\":%.3f,\"unpack\":%.3f,\"write\":%.3f},\"gpu_stage_ms\":{\"nr\":%.3f,\"composite\":%.3f}}\n",
        frames,cachedFrames,evaluatedFrames,resets,initialization,wall,nW,nH,uploadMs,flowMs,nrMs,composeMs,readbackMs,unpackNs.load()/1e6,writeNs.load()/1e6,gpuNrMs,gpuPostMs);
    featureOwner.reset();
    if(nvofActive)nvof.Shutdown();else if(flowEnabled)flow.Shutdown();
    vsr.reset();srCache.clear();ngx.Shutdown();
    LogInfo("done: %llu frames in %.1f s (%.1f fps)",frames,(wall-initialization)/1000,frames/std::max(.001,(wall-initialization)/1000));
    return frames>0&&!failed?0:1;
}
}

int RunNeuralRenderingVideo(const std::string& dllDir,int adapter,unsigned inW,unsigned inH,
                            const NrModelParams& model,float detail,float colour,bool hdr,float scale,
                            unsigned outWReq,unsigned outHReq,bool motionOn,bool motionVis,int motionEngine,
                            unsigned srPreset,bool verbose,const NrPostSettings& post) {
    SetLogToStderr(true);
    // Resolve before allocating GPU/model resources. Zero-sized, overflowing or
    // excessive geometry must never enter the stream-copy fast path.
    if(!inW||!inH||inW>16384||inH>16384||static_cast<uint64_t>(inW)*inH>100000000)
        throw ToolError("invalid video source dimensions");
    unsigned targetW=outWReq,targetH=outHReq;
    if(targetW&&!targetH)targetH=static_cast<unsigned>(std::llround(static_cast<double>(inH)*targetW/inW));
    else if(targetH&&!targetW)targetW=static_cast<unsigned>(std::llround(static_cast<double>(inW)*targetH/inH));
    else if(!targetW&&!targetH){targetW=static_cast<unsigned>(std::llround(inW*scale));targetH=static_cast<unsigned>(std::llround(inH*scale));}
    targetW=std::max(1u,targetW);targetH=std::max(1u,targetH);
    if(detail==0&&!motionVis&&targetW==inW&&targetH==inH)
        return CopyRawVideo(inW,inH,post.pixelFormat=="rgba64le");
    NrPostSettings effective=post;
    if(!post.cacheDir.empty()){
        unsigned w=outWReq,h=outHReq;
        if(w&&!h)h=static_cast<unsigned>(std::llround(static_cast<double>(inH)*w/inW));
        else if(h&&!w)w=static_cast<unsigned>(std::llround(static_cast<double>(inW)*h/inH));
        else if(!w&&!h){w=static_cast<unsigned>(std::llround(inW*scale));h=static_cast<unsigned>(std::llround(inH*scale));}
        w=std::max(1u,static_cast<unsigned>(std::lround(w*post.renderScale)));
        h=std::max(1u,static_cast<unsigned>(std::lround(h*post.renderScale)));
        if(!NrVideoCache::Eligible(w,h,post.cacheFrames)||motionVis){
            effective.cacheDir.clear();effective.cacheFrames=0;
            LogInfo("NR cache: not eligible (complete stage exceeds 1 GiB or diagnostic mode)");
        }
    }
    const bool tryVsr=VideoVsr::Built() && !hdr && post.transfer=="sdr" && post.pixelFormat=="rgba" &&
        ((outWReq>inW)||(outHReq>inH)||(!outWReq&&!outHReq&&scale>1));
    if(!effective.Enabled()&&!tryVsr)return RunNeuralRenderingVideoLegacy(dllDir,adapter,inW,inH,model,detail,colour,hdr,
        scale,outWReq,outHReq,motionOn,motionVis,motionEngine,srPreset,verbose,post.fps);
    return RunVideoQuality(dllDir,adapter,inW,inH,model,detail,colour,hdr,scale,outWReq,outHReq,
        motionOn,motionVis,motionEngine,srPreset,verbose,effective);
}
