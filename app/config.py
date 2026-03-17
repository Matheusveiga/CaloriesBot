import os
import logging
import time
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from google import genai
from supabase import create_client, Client
from groq import AsyncGroq
from fastapi import FastAPI
import httpx

load_dotenv()

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("CaloriesBot")

# Config
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
SERPER_API_KEY = os.getenv("SERPER_API_KEY") 
FATSECRET_CLIENT_ID = os.getenv("FATSECRET_CLIENT_ID")
FATSECRET_CLIENT_SECRET = os.getenv("FATSECRET_CLIENT_SECRET")
FATSECRET_PROXIES = [p.strip() for p in os.getenv("FATSECRET_PROXIES", "").split(",") if p.strip()]
WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL")

# Validation
missing_vars = []
if not TELEGRAM_TOKEN: missing_vars.append("TELEGRAM_BOT_TOKEN")
if not GEMINI_KEY: missing_vars.append("GEMINI_API_KEY")
if not SUPABASE_URL: missing_vars.append("SUPABASE_URL")
if not SUPABASE_KEY: missing_vars.append("SUPABASE_KEY")
if not WEBHOOK_URL: missing_vars.append("RENDER_EXTERNAL_URL")
if not FATSECRET_CLIENT_ID: missing_vars.append("FATSECRET_CLIENT_ID")
if not FATSECRET_CLIENT_SECRET: missing_vars.append("FATSECRET_CLIENT_SECRET")

if missing_vars:
    error_msg = f"❌ Faltando variáveis de ambiente: {', '.join(missing_vars)}"
    logger.error(error_msg)
    raise ValueError(error_msg)

if not SUPABASE_URL.startswith("https://"):
    error_msg = f"❌ SUPABASE_URL inválida: {SUPABASE_URL[:10]}..."
    logger.error(error_msg)
    raise ValueError(error_msg)

# Init Clients
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
fastapi_app = FastAPI()
ai_client = genai.Client(api_key=GEMINI_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
http_client = httpx.AsyncClient(timeout=httpx.Timeout(10.0))

# FatSecret Proxy Config
fs_proxy_index = 0

def get_fs_client():
    """Returns an httpx.AsyncClient with a rotated proxy from the list."""
    global fs_proxy_index
    if not FATSECRET_PROXIES:
        return http_client
    
    proxy_url = FATSECRET_PROXIES[fs_proxy_index % len(FATSECRET_PROXIES)]
    fs_proxy_index += 1
    
    # Note: We create a new client per request to ensure the proxy is used
    # In a very high traffic app, we'd cache these, but per-request is safer for rotation.
    return httpx.AsyncClient(proxies=proxy_url, timeout=httpx.Timeout(15.0))

groq_client = None
if GROQ_API_KEY:
    groq_client = AsyncGroq(api_key=GROQ_API_KEY)

# Constants
AI_MODEL = "gemini-2.0-flash" 
AI_MODEL_FALLBACK = "gemini-1.5-flash"

# Global state
fs_token = {"access_token": None, "expires_at": 0}
processed_messages = set()
user_history = {}
jailbreak_users = {}
AI_CACHE = {}
