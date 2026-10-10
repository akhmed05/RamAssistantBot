import os

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") or os.getenv("RAM_OPENAI_API_KEY")
BOT_PROXY = os.getenv("BOT_PROXY") or os.getenv("HTTPS_PROXY")
GOOGLE_SCRIPT_URL = os.getenv(
    "GOOGLE_SCRIPT_URL",
    "https://script.google.com/macros/s/AKfycbwM1QNWIrkTLivg4Zwc_aEf12rkobLUNzQ-RwropzKusB1Ons_POzJ6DZzmiVQ4Cxm0/exec",
)

# A model must be deliberately chosen from models available to YOUR OpenAI project.
# No hard-coded fallback to an unavailable model.
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "").strip()
OPENAI_MAX_TOKENS = int(os.getenv("OPENAI_MAX_TOKENS", "800"))
RAM_DB_PATH = os.getenv("RAM_DB_PATH", "data/ram_ai.sqlite3")
