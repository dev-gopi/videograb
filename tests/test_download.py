import asyncio
import shutil
from pathlib import Path
import pytest
from videograb.api import create_app
from videograb.http import SafeHTTP
from videograb.models import DownloadRequest
from videograb.storage import Storage
from videograb.detection.service import Detector
from videograb.downloads.manager import DownloadManager


@pytest.mark.asyncio
async def test_integrated_download_direct_and_resume_source(fixture_site,settings):
    http=SafeHTTP(settings)
    try:
        store=Storage(settings.database)
        detector=Detector(settings,http)
        result=await detector.scan(fixture_site+'/redirect')
        store.save_scan(result)
        manager=DownloadManager(settings,store,http)
        job=await manager.enqueue(DownloadRequest(scan_id=result.scan_id,item_id=result.items[0].id,format_id=result.items[0].formats[0].id))
        job=await manager.run(job)
        assert job.status=='completed',job.error
        assert job.received>100
        from pathlib import Path
        assert Path(job.output_path).read_bytes().startswith(b'\x00\x00')
        assert store.get_job(job.id).status=='completed'
    finally:await http.close()


@pytest.mark.skipif(shutil.which('ffmpeg') is None,reason='FFmpeg unavailable')
@pytest.mark.asyncio
async def test_ffmpeg_hls(fixture_site,settings):
    http=SafeHTTP(settings)
    try:
        store=Storage(settings.database)
        result=await Detector(settings,http).scan(fixture_site+'/stream.m3u8')
        store.save_scan(result)
        fmt=result.items[0].formats[0]
        job=await DownloadManager(settings,store,http).enqueue(DownloadRequest(scan_id=result.scan_id,item_id=result.items[0].id,format_id=fmt.id))
        job=await DownloadManager(settings,store,http).run(job)
        assert job.status=='completed',job.error
    finally:await http.close()


def test_fastapi_integrated_scan_queue(fixture_site,settings):
    from fastapi.testclient import TestClient
    app=create_app(settings)
    with TestClient(app) as client:
        assert client.get('/api/health').status_code==200
        assert 'VideoGrab' in client.get('/').text
        scan=client.post('/api/scan',json={'url':fixture_site+'/page.html'});assert scan.status_code==200,scan.text
        data=scan.json();assert len(data['items'])>=4
        item=data['items'][0]
        resp=client.post('/api/downloads',json={'scan_id':data['scan_id'],'item_id':item['id'],'format_id':item['formats'][0]['id']});assert resp.status_code==202,resp.text
        job=resp.json()
        import time
        for _ in range(80):
            final=client.get('/api/downloads/'+job['id']).json()
            if final['status'] in ('completed','failed'):break
            time.sleep(.08)
        assert final['status']=='completed',final
        assert client.get('/api/downloads').json()

        # Test video file streaming and range requests
        file_resp = client.get('/api/downloads/'+job['id']+'/file')
        assert file_resp.status_code == 200
        assert 'video' in file_resp.headers['content-type']
        assert 'inline' in file_resp.headers.get('content-disposition', '')

        range_resp = client.get('/api/downloads/'+job['id']+'/file', headers={'range': 'bytes=0-10'})
        assert range_resp.status_code == 206
        assert len(range_resp.content) == 11
        assert client.get('/api/downloads/'+job['id']+'/stream').status_code == 200

        sub_resp = client.get('/api/downloads/'+job['id']+'/subtitles')
        assert sub_resp.status_code == 200
        assert isinstance(sub_resp.json().get('subtitles'), list)

        assert client.delete('/api/downloads/'+job['id']).status_code == 204
        assert client.get('/api/downloads/'+job['id']).status_code == 404
        assert client.get('/api/downloads/'+job['id']+'/file').status_code == 404
        assert client.post('/api/downloads',json={'scan_id':'bad','item_id':'a'}).status_code==400
        assert client.get('/api/settings').json()['output_dir']==str(settings.output_dir)
        newdir=settings.output_dir/'new'
        assert client.patch('/api/settings',json={'output_dir':str(newdir)}).status_code==200
        assert settings.output_dir==newdir


