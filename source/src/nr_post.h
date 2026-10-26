// Host postprocessing contract. Linear HDR values use 203 cd/m2 as diffuse white.
// ST 2084 constants and HLG OETF/inverse follow ITU-R BT.2100; HDR signal gamut is BT.2020.
// This file deliberately has no Windows/GPU dependency so numerical behavior can be tested.
#pragma once

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <limits>
#include <string>
#include <vector>

struct NrPostSettings {
    std::string pixelFormat = "rgba";
    std::string transfer = "sdr";
    std::string primaries = "auto";
    float stability = 0.0f;
    std::array<float, 4> protect{{0, 0, 0, 0}};
    float protectSkin = 0.0f;
    float protectHighlights = 0.0f;
    float renderScale = 1.0f;
    float fps = 30.0f;
    std::string cacheDir;
    std::string cacheKey;
    uint32_t cacheFrames = 0;
    bool Enabled() const {
        return pixelFormat != "rgba" || transfer != "sdr" || stability > 0 ||
               protect[2] > 0 || protectSkin > 0 || protectHighlights > 0 || renderScale < 1 ||
               (!cacheDir.empty() && cacheFrames>0);
    }
};

namespace NrPost {
inline float Clamp01(float v) { return std::clamp(v, 0.0f, 1.0f); }
inline float Smooth(float a, float b, float v) {
    float t = Clamp01((v - a) / (b - a));
    return t * t * (3.0f - 2.0f * t);
}
inline float SrgbDecode(float v) {
    return v <= .04045f ? v / 12.92f : std::pow((v + .055f) / 1.055f, 2.4f);
}
inline float SrgbEncode(float v) {
    return v <= .0031308f ? 12.92f * v : 1.055f * std::pow(v, 1.0f / 2.4f) - .055f;
}
inline float Decode(float signal, const std::string& transfer) {
    const double v = Clamp01(signal);
    if (transfer == "pq") {
        constexpr double m1 = 2610.0 / 16384.0, m2 = 2523.0 / 32.0;
        constexpr double c1 = 3424.0 / 4096.0, c2 = 2413.0 / 128.0, c3 = 2392.0 / 128.0;
        const double p = std::pow(v, 1.0 / m2);
        const double nits = 10000.0 * std::pow(std::max(p - c1, 0.0) / std::max(c2 - c3 * p, 1e-12), 1.0 / m1);
        return static_cast<float>(nits / 203.0);
    }
    if (transfer == "hlg") {
        constexpr double a = .17883277, b = .28466892, c = .55991073;
        // BT.2100 HLG scene light. 0.75 signal maps to reference diffuse white.
        const double scene = v <= .5 ? v * v / 3.0 : (std::exp((v - c) / a) + b) / 12.0;
        return static_cast<float>(scene / .26496256);
    }
    return SrgbDecode(static_cast<float>(v));
}
inline float Encode(float linear, const std::string& transfer) {
    const double v = std::max(0.0, static_cast<double>(linear));
    if (transfer == "pq") {
        constexpr double m1 = 2610.0 / 16384.0, m2 = 2523.0 / 32.0;
        constexpr double c1 = 3424.0 / 4096.0, c2 = 2413.0 / 128.0, c3 = 2392.0 / 128.0;
        const double p = std::pow(std::min(v * 203.0 / 10000.0, 1.0), m1);
        return Clamp01(static_cast<float>(std::pow((c1 + c2 * p) / (1.0 + c3 * p), m2)));
    }
    if (transfer == "hlg") {
        constexpr double a = .17883277, b = .28466892, c = .55991073;
        const double scene = v * .26496256;
        return Clamp01(static_cast<float>(scene <= 1.0 / 12.0 ? std::sqrt(3 * scene)
                                            : a * std::log(12 * scene - b) + c));
    }
    return Clamp01(SrgbEncode(static_cast<float>(v)));
}
using Rgb = std::array<float, 3>;
inline Rgb To709(const Rgb& p) {
    return {{1.660491f*p[0]-.587641f*p[1]-.072850f*p[2],
             -.124550f*p[0]+1.132900f*p[1]-.008349f*p[2],
             -.018151f*p[0]-.100579f*p[1]+1.118730f*p[2]}};
}
inline Rgb To2020(const Rgb& p) {
    return {{.627404f*p[0]+.329283f*p[1]+.043313f*p[2],
             .069097f*p[0]+.919540f*p[1]+.011362f*p[2],
             .016391f*p[0]+.088013f*p[1]+.895595f*p[2]}};
}
// Derived from the D65 RGB/XYZ matrices in W3C CSS Color 4 sample conversions.
inline Rgb P3To709(const Rgb& p) {
    return {{1.224940176f*p[0]-.224940176f*p[1],
             -.042056955f*p[0]+1.042056955f*p[1],
             -.019637555f*p[0]-.078636046f*p[1]+1.098273600f*p[2]}};
}
inline Rgb ToP3(const Rgb& p) {
    return {{.822461969f*p[0]+.177538031f*p[1],
             .033194199f*p[0]+.966805801f*p[1],
             .017082631f*p[0]+.072397441f*p[1]+.910519929f*p[2]}};
}
inline float Luma(const Rgb& p, bool hdr = false) {
    return hdr ? .2627f*p[0]+.6780f*p[1]+.0593f*p[2]
               : .2126f*p[0]+.7152f*p[1]+.0722f*p[2];
}
// A fixed, luminance-based SDR proxy. Fixed exposure avoids exposure pumping.
inline float SourceLuma(const Rgb& p,bool hdr,const std::string& primaries) {
    if(hdr&&primaries=="smpte432")return .228974564f*p[0]+.691738522f*p[1]+.079286914f*p[2];
    return Luma(p,hdr&&primaries!="bt709");
}
inline Rgb Proxy(const Rgb& p, bool hdr, bool wide = true, bool p3 = false) {
    if (!hdr) return p;
    const float luminance=p3?(.228974564f*p[0]+.691738522f*p[1]+.079286914f*p[2]):Luma(p,wide);
    const float gain = 1.0f / (1.0f + std::max(0.0f,luminance));
    Rgb out{{p[0]*gain, p[1]*gain, p[2]*gain}};
    if(p3)out=P3To709(out);else if(wide)out=To709(out);
    for (float& c : out) c = Clamp01(c);
    return out;
}
inline std::vector<float> MakeProxy(const std::vector<float>& linear, bool hdr, bool wide = true, bool p3 = false) {
    if (!hdr) return linear;
    std::vector<float> out(linear.size());
    for (size_t i = 0; i < linear.size(); i += 4) {
        const Rgb p = Proxy({{linear[i],linear[i+1],linear[i+2]}}, true, wide, p3);
        for (int c = 0; c < 3; ++c) out[i+c] = p[c];
        out[i+3] = linear[i+3];
    }
    return out;
}
inline float Sample(const std::vector<float>& p, int w, int h, int channels, float x, float y, int c) {
    x = std::clamp(x,0.0f,static_cast<float>(w-1));
    y = std::clamp(y,0.0f,static_cast<float>(h-1));
    const int x0 = static_cast<int>(std::floor(x)), y0 = static_cast<int>(std::floor(y));
    const int x1 = std::min(w-1,x0+1), y1 = std::min(h-1,y0+1);
    const float fx=x-x0, fy=y-y0;
    auto at=[&](int xx,int yy){return p[(static_cast<size_t>(yy)*w+xx)*channels+c];};
    return (at(x0,y0)*(1-fx)+at(x1,y0)*fx)*(1-fy)+(at(x0,y1)*(1-fx)+at(x1,y1)*fx)*fy;
}
// Area resampling on shrink, bilinear on enlarge. Pixel centers stay aligned.
inline std::vector<float> Resize(const std::vector<float>& p, int w, int h, int nw, int nh, int channels=4) {
    if (w==nw && h==nh) return p;
    std::vector<float> out(static_cast<size_t>(nw)*nh*channels);
    const float sx=static_cast<float>(w)/nw, sy=static_cast<float>(h)/nh;
    if (sx>1 || sy>1) {
        for(int y=0;y<nh;++y) for(int x=0;x<nw;++x) {
            const float ax=x*sx,bx=(x+1)*sx,ay=y*sy,by=(y+1)*sy;
            for(int c=0;c<channels;++c) {
                double sum=0,weight=0;
                for(int yy=static_cast<int>(ay);yy<std::min(h,static_cast<int>(std::ceil(by)));++yy)
                    for(int xx=static_cast<int>(ax);xx<std::min(w,static_cast<int>(std::ceil(bx)));++xx) {
                        const float v=std::max(0.0f,std::min(bx,xx+1.0f)-std::max(ax,static_cast<float>(xx))) *
                                      std::max(0.0f,std::min(by,yy+1.0f)-std::max(ay,static_cast<float>(yy)));
                        sum += p[(static_cast<size_t>(yy)*w+xx)*channels+c]*v; weight+=v;
                    }
                out[(static_cast<size_t>(y)*nw+x)*channels+c]=static_cast<float>(sum/std::max(weight,1e-12));
            }
        }
    } else {
        for(int y=0;y<nh;++y) for(int x=0;x<nw;++x) for(int c=0;c<channels;++c)
            out[(static_cast<size_t>(y)*nw+x)*channels+c]=Sample(p,w,h,channels,(x+.5f)*sx-.5f,(y+.5f)*sy-.5f,c);
    }
    return out;
}
inline float SkinWeight(const Rgb& linear) {
    const float r=Clamp01(SrgbEncode(std::max(0.0f,linear[0])));
    const float g=Clamp01(SrgbEncode(std::max(0.0f,linear[1])));
    const float b=Clamp01(SrgbEncode(std::max(0.0f,linear[2])));
    const float y=.299f*r+.587f*g+.114f*b;
    const float cb=.5f-.168736f*r-.331264f*g+.5f*b;
    const float cr=.5f+.5f*r-.418688f*g-.081312f*b;
    // Conservative color-only protection, not a claim of semantic face detection.
    return Smooth(.30f,.34f,cb)*(1-Smooth(.48f,.52f,cb)) *
           Smooth(.51f,.55f,cr)*(1-Smooth(.68f,.73f,cr)) * Smooth(.03f,.10f,y);
}
inline float ProtectWeight(const Rgb& anchor, int x, int y, int w, int h, const NrPostSettings& s) {
    float p=0;
    if(s.protect[2]>0 && s.protect[3]>0) {
        const float px=(x+.5f)/w,py=(y+.5f)/h;
        const float ax=s.protect[0],ay=s.protect[1],bx=ax+s.protect[2],by=ay+s.protect[3];
        // Inside is exactly protected; a small outer feather avoids a hard seam.
        const float d=std::max({ax-px,px-bx,ay-py,py-by,0.0f});
        p=1-Smooth(0.0f,.0125f,d);
    }
    const bool hdr=s.transfer!="sdr";
    const bool wide=hdr&&s.primaries!="bt709";
    const Rgb proxy=Proxy(anchor,hdr,wide,hdr&&s.primaries=="smpte432");
    p=std::max(p,s.protectSkin*SkinWeight(proxy));
    p=std::max(p,s.protectHighlights*Smooth(hdr?1.0f:.65f,hdr?3.0f:.95f,SourceLuma(anchor,hdr,s.primaries)));
    return Clamp01(p);
}

class TemporalCorrection {
    int width_=0,height_=0;
    std::vector<float> previous_, correction_;
public:
    void Clear(){previous_.clear();correction_.clear();width_=height_=0;}
    // Motion is current -> previous displacement in this frame's pixels.
    void Stabilize(std::vector<float>& delta,const std::vector<float>& proxy,
                   const std::vector<float>& motion,int w,int h,bool reset,float strength,float fps) {
        if(strength<=0){Clear();return;}
        const bool history=!reset && width_==w && height_==h && previous_.size()==proxy.size() && correction_.size()==delta.size();
        if(history) {
            const float base=std::min(.90f,std::exp(-1.0f/(std::max(1.0f,fps)*.08f))*strength);
            const bool flow=motion.size()==static_cast<size_t>(w)*h*2;
            for(int y=0;y<h;++y) for(int x=0;x<w;++x) {
                const size_t i=(static_cast<size_t>(y)*w+x)*4;
                const size_t m=(static_cast<size_t>(y)*w+x)*2;
                const float px=x+(flow?motion[m]:0),py=y+(flow?motion[m+1]:0);
                if(!std::isfinite(px)||!std::isfinite(py)||px<0||py<0||px>w-1||py>h-1)continue;
                float error=0;
                for(int c=0;c<3;++c) error=std::max(error,std::fabs(Sample(previous_,w,h,4,px,py,c)-proxy[i+c]));
                // Occlusion/disocclusion is rejected, even if optical flow looks plausible.
                const float confidence=1-Smooth(.025f,.12f,error);
                const float weight=base*confidence;
                for(int c=0;c<3;++c) {
                    const float prev=Sample(correction_,w,h,4,px,py,c);
                    delta[i+c]=delta[i+c]*(1-weight)+prev*weight;
                }
            }
        }
        previous_=proxy;correction_=delta;width_=w;height_=h;
    }
};

inline std::vector<float> Correction(const std::vector<float>& proxy,const std::vector<float>& nrEncoded) {
    std::vector<float> delta(proxy.size(),0);
    for(size_t i=0;i<proxy.size();i+=4)for(int c=0;c<3;++c)
        delta[i+c]=SrgbDecode(Clamp01(nrEncoded[i+c]))-proxy[i+c];
    return delta;
}
inline std::vector<float> Compose(const std::vector<float>& anchor,int w,int h,
                                  const std::vector<float>& delta,int dw,int dh,float detail,float colour,
                                  const NrPostSettings& s) {
    std::vector<float> result(anchor.size());
    const bool hdr=s.transfer!="sdr";
    const bool wide=hdr&&s.primaries!="bt709";
    for(int y=0;y<h;++y)for(int x=0;x<w;++x) {
        const size_t i=(static_cast<size_t>(y)*w+x)*4;
        const Rgb original{{anchor[i],anchor[i+1],anchor[i+2]}};
        const float rx=(x+.5f)*dw/w-.5f,ry=(y+.5f)*dh/h-.5f;
        Rgb d{{Sample(delta,dw,dh,4,rx,ry,0),Sample(delta,dw,dh,4,rx,ry,1),Sample(delta,dw,dh,4,rx,ry,2)}};
        if(hdr) {
            if(s.primaries=="smpte432")d=ToP3(d);else if(wide)d=To2020(d);
            // Bounded local gain restores correction amplitude without inverse-tone-map poles.
            const float gain=std::min(4.0f,1+std::max(0.0f,SourceLuma(original,hdr,s.primaries)));
            for(float& c:d)c*=gain;
        }
        const float yDelta=SourceLuma(d,hdr,s.primaries);
        const float strength=detail*(1-ProtectWeight(original,x,y,w,h,s));
        const float originalY=std::max(1e-5f,SourceLuma(original,hdr,s.primaries));
        for(int c=0;c<3;++c) {
            // At colour=0 use the original chromaticity with the enhanced luminance.
            const float preserved=original[c]*yDelta/originalY;
            result[i+c]=std::max(0.0f,original[c]+strength*(preserved*(1-colour)+d[c]*colour));
        }
        result[i+3]=anchor[i+3];
    }
    return result;
}
inline std::vector<uint8_t> Pack(const std::vector<float>& linear,const NrPostSettings& s) {
    const bool sixteen=s.pixelFormat=="rgba64le";
    std::vector<uint8_t> out(linear.size()*(sixteen?2:1));
    for(size_t i=0;i<linear.size();++i) {
        const float v=(i%4==3)?Clamp01(linear[i]):Encode(linear[i],s.transfer);
        if(sixteen) {
            const unsigned n=static_cast<unsigned>(std::lround(v*65535));
            out[2*i]=static_cast<uint8_t>(n);out[2*i+1]=static_cast<uint8_t>(n>>8);
        } else out[i]=static_cast<uint8_t>(std::lround(v*255));
    }
    return out;
}
} // namespace NrPost
