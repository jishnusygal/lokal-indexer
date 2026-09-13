import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import func, select, text

from config import DEFAULTS, seed, settings, validate
from database import Base, DATA_DIR, SessionLocal, TORRENT_DIR, engine
from models import Release, ScraperLog, Setting
from scraper import ScraperWorker
import torznab

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('lokal-indexer')

templates = Jinja2Templates(directory=str(Path(__file__).parent / 'templates'))
SESSION_COOKIE = 'lokal_session'


def service_url(values, request):
    configured_url = os.environ.get('LOKAL_PUBLIC_URL') or values.get('public_url')
    if configured_url:
        return configured_url.rstrip('/')
    forwarded_proto = request.headers.get('x-forwarded-proto', '').split(',', 1)[0].strip()
    forwarded_host = request.headers.get('x-forwarded-host', '').split(',', 1)[0].strip()
    if forwarded_proto in ('http', 'https') and forwarded_host:
        return f'{forwarded_proto}://{forwarded_host}'
    return str(request.base_url).rstrip('/')


PASSWORD_PREFIX = 'scrypt$'


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return f'{PASSWORD_PREFIX}16384$8$1${salt.hex()}${digest.hex()}'


def verify_password(password, stored):
    if not stored.startswith(PASSWORD_PREFIX):
        return hmac.compare_digest(password.encode(), stored.encode())
    try:
        _, n, r, p, salt_hex, digest_hex = stored.split('$')
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex),
                                n=int(n), r=int(r), p=int(p), dklen=len(bytes.fromhex(digest_hex)))
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def session_user(request):
    token = request.cookies.get(SESSION_COOKIE)
    if not token or not request.app.state.admin_password_hash:
        return None
    try:
        payload = request.app.state.signer.loads(token, max_age=86400)
        return payload.get('username') if payload.get('username') == 'admin' else None
    except BadSignature:
        return None


def admin(request: Request):
    if not request.app.state.admin_password_hash:
        raise HTTPException(303, headers={'Location': '/setup'})
    if session_user(request) != 'admin':
        raise HTTPException(303, headers={'Location': '/login'})


def set_session(response, request):
    response.set_cookie(SESSION_COOKIE, request.app.state.signer.dumps({'username': 'admin'}),
                       max_age=86400, httponly=True, secure=request.url.scheme == 'https',
                       samesite='lax')


def check_csrf(request, token):
    try:
        request.app.state.signer.loads(token, max_age=3600)
    except BadSignature:
        raise HTTPException(403, 'Form expired or invalid. Reload settings.')


def schedule(app, interval):
    app.state.scheduler.add_job(app.state.worker.run, 'interval', minutes=int(interval), id='scrape',
                                replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=60)


def migrate_schema():
    with engine.begin() as connection:
        columns = connection.execute(text('PRAGMA table_info(scraper_logs)')).all()
        if columns and not any(row[1] == 'duration_seconds' for row in columns):
            connection.execute(text('ALTER TABLE scraper_logs ADD COLUMN duration_seconds FLOAT'))


@asynccontextmanager
async def lifespan(app):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TORRENT_DIR.mkdir(parents=True, exist_ok=True)
    credential = DATA_DIR / 'admin-password'
    stored_password = credential.read_text().strip() if credential.exists() else ''
    initial_password = os.environ.get('LOKAL_ADMIN_PASSWORD')
    if not stored_password and initial_password:
        stored_password = hash_password(initial_password)
        credential.write_text(stored_password)
        os.chmod(credential, 0o600)
    if stored_password and not stored_password.startswith(PASSWORD_PREFIX):
        stored_password = hash_password(stored_password)
        credential.write_text(stored_password)
        os.chmod(credential, 0o600)
    app.state.admin_password_hash = stored_password or None
    signer_secret = stored_password or secrets.token_urlsafe(32)
    app.state.signer = URLSafeTimedSerializer(signer_secret, salt='settings-session')
    Base.metadata.create_all(engine)
    migrate_schema()
    seed()
    app.state.worker = ScraperWorker()
    app.state.scheduler = AsyncIOScheduler(timezone='UTC')
    schedule(app, settings()['sync_interval'])
    app.state.scheduler.start()
    app.state.initial_sync = asyncio.create_task(app.state.worker.run())
    try:
        yield
    finally:
        app.state.scheduler.pause()
        # Wait for any active scrape to finish before disposing database resources.
        await app.state.initial_sync
        async with app.state.worker.lock:
            app.state.scheduler.shutdown(wait=False)
        await asyncio.sleep(0)
        engine.dispose()


