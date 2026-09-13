import asyncio
import re
from datetime import datetime, timezone
from xml.etree import ElementTree as ET
import bencodepy
import pytest
from sqlalchemy import select
import config
import main
import scraper
from models import Release, ScraperLog, Setting
from torznab import NS


def admin_form(client, path):
    login = client.post('/login', data={'username': 'admin', 'password': 'test-admin-password'}, follow_redirects=False)
    assert login.status_code == 303
    page = client.get('/settings')
    assert page.status_code == 200
    token = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
    return token


def test_admin_settings_csrf_and_schedule(service):
    client, factory, path = service
    assert client.get('/health').json() == {'status': 'ok'}
    response = client.get('/settings', follow_redirects=False)
    assert response.status_code == 303 and response.headers['location'] == '/login'
    token = admin_form(client, path)
    page = client.get('/settings')
    assert 'data-open-api-key' in page.text
    assert 'http://testserver/api' in page.text
    assert 'https://public.example/api' not in page.text
    assert 'id="public_url" type="url" value="http://testserver" readonly disabled' in page.text
    forwarded = client.get('/settings', headers={'x-forwarded-proto': 'http', 'x-forwarded-host': 'indexer'}).text
    assert 'http://indexer/api' in forwarded
    assert '<script nonce="' in page.text
    assert "script-src 'nonce-" in page.headers['content-security-policy']
    assert client.post('/settings', data={'sync_interval': '2'}).status_code == 403
    response = client.post('/settings', data={'csrf': token, 'sync_interval': '2', 'api_key': 'x' * 32})
    assert response.status_code == 200
    assert (path / 'admin-password').read_text().startswith('scrypt$')
    assert config.settings()['api_key'] == 'x' * 32
    assert main.app.state.scheduler.get_job('scrape').trigger.interval.total_seconds() == 120
    assert client.post('/settings', data={'csrf': token, 'sync_interval': '0'}).status_code == 200
    assert config.settings()['sync_interval'] == '2'
    config.seed()
    assert config.settings()['api_key'] == 'x' * 32
    assert client.post('/sync', data={'csrf': token}, follow_redirects=False).status_code == 303
    with factory.begin() as session:
        session.add(ScraperLog(status='success', items_added=2, duration_seconds=1.25))
    assert 'Duration' in client.get('/settings').text
    assert '1.2s' in client.get('/settings').text
    assert client.post('/logout', follow_redirects=False).status_code == 303
    assert client.get('/settings', follow_redirects=False).headers['location'] == '/login'


def test_api_key_can_be_regenerated_and_revoked(service):
    client, _, path = service
    token = admin_form(client, path)
    original = config.settings()['api_key']
    response = client.post('/account/api-key', data={'csrf': token, 'action': 'regenerate'}, follow_redirects=False)
    assert response.status_code == 303
    generated = config.settings()['api_key']
    assert generated != original and len(generated) >= 32
    assert client.get('/api', params={'t': 'caps', 'apikey': original}).status_code == 401
    assert client.get('/api', params={'t': 'caps', 'apikey': generated}).status_code == 200
    response = client.post('/account/api-key', data={'csrf': token, 'action': 'revoke'}, follow_redirects=False)
    assert response.status_code == 303
    assert config.settings()['api_key'] == ''
    assert client.get('/api', params={'t': 'caps', 'apikey': generated}).status_code == 401


def test_first_run_setup_creates_session(setup_service):
    client, _, path = setup_service
    response = client.get('/settings', follow_redirects=False)
    assert response.status_code == 303 and response.headers['location'] == '/setup'
    response = client.post('/setup', data={'password': 'setup-password-123', 'confirmation': 'setup-password-123'}, follow_redirects=False)
    assert response.status_code == 303 and response.headers['location'] == '/settings'
    assert client.get('/settings').status_code == 200
    assert (path / 'admin-password').read_text().startswith('scrypt$')
    assert client.get('/setup', follow_redirects=False).headers['location'] == '/login'


