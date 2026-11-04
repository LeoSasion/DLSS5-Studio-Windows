#include "vsr.h"
#include "nr_post.h"
#ifdef STUDIO_RTX_VIDEO_SDK
#include "studio_vsr_sdk.h" // generated outside the tracked source tree
#endif

bool VideoVsr::Built() {
#ifdef STUDIO_RTX_VIDEO_SDK
    return true;
#else
    return false;
#endif
}
VideoVsr::VideoVsr(GpuContext& gpu,NgxSession& session,unsigned iw,unsigned ih,unsigned ow,unsigned oh,bool eligible):gpu_(gpu) {
#ifdef STUDIO_RTX_VIDEO_SDK
    if(!eligible||!session.Initialized()||ow<=iw||oh<=ih)return;
    // No guessed feature IDs or parameter offsets: use the supplied SDK headers.
    auto status=NVSDK_NGX_D3D12_GetCapabilityParameters(&params_);
    int available=0,needsDriver=0;
    if(NVSDK_NGX_FAILED(status)||!params_ ||
       NVSDK_NGX_FAILED(NVSDK_NGX_Parameter_GetI(params_,NVSDK_NGX_Parameter_VSR_Available,&available))||!available) {
        LogInfo("RTX VSR unavailable; video upscale uses DLSS SR");return;
    }
    NVSDK_NGX_Parameter_GetI(params_,NVSDK_NGX_Parameter_VSR_NeedsUpdatedDriver,&needsDriver);
    if(needsDriver){LogWarn("RTX VSR requires a newer driver; using DLSS SR");return;}
    input_=gpu.CreateTexture(iw,ih,DXGI_FORMAT_R8G8B8A8_UNORM,true,L"vsrInput");
    output_=gpu.CreateTexture(ow,oh,DXGI_FORMAT_R8G8B8A8_UNORM,true,L"vsrOutput");
    NVSDK_NGX_Parameter_SetUI(params_,NVSDK_NGX_Parameter_CreationNodeMask,1);
    NVSDK_NGX_Parameter_SetUI(params_,NVSDK_NGX_Parameter_VisibilityNodeMask,1);
    auto* cmd=gpu.Begin();
    status=NVSDK_NGX_D3D12_CreateFeature(cmd,NVSDK_NGX_Feature_VSR,params_,&feature_);
    gpu.EndAndWait();
    if(NVSDK_NGX_FAILED(status)||!feature_) {
        if(feature_)NVSDK_NGX_D3D12_ReleaseFeature(feature_);
        if(params_)NVSDK_NGX_D3D12_DestroyParameters(params_);
        feature_=nullptr;params_=nullptr;
        throw ToolError("RTX VSR was available but feature creation failed");
    }
    ready_=true;LogInfo("video upscaler: RTX VSR High (quality 3), SDR8 only");
#else
    (void)session;(void)iw;(void)ih;(void)ow;(void)oh;(void)eligible;
#endif
}
VideoVsr::~VideoVsr() {
    if(feature_)NVSDK_NGX_D3D12_ReleaseFeature(feature_);
    if(params_)NVSDK_NGX_D3D12_DestroyParameters(params_);
}
ImageF VideoVsr::Upscale(const ImageF& image) {
#ifdef STUDIO_RTX_VIDEO_SDK
    if(!ready_||image.w!=input_.w||image.h!=input_.h)throw ToolError("RTX VSR source size changed");
    auto encoded=image.px;
    for(size_t i=0;i<encoded.size();i+=4)for(int c=0;c<3;++c)encoded[i+c]=NrPost::Encode(encoded[i+c],"sdr");
    gpu_.UploadRgbaFloat(input_,encoded);
    auto* cmd=gpu_.Begin();gpu_.Transition(input_,D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);gpu_.Transition(output_,D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
    NVSDK_NGX_D3D12_VSR_Eval_Params args{};
    args.pInput=input_.res.Get();args.pOutput=output_.res.Get();
    args.InputSubrectBase={0,0};args.InputSubrectSize={static_cast<unsigned>(input_.w),static_cast<unsigned>(input_.h)};
    args.OutputSubrectBase={0,0};args.OutputSubrectSize={static_cast<unsigned>(output_.w),static_cast<unsigned>(output_.h)};
    args.QualityLevel=static_cast<NVSDK_NGX_VSR_QualityLevel>(3);
    const auto status=NGX_D3D12_EVALUATE_VSR_EXT(cmd,feature_,params_,&args);gpu_.EndAndWait();
    if(NVSDK_NGX_FAILED(status))throw ToolError("RTX VSR evaluation failed; output was not committed");
    const auto bytes=gpu_.ReadbackRgba8(output_);
    ImageF result;result.w=output_.w;result.h=output_.h;result.px.resize(bytes.size());
    for(size_t i=0;i<bytes.size();++i)result.px[i]=(i%4==3)?bytes[i]/255.0f:NrPost::SrgbDecode(bytes[i]/255.0f);
    return result;
#else
    (void)image;throw ToolError("RTX VSR is not built; supply the NVIDIA RTX Video SDK");
#endif
}
