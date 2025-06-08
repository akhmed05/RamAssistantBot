import requests
from config import GOOGLE_SCRIPT_URL

def handle_youtube_summary(url: str) -> str:
    if "youtube.com" in url or "youtu.be" in url:
        return f"Краткое саммари для видео: {url}\n(в реальности здесь будет AI-резюме)"
    return None

def handle_google_form_submission(data: str) -> bool:
    try:
        name, phone, comment = [x.strip() for x in data.split("/", 2)]
        response = requests.post(GOOGLE_SCRIPT_URL, data={
            "Имя": name,
            "Телефон": phone,
            "Комментарий": comment
        })
        return response.status_code == 200
    except Exception:
        return False
