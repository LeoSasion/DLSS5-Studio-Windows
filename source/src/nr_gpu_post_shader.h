#pragma once
// Independent implementation of Studio's signed-residual post contract. The
// gain-compensated trust and cubic history sampling ideas are credited in NOTICE.
inline constexpr char kNrGpuPostShader[] = R"NRQ(
cbuffer Settings:register(b0) {
    uint2 Size;uint2 ModelSize;
    float Detail;float Colour;float Stability;float HistoryWeight;
    uint Reset;uint Transfer;uint Gamut;uint Sixteen;
    float4 Protect;
    float Skin;float Highlights;float2 Padding;
};
Texture2D<float4> A:register(t0);
Texture2D<float4> B:register(t1);
Texture2D<float4> C:register(t2);
Texture2D<float4> D:register(t3);
Texture2D<float2> Motion:register(t4);
RWTexture2D<float4> FloatOut:register(u0);
RWTexture2D<uint4> PackedOut:register(u1);
float3 dec(float3 v) {return lerp(pow(max((v+.055)/1.055,0),2.4),v/12.92,step(v,.04045));}
float3 enc(float3 v) {v=max(v,0);return lerp(1.055*pow(v,1.0/2.4)-.055,12.92*v,step(v,.0031308));}
float luma(float3 v) {return dot(v,float3(.2126,.7152,.0722));}
float sourceLuma(float3 v) {
    if(Transfer==0||Gamut==0)return luma(v);
    if(Gamut==2)return dot(v,float3(.228974564,.691738522,.079286914));
    return dot(v,float3(.2627,.6780,.0593));
}
float3 to709(float3 v) {
    if(Gamut==2)return mul(float3x3(1.224940176,-.224940176,0,-.042056955,1.042056955,0,-.019637555,-.078636046,1.098273600),v);
    if(Gamut==1)return mul(float3x3(1.660491,-.587641,-.072850,-.124550,1.132900,-.008349,-.018151,-.100579,1.118730),v);
    return v;
}
float3 from709(float3 v) {
    if(Gamut==2)return mul(float3x3(.822461969,.177538031,0,.033194199,.966805801,0,.017082631,.072397441,.910519929),v);
    if(Gamut==1)return mul(float3x3(.627404,.329283,.043313,.069097,.919540,.011362,.016391,.088013,.895595),v);
    return v;
}
float3 proxy(float3 v) {return Transfer==0?v:saturate(to709(v/(1+max(0,sourceLuma(v)))));}
float4 bilinear(Texture2D<float4> tex,float2 p,int2 dim) {
    int2 q=int2(floor(p));float2 f=frac(p);int2 hi=dim-1;
    return lerp(lerp(tex.Load(int3(clamp(q,0,hi),0)),tex.Load(int3(clamp(q+int2(1,0),0,hi),0)),f.x),
                lerp(tex.Load(int3(clamp(q+int2(0,1),0,hi),0)),tex.Load(int3(clamp(q+1,0,hi),0)),f.x),f.y);
}
float cubic(float x) {
    x=abs(x);return x<1?1.5*x*x*x-2.5*x*x+1:(x<2?-.5*x*x*x+2.5*x*x-4*x+2:0);
}
float3 history(float2 p) {
    int2 base=int2(floor(p)),hi=int2(ModelSize)-1;float3 sum=0;
    [unroll]for(int y=-1;y<=2;++y)[unroll]for(int x=-1;x<=2;++x)
        sum+=D.Load(int3(clamp(base+int2(x,y),0,hi),0)).rgb*cubic(p.x-base.x-x)*cubic(p.y-base.y-y);
    // Bound cubic ringing by the four surrounding signed residuals.
    float3 low=1e20,high=-1e20;
    [unroll]for(int yy=0;yy<2;++yy)[unroll]for(int xx=0;xx<2;++xx) {
        float3 v=D.Load(int3(clamp(base+int2(xx,yy),0,hi),0)).rgb;low=min(low,v);high=max(high,v);
    }
    return clamp(sum,low,high);
}
[numthreads(8,8,1)]void Prepare(uint3 id:SV_DispatchThreadID) {
    if(any(id.xy>=ModelSize))return;
    float2 scale=float2(Size)/float2(ModelSize);
    if(all(Size==ModelSize)) {
        float4 v=A.Load(int3(id.xy,0));FloatOut[id.xy]=float4(proxy(v.rgb),v.a);return;
    }
    // Proxy before area averaging, as in the host high-precision contract.
    float2 lo=float2(id.xy)*scale,hi=(float2(id.xy)+1)*scale;
    float4 sum=0;float weight=0;
    for(int y=int(floor(lo.y));y<min(int(Size.y),int(ceil(hi.y)));++y)
        for(int x=int(floor(lo.x));x<min(int(Size.x),int(ceil(hi.x)));++x) {
            float w=max(0,min(hi.x,x+1.0)-max(lo.x,float(x)))*max(0,min(hi.y,y+1.0)-max(lo.y,float(y)));
            float4 v=A.Load(int3(x,y,0));sum+=float4(proxy(v.rgb),v.a)*w;weight+=w;
        }
    FloatOut[id.xy]=sum/max(weight,1e-12);
}
[numthreads(8,8,1)]void Correction(uint3 id:SV_DispatchThreadID) {
    if(any(id.xy>=ModelSize))return;
    FloatOut[id.xy]=float4(dec(saturate(A.Load(int3(id.xy,0)).rgb))-B.Load(int3(id.xy,0)).rgb,0);
}
[numthreads(8,8,1)]void Stabilize(uint3 id:SV_DispatchThreadID) {
    if(any(id.xy>=ModelSize))return;
    float3 delta=A.Load(int3(id.xy,0)).rgb;
    if(Reset!=0||Stability<=0){FloatOut[id.xy]=float4(delta,0);return;}
    float2 p=float2(id.xy)+Motion.Load(int3(id.xy,0));
    if(any(p<0)||any(p>float2(ModelSize)-1)||!all(isfinite(p))){FloatOut[id.xy]=float4(delta,0);return;}
    float nowY=0,oldY=0;float3 now[4],before[4];
    [unroll]for(int k=0;k<4;++k) {
        float2 o=float2(k&1,k>>1)-.5;
        now[k]=bilinear(B,float2(id.xy)+o,int2(ModelSize)).rgb;
        before[k]=bilinear(C,p+o,int2(ModelSize)).rgb;
        nowY+=luma(now[k]);oldY+=luma(before[k]);
    }
    float gain=clamp((nowY+1e-5)/(oldY+1e-5),.5,2.0),error=0;
    [unroll]for(int j=0;j<4;++j){float3 diff=abs(now[j]-before[j]*gain);error+=max(diff.r,max(diff.g,diff.b))*.25;}
    float trust=1-smoothstep(.025,.12,error);
    FloatOut[id.xy]=float4(lerp(delta,history(p)*gain,HistoryWeight*trust),0);
}
float skinWeight(float3 v) {
    float3 c=saturate(enc(v));float y=dot(c,float3(.299,.587,.114));
    float cb=.5+dot(c,float3(-.168736,-.331264,.5));float cr=.5+dot(c,float3(.5,-.418688,-.081312));
    return smoothstep(.30,.34,cb)*(1-smoothstep(.48,.52,cb))*smoothstep(.51,.55,cr)*(1-smoothstep(.68,.73,cr))*smoothstep(.03,.10,y);
}
float3 encodeSignal(float3 v) {
    v=max(v,0);
    if(Transfer==1) {
        float3 p=pow(min(v*203.0/10000,1),2610.0/16384);
        return saturate(pow((3424.0/4096+2413.0/128*p)/(1+2392.0/128*p),2523.0/32));
    }
    if(Transfer==2) {
        float3 s=v*.26496256;
        return saturate(lerp(.17883277*log(max(12*s-.28466892,1e-12))+.55991073,sqrt(3*s),step(s,1.0/12)));
    }
    return saturate(enc(v));
}
[numthreads(8,8,1)]void Compose(uint3 id:SV_DispatchThreadID) {
    if(any(id.xy>=Size))return;
    float4 original=A.Load(int3(id.xy,0));float3 result=original.rgb;
    if(Detail!=0) {
        float2 at=(float2(id.xy)+.5)*float2(ModelSize)/float2(Size)-.5;
        float3 delta=bilinear(B,at,int2(ModelSize)).rgb;
        if(Transfer!=0)delta=from709(delta)*min(4,1+max(0,sourceLuma(original.rgb)));
        float protect=0;
        if(Protect.z>0&&Protect.w>0) {
            float2 pos=(float2(id.xy)+.5)/float2(Size);
            float2 distance=max(Protect.xy-pos,pos-Protect.xy-Protect.zw);
            protect=1-smoothstep(0,.0125,max(0,max(distance.x,distance.y)));
        }
        protect=max(protect,Skin*skinWeight(proxy(original.rgb)));
        protect=max(protect,Highlights*smoothstep(Transfer!=0?1:.65,Transfer!=0?3:.95,sourceLuma(original.rgb)));
        float dy=sourceLuma(delta),oy=max(1e-5,sourceLuma(original.rgb));
        result=max(0,original.rgb+Detail*(1-saturate(protect))*lerp(original.rgb*dy/oy,delta,Colour));
    }
    float4 encoded=float4(encodeSignal(result),saturate(original.a));
    PackedOut[id.xy]=uint4(floor(encoded*(Sixteen!=0?65535.0:255.0)+.5));
}
)NRQ";
