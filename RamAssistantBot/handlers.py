from typing import Optional
from urllib.parse import urlsplit

import requests

from config import GOOGLE_SCRIPT_URL


def is_youtube_url(url: str) -> bool:
    if not isinstance(url, str):
        return False
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and parsed.hostname in {
        "youtube.com", "www.youtube.com", "m.youtube.com",
        "music.youtube.com", "youtu.be", "www.youtu.be",
    }


def handle_youtube_summary(url: str) -> Optional[str]:
    if is_youtube_url(url):
        return f"Краткое саммари для видео: {url}\n(в реальности здесь будет AI-резюме)"
    return None


def handle_google_form_submission(data: str) -> bool:
    if not isinstance(data, str):
        return False
    fields = [x.strip() for x in data.split("/", 2)]
    if len(fields) != 3 or not all(fields):
        return False
    name, phone, comment = fields
    try:
        response = requests.post(GOOGLE_SCRIPT_URL, data={
            "Имя": name,
            "Телефон": phone,
            "Комментарий": comment
        }, timeout=15)
        return response.status_code == 200
    except requests.RequestException:
        return False
