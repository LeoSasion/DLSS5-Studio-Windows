"""Task reuse, queue, checkpoint and ownership contracts with real files."""
import io
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from fastapi.testclient import TestClient
from PIL import Image
import studio_cache
import studio_server as server
from gpu_runtime import Selection


class JobCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dlss-job-cache-")
        self.root = Path(self.temp.name)
        self.uploads, self.outputs = self.root / "uploads", self.root / "ui_out"
        self.uploads.mkdir(); self.outputs.mkdir()
        self.selection = Selection(2, "Fixture RTX", "fixture", "rtx50", self.root / "dll", "abc")
        self.identity = {"digest": "fixture-runtime", "driver": [{"name": "driver", "version": "one"}], "model": {"digest": "one"}}
        values = dict(ROOT=self.root, UPLOADS=self.uploads, OUTPUTS=self.outputs,
                      STATE_FILE=self.root / "studio-state.json", assets={}, jobs={}, runners={},
                      cancellation={}, uploading_ids=set(), shutdown_requested=threading.Event(),
                      source_preview_queued=set(), source_preview_runners={}, source_preview_cancellation={})
        self.patches = [patch.object(server, key, value) for key, value in values.items()]
        self.patches += [patch.object(server.GPU, "prepare", return_value=self.selection),
                         patch.object(server.GPU, "identity", return_value=self.identity),
                         patch.object(server.GPU, "mark_verified"),
                         patch.object(server.GPU, "mark_continuous_verified")]
        for item in self.patches:
            item.start()
        self.client = TestClient(server.api)

    def tearDown(self):
        self.client.close()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def upload(self):
        data = io.BytesIO()
        Image.new("RGB", (80, 60), "navy").save(data, "PNG")
        response = self.client.post("/api/upload", files={"file": ("photo.png", data.getvalue(), "image/png")})
        self.assertEqual(response.status_code, 200, response.text)
        return server.assets[response.json()["id"]]

    def submit(self, item, **settings):
        with patch.object(server.worker, "submit"):
            response = self.client.post("/api/jobs", json={"asset_id": item["id"], **settings})
        self.assertEqual(response.status_code, 200, response.text)
        return server.jobs[response.json()["id"]]

    def render_image(self, job, expected_stage=False):
        event = server.cancellation[job["id"]]
        calls = []
        def start(command, **kwargs):
            calls.append(command)
            folder = Path(command[command.index("--out") + 1])
            def wait():
                Image.new("RGB", (160, 120), "green").save(folder / "source_nr.png")
                return 0
            return SimpleNamespace(stdout=io.StringIO("done\n"), wait=wait)
        group = SimpleNamespace(start=start, check=lambda: None, close=Mock())
        with patch.object(server, "ProcessGroup", return_value=group):
            server.run_queued(job["id"])
        self.assertEqual(job["status"], "done", job)
        return calls

    def test_reuploaded_same_pixels_share_identity_but_request_ids_do_not(self):
        a, b = self.upload(), self.upload()
        sa = server.Settings(asset_id=a["id"], request_id="one")
        sb = server.Settings(asset_id=b["id"], request_id="two")
        self.assertEqual(studio_cache.render_keys(sa, a, self.identity), studio_cache.render_keys(sb, b, self.identity))
        sb.intensity = .5
        self.assertNotEqual(studio_cache.render_keys(sa, a, self.identity), studio_cache.render_keys(sb, b, self.identity))

    def test_neural_cache_ignores_encoding_but_not_timing_or_warmup(self):
        item = self.upload(); item.update(kind="video", duration=60)
        a = server.Settings(asset_id=item["id"], bitrate=6000, bitrate_mode="manual")
        b = a.model_copy(update={"bitrate": 9000, "container": "mkv", "audio": "none"})
        ka, na = studio_cache.render_keys(a, item, self.identity)
        kb, nb = studio_cache.render_keys(b, item, self.identity)
        self.assertNotEqual(ka, kb); self.assertEqual(na, nb)
        for setting in ({"warmup_frames": 32}, {"timing_mode": "cfr"}, {"segment_seconds": 5}):
            self.assertNotEqual(na, studio_cache.render_keys(a.model_copy(update=setting), item, self.identity)[1])

    def test_picture_composite_controls_reuse_model_stage(self):
        item = self.upload(); a = server.Settings(asset_id=item["id"])
        b = a.model_copy(update={"detail": .7, "color": .8, "protect_region": [.1, .1, .2, .2]})
        ka, na = studio_cache.render_keys(a, item, self.identity)
        kb, nb = studio_cache.render_keys(b, item, self.identity)
        self.assertNotEqual(ka, kb); self.assertEqual(na, nb)
        changed = {**self.identity, "driver": "two"}
        self.assertNotEqual(na, studio_cache.render_keys(a, item, changed)[1])

    def test_video_model_stage_separates_controls_from_temporal_composite(self):
        item = self.upload(); item.update(kind="video", duration=60)
        a = server.Settings(asset_id=item["id"])
        b = a.model_copy(update={"detail": .7, "color": .8, "stability": .5,
                                 "protect_region": [.1, .1, .2, .2], "protect_skin": .7})
        self.assertNotEqual(studio_cache.render_keys(a, item, self.identity)[1],
                            studio_cache.render_keys(b, item, self.identity)[1])
        self.assertEqual(studio_cache.model_stage_key(a, item, self.identity),
                         studio_cache.model_stage_key(b, item, self.identity))
        changed = a.model_copy(update={"intensity": .5})
        self.assertNotEqual(studio_cache.model_stage_key(a, item, self.identity),
                            studio_cache.model_stage_key(changed, item, self.identity))

    def test_new_exact_job_has_private_result_and_old_cleanup_is_safe(self):
        item = self.upload(); first = self.submit(item)
        self.assertEqual(len(self.render_image(first)), 1)
        second = self.submit(item, request_id="another")
        with patch.object(server, "ProcessGroup") as group:
            server.run_queued(second["id"])
            group.assert_not_called()
        self.assertTrue(second["cache_hit"])
        self.assertEqual(second["reused_from"], first["id"])
        self.assertNotEqual(first["result_path"], second["result_path"])
        expected = Path(second["result_path"]).read_bytes()
        self.client.post("/api/cache/cleanup", json={"include_results": True, "keep_job_ids": [second["id"]]})
        self.assertFalse(Path(first["result_path"]).exists())
        self.assertEqual(Path(second["result_path"]).read_bytes(), expected)

    def test_corrupt_cached_result_is_not_reused(self):
        item = self.upload(); first = self.submit(item); self.render_image(first)
        Path(first["result_path"]).write_bytes(b"changed")
        second = self.submit(item, request_id="another")
        self.assertEqual(len(self.render_image(second)), 1)
        self.assertFalse(second["cache_hit"])

    def test_queue_accepts_other_work_and_reports_order(self):
        item = self.upload()
        first = self.submit(item, intensity=.5)
        second = self.submit(item, intensity=.7)
        self.assertEqual(self.client.get(f"/api/jobs/{first['id']}").json()["queue_position"], 1)
        self.assertEqual(self.client.get(f"/api/jobs/{second['id']}").json()["queue_position"], 2)
        self.assertEqual(self.client.post(f"/api/jobs/{second['id']}/pause").json()["status"], "paused")
        with patch.object(server.worker, "submit") as enqueue:
            self.assertEqual(self.client.post(f"/api/jobs/{second['id']}/resume").json()["status"], "queued")
            enqueue.assert_called_once()
        self.assertEqual(self.client.post(f"/api/jobs/{second['id']}/cancel").json()["status"], "cancelled")
        with patch.object(server, "ProcessGroup") as process:
            server.run_queued(second["id"])
            process.assert_not_called()

    def test_real_serial_executor_runs_jobs_in_submission_order(self):
        item = self.upload(); entered = threading.Event(); release = threading.Event(); order = []
        def render(jid, settings, asset, selection):
            order.append(jid); server.jobs[jid]["status"] = "running"
            if len(order) == 1:
                entered.set(); release.wait(3)
            server.jobs[jid]["status"] = "done"
        with ThreadPoolExecutor(max_workers=1) as worker, patch.object(server, "worker", worker), patch.object(server, "render", side_effect=render):
            first = self.client.post("/api/jobs", json={"asset_id": item["id"], "intensity": .5}).json()["id"]
            self.assertTrue(entered.wait(2))
            second = self.client.post("/api/jobs", json={"asset_id": item["id"], "intensity": .7}).json()["id"]
            self.assertEqual(order, [first])
            self.assertEqual(server.jobs[second]["status"], "queued")
            release.set()
            worker.submit(lambda: None).result(timeout=3)
        self.assertEqual(order, [first, second])

    def test_restart_preserves_queue_and_pauses_interrupted_video(self):
        item = self.upload(); item.update(kind="video", duration=30)
        waiting = self.submit(item, intensity=.5)
        running = self.submit(item, intensity=.7); running["status"] = "running"
        server.persist()
        _, recovered = server.studio_storage.load(server.STATE_FILE, self.root)
        self.assertEqual(recovered[waiting["id"]]["status"], "queued")
        self.assertEqual(recovered[running["id"]]["status"], "paused")
        self.assertTrue(recovered[running["id"]]["resumable"])

    def test_video_pause_is_cooperative_and_checkpoint_survives_cleanup(self):
        item = self.upload(); item.update(kind="video", duration=30)
        job = self.submit(item); job["status"] = "running"
        work = self.outputs / job["id"] / "work"; work.mkdir(parents=True)
        (work / "part.mkv").write_bytes(b"completed segment")
        (work / "checkpoint.json").write_text(json.dumps({"render_key": job["nr_key"], "segments": [
            {"path": "part.mkv", "start": 0, "end": 10, "source_start": 0, "source_end": 10, "frames": 240, "completed": True}]}))
        paused = self.client.post(f"/api/jobs/{job['id']}/pause").json()
        self.assertEqual(paused["status"], "pausing")
        self.assertTrue((self.outputs / job["id"] / "pause.request").is_file())
        job["status"] = "paused"
        self.client.post("/api/cache/cleanup", json={"include_results": True})
        self.assertTrue((work / "part.mkv").is_file())
        self.assertEqual(self.client.get(f"/api/jobs/{job['id']}").json()["coverage"][0]["frames"], 240)

    def test_pause_before_first_numeric_progress_remains_resumable(self):
        item = self.upload(); item.update(kind="video", duration=1, fps=12)
        job = self.submit(item); job["progress"] = None
        process = SimpleNamespace(stdout=io.StringIO('NRSTATE {"status":"paused","completed_frames":0}\n'), wait=lambda: 75)
        group = SimpleNamespace(start=Mock(return_value=process), check=lambda: None, close=Mock())
        with patch.object(server, "ProcessGroup", return_value=group):
            server.run_queued(job["id"])
        result = self.client.get(f"/api/jobs/{job['id']}").json()
        self.assertEqual(result["status"], "paused", result)
        self.assertEqual(result["progress"], 0)
        self.assertTrue(result["resumable"])

    def test_pause_resume_worker_with_finished_history_and_null_timestamps(self):
        item = self.upload(); done = self.submit(item); self.render_image(done)
        second = self.submit(item, intensity=.7)
        self.assertEqual(self.client.post(f"/api/jobs/{second['id']}/pause").json()["status"], "paused")
        with patch.object(server.worker, "submit"):
            response = self.client.post(f"/api/jobs/{second['id']}/resume")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(second["finished_at"])
        self.assertEqual(len(self.render_image(second)), 1)
        self.assertEqual(second["status"], "done")
        # Old/null metadata must also remain safe for history and cache views.
        second["created_at"] = None
        self.assertEqual(self.client.get("/api/jobs").status_code, 200)
        self.assertEqual(self.client.get("/api/diagnostics").status_code, 200)

    def test_checkpoint_clone_validates_hashes_and_owns_new_files(self):
        source, target = self.root / "one", self.root / "two"
        source.mkdir(); payload = source / "segments/part.mkv"; payload.parent.mkdir(); payload.write_bytes(b"valid")
        checkpoint = {"render_key": "internal", "application_render_key": "expected", "segments": [
            {"path": "segments/part.mkv", "start": 0, "end": 1, "completed": True, "sha256": studio_cache.digest_file(payload)}]}
        (source / "checkpoint.json").write_text(json.dumps(checkpoint))
        self.assertFalse(studio_cache.clone_work(source, target, "wrong"))
        self.assertTrue(studio_cache.clone_work(source, target, "expected"))
        payload.unlink()
        self.assertEqual((target / "segments/part.mkv").read_bytes(), b"valid")

    def test_timeline_endpoint_returns_nearest_bounded_pts_window(self):
        item = self.upload(); item.update(kind="video", duration=10)
        job = self.submit(item)
        work = self.outputs / job["id"] / "work"; work.mkdir(parents=True)
        (work / "frame-map.json").write_text(json.dumps({"source_times": [5, 5.04, 5.11, 5.15],
            "result_times": [0, .04, .11, .15], "actual_start": 5, "timing": "preserve"}))
        response = self.client.get(f"/api/jobs/{job['id']}/timeline", params={"time": 5.105, "count": 1}).json()
        self.assertEqual(response["frames"], [{"index": 2, "source_time": 5.11, "result_time": .11}])
        self.assertTrue(response["exact"])

    def test_diagnostics_are_path_free_and_self_test_is_a_real_uncached_video_job(self):
        item = self.upload(); job = self.submit(item)
        job.update(metrics={"render_wall_ms": 12}, validation={"passed": True})
        diagnostic = self.client.get("/api/diagnostics").json()
        self.assertEqual(diagnostic["recent_jobs"][0]["metrics"]["render_wall_ms"], 12)
        self.assertNotIn(str(self.root), json.dumps(diagnostic))
        def fixture(folder, ffmpeg, probe):
            folder.mkdir(); path = folder / "source.mp4"; path.write_bytes(b"fixture")
            asset = dict(id=folder.name, name="test.mp4", kind="video", path=str(path), url=f"/api/assets/{folder.name}",
                         width=320, height=180, duration=1, fps=12, bytes=7, diagnostic=True, created_at=time.time())
            studio_cache.source_fingerprint(asset)
            return asset
        with patch.object(server.studio_diagnostics, "create_self_test_asset", side_effect=fixture), patch.object(server.worker, "submit"):
            result = self.client.post("/api/diagnostics/self-test")
        self.assertEqual(result.status_code, 200, result.text)
        created = server.jobs[result.json()["id"]]
        self.assertTrue(created["diagnostic"])
        self.assertTrue(created["resume_supported"])
        self.assertEqual(created["settings"]["codec"], "h264_cpu")

    def test_hdr_source_proxy_is_queued_without_blocking_upload_and_preserves_raw_asset(self):
        info = dict(w=320, h=180, duration=1, fps=12, frames=12, trc="smpte2084", primaries="bt2020", video_codec="hevc")
        with patch.object(server.nr_video, "probe", return_value=info), patch.object(server.preview_worker, "submit") as submit:
            started = time.monotonic()
            response = self.client.post("/api/upload", files={"file": ("hdr.mp4", b"high precision source", "video/mp4")})
        self.assertEqual(response.status_code, 200, response.text)
        item = response.json(); aid = item["id"]
        self.assertLess(time.monotonic() - started, .5)
        submit.assert_called_once()
        self.assertEqual(item["preview_status"], "preparing"); self.assertIsNone(item["preview_url"])
        server.assets[aid]["created_at"] = 0
        self.client.post("/api/cache/cleanup", json={"include_results": True})
        raw = Path(server.assets[aid]["path"])
        self.assertEqual(raw.read_bytes(), b"high precision source")
        def build(asset, uploads, runner, nvenc, progress):
            proxy = uploads / aid / "source_preview.mp4"; proxy.write_bytes(b"SDR display proxy")
            return dict(preview_status="ready", source_preview_path=str(proxy), preview_url=f"/api/assets/{aid}/preview")
        group = SimpleNamespace(check=lambda: None, close=Mock())
        with patch.object(server, "ProcessGroup", return_value=group), patch.object(server.studio_previews, "build", side_effect=build):
            server.prepare_source_preview(aid)
        ready = self.client.get(f"/api/assets/{aid}/info").json()
        self.assertEqual(ready["preview_status"], "ready"); self.assertNotIn("source_preview_path", ready)
        self.assertEqual(self.client.get(ready["preview_url"]).content, b"SDR display proxy")
        self.assertEqual(self.client.get(ready["url"]).content, b"high precision source")
        recovered_assets, _ = server.studio_storage.load(server.STATE_FILE, self.root)
        self.assertEqual(Path(recovered_assets[aid]["source_preview_path"]).parent.resolve(), raw.parent.resolve())
        group.close.assert_called_once()
        self.assertNotIn(aid, server.source_preview_queued)

    def test_failed_source_preview_can_retry_and_missing_proxy_recovers_as_preparing(self):
        item = self.upload(); item.update(kind="video", duration=1, fps=12, transfer="smpte2084", preview_status="preparing")
        group = SimpleNamespace(check=lambda: None, close=Mock())
        with patch.object(server, "ProcessGroup", return_value=group), patch.object(server.studio_previews, "build", side_effect=RuntimeError("fixture failure")):
            server.prepare_source_preview(item["id"])
        self.assertEqual(item["preview_status"], "failed")
        with patch.object(server.preview_worker, "submit") as submit:
            response = self.client.post(f"/api/assets/{item['id']}/preview")
        self.assertEqual(response.json()["preview_status"], "preparing")
        submit.assert_called_once()
        item.update(preview_status="ready", preview_url=f"/api/assets/{item['id']}/preview",
                    source_preview_path=str(self.uploads / item["id"] / "source_preview.mp4"))
        server.persist()
        recovered, _ = server.studio_storage.load(server.STATE_FILE, self.root)
        self.assertEqual(recovered[item["id"]]["preview_status"], "preparing")
        self.assertIsNone(recovered[item["id"]]["preview_url"])

    def test_source_scan_progress_is_indeterminate_with_real_counts(self):
        hashed = server.progress_values('NRINFO {"phase":"source_hash","bytes":1048576,"total_bytes":2097152}')
        self.assertEqual(hashed["phase"], "analyzing"); self.assertIsNone(hashed["progress"])
        self.assertIn("1.0 / 2.0 MiB", hashed["message"])
        scanned = server.progress_values('NRINFO {"phase":"timeline_scan","frames_scanned":120,"source_seconds":4}')
        self.assertIsNone(scanned["progress"]); self.assertEqual(scanned["source_frames_scanned"], 120)
        self.assertIn("120 帧", scanned["message"])

    def test_video_runtime_validation_requires_real_evaluation_and_profile_survives_exact_reuse(self):
        item = self.upload(); item.update(kind="video", duration=1, fps=12)
        def complete(job, evaluated):
            folder = self.outputs / job["id"]
            def start(command, **kwargs):
                def wait():
                    if "--work-dir" in command:
                        (folder / "result.mp4").write_bytes(b"video fixture")
                        work = folder / "work"; work.mkdir(exist_ok=True)
                        (work / "checkpoint.json").write_text(json.dumps({"segments": [], "total_frames": 12,
                            "metrics": {"nr_evaluated_frames": evaluated, "frames": 12},
                            "output": {"hdr_mode": "sdr", "codec": "h264_cpu"}}))
                    else:
                        Path(command[-1]).write_bytes(b"thumbnail fixture")
                    return 0
                return SimpleNamespace(stdout=io.StringIO("done\n"), wait=wait)
            group = SimpleNamespace(start=start, check=lambda: None, close=Mock())
            with patch.object(server, "ProcessGroup", return_value=group), patch.object(server.nr_video, "probe",
                    return_value={"w": 80, "h": 60, "frames": 12, "trc": "bt709"}):
                server.run_queued(job["id"])
            self.assertEqual(job["status"], "done", job)
        replay = self.submit(item, codec="h264_cpu", bit_depth=8, intensity=.5)
        complete(replay, 0)
        server.GPU.mark_verified.assert_not_called(); server.GPU.mark_continuous_verified.assert_not_called()
        actual = self.submit(item, codec="h264_cpu", bit_depth=8, intensity=.7)
        complete(actual, 12)
        server.GPU.mark_verified.assert_called_once(); server.GPU.mark_continuous_verified.assert_called_once()
        self.assertEqual(actual["output_profile"]["hdr_mode"], "sdr")
        server.GPU.mark_verified.reset_mock(); server.GPU.mark_continuous_verified.reset_mock()
        exact = self.submit(item, codec="h264_cpu", bit_depth=8, intensity=.7)
        with patch.object(server, "ProcessGroup") as process:
            server.run_queued(exact["id"])
            process.assert_not_called()
        self.assertEqual(exact["output_profile"], actual["output_profile"])
        server.GPU.mark_verified.assert_not_called(); server.GPU.mark_continuous_verified.assert_not_called()

    def test_source_preview_restart_requeues_and_shutdown_cancels_owned_work(self):
        item = self.upload(); item.update(kind="video", preview_status="preparing", preview_url=None)
        group = Mock()
        async def lifecycle():
            async with server.lifespan(server.api):
                server.source_preview_runners[item["id"]] = group
                self.assertIn(item["id"], server.source_preview_queued)
        with patch.object(server.preview_worker, "submit") as submit, patch.object(server.preview_worker, "shutdown"), \
                patch.object(server.worker, "shutdown"):
            asyncio.run(lifecycle())
        submit.assert_called_once()
        group.cancel.assert_called_once()
        self.assertTrue(server.source_preview_cancellation[item["id"]].is_set())

    @unittest.skipUnless(os.name == "nt", "Windows cache junction protection")
    def test_resume_and_cache_eviction_never_follow_a_work_junction(self):
        import _winapi
        item = self.upload(); item.update(kind="video", duration=1, fps=12)
        job = self.submit(item); job["status"] = "paused"
        folder = self.outputs / job["id"]; folder.mkdir()
        private = self.root / "private/nr/segment-000000"; private.mkdir(parents=True)
        sentinel = private / "data.bin"; sentinel.write_bytes(b"user data")
        work = folder / "work"
        _winapi.CreateJunction(str(self.root / "private"), str(work))
        try:
            self.assertEqual(self.client.post(f"/api/jobs/{job['id']}/resume").status_code, 409)
            server.trim_video_model_cache()
            self.assertEqual(sentinel.read_bytes(), b"user data")
        finally:
            work.rmdir()


if __name__ == "__main__":
    unittest.main()
