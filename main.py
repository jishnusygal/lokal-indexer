import asyncio
import hmac
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import func, select

from config import DEFAULTS, seed, settings, validate
from database import Base, DATA_DIR, SessionLocal, TORRENT_DIR, engine
from models import Release, ScraperLog, Setting
from scraper import ScraperWorker
import torznab

basic = HTTPBasic()
templates = Jinja2Templates(directory=str(Path(__file__).parent / 'templates'))


def admin(request: Request, credentials: HTTPBasicCredentials = Depends(basic)):
    if not (hmac.compare_digest(credentials.username.encode(), b'admin') and
            hmac.compare_digest(credentials.password.encode(), request.app.state.admin_password.encode())):
        raise HTTPException(401, 'Invalid admin credentials', headers={'WWW-Authenticate': 'Basic realm="Lokal"'})


def check_csrf(request, token):
    try:
        request.app.state.signer.loads(token, max_age=3600)
    except BadSignature:
        raise HTTPException(403, 'Form expired or invalid. Reload settings.')


def schedule(app, interval):
    app.state.scheduler.add_job(app.state.worker.run, 'interval', minutes=int(interval), id='scrape',
                                replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=60)


@asynccontextmanager
async def lifespan(app):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TORRENT_DIR.mkdir(parents=True, exist_ok=True)
    credential = DATA_DIR / 'admin-password'
    try:
        with open(credential, 'x', opener=lambda path, flags: os.open(path, flags, 0o600)) as output:
            output.write(secrets.token_urlsafe(32))
    except FileExistsError:
        pass
    app.state.admin_password = credential.read_text().strip()
    if not app.state.admin_password:
        raise RuntimeError('Admin password file is empty.')
    app.state.signer = URLSafeTimedSerializer(app.state.admin_password, salt='settings-csrf')
    Base.metadata.create_all(engine)
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
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Content-Security-Policy'] = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"
    return response


@app.get('/')
def home():
    return RedirectResponse('/settings')


@app.get('/health')
def health():
    with SessionLocal() as session:
        session.execute(select(1))
    return {'status': 'ok'}


@app.get('/settings', dependencies=[Depends(admin)])
def settings_page(request: Request):
    values = settings()
    with SessionLocal() as session:
        logs = session.scalars(select(ScraperLog).order_by(ScraperLog.id.desc()).limit(20)).all()
        count = session.scalar(select(func.count()).select_from(Release))
    return templates.TemplateResponse(request=request, name='settings.html', context={
        'values': values, 'logs': logs, 'count': count, 'saved': request.query_params.get('saved'),
        'csrf': request.app.state.signer.dumps(secrets.token_urlsafe(16)),
        'running': request.app.state.worker.lock.locked(),
    })


@app.post('/settings', dependencies=[Depends(admin)])
async def save_settings(request: Request):
    form = await request.form(max_fields=30)
    check_csrf(request, str(form.get('csrf', '')))
    current = settings()
    values = {key: str(form.get(key, current[key])).strip() for key in [*DEFAULTS, 'api_key']}
    # Blank secret fields retain the existing value; clearing Telegram is explicit.
    for key in ('api_key', 'telegram_bot_token'):
        if not values[key]:
            values[key] = current[key]
    if form.get('clear_telegram'):
        values['telegram_bot_token'] = values['telegram_chat_id'] = ''
    try:
        if any(len(value) > 4096 for value in values.values()):
            raise ValueError('Setting exceeds 4096 characters.')
        validate(values)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    with SessionLocal.begin() as session:
        for key, value in values.items():
            session.merge(Setting(key=key, value=value))
    schedule(request.app, values['sync_interval'])
    return RedirectResponse('/settings?saved=1', status_code=303)


@app.post('/sync', dependencies=[Depends(admin)])
async def sync_now(request: Request):
    form = await request.form()
    check_csrf(request, str(form.get('csrf', '')))
    # Reuse the same scheduler job so manual and scheduled triggers cannot overlap.
    request.app.state.scheduler.modify_job('scrape', next_run_time=datetime.now(timezone.utc))
    return RedirectResponse('/settings', status_code=303)


def authorized(request):
    key = request.query_params.get('apikey', '')
    return bool(key) and hmac.compare_digest(key.encode(), settings()['api_key'].encode())


def xml_response(body, status=200):
    return Response(body, media_type='application/xml', status_code=status)


@app.get('/api')
def api(request: Request):
    if not authorized(request):
        return xml_response(torznab.error(100, 'Incorrect API key'), 401)
    params = request.query_params
    kind = params.get('t', 'search')
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
        return xml_response(torznab.feed(releases, total, offset, values['public_url'] or str(request.base_url).rstrip('/'), values['api_key']))
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
