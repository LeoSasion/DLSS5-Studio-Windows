"""Reproducible same-PTS video diagnostics; no third-party model is downloaded.

Global-translation compensation is a diagnostic proxy, not dense optical flow
or a perceptual quality score. Source motion/grain and scene cuts are separated
from the signed enhancement correction so lower change is not called better NR.
"""
from __future__ import annotations
import argparse
import bisect
import json
import math
from pathlib import Path
import subprocess
import tempfile
import time

import numpy as np

import nr_video
from video_pipeline import atomic_json, source_timeline, sdr_proxy_filter


def rgb_frames(path, width, height, count, start=0):
    """Bound memory to a decoded frame and keep FFmpeg errors out of the pipe."""
    ffmpeg=nr_video.find_tool("ffmpeg")
    info=nr_video.probe(nr_video.find_tool("ffprobe"),str(path))
    conversion=sdr_proxy_filter(info,rgb_input=info.get("matrix")=="gbr")
    filters=([conversion] if conversion else [])+[f"scale={width}:{height}:flags=area","format=rgb24"]
    command=[ffmpeg,"-v","error","-ss",f"{max(0,start-.000001):.6f}","-i",str(path),"-map",f"0:v:{info.get('video_ordinal',0)}",
             "-frames:v",str(count),"-vf",",".join(filters),"-fps_mode","passthrough","-f","rawvideo","-"]
    with tempfile.TemporaryFile() as errors:
        process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=errors,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        try:
            size=width*height*3
            for _ in range(count):
                data=process.stdout.read(size)
                if not data:break
                if len(data)!=size:raise RuntimeError("诊断解码得到不完整帧。")
                yield np.frombuffer(data,np.uint8).reshape(height,width,3).astype(np.float32)/255
            if process.wait(timeout=30):
                errors.seek(0)
                raise RuntimeError(errors.read().decode("utf-8","replace")[-3000:])
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.kill();process.wait()


def luma(rgb):
    return np.sum(rgb*np.array([.2126,.7152,.0722],np.float32),axis=-1)


def global_shift(previous,current):
    """Integer translation from a phase-correlation peak, bounded to 1/8 frame."""
    height,width=current.shape
    window=np.outer(np.hanning(height),np.hanning(width))
    a=np.fft.rfft2((previous-previous.mean())*window)
    b=np.fft.rfft2((current-current.mean())*window)
    cross=b*np.conjugate(a)
    magnitude=np.abs(cross)
    if float(magnitude.max())<1e-8:return 0,0
    correlation=np.fft.irfft2(cross/np.maximum(magnitude,1e-12),s=current.shape)
    y,x=np.unravel_index(np.argmax(correlation),current.shape)
    if y>height//2:y-=height
    if x>width//2:x-=width
    if abs(x)>width//8 or abs(y)>height//8:return 0,0
    return int(y),int(x)


def aligned(previous,current,shift):
    dy,dx=shift
    height,width=current.shape[:2]
    y0,y1=max(0,dy),min(height,height+dy)
    x0,x1=max(0,dx),min(width,width+dx)
    return previous[y0-dy:y1-dy,x0-dx:x1-dx],current[y0:y1,x0:x1]


def global_ssim(a,b):
    ma,mb=float(a.mean()),float(b.mean())
    va,vb=float(a.var()),float(b.var())
    covariance=float(np.mean((a-ma)*(b-mb)))
    return ((2*ma*mb+.01**2)*(2*covariance+.03**2))/((ma*ma+mb*mb+.01**2)*(va+vb+.03**2))


