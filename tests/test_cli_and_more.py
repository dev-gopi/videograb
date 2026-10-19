from __future__ import annotations
import asyncio
import os
import shutil
from pathlib import Path
from typer.testing import CliRunner
import pytest
from videograb.cli import app
from videograb.http import SafeHTTP
from videograb.detection.service import Detector
from videograb.downloads.manager import DownloadManager
from videograb.models import DownloadRequest
from videograb.storage import Storage


def test_cli_scan_download_and_json(fixture_site, settings, monkeypatch):
    monkeypatch.setenv('VIDEOGRAB_ALLOW_PRIVATE','true')
    monkeypatch.setenv('VIDEOGRAB_DATA_DIR',str(settings.data_dir))
    monkeypatch.setenv('VIDEOGRAB_OUTPUT_DIR',str(settings.output_dir))
    runner=CliRunner()
    result=runner.invoke(app,['scan',fixture_site+'/media.mp4','--json'])
    assert result.exit_code==0,result.output
    assert '"scan_id"' in result.output
    result=runner.invoke(app,['download',fixture_site+'/media.mp4'])
    assert result.exit_code==0,result.output
    assert 'completed' in result.output
    result=runner.invoke(app,['jobs','--json'])
    assert result.exit_code==0,result.output
    assert '"status": "completed"' in result.output


@pytest.mark.skipif(shutil.which('ffmpeg') is None,reason='FFmpeg unavailable')
@pytest.mark.asyncio
async def test_dash_ffmpeg_download(fixture_site,settings):
    http=SafeHTTP(settings)
    try:
        store=Storage(settings.database)
        result=await Detector(settings,http).scan(fixture_site+'/manifest.mpd')
        store.save_scan(result)
        fmt=result.items[0].formats[0]
        manager=DownloadManager(settings,store,http)
        job=await manager.enqueue(DownloadRequest(scan_id=result.scan_id,item_id=result.items[0].id,format_id=fmt.id))
        completed=await manager.run(job)
        assert completed.status=='completed',completed.error
    finally:await http.close()


@pytest.mark.asyncio
async def test_queue_pause_resume_and_cancel(fixture_site,settings):
    http=SafeHTTP(settings)
    try:
        store=Storage(settings.database)
        scan=await Detector(settings,http).scan(fixture_site+'/media.mp4')
        store.save_scan(scan)
        manager=DownloadManager(settings,store,http)
        selected=DownloadRequest(scan_id=scan.scan_id,item_id=scan.items[0].id)
        job=await manager.enqueue(selected)
        paused=await manager.control(job.id,'pause')
        assert paused.status=='paused'
        resumed=await manager.control(job.id,'resume')
        assert resumed.status=='queued'
        await manager.start()
        await asyncio.wait_for(manager.queue.join(),timeout=10)
        assert store.get_job(job.id).status=='completed'
        await manager.stop()
        duplicate=await manager.enqueue(selected)
        assert duplicate.id == job.id  # completed-file deduplication
        job2=await manager.enqueue(DownloadRequest(scan_id=scan.scan_id,item_id=scan.items[0].id,audio_only=True))
        cancelled=await manager.control(job2.id,'cancel')
        assert cancelled.status=='cancelled'
    finally:await http.close()

@pytest.mark.asyncio
async def test_ytdlp_separate_track_selector_with_mock(settings,monkeypatch):
    """Offline adapter contract; not a live website extractor/network test."""
    import sys, types
    from videograb.models import ScanResult,MediaItem,MediaFormat
    chosen=[]
    class FakeDL:
        def __init__(self,opts):self.opts=opts
        def __enter__(self):return self
        def __exit__(self,*args):return None
        def download(self,urls):
            chosen.append(self.opts['format'])
            self.opts['progress_hooks'][0]({'status':'downloading','downloaded_bytes':300,'total_bytes':300,'speed':200})
            Path(self.opts['outtmpl'].replace('%(ext)s','mp4')).write_bytes(b'123' * 100)
            return 0
    monkeypatch.setitem(sys.modules,'yt_dlp',types.SimpleNamespace(YoutubeDL=FakeDL))
    url='https://example.org/video'
    item=MediaItem(id='fixture',title='Separate audio and video',source_url=url,extractor='yt-dlp',formats=[
        MediaFormat(id='f1',url='https://cdn.example.org/v',backend='ytdlp',selector='vid1080',vcodec='avc1',acodec='none')])
    scan=ScanResult(scan_id='scan-123',url=url,items=[item])
    store=Storage(settings.database);store.save_scan(scan)
    http=SafeHTTP(settings)
    try:
        manager=DownloadManager(settings,store,http)
        job=await manager.enqueue(DownloadRequest(scan_id=scan.scan_id,item_id=item.id,format_id='f1'))
        job=await manager.run(job)
        assert job.status=='completed',job.error
        assert chosen==['vid1080+bestaudio/vid1080']
    finally:await http.close()