def test_fastapi_accepts_bracketed_ipv6_host(settings):
    from fastapi.testclient import TestClient
    with TestClient(create_app(settings)) as client:
        response = client.get('/api/health', headers={'host': '[::1]:8765'})
    assert response.status_code == 200

@pytest.mark.skipif(shutil.which('ffmpeg') is None,reason='FFmpeg unavailable')
@pytest.mark.asyncio
async def test_hls_variant_with_subtitles(fixture_site,settings):
    from pathlib import Path
    http=SafeHTTP(settings)
    try:
        store=Storage(settings.database)
        scan=await Detector(settings,http).scan(fixture_site+'/master.m3u8')
        assert scan.items[0].subtitles
        store.save_scan(scan)
        manager=DownloadManager(settings,store,http)
        job=await manager.enqueue(DownloadRequest(scan_id=scan.scan_id,item_id=scan.items[0].id,subtitles=True))
        job=await manager.run(job)
        assert job.status=='completed',job.error
        assert Path(job.output_path).is_file()
        assert list(settings.output_dir.glob('*.en.vtt'))
        # Test subtitles serving via API
        from fastapi.testclient import TestClient
        with TestClient(create_app(settings)) as client:
            subs = client.get(f'/api/downloads/{job.id}/subtitles').json()
            assert len(subs['subtitles']) >= 1
            sub_url = subs['subtitles'][0]['url']
            sub_file_resp = client.get(sub_url)
            assert sub_file_resp.status_code == 200
            assert 'vtt' in sub_file_resp.headers['content-type']
            # Test path traversal rejection
            assert client.get(f'/api/downloads/{job.id}/subtitles/..%2fpasswd').status_code in (400, 404)
    finally:await http.close()


def test_api_media_streaming_edge_cases(settings):
    from fastapi.testclient import TestClient
    from videograb.models import DownloadJob, MediaItem
    from videograb.storage import now
    app = create_app(settings)
    store = Storage(settings.database)

    # Job without output path
    job = DownloadJob(id="no-output", title="No Output", source_url="https://example.com",
                      item=MediaItem(id="m1", title="No Output", source_url="https://example.com", extractor="direct"),
                      created_at=now(), updated_at=now(), status="queued")
    store.save_job(job)

    # Job with missing file on disk
    job_missing = DownloadJob(id="missing-file", title="Missing File", source_url="https://example.com",
                              item=MediaItem(id="m2", title="Missing File", source_url="https://example.com", extractor="direct"),
                              created_at=now(), updated_at=now(), status="completed",
                              output_path=str(settings.output_dir / "does_not_exist.mp4"))
    store.save_job(job_missing)

    with TestClient(app) as client:
        assert client.get("/api/downloads/unknown-job/file").status_code == 404
        assert client.get("/api/downloads/no-output/file").status_code == 404
        assert client.get("/api/downloads/missing-file/file").status_code == 404
        assert client.get("/api/downloads/unknown-job/subtitles").json() == {"subtitles": []}
        assert client.get("/api/downloads/unknown-job/subtitles/test.vtt").status_code == 404


def test_preview_media_before_download(fixture_site, settings):
    from fastapi.testclient import TestClient
    app = create_app(settings)
    with TestClient(app) as client:
        # Scan page
        scan_resp = client.post('/api/scan', json={'url': fixture_site + '/page.html'})
        assert scan_resp.status_code == 200
        scan_data = scan_resp.json()
        item = scan_data['items'][0]
        fmt = item['formats'][0]

        # Preview stream before downloading
        preview_resp = client.get(f"/api/scans/{scan_data['scan_id']}/preview/{item['id']}/{fmt['id']}")
        assert preview_resp.status_code == 200
        assert "video" in preview_resp.headers.get("content-type", "")
        assert preview_resp.headers.get("accept-ranges") == "bytes"
        assert len(preview_resp.content) > 0

        # Range request in preview
        range_resp = client.get(f"/api/scans/{scan_data['scan_id']}/preview/{item['id']}/{fmt['id']}", headers={'range': 'bytes=0-10'})
        assert range_resp.status_code in (200, 206)

        # Non-existent scan/item/format
        assert client.get(f"/api/scans/bad-scan/preview/{item['id']}/{fmt['id']}").status_code == 404
        assert client.get(f"/api/scans/{scan_data['scan_id']}/preview/bad-item/{fmt['id']}").status_code == 404
        assert client.get(f"/api/scans/{scan_data['scan_id']}/preview/{item['id']}/bad-format").status_code == 404


