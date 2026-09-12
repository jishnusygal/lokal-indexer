"""Configurable CSS adapter: one release per row, optional same-origin pagination."""
import asyncio
import base64
import hashlib
import os
import re
import tempfile
from datetime import datetime, timezone
from urllib.parse import parse_qs, urljoin, urlsplit

import bencodepy
from playwright.async_api import async_playwright
from sqlalchemy import delete, select
from config import http_url, settings
from database import SessionLocal, TORRENT_DIR
from models import Release, ScraperLog, utcnow
from notifier import notify_failure

MAX_TORRENT_BYTES = 10 * 1024 * 1024
MAX_ROWS = 2000


class ScrapeError(ValueError):
    """A safe, actionable error message suitable for logs and notifications."""



def parse_size(value):
    match = re.fullmatch(r'\s*([\d,.]+)\s*([KMGTPE]?i?B)?\s*', value, re.I)
    if not match:
        raise ScrapeError('Invalid release size; use bytes or a unit such as GiB.')
    number, unit = match.groups()
    unit = (unit or 'B').upper()
    power = 'BKMGTPE'.index(unit[0])
    return int(float(number.replace(',', '')) * (1024 if 'I' in unit else 1000) ** power)


def magnet_hash(uri):
    if urlsplit(uri).scheme != 'magnet':
        raise ScrapeError('Invalid magnet URI.')
    for xt in parse_qs(urlsplit(uri).query).get('xt', []):
        if xt.lower().startswith('urn:btih:'):
            digest = xt[9:]
            if re.fullmatch(r'[a-fA-F0-9]{40}', digest):
                return digest.lower()
            if re.fullmatch(r'[A-Z2-7a-z]{32}', digest):
                return base64.b32decode(digest.upper()).hex()
    raise ScrapeError('Magnet must contain a valid BitTorrent v1 info hash.')


