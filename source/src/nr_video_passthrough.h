#pragma once
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <vector>

// Native-size detail=0 is an identity on the decoded raw stream. It must not
// create NGX or quantize through float/half; FFmpeg still handles export/muxing.
inline int CopyRawVideo(unsigned w,unsigned h,bool sixteen) {
    SetLogToStderr(true);
    _setmode(_fileno(stdin),_O_BINARY);_setmode(_fileno(stdout),_O_BINARY);
    const auto start=std::chrono::steady_clock::now();
    const uint64_t frameBytes=static_cast<uint64_t>(w)*h*(sixteen?8:4);
    std::vector<uint8_t> buffer(static_cast<size_t>(std::min<uint64_t>(frameBytes,1024*1024)));
    uint64_t remaining=frameBytes,frames=0;
    bool failed=false;
    LogInfo("NR bypass: detail=0 at native size, byte-preserving raw copy (no GPU/NGX initialization)");
    for(;;) {
        const size_t requested=static_cast<size_t>(std::min<uint64_t>(remaining,buffer.size()));
        const size_t got=std::fread(buffer.data(),1,requested,stdin);
        if(!got) {
            failed=std::ferror(stdin)!=0||remaining!=frameBytes;
            if(failed)LogErr("NR bypass: input error or truncated raw frame");
            break;
        }
        if(std::fwrite(buffer.data(),1,got,stdout)!=got){failed=true;LogErr("NR bypass: output write failed");break;}
        remaining-=got;
        if(!remaining) {
            ++frames;remaining=frameBytes;
            if(frames%5==0){const double seconds=std::chrono::duration<double>(std::chrono::steady_clock::now()-start).count();
                std::fprintf(stderr,"NRPROG %llu %.3f\n",frames,frames/std::max(.001,seconds));std::fflush(stderr);}
        }
    }
    if(std::fflush(stdout)!=0)failed=true;
    const double ms=std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now()-start).count();
    std::fprintf(stderr,"NRTRANSFER {\"upload_allocations\":0,\"readback_allocations\":0,\"fence_waits\":0}\n");
    std::fprintf(stderr,"NRMETRIC {\"version\":1,\"frames\":%llu,\"bypassed_nr_frames\":%llu,\"cached_nr_frames\":0,\"nr_evaluated_frames\":0,\"reset_count\":0,\"initialization_ms\":0,\"wall_ms\":%.3f,\"bypass\":\"detail_zero_native_size\",\"stage_ms\":{\"nr\":0,\"raw_copy\":%.3f},\"gpu_stage_ms\":{\"nr\":0}}\n",frames,frames,ms,ms);
    return frames&&!failed?0:1;
}
