#pragma once
#include <algorithm>
#include <array>
#include <cmath>
#include <vector>

// Thumbnail-only cut evidence. Motion alignment prevents a pan from looking like
// a cut; histogram evidence alone is weak and must not repeatedly reset history.
// Inspired by 2600th/dlss5-video-player's MIT SceneCut policy. Thresholds here are
// conservative starting values, not measurements on Studio's video corpus.
class SceneHistory {
    std::vector<float> previous_;
    long long frame_=0, lastCut_=-1000000;
public:
    bool Update(const std::vector<float>& gray,int w,int h,float fps=30) {
        if(previous_.size()!=gray.size() || gray.empty()) {
            // Initialization must not debounce a genuine cut immediately after it.
            previous_=gray;lastCut_=-1000000;++frame_;return true;
        }
        std::array<double,32> a{},b{};
        for(size_t i=0;i<gray.size();++i) {
            ++a[std::clamp(static_cast<int>(gray[i]*32),0,31)];
            ++b[std::clamp(static_cast<int>(previous_[i]*32),0,31)];
        }
        double overlap=0;
        for(size_t i=0;i<a.size();++i)overlap+=std::min(a[i],b[i]);
        overlap/=gray.size();
        double residual=1;
        const int radius=std::min(4,std::max(0,std::min(w,h)/8));
        // Compare the same interior for every candidate; border clamping must
        // not give a large translation an unfairly small comparison region.
        for(int dy=-radius;dy<=radius;++dy)for(int dx=-radius;dx<=radius;++dx) {
            double sum=0;size_t count=0;
            for(int y=radius;y<h-radius;++y)for(int x=radius;x<w-radius;++x) {
                sum+=std::fabs(gray[static_cast<size_t>(y)*w+x]-previous_[static_cast<size_t>(y+dy)*w+x+dx]);++count;
            }
            if(count)residual=std::min(residual,sum/count);
        }
        const bool strong=residual>.30;
        const bool weak=residual>.10 && overlap<.85;
        const bool cut=strong || (weak && frame_-lastCut_>=std::max(1LL,static_cast<long long>(std::ceil(.30*std::max(1.0f,fps)))));
        if(cut)lastCut_=frame_;
        ++frame_;previous_=gray;return cut;
    }
};
