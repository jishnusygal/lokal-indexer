"""Configurable CSS adapter: one release per row, or listing with detail pages."""
import asyncio
import base64
import hashlib
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from urllib.parse import parse_qs, urljoin, urlsplit

import bencodepy
from playwright.async_api import TimeoutError as PlaywrightTimeoutError, async_playwright
from sqlalchemy import delete, select
from config import http_url, settings
from database import SessionLocal, TORRENT_DIR
from models import Release, ScraperLog, utcnow
from notifier import notify_failure

logger = logging.getLogger(__name__)

MAX_TORRENT_BYTES = 10 * 1024 * 1024
MAX_ROWS = 2000


class ScrapeError(ValueError):
    """A safe, actionable error message suitable for logs and notifications."""


async def inspect_and_auto_detect_selectors(page, values):
    """
    Auto-detect suitable selectors if not explicitly provided or if defaults fail.
    Specifically detects forum topics or standard release tables.
    """
    val = dict(values)
    # Check if we are on an IPS-style forum index or category page
    has_topic_links = await page.locator('a[href*="/forums/topic/"], a[href*="/topic/"]').count()
    if has_topic_links > 0:
        if not val.get('detail_selector') or val.get('row_selector') == '.release':
            val['row_selector'] = 'a[href*="/forums/topic/"], a[href*="/topic/"]'
            val['detail_selector'] = 'self'
            val['title_selector'] = 'self'
            val['magnet_selector'] = 'a[href^="magnet:"]'
            val['torrent_selector'] = 'a[href*="attachment.php"], a[href$=".torrent"]'
            val['next_selector'] = 'li.ipsPagination_next a, a[rel="next"]'
    return val


def parse_size(value):
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        match = re.fullmatch(r'\s*([\d,.]+)\s*([KMGTPE]?i?B)?\s*', value, re.I)
        if match:
            number, unit = match.groups()
            unit = (unit or 'B').upper()
            power = 'BKMGTPE'.index(unit[0])
            return int(float(number.replace(',', '')) * (1024 if 'I' in unit else 1000) ** power)
        submatch = re.search(r'\b([\d,.]+)\s*([KMGTPE]i?B)\b', value, re.I)
        if submatch:
            number, unit = submatch.groups()
            unit = unit.upper()
            power = 'BKMGTPE'.index(unit[0])
            return int(float(number.replace(',', '')) * (1024 if 'I' in unit else 1000) ** power)
    raise ScrapeError('Invalid release size; use bytes or a unit such as GiB.')


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


def magnet_params(uri):
    if not uri or urlsplit(uri).scheme != 'magnet':
        return {}
    return parse_qs(urlsplit(uri).query)


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
    if selector.strip().lower() in ('self', '.'):
        return ((await row.get_attribute(attribute) if attribute else await row.inner_text()) or '').strip()
    node = row.locator(selector).first
    if not await node.count():
        return ''
    return ((await node.get_attribute(attribute) if attribute else await node.inner_text()) or '').strip()


async def download_torrent(context, torrent_url):
    download = await context.request.get(torrent_url, timeout=60000)
    try:
        if not download.ok or int(download.headers.get('content-length', '0')) > MAX_TORRENT_BYTES:
            raise ScrapeError('Torrent download failed or exceeds 10 MiB.')
        body = await download.body()
        torrent_digest, size = torrent_metadata(body)
        path = save_torrent(torrent_digest, body)
        return torrent_digest, size, path
    finally:
        await download.dispose()


def merge_release(releases, rel):
    if not rel:
        return
    digest = rel['id']
    if digest in releases:
        existing = releases[digest]
        if not existing.get('torrent_file_path') and rel.get('torrent_file_path'):
            existing['torrent_file_path'] = rel['torrent_file_path']
        if not existing.get('magnet_uri') and rel.get('magnet_uri'):
            existing['magnet_uri'] = rel['magnet_uri']
    else:
        releases[digest] = rel


