#include "nr_post.h"
#include <cstdio>
#include <stdexcept>

static int checks=0;
static void Require(bool value,const char* message){++checks;if(!value)throw std::runtime_error(message);}
static void Near(float a,float b,float tolerance,const char* message){Require(std::fabs(a-b)<=tolerance,message);}

static void TestTransferReference(){
    for(const NrPost::Rgb rgb: {NrPost::Rgb{{1,1,1}},NrPost::Rgb{{.8f,.2f,.03f}},NrPost::Rgb{{.1f,.6f,.9f}}}){
        const auto back=NrPost::ToP3(NrPost::P3To709(rgb));
        for(int c=0;c<3;++c)Near(back[c],rgb[c],.000001f,"P3 D65 matrix roundtrip");
    }
    Near(NrPost::SourceLuma({{0,1,0}},true,"smpte432"),.691738522f,1e-7f,"P3 D65 luminance weight");
    // Published ST 2084 code values at absolute luminance reference points.
    Near(NrPost::Encode(100.0f/203,"pq"),.5080784f,.000002f,"PQ 100 nit code");
    Near(NrPost::Encode(1000.0f/203,"pq"),.7518271f,.000002f,"PQ 1000 nit code");
    Near(NrPost::Decode(1,"pq"),10000.0f/203,.0001f,"PQ 10000 nit peak");
    Near(NrPost::Decode(0,"pq"),0,1e-9f,"PQ black");
    Near(NrPost::Encode(1,"hlg"),.75f,.000002f,"HLG diffuse white");
    for(const std::string tr:{"sdr","pq","hlg"}){
        float prior=-1;
        for(int i=0;i<=4096;++i){
            const float v=i/4096.0f,linear=NrPost::Decode(v,tr);
            Require(linear>=prior,"transfer monotonic");prior=linear;
            Near(NrPost::Encode(linear,tr),v,.000002f,"transfer roundtrip");
        }
    }
}
static void TestPrecisionAndIdentity(){
    NrPostSettings s;s.pixelFormat="rgba64le";s.transfer="pq";
    std::vector<float> anchor(1024*4);
    for(int i=0;i<1024;++i){for(int c=0;c<3;++c)anchor[4*i+c]=NrPost::Decode((10000+i*7)/65535.0f,"pq");anchor[4*i+3]=.25f;}
    std::vector<float> delta(16*4,0);
    auto result=NrPost::Compose(anchor,1024,1,delta,16,1,1,1,s);
    auto packed=NrPost::Pack(result,s);
    int unique=0,previous=-1;
    for(int i=0;i<1024;++i){
        const int actual=packed[8*i]|(packed[8*i+1]<<8);
        Require(std::abs(actual-(10000+i*7))<=1,"16-bit identity precision");
        if(actual!=previous)++unique;previous=actual;
        Near(result[4*i+3],.25f,0,"source alpha");
    }
    Require(unique==1024,"16-bit chain must retain more than 256 levels");
    s.primaries="smpte432";
    const auto p3packed=NrPost::Pack(NrPost::Compose(anchor,1024,1,delta,16,1,0,1,s),s);
    Require(p3packed==packed,"P3 HDR original is not passed through a lossy gamut conversion");
    // Low-resolution inference must never resize the native anchor geometry or texture.
    std::vector<float> chart(64*4),zero(4*4,0);
    for(int i=0;i<64;++i){chart[4*i]=(i%2)?1.0f:0.0f;chart[4*i+1]=.2f;chart[4*i+2]=.3f;chart[4*i+3]=1;}
    NrPostSettings sdr;
    const auto stable=NrPost::Compose(chart,64,1,zero,4,1,1,1,sdr);
    Require(chart==stable,"matched residual preserves full-resolution anchor");
}
static void TestProtection(){
    std::vector<float> anchor(8*8*4,.2f),delta(8*8*4,.15f);
    for(size_t i=3;i<anchor.size();i+=4)anchor[i]=.6f;
    NrPostSettings s;s.protect={{.25f,.25f,.5f,.5f}};
    const auto p=NrPost::Compose(anchor,8,8,delta,8,8,1,1,s);
    const size_t center=(4*8+4)*4;
    Near(p[center],anchor[center],0,"protected original exactly");
    Require(p[0]>.3f,"outside protection still enhanced");
    Near(p[center+3],.6f,0,"protection preserves alpha");
    Require(NrPost::SkinWeight({{.5f,.25f,.16f}})>NrPost::SkinWeight({{.05f,.15f,.8f}}),"skin-color protection distinguishes blue");
    s.protect={{0,0,0,0}};s.protectHighlights=1;
    Near(NrPost::ProtectWeight({{1,1,1}},0,0,1,1,s),1,0,"highlight white protection");
}
static void TestTemporalMotionAndRejection(){
    constexpr int w=8,h=2;
    std::vector<float> guide(w*h*4,.2f),delta(w*h*4,.1f),flow(w*h*2,0);
    for(size_t i=3;i<guide.size();i+=4)guide[i]=1;
    NrPost::TemporalCorrection temporal;
    temporal.Stabilize(delta,guide,flow,w,h,true,1,30);
    std::fill(delta.begin(),delta.end(),-.1f);
    temporal.Stabilize(delta,guide,flow,w,h,false,1,30);
    Require(std::fabs(delta[4*3])<.06f,"stationary correction flicker reduced");
    // A disoccluded/new source is never blended with stale correction history.
    auto changed=guide;for(size_t i=0;i<changed.size();i+=4)for(int c=0;c<3;++c)changed[i+c]=.8f;
    std::fill(delta.begin(),delta.end(),.3f);
    temporal.Stabilize(delta,changed,flow,w,h,false,1,30);
    Near(delta[4*3],.3f,1e-7f,"disocclusion rejects history");
    std::fill(delta.begin(),delta.end(),-.4f);
    temporal.Stabilize(delta,changed,flow,w,h,true,1,30);
    Near(delta[4*3],-.4f,0,"scene cut clears history");
    // Current->previous motion transports a correction with the moving image.
    temporal.Clear();
    std::fill(guide.begin(),guide.end(),.2f);std::fill(delta.begin(),delta.end(),0.0f);
    guide[4*2]=.6f;delta[4*2]=.2f;
    temporal.Stabilize(delta,guide,flow,w,h,true,1,30);
    auto moved=guide;moved[4*2]=.2f;moved[4*3]=.6f;
    std::fill(delta.begin(),delta.end(),0.0f);
    for(size_t i=0;i<flow.size();i+=2)flow[i]=-1;
    temporal.Stabilize(delta,moved,flow,w,h,false,1,30);
    Require(delta[4*3]>.1f,"motion compensation follows source feature");
    Near(delta[4*2],0,1e-6f,"motion compensation does not leave source ghost");
}
int main(){
    try{TestTransferReference();TestPrecisionAndIdentity();TestProtection();TestTemporalMotionAndRejection();}
    catch(const std::exception& e){std::fprintf(stderr,"FAIL: %s\n",e.what());return 1;}
    std::printf("PASS: %d numerical checks (HDR reference, precision, texture, alpha, protection, temporal motion)\n",checks);
    return 0;
}