def torrent_metadata(body):
    if not body or len(body) > MAX_TORRENT_BYTES:
        raise ScrapeError('Torrent exceeds size limit or is empty.')
    data = bencodepy.decode(body)
    info = data.get(b'info') if isinstance(data, dict) else None
    if not isinstance(info, dict) or not info.get(b'name'):
        raise ScrapeError('Download is not a valid torrent.')
    # Require canonical encoding so the re-encoded info dictionary has the original hash.
    if bencodepy.encode(data) != body:
        raise ScrapeError('Torrent uses unsupported noncanonical bencoding.')
    if b'files' in info:
        lengths = [entry[b'length'] for entry in info[b'files']]
    else:
        lengths = [info[b'length']]
    if any(not isinstance(length, int) or length < 0 for length in lengths):
        raise ScrapeError('Invalid file length in torrent metadata.')
    size = sum(lengths)
    piece_length = info.get(b'piece length')
    pieces = info.get(b'pieces')
    if (not isinstance(piece_length, int) or piece_length <= 0 or
            not isinstance(pieces, bytes) or len(pieces) != 20 * ((size + piece_length - 1) // piece_length)):
        raise ScrapeError('Invalid or unsupported v1 torrent piece metadata.')
    if not isinstance(size, int) or size < 0:
        raise ScrapeError('Invalid torrent payload size.')
    return hashlib.sha1(bencodepy.encode(info)).hexdigest(), size


def save_torrent(digest, body):
    TORRENT_DIR.mkdir(parents=True, exist_ok=True)
    path = TORRENT_DIR / f'{digest}.torrent'
    fd, temporary = tempfile.mkstemp(dir=TORRENT_DIR, suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path.name


async def field(row, selector, attribute=None):
    if not selector:
        return ''
    node = row.locator(selector).first
    if not await node.count():
        return ''
    return ((await node.get_attribute(attribute) if attribute else await node.inner_text()) or '').strip()


async def scrape(values):
    target = http_url(values['target_url'])
    origin = urlsplit(target).netloc
    releases = {}
    seen_pages = set()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, args=['--disable-dev-shm-usage'])
        try:
            context = await browser.new_context(accept_downloads=False, service_workers='block')
            context.set_default_timeout(30000)
            page = await context.new_page()
            for _ in range(int(values['max_pages'])):
                if target in seen_pages:
                    break
                seen_pages.add(target)
                response = await page.goto(target, wait_until='domcontentloaded', timeout=60000)
                if response is None or response.status >= 400:
                    raise ScrapeError('Target returned an unsuccessful HTTP response.')
                await page.locator(values['row_selector']).first.wait_for(state='attached')
                rows = page.locator(values['row_selector'])
                count = await rows.count()
                if count == 0 or count > MAX_ROWS:
                    raise ScrapeError('Zero release rows found, or page exceeds the 2000-row limit.')
                for index in range(count):
                    row = rows.nth(index)
                    title = await field(row, values['title_selector'])
                    if not title or len(title) > 1000:
                        raise ScrapeError('Release has a missing or oversized title.')
                    magnet = await field(row, values['magnet_selector'], 'href') or None
                    torrent_url = await field(row, values['torrent_selector'], 'href')
                    digest = magnet_hash(magnet) if magnet else None
                    path = None
                    if torrent_url:
                        torrent_url = http_url(urljoin(page.url, torrent_url))
                        download = await context.request.get(torrent_url, timeout=60000)
                        try:
                            if not download.ok or int(download.headers.get('content-length', '0')) > MAX_TORRENT_BYTES:
                                raise ScrapeError('Torrent download failed or exceeds 10 MiB.')
                            body = await download.body()
                            torrent_digest, size = torrent_metadata(body)
                            if digest and digest != torrent_digest:
                                raise ScrapeError('Torrent and magnet hashes do not match.')
                            digest = torrent_digest
                            path = save_torrent(digest, body)
                        finally:
                            await download.dispose()
                    elif magnet:
                        size = parse_size(await row.get_attribute('data-size') or await field(row, values['size_selector']))
                    else:
                        raise ScrapeError('Release has neither a torrent link nor a valid magnet.')
                    match = re.search(r'(?i)\bS(\d{1,3})(?:E(\d{1,4}))?\b', title)
                    season = int(match[1]) if match else None
                    episode = int(match[2]) if match and match[2] else None
                    category = await row.get_attribute('data-category') or ('tv' if match else 'movie')
                    if category not in ('movie', 'tv'):
                        raise ScrapeError('Release category must be movie or tv.')
                    date_text = await field(row, values['date_selector'], 'datetime')
                    date = datetime.fromisoformat(date_text.replace('Z', '+00:00')) if date_text else utcnow()
                    date = date.replace(tzinfo=timezone.utc) if date.tzinfo is None else date.astimezone(timezone.utc)
                    imdb = await row.get_attribute('data-imdb')
                    if imdb:
                        if not re.fullmatch(r'(?:tt)?\d{7,10}', imdb):
                            raise ScrapeError('Invalid IMDb ID.')
                        imdb = imdb.removeprefix('tt')
                    releases[digest] = dict(id=digest, title=title, category=category, size=size,
                                            pub_date=date, magnet_uri=magnet, torrent_file_path=path,
                                            imdb_id=imdb, season=season, episode=episode)
                next_link = await field(page, values['next_selector'], 'href')
                if not next_link:
                    break
                target = http_url(urljoin(page.url, next_link))
                if urlsplit(target).netloc != origin:
                    raise ScrapeError('Pagination must remain on the configured host.')
        finally:
            await browser.close()
    if not releases:
        raise ScrapeError('No valid releases parsed; check the target and selectors.')
    with SessionLocal.begin() as session:
        added = 0
        for digest, data in releases.items():
            if session.get(Release, digest) is None:
                session.add(Release(**data))
                added += 1
        session.add(ScraperLog(status='success', items_added=added))
    return added


class ScraperWorker:
    def __init__(self):
        self.lock = asyncio.Lock()

    async def run(self):
        if self.lock.locked():
            return
        async with self.lock:
            values = settings()
            if not values.get('target_url'):
                return  # An unconfigured installation is deliberately idle.
            try:
                async with asyncio.timeout(900):
                    await scrape(values)
            except Exception as exc:
                # Playwright exceptions may contain credential-bearing URLs and page contents.
                message = str(exc) if isinstance(exc, ScrapeError) else f'{type(exc).__name__}: scrape failed. Check target availability, CSS selectors, and release metadata.'
                with SessionLocal.begin() as session:
                    session.add(ScraperLog(status='failure', items_added=0, error_message=message))
                await asyncio.to_thread(notify_failure, message)
            finally:
                with SessionLocal.begin() as session:
                    keep = select(ScraperLog.id).order_by(ScraperLog.id.desc()).limit(1000)
                    session.execute(delete(ScraperLog).where(ScraperLog.id.not_in(keep)))
