import asyncio
import shutil
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
        assert client.delete('/api/downloads/'+job['id']).status_code == 204
        assert client.get('/api/downloads/'+job['id']).status_code == 404
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
    finally:await http.close()
