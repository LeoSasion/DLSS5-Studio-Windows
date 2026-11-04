#pragma once
#include "gpu.h"
#include "nr_post.h"

// FP32 correction/history/anchor; only the NR input/output uses model FP16.
// Caches retain raw model output, so post settings can still change without NR.
class NrGpuPost {
public:
    NrGpuPost(GpuContext& gpu,int w,int h,int nw,int nh,const NrPostSettings& settings);
    void Prepare(const std::vector<float>& anchor,GpuTexture* modelInput);
    GpuTexture& Proxy() {return aliasProxy_?anchor_:proxy_;}
    double LastGpuMs() const {return lastGpuMs_;}
    std::vector<uint8_t> Process(const std::vector<float>& anchor,
        GpuTexture& neural,GpuTexture& motion,bool reset,float detail,float colour);
private:
    void Dispatch(ID3D12PipelineState* pso,const std::vector<GpuTexture*>& inputs,GpuTexture& output,
                  const std::vector<uint32_t>& constants);
    GpuContext& gpu_;
    NrPostSettings settings_;
    GpuTexture anchor_,proxy_,previousProxy_,delta_,history_[2],packed_;
    ComPtr<ID3D12RootSignature> signature_;
    ComPtr<ID3D12DescriptorHeap> heap_;
    ComPtr<ID3D12PipelineState> prepare_,correction_,stabilize_,compose_;
    UINT stride_=0;
    int descriptorNext_=0;
    int current_=0;
    bool hasHistory_=false,aliasProxy_=false;
    double lastGpuMs_=0;
};
