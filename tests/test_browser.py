"""Real browser integration test against an isolated local HTTP server."""
import asyncio
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import bencodepy
import pytest
from sqlalchemy import select, func
import config
import scraper
from models import Release, ScraperLog, Setting


def test_browser_scrape_dedup_and_empty_guard(service, tmp_path, monkeypatch):
    _, factory, _ = service
    site = tmp_path / 'site'
    site.mkdir()
    torrent = bencodepy.encode({b'info': {b'name': b'fixture', b'length': 1024, b'piece length': 16384, b'pieces': b'x' * 20}})
    (site / 'fixture.torrent').write_bytes(torrent)
    (site / 'index.html').write_text('''<html><body><article class="release" data-category="tv">
    <span class="title">Fixture.S02E03</span><a href="fixture.torrent">Download</a>
    <time datetime="2026-09-12T10:00:00Z"></time></article></body></html>''')
    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(site)))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        values = {**config.settings(), 'target_url': f'http://127.0.0.1:{server.server_port}/index.html'}
        assert asyncio.run(scraper.scrape(values)) == 1
        assert asyncio.run(scraper.scrape(values)) == 0
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(Release)) == 1
            release = session.scalar(select(Release))
            assert release.season == 2 and release.episode == 3
            assert release.size == 1024
        # A valid row followed by malformed metadata rolls back all database inserts.
        (site / 'index.html').write_text('''<article class="release" data-size="99"><span class="title">Other</span>
        <a href="magnet:?xt=urn:btih:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa">Magnet</a></article>
        <article class="release"><span class="title">Invalid</span></article>''')
        with pytest.raises(ValueError):
            asyncio.run(scraper.scrape(values))
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(Release)) == 1
        (site / 'index.html').write_text('<html><body>No release rows</body></html>')
        with factory.begin() as session:
            session.get(Setting, 'target_url').value = values['target_url']
        alerts = []
        monkeypatch.setattr(scraper, 'notify_failure', alerts.append)
        asyncio.run(scraper.ScraperWorker().run())
        with factory() as session:
            log = session.scalar(select(ScraperLog).order_by(ScraperLog.id.desc()))
            assert log.status == 'failure' and log.items_added == 0
        assert len(alerts) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