def test_filesystem_directory_explorer_and_open_folder(settings):
    from fastapi.testclient import TestClient
    from videograb.models import DownloadJob, MediaItem
    from videograb.storage import now
    app = create_app(settings)
    store = Storage(settings.database)

    # Create dummy completed job with output file
    test_file = settings.output_dir / "sample.mp4"
    test_file.write_bytes(b"sample video bytes")
    job = DownloadJob(id="completed-test", title="Sample", source_url="https://example.com",
                      item=MediaItem(id="s1", title="Sample", source_url="https://example.com", extractor="direct"),
                      created_at=now(), updated_at=now(), status="completed",
                      output_path=str(test_file))
    store.save_job(job)

    with TestClient(app) as client:
        # 1. List directories
        dir_resp = client.get("/api/fs/directories")
        assert dir_resp.status_code == 200
        dir_data = dir_resp.json()
        assert "current" in dir_data
        assert "quick_links" in dir_data
        assert len(dir_data["quick_links"]) >= 1

        # 2. Create subfolder
        create_resp = client.post("/api/fs/create-directory", json={"parent": str(settings.output_dir), "name": "my_videos"})
        assert create_resp.status_code == 200
        new_dir_path = create_resp.json()["path"]
        assert Path(new_dir_path).is_dir()

        # Reject invalid subfolder creation
        assert client.post("/api/fs/create-directory", json={"parent": str(settings.output_dir), "name": "../sneaky"}).status_code == 400

        # Navigate into the new subfolder
        sub_resp = client.get(f"/api/fs/directories?path={new_dir_path}")
        assert sub_resp.status_code == 200
        assert sub_resp.json()["current"] == str(Path(new_dir_path).resolve())

        # 3. Open folder in file manager
        open_resp = client.post("/api/downloads/completed-test/open-folder")
        assert open_resp.status_code == 200
        assert open_resp.json()["folder"] == str(settings.output_dir)

        # 4. Native browse endpoint
        browse_resp = client.post("/api/settings/browse-native")
        assert browse_resp.status_code == 200
        assert "ok" in browse_resp.json()


def test_settings_system_detection_and_preferences(settings):
    from fastapi.testclient import TestClient
    app = create_app(settings)
    with TestClient(app) as client:
        # Check initial settings and system info detection
        s_resp = client.get("/api/settings")
        assert s_resp.status_code == 200
        s_data = s_resp.json()
        assert "system_info" in s_data
        assert "cpu_count" in s_data["system_info"]
        assert s_data["system_info"]["cpu_count"] >= 1
        assert "os" in s_data["system_info"]
        assert "ffmpeg_available" in s_data
        assert "theme" in s_data

        # Update preferences
        update_resp = client.patch("/api/settings", json={
            "theme": "dark",
            "browser_enabled": True,
            "deep_scan": True,
            "workers": 4,
        })
        assert update_resp.status_code == 200
        updated = update_resp.json()
        assert updated["theme"] == "dark"
        assert updated["browser_enabled"] is True
        assert updated["deep_scan"] is True
        assert updated["workers"] == 4

        # Verify persistence on subsequent GET
        recheck = client.get("/api/settings").json()
        assert recheck["theme"] == "dark"
        assert recheck["browser_available"] is True
        assert recheck["deep_scan"] is True
        assert recheck["workers"] == 4

        # Validate theme constraint
        bad_theme = client.patch("/api/settings", json={"theme": "unsupported-theme"})
        assert bad_theme.status_code == 422
