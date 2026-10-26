# DLSS Studio native renderer protocol 2

The Studio invokes this renderer directly for images and through `app/nr_video.py` for video. Query `video2dlssnr.exe --nr-capabilities` before using optional protocol features. A matched DLL is not proof that NGX evaluation works on a particular GPU or driver.

Build the renderer and its matching forwarder with:

```powershell
app/.venv/Scripts/python.exe -X utf8 -B packaging/build_engine.py --tests
```

The build script uses the extracted Microsoft compiler/SDK and NVIDIA import library prepared by `packaging/bootstrap_compiler.py`; it does not require a system Visual Studio installation. Outputs go to `source/out`, without overwriting the application runtime. Keep the forwarder and executable from the same validated build.

## NR model preset typing (2026-10-27)

The existing `--nr-preset` selects the NR creation hint; no additional UI or CLI is required. For the identified 310.9.16 runtimes, 1 selects legacy model A and 2 selects the lightweight model B; 0 leaves selection automatic. This is independent of appearance style and the internal SR preset. Changing the hint requires a new feature, and the effective hint belongs in model/cache identity.

NR creation writes `DLSSNR.Hint.Render.Preset` as an I32. The driver-core capability block has a separately negotiated signed setter: the host only considers its known 32-bit integer slots 3/4 after identifying UInt and float, excludes those identified slots and the ULL slot, and round-trips a positive integer with GetI. It does not scan possible float/double/resource/pointer setters. An unknown layout is not guessed: the legacy automatic preset can retain its old UInt route, while an explicit preset fails if the new host cannot establish an I32 setter.

The forwarder retains the two-argument `fwd_set_slots` export and the `NrModelParams` POD layout. The optional `fwd_set_int_slot(int)` extension configures signed dispatch; older hosts keep their previous route. Image, default video and high-precision video configure it through the same loader. The executable and forwarder should be deployed together. This source change does not rewrite GPU architecture, alter model binaries or promise compatibility with a new runtime/driver; no rendering or tests were run for this typing-only patch.

## Video pixels and color

- `--nr-pixel-format rgba`: interleaved unsigned 8-bit RGBA, the default.
- `--nr-pixel-format rgba64le`: interleaved unsigned 16-bit little-endian RGBA. Input and output use the same format and transfer.
- `--nr-transfer sdr|pq|hlg`: HDR requires `rgba64le`. `sdr` retains the existing sRGB approximation; it is not a claim of fully calibrated SDR mastering.
- `--nr-primaries auto|bt709|bt2020|smpte432`: HDR RGB primaries. `auto` uses BT.2020 for HDR. `smpte432` means P3-D65. Other HDR primaries need an explicitly supported conversion or a clear unsupported-format response in the caller.
- `--nr-fps <rate>`: media rate used by the temporal response, default 30. The caller owns source PTS and output time mapping; the native raw stream itself carries no timestamps.

PQ is converted to linear values relative to 203 cd/m² diffuse white. HLG uses scene-relative inverse OETF with code value 0.75 as diffuse white; no display-specific HLG OOTF is inferred. The model sees a fixed-exposure SDR proxy. HDR RGB is transformed to BT.709 for that proxy, with signed model corrections transformed back to the source gamut and added to the original linear anchor. The original HDR anchor is never round-tripped through a quantized SDR or different-gamut video file.