async def resolve_size(row, values, magnet, title, fallback_title):
    raw_size = await row.get_attribute('data-size') or await field(row, values.get('size_selector'))
    if raw_size:
        return parse_size(raw_size)
    if magnet:
        m_params = magnet_params(magnet)
        if m_params.get('xl'):
            return int(m_params['xl'][0])
    try:
        parent = row.locator('xpath=..')
        if await parent.count():
            p_size = await parent.first.get_attribute('data-size')
            if p_size:
                return parse_size(p_size)
    except Exception:
        pass
    for text in (title, fallback_title):
        if text:
            try:
                return parse_size(text)
            except Exception:
                pass
    try:
        parent = row.locator('xpath=..')
        if await parent.count():
            p_text = await parent.first.inner_text()
            if p_text:
                return parse_size(p_text)
    except Exception:
        pass
    raise ScrapeError('Release has neither a valid size attribute nor an xl parameter in magnet.')


GENERIC_TITLES = {'magnet', 'download', 'torrent', 'direct link', 'link', 'get torrent', 'click here', 'file'}


def normalize_release_title(title):
    """Remove source branding and file syntax while keeping filterable tags."""
    normalized = title.strip()
    normalized = re.sub(r'(?i)^www\.[^\s]+\s*[-|:]\s*', '', normalized)
    normalized = re.sub(r'(?i)\.(?:mkv|mp4|avi|mov|wmv|webm|ts)$', '', normalized)
    normalized = re.sub(r'[._]+', ' ', normalized)
    normalized = re.sub(r'\s*-\s*', ' ', normalized)
    return re.sub(r'\s+', ' ', normalized).strip()


async def extract_release(row, context, page, values, fallback_title='', fallback_date=None):
    title = await field(row, values.get('title_selector'))
    magnet = await field(row, values.get('magnet_selector') or 'a[href^="magnet:"]', 'href')
    if not magnet and (await row.get_attribute('href') or '').startswith('magnet:'):
        magnet = await row.get_attribute('href')
    magnet = magnet or None

    torrent_url = await field(row, values.get('torrent_selector'), 'href')
    if not torrent_url and (await row.get_attribute('href') or '').endswith('.torrent'):
        torrent_url = await row.get_attribute('href')

    digest = magnet_hash(magnet) if magnet else None
    path, size = None, None

    if torrent_url:
        try:
            torrent_url = http_url(urljoin(page.url, torrent_url))
            torrent_digest, size, path = await download_torrent(context, torrent_url)
            if digest and digest != torrent_digest:
                raise ScrapeError('Torrent and magnet hashes do not match.')
            digest = torrent_digest
        except Exception as err:
            logger.debug('Torrent file download failed (%s): %s', torrent_url, err)
            if not magnet:
                raise
            # If download fails but we have magnet, proceed with magnet
            size = await resolve_size(row, values, magnet, title, fallback_title)
    elif magnet:
        size = await resolve_size(row, values, magnet, title, fallback_title)
    else:
        raise ScrapeError('Release has neither a torrent link nor a valid magnet.')

    if not title or title.lower().strip() in GENERIC_TITLES or len(title.strip()) < 4:
        if magnet and magnet_params(magnet).get('dn'):
            title = magnet_params(magnet)['dn'][0].strip()
        elif fallback_title:
            title = fallback_title.strip()
        else:
            title = (await row.inner_text()).strip()

    if not title or len(title) > 1000:
        raise ScrapeError('Release has a missing or oversized title.')
    title = normalize_release_title(title)
    if not title:
        raise ScrapeError('Release has a missing or oversized title.')

    match = re.search(r'(?i)\bS(\d{1,3})(?:E(\d{1,4}))?\b', title)
    if not match and fallback_title:
        match = re.search(r'(?i)\bS(\d{1,3})(?:E(\d{1,4}))?\b', fallback_title)
    season = int(match[1]) if match else None
    episode = int(match[2]) if match and match[2] else None
    category = await row.get_attribute('data-category') or ('tv' if match else 'movie')
    if category not in ('movie', 'tv'):
        raise ScrapeError('Release category must be movie or tv.')

    date_text = await field(row, values.get('date_selector'), 'datetime')
    if date_text:
        date = datetime.fromisoformat(date_text.replace('Z', '+00:00'))
    elif fallback_date:
        date = fallback_date
    else:
        date = utcnow()
    date = date.replace(tzinfo=timezone.utc) if date.tzinfo is None else date.astimezone(timezone.utc)

    imdb = await row.get_attribute('data-imdb')
    if imdb:
        if not re.fullmatch(r'(?:tt)?\d{7,10}', imdb):
            raise ScrapeError('Invalid IMDb ID.')
        imdb = imdb.removeprefix('tt')

    return dict(id=digest, title=title, category=category, size=size,
                pub_date=date, magnet_uri=magnet, torrent_file_path=path,
                imdb_id=imdb, season=season, episode=episode)


