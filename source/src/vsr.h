#pragma once
#include "dlss.h"
#include "image.h"

// Optional RTX Video SDK adapter. It uses the existing NGX device/session and
// never downloads a runtime. Build-time SDK + locally staged runtime are required.
class VideoVsr {
public:
    VideoVsr(GpuContext& gpu,NgxSession& session,unsigned iw,unsigned ih,unsigned ow,unsigned oh,bool eligible);
    ~VideoVsr();
    bool Available() const {return ready_;}
    ImageF Upscale(const ImageF& image);
    static bool Built();
private:
    GpuContext& gpu_;
    bool ready_=false;
    NVSDK_NGX_Parameter* params_=nullptr;
    NVSDK_NGX_Handle* feature_=nullptr;
    GpuTexture input_,output_;
};
