"""Regression tests for upload, job lifecycle, recovery and cache ownership. No GPU required."""
import asyncio
import io
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import httpx
from PIL import Image
from fastapi.testclient import TestClient
import studio_server as server
from gpu_runtime import Selection
from process_runner import RenderCancelled


class StudioContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dlss-contract-")
        self.root = Path(self.temp.name)
        self.uploads, self.outputs = self.root / "uploads", self.root / "ui_out"
        self.uploads.mkdir(); self.outputs.mkdir()
        self.selection = Selection(2, "Fixture RTX", "fixture", "rtx50", self.root / "dll", "abc")
        replacements = dict(ROOT=self.root, UPLOADS=self.uploads, OUTPUTS=self.outputs,
            STATE_FILE=self.root/"studio-state.json", assets={}, jobs={}, runners={}, cancellation={}, uploading_ids=set())
        self.patches = [patch.object(server, key, value) for key, value in replacements.items()]
        self.patches += [patch.object(server.GPU, "prepare", return_value=self.selection),
                         patch.object(server.GPU, "mark_verified")]
        for item in self.patches: item.start()
        self.client = TestClient(server.api)

    def tearDown(self):
        self.client.close()
        for item in reversed(self.patches): item.stop()
        self.temp.cleanup()

    def upload(self):
        data = io.BytesIO(); Image.new("RGB", (80, 60), "navy").save(data, "PNG")
        response = self.client.post("/api/upload", files={"file": ("photo.png", data.getvalue(), "image/png")})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def render_stub(self, jid, item, code=0, cancel=False):
        event = server.cancellation.setdefault(jid, threading.Event())
        if cancel: event.set()
        def wait():
            if not code: Image.new("RGB", (160, 120), "green").save(self.outputs / jid / "source_nr.png")
            return code
        process = SimpleNamespace(stdout=io.StringIO("NGX OutOfDate 0xBAD0000C\n" if code else "done\n"), wait=wait)
        def check():
            if event.is_set(): raise RenderCancelled()
        group = SimpleNamespace(start=Mock(return_value=process), check=check, close=Mock())
        with patch.object(server, "ProcessGroup", return_value=group):
            server.render(jid, server.Settings(asset_id=item["id"]), server.assets[item["id"]], self.selection)
        return group

    def job(self, item):
        jid = uuid.uuid4().hex
        server.jobs[jid] = dict(id=jid, status="queued", asset_id=item["id"], created_at=time.time(), log="")
        return jid

    def test_upload_validation_geometry_and_failed_file_cleanup(self):
        bad = self.client.post("/api/upload", files={"file": ("bad.png", b"not an image", "image/png")})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(list(self.uploads.iterdir()), [])
        item = self.upload()
        self.assertEqual((item["width"], item["height"]), (80, 60))
        self.assertNotIn("path", item)
        self.assertEqual(self.client.get(item["url"]).status_code, 200)
        self.assertEqual(self.client.get(item["url"]+"/info").json()["id"], item["id"])

    def test_reject_invalid_settings_and_unready_download(self):
        item = self.upload()
        for invalid in ({"style":"unknown"}, {"codec":"prores","container":"mp4"}, {"job_kind":"frame"}):
            self.assertEqual(self.client.post("/api/jobs", json={"asset_id":item["id"], **invalid}).status_code, 400)
        self.assertEqual(self.client.get("/api/jobs/missing/download").status_code, 404)

    def test_image_and_video_parameter_mapping(self):
        item = self.upload()
        s = server.Settings(asset_id=item["id"], size="1080p (1920×1080)", style="自然", intensity=.7)
        cmd = server.build_command(s, server.assets[item["id"]], self.outputs, self.selection)
        self.assertEqual(cmd[cmd.index("--nr-width")+1], "1440")
        self.assertEqual(cmd[cmd.index("--nr-height")+1], "1080")
        self.assertEqual(cmd[cmd.index("--nr-style")+1], "1")
        self.assertEqual(cmd[cmd.index("--adapter")+1], "2")
        s = server.Settings(asset_id=item["id"], codec="prores",container="mov",frames=30,audio="none")
        cmd = server.build_command(s, {**server.assets[item["id"]],"kind":"video"}, self.outputs, self.selection)
        for key, value in (("--codec","prores"),("--frames","30"),("--audio","none")):
            self.assertEqual(cmd[cmd.index(key)+1],value)

    def test_clip_bounds_and_command_preserve_source_offset(self):
        item = self.upload()
        server.assets[item['id']].update(kind='video', duration=4, fps=8)
        base = dict(asset_id=item['id'], job_kind='clip', codec='prores', container='mov')
        for values in ({'clip_start':4}, {'clip_start':2,'clip_duration':3}, {'frames':2}, {'clip_duration':0}):
            self.assertIn(self.client.post('/api/jobs', json={**base, **values}).status_code, (400,422))
        with patch.object(server.worker, 'submit'):
            response = self.client.post('/api/jobs', json={**base,'clip_start':1,'clip_duration':2})
        self.assertEqual(response.status_code,200,response.text)
        job = server.jobs[response.json()['id']]
        s = server.Settings(**job['settings'])
        command = server.build_command(s, server.assets[item['id']], self.outputs, self.selection)
        self.assertEqual(command[command.index('--start')+1], '1.0')
        self.assertEqual(command[command.index('--duration')+1], '2.0')
        self.assertEqual(job['clip_start'],1)

    def test_timeline_selection_can_exceed_sixty_seconds_within_source(self):
        item = self.upload()
        server.assets[item['id']].update(kind='video', duration=120, fps=30)
        payload = dict(asset_id=item['id'], job_kind='clip', codec='prores', container='mov', clip_start=20, clip_duration=80)
        self.assertEqual(self.client.post('/api/jobs', json={**payload, 'clip_start':50}).status_code,400)
        with patch.object(server.worker, 'submit'):
            response = self.client.post('/api/jobs', json=payload)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(server.jobs[response.json()['id']]['settings']['clip_duration'],80)

    def test_version_labels_survive_restart_and_sources_group_frames(self):
        item = self.upload(); jid = self.job(item); self.render_stub(jid,item)
        response = self.client.patch(f'/api/jobs/{jid}',json={'name':'  满意版本  ','favorite':True})
        self.assertEqual(response.status_code,200)
        _, saved = server.studio_storage.load(server.STATE_FILE, self.root)
        self.assertEqual(saved[jid]['name'],'满意版本');self.assertTrue(saved[jid]['favorite'])
        self.assertIn('thumbnail_path',saved[jid])
        frame = self.job(item);server.jobs[frame].update(source_asset_id=item['id'],job_kind='frame')
        groups = self.client.get('/api/history/sources').json()['sources']
        self.assertEqual(groups[0]['count'],2)
        self.assertEqual(self.client.get('/api/jobs',params={'source':item['id']}).json()['total'],2)
        self.assertEqual(self.client.patch(f'/api/jobs/{jid}',json={'name':'x'*81,'favorite':True}).status_code,422)

    def test_cleanup_preview_is_read_only_and_rejects_stale_plan(self):
        item=self.upload(); server.assets[item['id']]['created_at']=0
        jid=self.job(item);self.render_stub(jid,item)
        request={'include_results':True}
        preview=self.client.post('/api/cache/preview',json=request).json()
        self.assertEqual(preview['counts']['result'],1)
        self.assertEqual(preview['counts']['asset'],1)
        self.assertTrue((self.outputs/jid).exists())
        server.jobs[jid]['favorite']=True
        response=self.client.post('/api/cache/cleanup',json={**request,'preview_token':preview['preview_token']})
        self.assertEqual(response.status_code,409)
        protected=self.client.post('/api/cache/preview',json=request).json()
        self.assertEqual(protected['items'],[])
        server.jobs[jid]['favorite']=False
        fresh=self.client.post('/api/cache/preview',json=request).json()
        done=self.client.post('/api/cache/cleanup',json={**request,'preview_token':fresh['preview_token']}).json()
        self.assertEqual(done['freed_bytes'],fresh['bytes'])
        self.assertFalse((self.outputs/jid).exists())

    def test_renderer_failure_does_not_enable_download(self):
        item=self.upload(); jid=self.job(item); group=self.render_stub(jid,item,code=1)
        self.assertEqual(server.jobs[jid]["status"],"error")
        self.assertIn("初始化",server.jobs[jid]["message"])
        self.assertEqual(self.client.get(f"/api/jobs/{jid}/download").status_code,404)
        group.close.assert_called_once()

    def test_success_exact_download_and_history_survive_restart(self):
        item=self.upload(); jid=self.job(item); self.render_stub(jid,item)
        job=self.client.get(f"/api/jobs/{jid}").json()
        self.assertEqual(job["status"],"done"); self.assertNotIn("result_path",job)
        self.assertNotIn("path",job["asset"])
        self.assertEqual((job["width"],job["height"]),(160,120))
        self.assertEqual(self.client.get(job["preview"]).content,self.client.get(job["download"]).content)
        recovered_assets,recovered_jobs=server.studio_storage.load(server.STATE_FILE,self.root)
        self.assertEqual(recovered_jobs[jid]["status"],"done")
        self.assertTrue(Path(recovered_assets[item["id"]]["path"]).is_file())
        self.assertEqual(self.client.get('/api/jobs').json()['total'],1)

    def test_mkdir_failure_releases_queue(self):
        item=self.upload(); jid=self.job(item)
        with patch.object(Path,"mkdir",side_effect=OSError("disk full")):
            server.render(jid,server.Settings(asset_id=item["id"]),server.assets[item["id"]],self.selection)
        self.assertEqual(server.jobs[jid]["status"],"error")
        self.assertFalse(server.health()["busy"])
        with patch.object(server.worker,"submit") as submit:
            self.assertEqual(self.client.post('/api/jobs',json={'asset_id':item['id']}).status_code,200)
            submit.assert_called_once()

    def test_failed_metadata_save_rejects_without_queuing(self):
        item=self.upload()
        with patch.object(server,'persist',side_effect=OSError('disk full')),patch.object(server.worker,'submit') as submit:
            self.assertEqual(self.client.post('/api/jobs',json={'asset_id':item['id']}).status_code,507)
            submit.assert_not_called()
        self.assertFalse(server.health()['busy'])

    def test_duplicate_submission_is_idempotent_and_cancelled_queue_finishes(self):
        item=self.upload(); payload={'asset_id':item['id'],'request_id':'same-request'}
        with patch.object(server.worker,'submit') as submit:
            first=self.client.post('/api/jobs',json=payload).json()
            self.assertEqual(self.client.post('/api/jobs',json=payload).json(),first)
            submit.assert_called_once()
            self.assertEqual(self.client.post('/api/jobs',json={**payload,'intensity':.5}).status_code,409)
        jid=first['id']; self.assertEqual(self.client.post(f'/api/jobs/{jid}/cancel').json()['status'],'cancelled')
        with patch.object(server.GPU, 'prepare') as prepare:
            server.run_queued(jid)
            prepare.assert_not_called()
        self.assertEqual(server.jobs[jid]['status'],'cancelled');self.assertFalse(server.health()['busy'])
        self.assertNotIn(jid, server.cancellation)
        self.assertFalse((self.outputs/jid).exists())

    def test_cancelled_queue_stays_cancelled_if_gpu_preparation_fails(self):
        item = self.upload()
        with patch.object(server.worker, 'submit'):
            jid = self.client.post('/api/jobs', json={'asset_id': item['id']}).json()['id']

        def cancel_and_fail():
            response = self.client.post(f'/api/jobs/{jid}/cancel')
            self.assertEqual(response.json()['status'], 'cancelled')
            raise OSError('GPU initialization failed')

        with patch.object(server.GPU, 'prepare', side_effect=cancel_and_fail):
            server.run_queued(jid)
        self.assertEqual(server.jobs[jid]['status'], 'cancelled')
        self.assertNotIn(jid, server.cancellation)

    def test_running_cancel_reaches_owned_process_group(self):
        item=self.upload(); jid=self.job(item);server.jobs[jid]['status']='running'
        server.cancellation[jid]=threading.Event();group=Mock();server.runners[jid]=group
        self.client.post(f'/api/jobs/{jid}/cancel')
        group.cancel.assert_called_once();self.assertTrue(server.cancellation[jid].is_set())

    def test_restart_marks_interrupted_jobs_terminal(self):
        item=self.upload();jid=self.job(item);server.persist()
        _, recovered=server.studio_storage.load(server.STATE_FILE,self.root)
        self.assertEqual(recovered[jid]['status'],'error')
        self.assertIn('中断',recovered[jid]['message'])

    def test_cleanup_preserves_completed_and_active_files_by_default(self):
        item=self.upload();jid=self.job(item);self.render_stub(jid,item)
        failed=self.job(item);server.jobs[failed]['status']='error';(self.outputs/failed).mkdir();(self.outputs/failed/'partial').write_bytes(b'bad')
        result=self.client.post('/api/cache/cleanup',json={}).json()
        self.assertGreaterEqual(result['freed_bytes'],3)
        self.assertTrue((self.outputs/jid/'source_nr.png').exists())
        self.assertFalse((self.outputs/failed).exists())
        self.client.post('/api/cache/cleanup',json={'include_results':True,'keep_job_ids':[jid]})
        self.assertTrue((self.outputs/jid).exists())
        self.client.post('/api/cache/cleanup',json={'include_results':True})
        self.assertFalse((self.outputs/jid).exists())
        self.assertEqual(self.client.get(f'/api/jobs/{jid}').status_code,404)

    def test_cache_refuses_external_directory(self):
        with self.assertRaises(ValueError):server.remove_folder(self.root,self.uploads)
        self.assertTrue(self.root.exists())

    def test_slow_probe_keeps_event_loop_responsive(self):
        def slow(*args):
            time.sleep(.35);return dict(w=80,h=60)
        async def scenario():
            transport=httpx.ASGITransport(app=server.api)
            async with httpx.AsyncClient(transport=transport,base_url='http://test') as client:
                began=time.perf_counter()
                task=asyncio.create_task(client.post('/api/upload',files={'file':('video.mp4',b'fixture','video/mp4')}))
                await asyncio.sleep(.05)
                response=await client.get('/api/health');elapsed=time.perf_counter()-began
                self.assertEqual(response.status_code,200)
                await task
                return elapsed
        with patch.object(server.nr_video,'probe',side_effect=slow):
            self.assertLess(asyncio.run(scenario()),.25)

    def test_structured_progress_has_no_premature_done(self):
        value=server.progress_values('[====] 30/30 (100%) 20.0 fps ETA 00:00')
        self.assertEqual(value['progress'],99);self.assertEqual(value['frame'],30)


if __name__ == '__main__': unittest.main()