async def scrape_detail_page(context, detail_url, values, topic_title, topic_date, releases):
    logger.info('Opening detail page: %s', detail_url)
    detail_page = await context.new_page()
    try:
        response = await detail_page.goto(detail_url, wait_until='domcontentloaded', timeout=60000)
        if response is None or response.status >= 400:
            logger.warning('Detail page %s returned status %s', detail_url, response.status if response else 'None')
            return
        date = topic_date
        if not date:
            d_date_text = await field(detail_page, values.get('date_selector'), 'datetime')
            if d_date_text:
                try:
                    date = datetime.fromisoformat(d_date_text.replace('Z', '+00:00'))
                except Exception:
                    pass

        detail_row_sel = values.get('detail_row_selector')
        if detail_row_sel:
            sub_rows = detail_page.locator(detail_row_sel)
            for idx in range(await sub_rows.count()):
                try:
                    rel = await extract_release(sub_rows.nth(idx), context, detail_page, values, topic_title, date)
                    if rel:
                        merge_release(releases, rel)
                        logger.info('Extracted release: %s (%s)', rel.get('title'), rel.get('category'))
                except Exception as e:
                    logger.debug('Detail row %d failed on %s: %s', idx, detail_url, e)
        else:
            mag_sel = values.get('magnet_selector') or 'a[href^="magnet:"]'
            tor_sel = values.get('torrent_selector') or 'a[href*="attachment.php"], a[href$=".torrent"]'
            downloads = detail_page.locator(f'{mag_sel}, {tor_sel}')
            d_count = await downloads.count()
            if d_count == 0 and await detail_page.locator(values['row_selector']).count() > 0:
                d_rows = detail_page.locator(values['row_selector'])
                for idx in range(await d_rows.count()):
                    try:
                        rel = await extract_release(d_rows.nth(idx), context, detail_page, values, topic_title, date)
                        if rel:
                            merge_release(releases, rel)
                            logger.info('Extracted release: %s (%s)', rel.get('title'), rel.get('category'))
                    except Exception as e:
                        logger.debug('Detail fallback row %d failed on %s: %s', idx, detail_url, e)
            else:
                for idx in range(d_count):
                    try:
                        rel = await extract_release(downloads.nth(idx), context, detail_page, values, topic_title, date)
                        if rel:
                            merge_release(releases, rel)
                            logger.info('Extracted release: %s (%s)', rel.get('title'), rel.get('category'))
                    except Exception as e:
                        logger.debug('Detail download link %d failed on %s: %s', idx, detail_url, e)
    finally:
        await detail_page.close()


