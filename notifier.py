"""Telegram errors never hide the original scrape failure or expose credentials."""
import logging
import re
import requests
from config import settings

logger = logging.getLogger(__name__)

def notify_failure(message):
    values = settings()
    token, chat = values.get('telegram_bot_token'), values.get('telegram_chat_id')
    if not token or not chat:
        return False
    escaped = re.sub(r'([_*\[\]()~`>#+\-=|{}.!\\])', r'\\\1', message[:2000])
    try:
        response = requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                                 json={'chat_id': chat, 'text': '*Lokal indexer failure*\n' + escaped,
                                       'parse_mode': 'MarkdownV2'}, timeout=(5, 15))
        response.raise_for_status()
        return bool(response.json().get('ok'))
    except (requests.RequestException, ValueError):
        logger.warning('Telegram notification failed; check credentials and connectivity.')
        return False
