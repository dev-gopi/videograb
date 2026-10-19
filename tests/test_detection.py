import shutil
from pathlib import Path
import pytest
from videograb.http import SafeHTTP
from videograb.detection.service import Detector
from videograb.detection.manifests import parse_hls,parse_dash
from videograb.detection.static import find_urls
from videograb.detection.ytdlp import info_to_items
from videograb.security import UnsafeURL,validate,safe_name


@pytest.mark.asyncio
async def test_html_js_iframe_hls_dash_multiple(fixture_site,settings):
    http=SafeHTTP(settings)
    try:
        result=await Detector(settings,http).scan(fixture_site+'/page.html')
        urls=[f.url for x in result.items for f in x.formats]
        assert any('/media.mp4' in u for u in urls)
        assert any('/other.mp4' in u for u in urls)
        assert any('/iframe.mp4' in u for u in urls)
        assert any('/stream.m3u8' in u for u in urls)
        assert any('/manifest.mpd' in u for u in urls)
        assert result.inspected_pages == 2
        assert any(f.accessibility == 'response_ok' for item in result.items for f in item.formats)
        assert all(sum(f.recommended for f in item.formats) <= 1 for item in result.items)
    finally:await http.close()


@pytest.mark.asyncio
async def test_direct_redirect_and_manifest(fixture_site,settings):
    http=SafeHTTP(settings)
    try:
        detector=Detector(settings,http)
        assert (await detector.scan(fixture_site+'/large.mp4')).items[0].formats[0].url.endswith('large.mp4')
        assert (await detector.scan(fixture_site+'/redirect')).items[0].formats[0].url.endswith('media.mp4')
        hls=await detector.scan(fixture_site+'/master.m3u8')
        assert hls.items[0].formats[0].height==90 or hls.items[0].formats[-1].height==90
        assert any(s.language=='en' for s in hls.items[0].subtitles)
        dash=await detector.scan(fixture_site+'/manifest.mpd')
        assert any(f.protocol=='mpd' for f in dash.items[0].formats)
    finally:await http.close()


def test_static_and_ytdlp_mapping():
    assert find_urls('const player={file:"/folder/video.mp4?token=abc"}', 'https://example.org/base')[0]=='https://example.org/folder/video.mp4?token=abc'
    assert find_urls('const player={playbackUrl:"/api/play?id=42"}', 'https://example.org/base') == ['https://example.org/api/play?id=42']
    fake={'id':'x','title':'Synthetic yt-dlp fixture','webpage_url':'https://example.org/watch','formats':[
        {'format_id':'vid','url':'https://cdn.example.org/video.m4s','height':1080,'vcodec':'avc1','acodec':'none'},
        {'format_id':'aud','url':'https://cdn.example.org/audio.m4a','vcodec':'none','acodec':'mp4a'}]}
    items=info_to_items(fake,'https://example.org/watch')
    assert len(items)==1 and len(items[0].formats)==2
    assert items[0].formats[0].backend=='ytdlp'


def test_extensionless_contextual_media_fields():
    from videograb.detection.static import parse_html
    items, _, _ = parse_html('<video src="/stream?signature=abc"></video><track kind="captions" srclang="en" src="/captions.vtt">', 'https://example.org/watch')
    assert len(items) == 1
    assert items[0].formats[0].url == 'https://example.org/stream?signature=abc'
    assert items[0].subtitles[0].language == 'en'


def test_json_player_configuration_discovery():
    from videograb.detection.static import parse_html
    document = '<script type="application/json">{"sources":[{"playbackUrl":"/api/stream?id=42"}]}</script>'
    items, _, _ = parse_html(document, 'https://example.org/watch')
    assert items[0].formats[0].url == 'https://example.org/api/stream?id=42'


def test_html_base_url_is_used_for_player_assets():
    from videograb.detection.static import parse_html
    items, iframes, scripts = parse_html('<base href="https://cdn.example.org/player/"><video src="video.mp4"></video><iframe src="frame"></iframe><script src="app.js"></script>', 'https://example.org/watch')
    assert items[0].formats[0].url == 'https://cdn.example.org/player/video.mp4'
    assert iframes == ['https://cdn.example.org/player/frame']
    assert scripts == ['https://cdn.example.org/player/app.js']


def test_invalid_numeric_environment_is_safe(monkeypatch):
    from videograb.config import Settings
    monkeypatch.setenv('VIDEOGRAB_MAX_BYTES', 'not-a-number')
    monkeypatch.setenv('VIDEOGRAB_BROWSER_TIMEOUT_MS', '-10')
    settings = Settings()
    assert settings.max_download_bytes == 4 * 1024**3
    assert settings.browser_timeout_ms == 1000


@pytest.mark.asyncio
async def test_ssrf_protections():
    with pytest.raises(UnsafeURL):await validate('http://127.0.0.1/admin')
    with pytest.raises(UnsafeURL):await validate('http://169.254.169.254/latest/meta-data')
    with pytest.raises(UnsafeURL):await validate('file:///etc/passwd')
    with pytest.raises(UnsafeURL):await validate('http://localhost/foo')
    assert await validate('http://127.0.0.1:8000',True)
    assert '/' not in safe_name('../../etc/passwd')
