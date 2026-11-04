#include "scene_history.h"
#include <cstdio>
#include <stdexcept>

int main() {
    auto require=[](bool value,const char* label){if(!value)throw std::runtime_error(label);};
    const int w=64,h=36;
    const std::vector<float> dark(w*h,0),dim(w*h,.2f),bright(w*h,.9f);
    SceneHistory immediate;
    require(immediate.Update(dark,w,h,30),"first frame must reset");
    require(immediate.Update(dim,w,h,30),"a genuine weak cut immediately after initialization must not be debounced");
    require(!immediate.Update(dark,w,h,30),"a weak transient return must be debounced");
    require(immediate.Update(bright,w,h,30),"strong discontinuity must bypass debounce");
    SceneHistory stationary;
    require(stationary.Update(dim,w,h,60),"stationary first frame");
    for(int i=0;i<100;++i)require(!stationary.Update(dim,w,h,60),"held frames must keep history");
    std::vector<float> pattern(w*h),pan(w*h);
    for(int y=0;y<h;++y)for(int x=0;x<w;++x)pattern[y*w+x]=((x*13+y*7)%17)/17.0f;
    for(int y=0;y<h;++y)for(int x=0;x<w;++x)pan[y*w+x]=pattern[y*w+std::min(w-1,x+2)];
    SceneHistory movement;movement.Update(pattern,w,h,30);
    require(!movement.Update(pan,w,h,30),"a matched translation must not reset history");
    std::puts("Scene history: initialization, transient, strong cut, held frames and pan passed.");
}
