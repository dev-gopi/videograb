from pathlib import Path
import pytest
from videograb.http import SafeHTTP
from videograb.detection.service import Detector


@pytest.mark.asyncio
async def test_dynamic_browser_fixture(fixture_site,settings):
    pytest.importorskip('playwright')
    from playwright.async_api import async_playwright
    async with async_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).exists():
            pytest.skip('Chromium browser binaries not installed')
    http=SafeHTTP(settings)
    try:
        result=await Detector(settings,http).scan(fixture_site+'/dynamic.html',browser_enabled=True)
        assert any('/media.mp4' in f.url for i in result.items for f in i.formats),result.warnings
    finally:await http.close()