app = FastAPI(title='Lokal Indexer', lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware('http')
async def security_headers(request, call_next):
    csp_nonce = secrets.token_urlsafe(24)
    request.state.csp_nonce = csp_nonce
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Content-Security-Policy'] = f"default-src 'none'; script-src 'nonce-{csp_nonce}'; style-src 'unsafe-inline'; img-src 'self' data:; form-action 'self'; frame-ancestors 'none'"
    return response


@app.get('/')
def home():
    return RedirectResponse('/settings')


@app.get('/health')
def health():
    with SessionLocal() as session:
        session.execute(select(1))
    return {'status': 'ok'}


@app.get('/setup')
def setup_page(request: Request):
    if request.app.state.admin_password_hash:
        return RedirectResponse('/login', status_code=303)
    return templates.TemplateResponse(request=request, name='setup.html', context={'error': None})


@app.post('/setup')
async def setup_account(request: Request):
    if request.app.state.admin_password_hash:
        return RedirectResponse('/login', status_code=303)
    form = await request.form(max_fields=10)
    password = str(form.get('password', ''))
    confirmation = str(form.get('confirmation', ''))
    if len(password) < 12:
        error = 'Password must be at least 12 characters.'
    elif len(password) > 4096:
        error = 'Password exceeds 4096 characters.'
    elif password != confirmation:
        error = 'Password confirmation does not match.'
    else:
        password_hash = hash_password(password)
        credential = DATA_DIR / 'admin-password'
        credential.write_text(password_hash)
        os.chmod(credential, 0o600)
        request.app.state.admin_password_hash = password_hash
        request.app.state.signer = URLSafeTimedSerializer(password_hash, salt='settings-session')
        response = RedirectResponse('/settings', status_code=303)
        set_session(response, request)
        return response
    return templates.TemplateResponse(request=request, name='setup.html', context={'error': error}, status_code=400)


@app.get('/login')
def login_page(request: Request):
    if not request.app.state.admin_password_hash:
        return RedirectResponse('/setup', status_code=303)
    if session_user(request) == 'admin':
        return RedirectResponse('/settings', status_code=303)
    return templates.TemplateResponse(request=request, name='login.html', context={'error': None})


@app.post('/login')
async def login(request: Request):
    if not request.app.state.admin_password_hash:
        return RedirectResponse('/setup', status_code=303)
    form = await request.form(max_fields=10)
    if str(form.get('username', '')) != 'admin' or not verify_password(str(form.get('password', '')), request.app.state.admin_password_hash):
        return templates.TemplateResponse(request=request, name='login.html', context={'error': 'Invalid username or password.'}, status_code=401)
    response = RedirectResponse('/settings', status_code=303)
    set_session(response, request)
    return response


@app.post('/logout')
def logout():
    response = RedirectResponse('/login', status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get('/settings', dependencies=[Depends(admin)])
def settings_page(request: Request):
    values = settings()
    query = request.query_params.get('q', '').strip()
    try:
        page_num = max(1, int(request.query_params.get('page', 1)))
    except (TypeError, ValueError):
        page_num = 1
    page_size = 25
    with SessionLocal() as session:
        logs = session.scalars(select(ScraperLog).order_by(ScraperLog.id.desc()).limit(20)).all()
        count = session.scalar(select(func.count()).select_from(Release))
        rel_query = select(Release).order_by(Release.pub_date.desc())
        if query:
            rel_query = rel_query.where(Release.title.ilike(f'%{query}%'))
            filtered_count = session.scalar(select(func.count()).select_from(Release).where(Release.title.ilike(f'%{query}%')))
        else:
            filtered_count = count
        total_pages = max(1, (filtered_count + page_size - 1) // page_size)
        if page_num > total_pages:
            page_num = total_pages
        recent_releases = session.scalars(rel_query.offset((page_num - 1) * page_size).limit(page_size)).all()
    service_base_url = service_url(values, request)
    sonarr_key = values.get('sonarr_api_key', values['api_key'])
    radarr_key = values.get('radarr_api_key', values['api_key'])
    indexer_base_url = f'{service_base_url.rstrip("/")}/api'
    return templates.TemplateResponse(request=request, name='settings.html', context={
        'values': values, 'logs': logs, 'count': count, 'saved': request.query_params.get('saved'),
        'error': request.query_params.get('error'),
        'csrf': request.app.state.signer.dumps(secrets.token_urlsafe(16)),
        'csp_nonce': request.state.csp_nonce,
        'running': request.app.state.worker.lock.locked(),
        'indexer_base_url': indexer_base_url,
        'indexer_service_url': service_base_url,
        'sonarr_caps_url': f'{indexer_base_url}?t=caps&apikey={sonarr_key}',
        'radarr_caps_url': f'{indexer_base_url}?t=caps&apikey={radarr_key}',
        'last_log': logs[0] if logs else None,
        'recent_releases': recent_releases,
        'search_query': query,
        'page_num': page_num,
        'total_pages': total_pages,
        'filtered_count': filtered_count,
    })


@app.post('/settings', dependencies=[Depends(admin)])
async def save_settings(request: Request):
    form = await request.form(max_fields=40)
    check_csrf(request, str(form.get('csrf', '')))
    current = settings()
    keys = [*DEFAULTS, 'api_key', 'sonarr_api_key', 'radarr_api_key']
    values = {key: str(form.get(key, current.get(key, ''))).strip() for key in keys}
    # Blank secret fields retain the existing value; clearing Telegram is explicit.
    for key in ('api_key', 'sonarr_api_key', 'radarr_api_key', 'telegram_bot_token'):
        if not values[key]:
            values[key] = current.get(key, '')
    if not values['api_key']:
        values['api_key'] = values['sonarr_api_key']
    if form.get('clear_telegram'):
        values['telegram_bot_token'] = values['telegram_chat_id'] = ''
    try:
        if any(len(value) > 4096 for value in values.values()):
            raise ValueError('Setting exceeds 4096 characters.')
        validate(values)
    except ValueError as exc:
        return RedirectResponse(f'/settings?error={quote(str(exc))}', status_code=303)
    with SessionLocal.begin() as session:
        for key, value in values.items():
            session.merge(Setting(key=key, value=value))
    schedule(request.app, values['sync_interval'])
    return RedirectResponse('/settings?saved=1', status_code=303)


@app.post('/account/api-key', dependencies=[Depends(admin)])
async def manage_api_key(request: Request):
    form = await request.form(max_fields=10)
    check_csrf(request, str(form.get('csrf', '')))
    action = str(form.get('action', ''))
    if action == 'regenerate':
        value = secrets.token_urlsafe(32)
        message = 'regenerated'
    elif action == 'revoke':
        value = ''
        message = 'revoked'
    else:
        return RedirectResponse('/settings?error=Invalid+API+key+action', status_code=303)
    with SessionLocal.begin() as session:
        for key in ('api_key', 'sonarr_api_key', 'radarr_api_key'):
            session.merge(Setting(key=key, value=value))
    return RedirectResponse(f'/settings?saved=api-key-{message}', status_code=303)


@app.post('/account/password', dependencies=[Depends(admin)])
async def change_password(request: Request):
    form = await request.form(max_fields=10)
    check_csrf(request, str(form.get('csrf', '')))
    current_password = str(form.get('current_password', ''))
    new_password = str(form.get('new_password', ''))
    confirm_password = str(form.get('confirm_password', ''))
    try:
        if not verify_password(current_password, request.app.state.admin_password_hash):
            raise ValueError('Current password is incorrect.')
        if new_password != confirm_password:
            raise ValueError('New password confirmation does not match.')
        if len(new_password) < 12:
            raise ValueError('New password must be at least 12 characters.')
        if len(new_password) > 4096:
            raise ValueError('New password exceeds 4096 characters.')
    except ValueError as exc:
        return RedirectResponse(f'/settings?error={quote(str(exc))}', status_code=303)
    credential = DATA_DIR / 'admin-password'
    password_hash = hash_password(new_password)
    credential.write_text(password_hash)
    os.chmod(credential, 0o600)
    request.app.state.admin_password_hash = password_hash
    request.app.state.signer = URLSafeTimedSerializer(password_hash, salt='settings-csrf')
    return RedirectResponse('/settings?saved=password', status_code=303)


@app.post('/sync', dependencies=[Depends(admin)])
async def sync_now(request: Request):
    form = await request.form()
    check_csrf(request, str(form.get('csrf', '')))
    # Reuse the same scheduler job so manual and scheduled triggers cannot overlap.
    request.app.state.scheduler.modify_job('scrape', next_run_time=datetime.now(timezone.utc))
    return RedirectResponse('/settings', status_code=303)


def authorized(request, kind=None):
    key = request.query_params.get('apikey', '')
    values = settings()
    if kind == 'tvsearch':
        allowed = [values.get('sonarr_api_key', values.get('api_key', ''))]
    elif kind == 'movie':
        allowed = [values.get('radarr_api_key', values.get('api_key', ''))]
    else:
        allowed = [values.get('sonarr_api_key', ''), values.get('radarr_api_key', ''), values.get('api_key', '')]
    return bool(key) and any(secret and hmac.compare_digest(key.encode(), secret.encode()) for secret in allowed)


def xml_response(body, status=200):
    return Response(body, media_type='application/xml', status_code=status)


@app.get('/api')
def api(request: Request):
    params = request.query_params
    kind = params.get('t', 'search')
    if not authorized(request, kind):
        return xml_response(torznab.error(100, 'Incorrect API key'), 401)
    if kind == 'caps':
        return xml_response(torznab.caps())
    if kind not in ('search', 'movie', 'tvsearch'):
        return xml_response(torznab.error(202, 'Unsupported function'))
    try:
        limit, offset = int(params.get('limit', '100')), int(params.get('offset', '0'))
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError()
        conditions = []
        if kind != 'search':
            conditions.append(Release.category == ('movie' if kind == 'movie' else 'tv'))
        if params.get('cat'):
            categories = {int(value) // 1000 for value in params['cat'].split(',')}
            conditions.append(Release.category.in_([name for number, name in [(2, 'movie'), (5, 'tv')] if number in categories]))
        for word in params.get('q', '').split():
            conditions.append(Release.title.icontains(word, autoescape=True))
        if params.get('imdbid'):
            imdb = params['imdbid'].removeprefix('tt')
            if not imdb.isdigit():
                raise ValueError()
            conditions.append(Release.imdb_id == imdb)
        for field, column in [('season', Release.season), ('ep', Release.episode)]:
            if params.get(field):
                number = int(params[field])
                if number < 0:
                    raise ValueError()
                conditions.append(column == number)
        with SessionLocal() as session:
            total = session.scalar(select(func.count()).select_from(Release).where(*conditions))
            releases = session.scalars(select(Release).where(*conditions).order_by(Release.pub_date.desc(), Release.id).offset(offset).limit(limit)).all()
        values = settings()
        api_key = values.get('api_key') or values.get('radarr_api_key' if kind == 'movie' else 'sonarr_api_key', '')
        return xml_response(torznab.feed(releases, total, offset, service_url(values, request), api_key))
    except ValueError:
        return xml_response(torznab.error(201, 'Invalid search parameters'))


@app.get('/download/{release_id}')
def download(release_id: str, request: Request):
    if not authorized(request):
        raise HTTPException(401, 'Incorrect API key')
    with SessionLocal() as session:
        release = session.get(Release, release_id)
    if release is None:
        raise HTTPException(404, 'Release not found')
    if release.torrent_file_path:
        root = TORRENT_DIR.resolve()
        path = (root / release.torrent_file_path).resolve()
        if path.parent != root or not path.is_file():
            raise HTTPException(404, 'Cached torrent not found')
        return FileResponse(path, media_type='application/x-bittorrent', filename=f'{release.id}.torrent')
    if release.magnet_uri:
        return RedirectResponse(release.magnet_uri, status_code=302)
    raise HTTPException(404, 'No download available')
