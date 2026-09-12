"""Notification errors never hide the original scrape failure or expose credentials."""
import logging
import re
import requests
from config import settings

logger = logging.getLogger(__name__)

def notify_failure(message):
    values = settings()
    sent = False
    token, chat = values.get('telegram_bot_token'), values.get('telegram_chat_id')
    if token and chat:
        escaped = re.sub(r'([_*\[\]()~`>#+\-=|{}.!\\])', r'\\\1', message[:2000])
        try:
            response = requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                                     json={'chat_id': chat, 'text': '*Lokal indexer failure*\n' + escaped,
                                           'parse_mode': 'MarkdownV2'}, timeout=(5, 15))
            response.raise_for_status()
            sent = bool(response.json().get('ok')) or sent
        except (requests.RequestException, ValueError):
            logger.warning('Telegram notification failed; check credentials and connectivity.')
    webhook_url = values.get('webhook_url')
    if webhook_url:
        try:
            response = requests.post(webhook_url, json={'service': 'lokal-indexer', 'event': 'scrape_failure',
                                                        'message': message[:4000]}, timeout=(5, 15))
            response.raise_for_status()
            sent = True
        except requests.RequestException:
            logger.warning('Webhook notification failed; check the configured URL and receiver.')
    return sent
