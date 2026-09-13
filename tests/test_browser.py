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


def test_browser_scrape_detail_pages(service, tmp_path):
    _, factory, _ = service
    site = tmp_path / 'site_detail'
    site.mkdir()
    (site / 'index.html').write_text('''<html><body>
    <div class="topic-row"><a class="topic-link" href="topic1.html">Ghamasaan (2026)</a></div>
    <div class="topic-row"><a class="topic-link" href="topic2.html">Reacher S04E07</a></div>
    </body></html>''')
    (site / 'topic1.html').write_text('''<html><body>
    <h1>Ghamasaan (2026)</h1>
    <div class="post-content">
      <a href="magnet:?xt=urn:btih:1111111111111111111111111111111111111111&dn=Ghamasaan.2026.1080p.WEB-DL&xl=2147483648">1080p Magnet</a>
      <a href="magnet:?xt=urn:btih:2222222222222222222222222222222222222222&dn=Ghamasaan.2026.720p.WEB-DL&xl=1073741824">720p Magnet</a>
    </div>
    </body></html>''')
    (site / 'topic2.html').write_text('''<html><body>
    <article class="sub-item" data-category="tv" data-size="500MB">
      <span class="sub-title">Reacher.S04E07.1080p</span>
      <a href="magnet:?xt=urn:btih:3333333333333333333333333333333333333333">Magnet</a>
    </article>
    </body></html>''')
    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(site)))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        values = {
            **config.settings(),
            'target_url': f'http://127.0.0.1:{server.server_port}/index.html',
            'row_selector': '.topic-row',
            'title_selector': '.topic-link, .sub-title, h1',
            'detail_selector': '.topic-link',
            'magnet_selector': 'a[href^="magnet:"]',
        }
        added = asyncio.run(scraper.scrape(values))
        assert added == 3
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(Release)) == 3
            tv = session.get(Release, '3333333333333333333333333333333333333333')
            assert tv.category == 'tv' and tv.season == 4 and tv.episode == 7
            assert tv.size == 500000000
            m1 = session.get(Release, '1111111111111111111111111111111111111111')
            assert m1.category == 'movie' and m1.size == 2147483648
            assert m1.title == 'Ghamasaan 2026 1080p WEB DL'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_browser_scrape_detail_pages_with_pagination(service, tmp_path):
    _, factory, _ = service
    site = tmp_path / 'site_paged'
    site.mkdir()
    (site / 'page1.html').write_text('''<html><body>
    <div class="topic"><a class="link" href="p1_item.html">Movie 1</a></div>
    <a class="next" href="page2.html">Next Page</a>
    </body></html>''')
    (site / 'page2.html').write_text('''<html><body>
    <div class="topic"><a class="link" href="p2_item.html">Movie 2</a></div>
    </body></html>''')
    (site / 'p1_item.html').write_text('''<html><body>
    <h1>Movie 1</h1>
    <a href="magnet:?xt=urn:btih:4444444444444444444444444444444444444444&dn=Movie.1.1080p&xl=1000000">Magnet</a>
    </body></html>''')
    (site / 'p2_item.html').write_text('''<html><body>
    <h1>Movie 2</h1>
    <a href="magnet:?xt=urn:btih:5555555555555555555555555555555555555555&dn=Movie.2.1080p&xl=2000000">Magnet</a>
    </body></html>''')
    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(site)))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        values = {
            **config.settings(),
            'target_url': f'http://127.0.0.1:{server.server_port}/page1.html',
            'row_selector': '.topic',
            'title_selector': '.link, h1',
            'detail_selector': '.link',
            'next_selector': 'a.next',
            'max_pages': '5',
            'magnet_selector': 'a[href^="magnet:"]',
        }
        added = asyncio.run(scraper.scrape(values))
        assert added == 2
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(Release)) == 2
            r1 = session.get(Release, '4444444444444444444444444444444444444444')
            r2 = session.get(Release, '5555555555555555555555555555555555555555')
            assert r1.title == 'Movie 1 1080p'
            assert r2.title == 'Movie 2 1080p'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