async def scrape_page_releases(page, context, values, releases, seen_detail_urls, origin):
    try:
        await page.locator(values['row_selector']).first.wait_for(state='attached', timeout=30000)
    except PlaywrightTimeoutError:
        raise ScrapeError(f"Timeout waiting for row selector '{values['row_selector']}'. Target page loaded, but no matching rows were found.")
    rows = page.locator(values['row_selector'])
    count = await rows.count()
    if count == 0:
        raise ScrapeError(f"Zero release rows found matching '{values['row_selector']}'.")
    if count > MAX_ROWS:
        raise ScrapeError(f"Page rows ({count}) exceeds the 2000-row limit.")

    detail_sel = values.get('detail_selector')
    if detail_sel:
        for idx in range(count):
            row = rows.nth(idx)
            detail_href = await field(row, detail_sel, 'href')
            if not detail_href and await row.get_attribute('href'):
                detail_href = await row.get_attribute('href')
            if not detail_href:
                continue
            detail_url = http_url(urljoin(page.url, detail_href).split('#')[0])
            if urlsplit(detail_url).netloc != origin:
                raise ScrapeError('Pagination must remain on the configured host.')
            if detail_url in seen_detail_urls:
                continue
            seen_detail_urls.add(detail_url)
            topic_title = (await field(row, values.get('title_selector'))) or (await row.inner_text() or '').strip()
            topic_date_text = await field(row, values.get('date_selector'), 'datetime')
            topic_date = datetime.fromisoformat(topic_date_text.replace('Z', '+00:00')) if topic_date_text else None
            try:
                await scrape_detail_page(context, detail_url, values, topic_title, topic_date, releases)
            except Exception:
                # Allow scraping to continue if an individual topic page has issues
                pass
    else:
        for idx in range(count):
            rel = await extract_release(rows.nth(idx), context, page, values)
            merge_release(releases, rel)


async def scrape(values):
    target = http_url(values['target_url'])
    origin = urlsplit(target).netloc
    releases = {}
    seen_pages = set()
    seen_detail_urls = set()
    user_agent = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36')
    logger.info('Starting scrape for target: %s', target)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, args=['--disable-dev-shm-usage'])
        try:
            context = await browser.new_context(
                user_agent=user_agent,
                locale='en-US',
                accept_downloads=False,
                service_workers='block'
            )
            context.set_default_timeout(30000)
            page = await context.new_page()
            for page_num in range(1, int(values['max_pages']) + 1):
                if target in seen_pages:
                    break
                seen_pages.add(target)
                logger.info('Loading page %d: %s', page_num, target)
                try:
                    response = await page.goto(target, wait_until='domcontentloaded', timeout=60000)
                except PlaywrightTimeoutError:
                    raise ScrapeError(f"Timeout loading '{target}'. Page did not complete loading within 60 seconds.")
                except Exception as exc:
                    raise ScrapeError(f"Failed to navigate to '{target}': {type(exc).__name__}")
                if response is None:
                    raise ScrapeError(f"No response received from '{target}'.")
                if response.status >= 400:
                    raise ScrapeError(f"Target '{target}' returned HTTP status {response.status}.")
                active_values = await inspect_and_auto_detect_selectors(page, values)
                await scrape_page_releases(page, context, active_values, releases, seen_detail_urls, origin)
                logger.info('Found %d unique releases so far', len(releases))
                next_link = await field(page, active_values.get('next_selector'), 'href')
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
    logger.info('Scrape completed: %d new releases added to database.', added)
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
                if isinstance(exc, ScrapeError):
                    message = str(exc)
                elif isinstance(exc, PlaywrightTimeoutError):
                    message = f'TimeoutError: scraper timed out on {type(exc).__name__}. Check target availability and page load.'
                else:
                    message = f'{type(exc).__name__}: scrape failed. Check target availability, CSS selectors, and release metadata.'
                with SessionLocal.begin() as session:
                    session.add(ScraperLog(status='failure', items_added=0, error_message=message))
                await asyncio.to_thread(notify_failure, message)
            finally:
                with SessionLocal.begin() as session:
                    keep = select(ScraperLog.id).order_by(ScraperLog.id.desc()).limit(1000)
                    session.execute(delete(ScraperLog).where(ScraperLog.id.not_in(keep)))