def test_api_filters_xml_pagination_download(service):
    client, factory, path = service
    key = config.settings()['api_key']
    assert client.get('/api').status_code == 401
    caps = ET.fromstring(client.get('/api', params={'t': 'caps', 'apikey': key}).content)
    assert caps.find('searching/tv-search').get('supportedParams') == 'q,season,ep'
    torrent = bencodepy.encode({b'info': {b'name': b'example', b'length': 42, b'piece length': 16384, b'pieces': b'x' * 20}})
    digest, size = scraper.torrent_metadata(torrent)
    filename = scraper.save_torrent(digest, torrent)
    with factory.begin() as session:
        session.add(Release(id=digest, title='A & B S01E02 100%', category='tv', size=size,
                            pub_date=datetime.now(timezone.utc), season=1, episode=2, torrent_file_path=filename))
        session.add(Release(id='b' * 40, title='Movie', category='movie', size=12,
                            pub_date=datetime.now(timezone.utc), imdb_id='1234567', magnet_uri='magnet:?xt=urn:btih:' + 'b' * 40))
    params = {'t': 'tvsearch', 'apikey': key, 'season': 1, 'ep': 2, 'q': '100%', 'cat': '5000', 'limit': 1}
    root = ET.fromstring(client.get('/api', params=params).content)
    assert root.find('channel/item/title').text == 'A & B S01E02 100%'
    assert root.find(f'channel/{{{NS}}}response').get('total') == '1'
    url = root.find('channel/item/enclosure').get('url')
    assert client.get(url).content == torrent
    assert client.get(f'/download/{digest}').status_code == 401
    root = ET.fromstring(client.get('/api', params={**params, 'offset': 1}).content)
    assert root.find('channel/item') is None
    assert root.find(f'channel/{{{NS}}}response').get('total') == '1'
    root = ET.fromstring(client.get('/api', params={'apikey': key, 't': 'movie', 'imdbid': 'tt1234567'}).content)
    assert root.find('channel/item/enclosure').get('url').startswith('magnet:')
    assert ET.fromstring(client.get('/api', params={'apikey': key, 'limit': '-1'}).content).tag == 'error'
    with factory.begin() as session:
        session.get(Release, digest).torrent_file_path = '../admin-password'
    assert client.get(f'/download/{digest}', params={'apikey': key}).status_code == 404


def test_failure_notification_and_overlap(service, monkeypatch):
    _, factory, _ = service
    alerts = []
    with factory.begin() as session:
        session.get(Setting, 'target_url').value = 'https://example.test'
    async def fail(values):
        raise ValueError('secret should not be logged')
    monkeypatch.setattr(scraper, 'scrape', fail)
    monkeypatch.setattr(scraper, 'notify_failure', alerts.append)
    worker = scraper.ScraperWorker()
    asyncio.run(worker.run())
    with factory() as session:
        log = session.scalar(select(ScraperLog))
        assert log.status == 'failure'
        assert 'secret' not in log.error_message
    assert len(alerts) == 1
    async def overlap():
        async with worker.lock:
            await worker.run()
    asyncio.run(overlap())
    assert len(alerts) == 1


@pytest.mark.parametrize('text,expected', [('1 GiB', 1073741824), ('1.5 MB', 1500000), ('42', 42)])
def test_sizes(text, expected):
    assert scraper.parse_size(text) == expected


@pytest.mark.parametrize('text, expected', [
    ('www.example-source.test - Haiwaan (2026) Hindi HQ PreDVD - x264 - HQ Clean - AAC - 400MB.mkv',
     'Haiwaan (2026) Hindi HQ PreDVD x264 HQ Clean AAC 400MB'),
    ('Mareechika (2026) TRUE WEB-DL 4K DD+5.1 AAC 9.5GB.mkv',
     'Mareechika (2026) TRUE WEB DL 4K DD+5.1 AAC 9.5GB'),
    ('Reacher.S04E07.1080p.WEB-DL.mkv', 'Reacher S04E07 1080p WEB DL'),
    ('Movie (2026)', 'Movie (2026)'),
])
def test_normalize_release_title(text, expected):
    assert scraper.normalize_release_title(text) == expected


def test_invalid_torrent_and_magnet():
    with pytest.raises(Exception):
        scraper.torrent_metadata(b'<html>Login required</html>')
    with pytest.raises(ValueError):
        scraper.magnet_hash('magnet:?xt=urn:btih:nope')


def test_notifier_reads_dynamic_credentials(service, monkeypatch):
    import notifier
    import requests
    _, factory, _ = service
    calls = []
    class OK:
        def raise_for_status(self):
            pass
        def json(self):
            return {'ok': True}
    def post(url, **kwargs):
        calls.append((url, kwargs))
        return OK()
    monkeypatch.setattr(notifier.requests, 'post', post)
    assert notifier.notify_failure('No credentials') is False
    with factory.begin() as session:
        session.get(Setting, 'telegram_bot_token').value = 'example-token'
        session.get(Setting, 'telegram_chat_id').value = '-123'
    assert notifier.notify_failure('Failed [source]!') is True
    assert calls[0][1]['json']['chat_id'] == '-123'
    assert calls[0][1]['json']['text'].endswith(r'Failed \[source\]\!')
    def fail(*args, **kwargs):
        raise requests.ConnectionError('private token')
    monkeypatch.setattr(notifier.requests, 'post', fail)
    assert notifier.notify_failure('Failure') is False
