// Complete, bounded half-precision NR output cache. Model stages never alias postprocessing.
#pragma once
#include <DirectXPackedVector.h>
#include <filesystem>
#include <fstream>

namespace NrVideoCache {
constexpr uint64_t Budget=1024ull*1024*1024;
inline uint64_t Hash(const void* memory,size_t size,uint64_t h=1469598103934665603ull){
    const auto* p=static_cast<const uint8_t*>(memory);
    for(size_t i=0;i<size;++i){h^=p[i];h*=1099511628211ull;}
    return h;
}
inline bool Eligible(unsigned w,unsigned h,uint32_t frames){
    if(!w||!h||!frames)return false;
    return static_cast<uint64_t>(w)*h*8+16 <= (Budget-64)/frames;
}
class Store {
    std::filesystem::path path_,temp_;
    std::fstream stream_;
    uint64_t header_[8]{};
    size_t values_=0;
    uint32_t count_=0;
    bool replay_=false,enabled_=false;
public:
    Store(const std::wstring& directory,unsigned w,unsigned h,uint32_t frames,uint64_t signature){
        if(directory.empty()||!Eligible(w,h,frames))return;
        std::filesystem::create_directories(directory);
        path_=std::filesystem::path(directory)/L"model.nrvc";
        temp_=std::filesystem::path(directory)/L"model.nrvc.tmp";
        values_=static_cast<size_t>(w)*h*4;
        header_[0]=0x324356524e535344ull; // DSSNRVC2
        header_[1]=2;header_[2]=w;header_[3]=h;header_[4]=frames;header_[5]=signature;
        header_[6]=values_*2;header_[7]=Budget;
        if(std::filesystem::is_regular_file(path_)){
            stream_.open(path_,std::ios::in|std::ios::binary|std::ios::ate);
            const auto bytes=stream_.tellg();
            const auto expected=64+static_cast<uint64_t>(frames)*(values_*2+16);
            uint64_t saved[8]{};
            stream_.seekg(0);stream_.read(reinterpret_cast<char*>(saved),64);
            if(stream_ && static_cast<uint64_t>(bytes)==expected &&
               std::equal(std::begin(header_),std::end(header_),std::begin(saved))){replay_=enabled_=true;return;}
            stream_.close();stream_.clear();
        }
        stream_.open(temp_,std::ios::out|std::ios::binary|std::ios::trunc);
        if(!stream_)return;
        stream_.write(reinterpret_cast<const char*>(header_),64);enabled_=true;
    }
    ~Store(){
        stream_.close();
        if(!temp_.empty()){std::error_code ec;std::filesystem::remove(temp_,ec);}
    }
    bool Replay()const{return replay_&&enabled_;}
    bool Enabled()const{return enabled_;}
    std::vector<float> Read(uint64_t sourceHash){
        if(!Replay()||count_>=header_[4])throw ToolError("NR cache identity mismatch: frame count");
        uint64_t saved[2]{};stream_.read(reinterpret_cast<char*>(saved),16);
        std::vector<uint16_t> half(values_);stream_.read(reinterpret_cast<char*>(half.data()),half.size()*2);
        if(!stream_||saved[0]!=sourceHash||saved[1]!=Hash(half.data(),half.size()*2)){
            stream_.close();std::error_code ec;std::filesystem::remove(path_,ec);
            throw ToolError("NR cache identity mismatch: source or payload");
        }
        std::vector<float> output(values_);
        for(size_t i=0;i<values_;++i)output[i]=DirectX::PackedVector::XMConvertHalfToFloat(half[i]);
        ++count_;return output;
    }
    void Write(uint64_t sourceHash,const std::vector<float>& output){
        if(!enabled_||replay_)return;
        if(count_>=header_[4]||output.size()!=values_){enabled_=false;return;}
        std::vector<uint16_t> half(values_);
        for(size_t i=0;i<values_;++i)half[i]=DirectX::PackedVector::XMConvertFloatToHalf(output[i]);
        const uint64_t marker[2]={sourceHash,Hash(half.data(),half.size()*2)};
        stream_.write(reinterpret_cast<const char*>(marker),16);
        stream_.write(reinterpret_cast<const char*>(half.data()),half.size()*2);
        if(!stream_){enabled_=false;return;}
        ++count_;
    }
    void Finish(bool success){
        if(!enabled_||replay_)return;
        stream_.flush();const bool valid=static_cast<bool>(stream_);stream_.close();
        if(success&&valid&&count_==header_[4]){
            std::error_code ec;std::filesystem::remove(path_,ec);
            std::filesystem::rename(temp_,path_);
        }
    }
};
}