def analyze(source,result,source_start=0,max_frames=120,width=320):
    if not 2<=max_frames<=2000 or not 64<=width<=1024:
        raise ValueError("诊断帧数应为2–2000，采样宽度64–1024。")
    started=time.monotonic()
    ffprobe=nr_video.find_tool("ffprobe")
    source_info=nr_video.probe(ffprobe,str(source));result_info=nr_video.probe(ffprobe,str(result))
    source_pts=source_timeline(ffprobe,str(source),source_info)
    result_pts=source_timeline(ffprobe,str(result),result_info)
    selected=result_pts["pts"][:max_frames]
    height=max(2,round(width*source_info["h"]/source_info["w"]))
    targets=[]
    for stamp in selected:
        wanted=source_start+stamp
        index=bisect.bisect_left(source_pts["pts"],wanted)
        if index==len(source_pts["pts"]):index-=1
        if index and abs(source_pts["pts"][index-1]-wanted)<abs(source_pts["pts"][index]-wanted):index-=1
        targets.append(index)
    first,last=min(targets),max(targets)
    wanted_set=set(targets)
    originals={}
    for offset,frame in enumerate(rgb_frames(source,width,height,last-first+1,source_pts["pts"][first])):
        index=first+offset
        if index in wanted_set:originals[index]=frame
    square,values,ssim,colour,brightness=0.,0,[],[],[]
    source_temporal,output_temporal,delta_temporal=[],[],[]
    details=[];previous=None;cuts=0
    for index,enhanced in enumerate(rgb_frames(result,width,height,len(selected))):
        original=originals.get(targets[index])
        if original is None:continue
        correction=enhanced-original
        mse=float(np.mean(correction**2));square+=float(np.sum(correction**2));values+=correction.size
        original_y,enhanced_y=luma(original),luma(enhanced)
        ssim.append(global_ssim(original_y,enhanced_y))
        colour.append(np.mean(correction,axis=(0,1)).tolist())
        brightness.append(float(np.mean(enhanced_y-original_y)))
        if previous is not None:
            old_original,old_enhanced,old_correction=previous
            old_y=luma(old_original)
            if float(np.mean(np.abs(original_y-old_y)))>.24:
                cuts+=1
            else:
                shift=global_shift(old_y,original_y)
                a,b=aligned(old_y,original_y,shift);source_temporal.append(float(np.mean(np.abs(b-a))))
                a,b=aligned(luma(old_enhanced),enhanced_y,shift);output_temporal.append(float(np.mean(np.abs(b-a))))
                a,b=aligned(luma(old_correction),luma(correction),shift);delta_temporal.append(float(np.mean(np.abs(b-a))))
        previous=original,enhanced,correction
        details.append({"source_time":source_pts["pts"][targets[index]],"result_time":selected[index],
                        "pts_error_ms":round(abs(source_pts["pts"][targets[index]]-source_start-selected[index])*1000,6),
                        "psnr_db":None if mse==0 else -10*math.log10(mse),"exact_pixels":mse==0})
    mse=square/values if values else None
    mean=lambda seq:float(np.mean(seq)) if seq else None
    return {"version":1,"source":str(Path(source).resolve()),"result":str(Path(result).resolve()),
            "source_start":source_start,"matched_frames":len(details),"sample_geometry":[width,height],
            "source_frames":len(source_pts["pts"]),"result_frames":len(result_pts["pts"]),
            "source_duration":source_pts["end"],"result_duration":result_pts["end"],
            "psnr_db":None if not mse else -10*math.log10(mse),"exact_pixels":mse==0,
            "global_luma_ssim":mean(ssim),"mean_rgb_shift":np.mean(colour,axis=0).tolist() if colour else None,
            "mean_luma_shift":mean(brightness),"source_temporal_error":mean(source_temporal),
            "result_temporal_error":mean(output_temporal),"correction_temporal_error":mean(delta_temporal),
            "added_temporal_error":mean(output_temporal)-mean(source_temporal) if source_temporal else None,
            "scene_cuts_excluded":cuts,"motion_model":"global integer translation phase correlation",
            "limitations":"Diagnostic proxies, not dense motion compensation, local-window SSIM, or a perceptual quality ranking. HDR is measured after the same explicit SDR display transform.",
            "analysis_wall_ms":(time.monotonic()-started)*1000,"frames":details}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source",required=True);parser.add_argument("--result",required=True)
    parser.add_argument("--source-start",type=float,default=0)
    parser.add_argument("--frames",type=int,default=120);parser.add_argument("--width",type=int,default=320)
    parser.add_argument("--out",default="")
    args=parser.parse_args()
    report=analyze(args.source,args.result,args.source_start,args.frames,args.width)
    if args.out:atomic_json(args.out,report)
    print(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False))


if __name__=="__main__":main()
