#include "nr_gpu_post.h"
#include "nr_gpu_post_shader.h"
#include <cstring>
#ifdef STUDIO_PRECOMPILED_SHADERS
#include "studio_shader_bytecode.h"
#endif

namespace {
struct Constants {
    uint32_t size[2],model[2];float detail,colour,stability,weight;
    uint32_t reset,transfer,gamut,sixteen;float protect[4],skin,highlights,padding[2];
};
static_assert(sizeof(Constants)==80,"HLSL settings layout");
}
NrGpuPost::NrGpuPost(GpuContext& gpu,int w,int h,int nw,int nh,const NrPostSettings& settings)
    :gpu_(gpu),settings_(settings) {
    const auto fp=DXGI_FORMAT_R32G32B32A32_FLOAT;
    anchor_=gpu.CreateTexture(w,h,fp,false,L"postAnchor");
    aliasProxy_=w==nw&&h==nh&&settings.transfer=="sdr";
    if(!aliasProxy_)proxy_=gpu.CreateTexture(nw,nh,fp,true,L"postSource");
    if(settings.stability<=0)delta_=gpu.CreateTexture(nw,nh,fp,true,L"postDelta");
    if(settings.stability>0) {
        previousProxy_=gpu.CreateTexture(nw,nh,fp,!aliasProxy_,L"postPreviousSource");
        for(auto& t:history_)t=gpu.CreateTexture(nw,nh,fp,true,L"postHistory");
    }
    packed_=gpu.CreateTexture(w,h,settings.pixelFormat=="rgba64le"?DXGI_FORMAT_R16G16B16A16_UINT:DXGI_FORMAT_R8G8B8A8_UINT,true,L"postPacked");
    D3D12_DESCRIPTOR_RANGE ranges[2]{};
    ranges[0]={D3D12_DESCRIPTOR_RANGE_TYPE_SRV,5,0,0,0};
    ranges[1]={D3D12_DESCRIPTOR_RANGE_TYPE_UAV,2,0,0,5};
    D3D12_ROOT_PARAMETER params[2]{};
    params[0].ParameterType=D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
    params[0].Constants={0,0,sizeof(Constants)/4};
    params[1].ParameterType=D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
    params[1].DescriptorTable={2,ranges};
    D3D12_ROOT_SIGNATURE_DESC rs{};rs.NumParameters=2;rs.pParameters=params;
    ComPtr<ID3DBlob> blob,error;
    CHECK_HR(D3D12SerializeRootSignature(&rs,D3D_ROOT_SIGNATURE_VERSION_1,&blob,&error));
    CHECK_HR(gpu.Device()->CreateRootSignature(0,blob->GetBufferPointer(),blob->GetBufferSize(),IID_PPV_ARGS(&signature_)));
    auto compile=[&](const char* entry,ComPtr<ID3D12PipelineState>& pso) {
        D3D12_SHADER_BYTECODE bytecode{};
#ifdef STUDIO_PRECOMPILED_SHADERS
        if(!strcmp(entry,"Prepare"))bytecode={StudioShader::prepare,sizeof(StudioShader::prepare)};
        else if(!strcmp(entry,"Correction"))bytecode={StudioShader::correction,sizeof(StudioShader::correction)};
        else if(!strcmp(entry,"Stabilize"))bytecode={StudioShader::stabilize,sizeof(StudioShader::stabilize)};
        else if(!strcmp(entry,"Compose"))bytecode={StudioShader::compose,sizeof(StudioShader::compose)};
        else throw ToolError("unknown embedded post shader");
#else
        ComPtr<ID3DBlob> shader,errors;
        const auto hr=D3DCompile(kNrGpuPostShader,strlen(kNrGpuPostShader),"nr_gpu_post",nullptr,nullptr,entry,"cs_5_0",D3DCOMPILE_OPTIMIZATION_LEVEL3,0,&shader,&errors);
        if(FAILED(hr))throw ToolError(errors?static_cast<const char*>(errors->GetBufferPointer()):"post shader compilation failed");
        bytecode={shader->GetBufferPointer(),shader->GetBufferSize()};
#endif
        D3D12_COMPUTE_PIPELINE_STATE_DESC desc{};desc.pRootSignature=signature_.Get();desc.CS=bytecode;
        CHECK_HR(gpu.Device()->CreateComputePipelineState(&desc,IID_PPV_ARGS(&pso)));
    };
    compile("Prepare",prepare_);compile("Correction",correction_);compile("Stabilize",stabilize_);compile("Compose",compose_);
    D3D12_DESCRIPTOR_HEAP_DESC hd{};hd.Type=D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV;hd.NumDescriptors=28;hd.Flags=D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE;
    CHECK_HR(gpu.Device()->CreateDescriptorHeap(&hd,IID_PPV_ARGS(&heap_)));
    stride_=gpu.Device()->GetDescriptorHandleIncrementSize(hd.Type);
}
void NrGpuPost::Dispatch(ID3D12PipelineState* pso,const std::vector<GpuTexture*>& inputs,GpuTexture& output,const std::vector<uint32_t>& constants) {
    CHECK(!inputs.empty()&&inputs.size()<=5&&constants.size()==sizeof(Constants)/4);
    CHECK(descriptorNext_+7<=28);
    const int base=descriptorNext_;descriptorNext_+=7;
    // Each pass keeps its own descriptors until the one final fence completes.
    auto cpu=[&](int i){auto h=heap_->GetCPUDescriptorHandleForHeapStart();h.ptr+=static_cast<SIZE_T>(base+i)*stride_;return h;};
    for(int i=0;i<5;++i) {
        GpuTexture* t=i<static_cast<int>(inputs.size())?inputs[i]:nullptr;
        if(t)gpu_.Transition(*t,D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
        D3D12_SHADER_RESOURCE_VIEW_DESC d{};d.Format=t?t->fmt:(i==4?DXGI_FORMAT_R16G16_FLOAT:DXGI_FORMAT_R32G32B32A32_FLOAT);d.ViewDimension=D3D12_SRV_DIMENSION_TEXTURE2D;d.Shader4ComponentMapping=D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING;d.Texture2D.MipLevels=1;
        gpu_.Device()->CreateShaderResourceView(t?t->res.Get():nullptr,&d,cpu(i));
    }
    gpu_.Transition(output,D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
    // Unused UAVs are explicitly null. Never alias one texture as both SRV/UAV.
    for(int i=0;i<2;++i) {
        const bool used=(i==1)==(output.fmt!=DXGI_FORMAT_R32G32B32A32_FLOAT);
        D3D12_UNORDERED_ACCESS_VIEW_DESC d{};d.Format=used?output.fmt:(i?DXGI_FORMAT_R8G8B8A8_UINT:DXGI_FORMAT_R32G32B32A32_FLOAT);d.ViewDimension=D3D12_UAV_DIMENSION_TEXTURE2D;
        gpu_.Device()->CreateUnorderedAccessView(used?output.res.Get():nullptr,nullptr,&d,cpu(5+i));
    }
    ID3D12DescriptorHeap* heaps[]={heap_.Get()};
    // Begin/End belong to the caller; this pass only records commands.
    auto* cmd=gpu_.CommandList();cmd->SetDescriptorHeaps(1,heaps);
    cmd->SetComputeRootSignature(signature_.Get());cmd->SetPipelineState(pso);
    cmd->SetComputeRoot32BitConstants(0,static_cast<UINT>(constants.size()),constants.data(),0);
    auto table=heap_->GetGPUDescriptorHandleForHeapStart();table.ptr+=static_cast<UINT64>(base)*stride_;
    cmd->SetComputeRootDescriptorTable(1,table);
    cmd->Dispatch((output.w+7)/8,(output.h+7)/8,1);
    gpu_.UavBarrier(output);
}
void NrGpuPost::Prepare(const std::vector<float>& anchor,GpuTexture* modelInput) {
    gpu_.UploadRgbaFloat(anchor_,anchor);
    if(aliasProxy_&&!modelInput)return;
    Constants c{};c.size[0]=anchor_.w;c.size[1]=anchor_.h;c.model[0]=Proxy().w;c.model[1]=Proxy().h;
    c.transfer=settings_.transfer=="pq"?1:settings_.transfer=="hlg"?2:0;
    c.gamut=c.transfer?(settings_.primaries=="smpte432"?2:settings_.primaries=="bt709"?0:1):0;
    std::vector<uint32_t> words(sizeof(c)/4);memcpy(words.data(),&c,sizeof(c));
    descriptorNext_=0;gpu_.Begin();
    if(!aliasProxy_)Dispatch(prepare_.Get(),{&anchor_},proxy_,words);
    if(modelInput) {
        gpu_.Transition(Proxy(),D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
        gpu_.Transition(*modelInput,D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
        gpu_.RecordEncodeSrgb(Proxy(),*modelInput);gpu_.UavBarrier(*modelInput);
    }
    gpu_.EndAndWait();
}
std::vector<uint8_t> NrGpuPost::Process(const std::vector<float>& anchor,
    GpuTexture& neural,GpuTexture& motion,bool reset,float detail,float colour) {
    lastGpuMs_=0;
    if(detail==0)return NrPost::Pack(anchor,settings_); // retain exact host round trip
    Constants c{};c.size[0]=anchor_.w;c.size[1]=anchor_.h;c.model[0]=Proxy().w;c.model[1]=Proxy().h;
    c.detail=detail;c.colour=colour;c.stability=settings_.stability;
    c.weight=std::min(.90f,std::exp(-1.0f/(std::max(1.0f,settings_.fps)*.08f))*settings_.stability);
    c.reset=reset||!hasHistory_;c.transfer=settings_.transfer=="pq"?1:settings_.transfer=="hlg"?2:0;
    c.gamut=c.transfer?(settings_.primaries=="smpte432"?2:settings_.primaries=="bt709"?0:1):0;
    c.sixteen=settings_.pixelFormat=="rgba64le";
    std::copy(settings_.protect.begin(),settings_.protect.end(),c.protect);c.skin=settings_.protectSkin;c.highlights=settings_.protectHighlights;
    std::vector<uint32_t> words(sizeof(c)/4);memcpy(words.data(),&c,sizeof(c));
    descriptorNext_=0;gpu_.Begin();gpu_.RecordTimestampBegin();
    GpuTexture* correction=&delta_;
    if(settings_.stability>0) {
        Dispatch(stabilize_.Get(),{&neural,&Proxy(),&previousProxy_,&history_[current_^1],&motion},history_[current_],words);
        correction=&history_[current_];
    } else Dispatch(correction_.Get(),{&neural,&Proxy()},delta_,words);
    Dispatch(compose_.Get(),{&anchor_,correction},packed_,words);
    gpu_.RecordTimestampEnd();
    // Readback appends its copy to this same command list, then submits/waits.
    auto result=gpu_.ReadbackPackedRgba(packed_);lastGpuMs_=gpu_.LastGpuMs();
    if(settings_.stability>0) {
        if(aliasProxy_)std::swap(anchor_,previousProxy_);
        else std::swap(proxy_,previousProxy_);
        hasHistory_=true;current_^=1;
    }
    return result;
}
