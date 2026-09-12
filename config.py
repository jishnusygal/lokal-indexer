import secrets
from urllib.parse import urlsplit
from sqlalchemy import select
from database import SessionLocal
from models import Setting

DEFAULTS = {
    'target_url': '', 'public_url': '', 'telegram_bot_token': '', 'telegram_chat_id': '',
    'sync_interval': '60', 'row_selector': '.release', 'title_selector': '.title',
    'torrent_selector': 'a[href$=".torrent"]', 'magnet_selector': 'a[href^="magnet:"]',
    'size_selector': '.size', 'date_selector': 'time', 'next_selector': '', 'max_pages': '5',
}

def settings():
    with SessionLocal() as session:
        return dict(session.execute(select(Setting.key, Setting.value)).all())

def seed():
    with SessionLocal.begin() as session:
        for key, value in {**DEFAULTS, 'api_key': secrets.token_urlsafe(32)}.items():
            if session.get(Setting, key) is None:
                session.add(Setting(key=key, value=value))

def http_url(value):
    parts = urlsplit(value)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise ValueError('URLs must be HTTP(S), with a hostname and without embedded credentials.')
    return value

def validate(values):
    if not 16 <= len(values['api_key']) <= 256:
        raise ValueError('API key must contain 16–256 characters.')
    for key in ('target_url', 'public_url'):
        if values[key]:
            http_url(values[key])
    if values['public_url'] and (urlsplit(values['public_url']).query or urlsplit(values['public_url']).fragment):
        raise ValueError('Public URL cannot contain a query or fragment.')
    if not 1 <= int(values['sync_interval']) <= 10080:
        raise ValueError('Sync interval must be 1–10080 minutes.')
    if not 1 <= int(values['max_pages']) <= 100:
        raise ValueError('Maximum pages must be 1–100.')
    if not values['row_selector'] or not values['title_selector']:
        raise ValueError('Row and title selectors are required.')
    if bool(values['telegram_bot_token']) != bool(values['telegram_chat_id']):
        raise ValueError('Set both Telegram token and chat ID, or leave both empty.')
