import os

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") or os.getenv("RAM_OPENAI_API_KEY")
BOT_PROXY = os.getenv("BOT_PROXY") or os.getenv("HTTPS_PROXY")
GOOGLE_SCRIPT_URL = os.getenv(
    "GOOGLE_SCRIPT_URL",
    "https://script.google.com/macros/s/AKfycbwM1QNWIrkTLivg4Zwc_aEf12rkobLUNzQ-RwropzKusB1Ons_POzJ6DZzmiVQ4Cxm0/exec",
)