The P3-D65 matrices were derived by multiplying the linear RGB/XYZ rational matrices in the [W3C CSS Color 4 conversion reference](https://www.w3.org/TR/css-color-4/#color-conversion-code). PQ/HLG definitions follow BT.2100 transfer conventions. These transforms do not turn SDR input into a mastered HDR source.

## Postprocessing controls

- `--nr-stability 0..1`: stabilize signed enhancement corrections, default 0. History is motion compensated and rejected on source mismatch, off-frame motion or scene cuts. It does not average the original image.
- `--nr-protect x,y,width,height`: normalized original-image protection rectangle. `--nr-protect-region` is an alias. The inside is fully protected, with a narrow outer feather.
- `--nr-protect-skin 0..1`: conservative color-based skin protection. This is not semantic face recognition or identity restoration.
- `--nr-protect-highlights 0..1`: reduce enhancement corrections in source highlights.
- `--nr-render-scale .5..1`: scale only the NR proxy and motion guides, then reconstruct corrections onto the full-resolution anchor. Output dimensions and original high-frequency texture are retained. Default 1 does not reduce inference resolution.

The existing model controls keep their previous ranges, including 0–200% controls and 0–100% color blending. `--nr-detail` and `--nr-color` are output composition controls; they do not change the model identity.

The unchanged SDR/default route keeps GPU composition. High precision, HDR, protection, temporal stability and model-stage replay use the tested host composition route. Its additional CPU and readback costs are visible in metrics; it is not advertised as a universally faster path.

## Bounded stage reuse

Images: `--nr-cache-dir <directory>` alongside `--nr-run` writes a `.nrstage` with the full-resolution linear anchor and native-resolution signed model correction. The complete stage is limited to 256 MiB. `--nr-recompose <stage> --out <directory>` applies detail, color and protection without initializing the GPU or NGX. Malformed, truncated, unsupported or oversized stages are rejected. Image stages are linear SDR.

Videos: pass all of `--nr-cache-dir <directory> --nr-cache-frames <N> --nr-cache-key <identity>`. The identity must cover source content, decoded geometry, the exact segment including warmup/reset policy, actual runtime/driver/model identity and native model/motion settings. It excludes detail/color/protection/stability and final encoding controls.

The native header also binds model parameters, dimensions, motion, SR preset, transfer, raw format and primaries. Every frame binds the source proxy fingerprint and the stored NR payload checksum. Only complete model stages whose estimated size is at most 1 GiB are enabled. Native half-float NR output is stored; original full-resolution floating frames are not duplicated in this video cache. The caller still decodes the source and reconstructs the anchor and flow on replay.

The final marker is an atomic rename after every expected frame has completed. Incomplete stages are never replayed. Oversized stages fall back to normal processing. An identity or payload mismatch removes the invalid complete cache and returns an explicit `NR cache identity mismatch` failure. The caller can retry once without that cache. The native fingerprints detect mismatches and accidental corruption; the caller's SHA-based identity supplies the stronger content/runtime attribution.

## Timing and ownership

`NRMETRIC {JSON}` reports version, frame/reset counts, initialization and wall time, synchronized stage wall times and available GPU timestamp durations. Model-cache replay reports `cached_nr_frames` and zero `stage_ms.nr`/`gpu_stage_ms.nr`. Image recomposition reports `cache: "recompose"`.

`NRTRANSFER {JSON}` records upload/readback buffer allocation counts and fence waits. Upload and readback staging resources are reused only after the synchronous transfer fence has completed. Default GPU composition and the following readback copy share a submission; allocator and descriptor lifetime still extends to the completed fence. Pipeline stage wall durations are diagnostic components and can overlap threaded I/O; do not add them to infer total elapsed time.

Reader and writer queues are bounded by frame memory. Truncated input, broken output or evaluation exceptions close queues and cancel synchronous pipe I/O before joining threads. Application-level cancellation and process ownership remain the responsibility of `ProcessGroup`.

## Validation

`tests/nr_post_tests.cpp` checks PQ reference points, HLG diffuse white, transfer round trips, more than 256 retained levels, P3 matrix reversibility, full-resolution anchor preservation, alpha, protection and temporal motion/disocclusion behavior. `tests/test_main.cpp` covers CLI validation and GPU resource/transfer behavior, including buffer reuse and one-fence producer/readback submission.

Integration evidence is under `outputs/core-optimization-validation` and `outputs/optimization-video-2026-10-26` in the task workspace. GPU support and performance claims remain specific to the tested hardware, runtime, sample and settings; no cross-GPU or blanket 4K speedup is implied.
