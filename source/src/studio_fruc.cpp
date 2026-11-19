// CUDA pipe adapter for the separately installed NVIDIA Optical Flow SDK 5.0.7.
#define NOMINMAX
#include <windows.h>
#include <NvOFFRUC.h>
#include <array>
#include <cstdio>
#include <cstdlib>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <stdexcept>
#include <string>
#include <vector>
#include <fcntl.h>
#include <io.h>
#include "scene_history.h"

template<class T> T entry(HMODULE dll, const char* name) {
    const auto result = reinterpret_cast<T>(GetProcAddress(dll, name));
    if (!result) throw std::runtime_error(std::string("Missing export: ") + name);
    return result;
}

static void checked(int error, const char* op) {
    if (error) throw std::runtime_error(std::string(op) + " failed: " + std::to_string(error));
}

int main(int argc, char** argv) try {
    if (argc != 5) throw std::runtime_error("Expected width height adapter-LUID fps");
    const int width = std::stoi(argv[1]), height = std::stoi(argv[2]);
    const std::string luid(argv[3]);
    const float fps = std::stof(argv[4]);
    if (width < 16 || height < 16 || width > 8192 || height > 8192 || luid.size() != 16 || !(fps > 0))
        throw std::runtime_error("Invalid FRUC input geometry, GPU or fps");
    if (_setmode(_fileno(stdin), _O_BINARY)<0 || _setmode(_fileno(stdout), _O_BINARY)<0)
        throw std::runtime_error("Could not open the raw-video pipe in binary mode");
    wchar_t exe[32768]{};
    const auto chars = GetModuleFileNameW(nullptr, exe, static_cast<DWORD>(std::size(exe)));
    if (!chars || chars >= std::size(exe)) throw std::runtime_error("Could not locate FRUC adapter");
    auto library = std::filesystem::path(exe).parent_path() / "NvOFFRUC.dll";
    HMODULE fruc = LoadLibraryExW(library.c_str(), nullptr, LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_SYSTEM32);
    HMODULE cuda = LoadLibraryExW(L"nvcuda.dll", nullptr, LOAD_LIBRARY_SEARCH_SYSTEM32);
    if (!cuda || !fruc) throw std::runtime_error("NVIDIA FRUC or its driver dependencies could not be loaded");
    using Ptr = uint64_t;
    checked(entry<int(WINAPI*)(unsigned)>(cuda,"cuInit")(0), "cuInit");
    int count=0, device=-1;
    checked(entry<int(WINAPI*)(int*)>(cuda,"cuDeviceGetCount")(&count), "cuDeviceGetCount");
    for (int ordinal=0; ordinal<count; ++ordinal) {
        int id; unsigned mask; uint8_t raw[8]; char hex[17]{};
        checked(entry<int(WINAPI*)(int*,int)>(cuda,"cuDeviceGet")(&id,ordinal), "cuDeviceGet");
        if (entry<int(WINAPI*)(void*,unsigned*,int)>(cuda,"cuDeviceGetLuid")(raw,&mask,id)) continue;
        for (int i=0;i<8;++i) std::snprintf(hex+2*i,3,"%02x",unsigned(raw[i]));
        if (luid==hex) { device=id; break; }
    }
    if (device<0) throw std::runtime_error("FRUC CUDA and DLSS adapters do not match");
    void* context=nullptr;
    checked(entry<int(WINAPI*)(void**,int)>(cuda,"cuDevicePrimaryCtxRetain")(&context,device), "cuDevicePrimaryCtxRetain");
    checked(entry<int(WINAPI*)(void*)>(cuda,"cuCtxSetCurrent")(context), "cuCtxSetCurrent");
    const size_t size=size_t(width)*height*4;
    std::array<Ptr,3> buffers{};
    for (auto& ptr : buffers)
        checked(entry<int(WINAPI*)(Ptr*,size_t)>(cuda,"cuMemAlloc_v2")(&ptr,size), "cuMemAlloc");
    const auto upload=entry<int(WINAPI*)(Ptr,const void*,size_t)>(cuda,"cuMemcpyHtoD_v2");
    const auto download=entry<int(WINAPI*)(void*,Ptr,size_t)>(cuda,"cuMemcpyDtoH_v2");
    NvOFFRUC_CREATE_PARAM create{};
    create.uiWidth=width;
    create.uiHeight=height;
    create.eResourceType=CudaResource;
    create.eSurfaceFormat=ARGBSurface;
    create.eCUDAResourceType=CudaResourceCuDevicePtr;
    NvOFFRUCHandle instance=nullptr;
    checked(entry<PtrToFuncNvOFFRUCCreate>(fruc,CreateProcName)(&create,&instance), "NvOFFRUCCreate");
    NvOFFRUC_REGISTER_RESOURCE_PARAM registration{};
    registration.uiCount=3;
    for (int i=0; i<3; ++i) registration.pArrResource[i]=reinterpret_cast<void*>(buffers[i]);
    checked(entry<PtrToFuncNvOFFRUCRegisterResource>(fruc,RegisterResourceProcName)(instance,&registration), "NvOFFRUCRegisterResource");
    const auto process=entry<PtrToFuncNvOFFRUCProcess>(fruc,ProcessProcName);
    std::vector<uint8_t> current(size), previous(size), output(size);
    std::vector<float> thumbnail(32*18);
    SceneHistory scenes;
    auto write=[&](const auto& data){
        if (std::fwrite(data.data(),1,size,stdout)!=size) throw std::runtime_error("FRUC output pipe closed");
    };
    uint64_t frame=0;
    for (;;) {
        const auto read=std::fread(current.data(),1,size,stdin);
        if (!read && std::feof(stdin)) break;
        if (read!=size) throw std::runtime_error("Incomplete FRUC input frame");
        for (int y=0; y<18; ++y) for (int x=0; x<32; ++x) {
            const auto* p=&current[(size_t(y*height/18)*width+x*width/32)*4];
            thumbnail[y*32+x]=(0.0722f*p[0]+0.7152f*p[1]+0.2126f*p[2])/255.f;
        }
        const bool cut=scenes.Update(thumbnail,32,18,fps);
        const auto src=buffers[frame%2];
        checked(upload(src,current.data(),size), "cuMemcpyHtoD");
        NvOFFRUC_PROCESS_IN_PARAMS in{};
        NvOFFRUC_PROCESS_OUT_PARAMS out{};
        bool repeated=false;
        in.stFrameDataInput.pFrame=reinterpret_cast<void*>(src);
        in.stFrameDataInput.nTimeStamp=double(frame)/fps;
        out.stFrameDataOutput.pFrame=reinterpret_cast<void*>(buffers[2]);
        out.stFrameDataOutput.nTimeStamp=(double(frame)-(frame?.5:0.))/fps;
        out.stFrameDataOutput.bHasFrameRepetitionOccurred=&repeated;
        checked(process(instance,&in,&out), "NvOFFRUCProcess");
        if (frame) {
            if (cut) write(previous);
            else { checked(download(output.data(),buffers[2],size), "cuMemcpyDtoH"); write(output); }
        }
        write(current);
        current.swap(previous);
        if (++frame==1 || frame%8==0) { std::fprintf(stderr,"VFXPROG %llu\n",static_cast<unsigned long long>(frame)); std::fflush(stderr); }
    }
    if (!frame) throw std::runtime_error("No FRUC input frames");
    write(previous);
    if (std::fflush(stdout)!=0) throw std::runtime_error("FRUC output pipe closed");
    NvOFFRUC_UNREGISTER_RESOURCE_PARAM freed{};
    freed.uiCount=registration.uiCount;
    for (unsigned i=0; i<freed.uiCount; ++i) freed.pArrResource[i]=registration.pArrResource[i];
    checked(entry<PtrToFuncNvOFFRUCUnregisterResource>(fruc,UnregisterResourceProcName)(instance,&freed), "NvOFFRUCUnregisterResource");
    checked(entry<PtrToFuncNvOFFRUCDestroy>(fruc,DestroyProcName)(instance), "NvOFFRUCDestroy");
    return 0;
} catch (const std::exception& error) {
    std::fprintf(stderr,"%s\n",error.what());
    return 1;
}
