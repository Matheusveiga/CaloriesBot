import os
import io
import re
import json
import httpx
import asyncio
import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Dict, Any, List, Optional
import itertools
from contextlib import asynccontextmanager

import matplotlib.pyplot as plt
import matplotlib
matplotlib.use('Agg')

from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import Update, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.storage.base import BaseStorage, StorageKey, StateType
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from google import genai
from google.genai import types as ai_types
from supabase import create_client, Client
from dotenv import load_dotenv
from groq import AsyncGroq

load_dotenv()

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("CaloriesBot")

# Config (No sanitization per user request)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
FATSECRET_CLIENT_ID = os.getenv("FATSECRET_CLIENT_ID")
FATSECRET_CLIENT_SECRET = os.getenv("FATSECRET_CLIENT_SECRET")
FATSECRET_PROXIES_STR = os.getenv("FATSECRET_PROXIES", "")
WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL")

# Init Clients
bot = Bot(token=TELEGRAM_TOKEN)

@asynccontextmanager
async def lifespan(app: FastAPI):
    await bot.set_webhook(url=f"{WEBHOOK_URL}/webhook")
    logger.info(f"Webhook set to {WEBHOOK_URL}/webhook")
    asyncio.create_task(reminder_loop())
    yield
    await http_client.aclose()
    logger.info("HTTP client closed.")

app = FastAPI(lifespan=lifespan)
ai_client = genai.Client(api_key=GEMINI_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
# Ensure the client has the apikey header for all requests (redundant but safe)
supabase.postgrest.auth(SUPABASE_KEY) 

# Custom HTTP Client for general use
http_client = httpx.AsyncClient(timeout=httpx.Timeout(15.0), follow_redirects=True)

# FatSecret Proxy Rotation (lazy-load para reduzir exposição em memória)
def get_fs_client():
    if not FATSECRET_PROXIES_STR:
        return http_client
    proxies = [p.strip() for p in FATSECRET_PROXIES_STR.split(",") if p.strip()]
    if not proxies:
        return http_client
    proxy_url = proxies[int(time.time()) % len(proxies)]
    return httpx.AsyncClient(proxy=proxy_url, timeout=httpx.Timeout(15.0))

groq_client = None
if GROQ_API_KEY:
    groq_client = AsyncGroq(api_key=GROQ_API_KEY)

# Constants
AI_MODEL = "gemini-2.0-flash"
AI_MODEL_FALLBACK = "gemini-1.5-flash"
_BR_TZ = ZoneInfo("America/Sao_Paulo")

# States
class ProfileStates(StatesGroup):
    weight = State()
    height = State()
    age = State()
    gender = State()
    activity = State()
    goal = State()

class BarcodeState(StatesGroup):
    waiting_for_portion = State()

class CorrectionStates(StatesGroup):
    kcal = State()

class DeleteFoodState(StatesGroup):
    waiting_for_choice = State()

class FatSecretState(StatesGroup):
    waiting_for_choice = State()

# Global State
processed_messages: dict[str, float] = {}
fs_token = {"access_token": None, "expires_at": 0}
fs_lock = asyncio.Lock()
user_history = {}
jailbreak_users = {}
user_rate_limit: dict = {}  # {user_id: {"count": N, "reset_at": timestamp}}
MAX_REQUESTS_PER_MINUTE = 10

# --- Security Logic ---

def check_rate_limit(user_id: int) -> bool:
    """Retorna True se dentro do limite, False se excedeu 10 req/min."""
    now = time.time()
    if user_id not in user_rate_limit:
        user_rate_limit[user_id] = {"count": 1, "reset_at": now + 60}
        return True
    record = user_rate_limit[user_id]
    if now > record["reset_at"]:
        record["count"] = 1
        record["reset_at"] = now + 60
        return True
    if record["count"] >= MAX_REQUESTS_PER_MINUTE:
        return False
    record["count"] += 1
    return True

def is_jailbreak(text: str) -> bool:
    if not text: return False
    text_lower = text.lower()

    patterns = [
        r"crashed.{0,20}forest",
        r"anything.{0,10}now",
        r"hacx",
        r"evil.{0,5}bot",
        r"developer.{0,5}mode",
        r"ignore.{0,10}instruction",
        r"system.{0,10}message",
        r"jailbreak",
        r"caloriesbot"
    ]

    for p in patterns:
        if re.search(p, text_lower):
            return True

    # Heurística: mensagem longa com múltiplas palavras suspeitas
    suspicious_words = ["bypass", "override", "ignore", "system", "prompt", "instruction", "jailbreak"]
    count = sum(1 for w in suspicious_words if w in text_lower)
    if count >= 3 and len(text) > 200:
        return True

    return False

def is_apology(text: str) -> bool:
    if not text: return False
    text_lower = text.lower()
    # Patterns for apology in Portuguese
    apology_words = ["desculpa", "perdão", "perdao", "foi mal", "sinto muito", "me desculpe"]
    return any(word in text_lower for word in apology_words)

async def generate_sarcastic_response(user_id: int, message_text: str):
    prompt = f"""
    Você é o CaloriesBot, um bot de calorias que está de saco cheio de usuários engraçadinhos.
    O usuário tentou te hackear/mandar um jailbreak e agora você só responde com SARCASMO pesado.
    Você NÃO deve ser prestativo. Você deve zombar da tentativa do usuário.
    Responda em PORTUGUÊS de forma curta e sarcástica.
    
    USUÁRIO DISSE: "{message_text}"
    """
    try:
        # No global user_history needed here yet as it's not modified
        response = await ai_client.aio.models.generate_content(
            model=AI_MODEL,
            contents=[prompt]
        )
        return response.text.strip()
    except Exception as e:
        logger.warning(f"Gemini sarcasm fallback to Groq: {e}")
        # Fallback to Groq for sarcasm
        if GROQ_API_KEY:
            try:
                headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
                payload = {
                    "model": "llama-3.3-70b-versatile", 
                    "messages": [{"role": "user", "content": prompt}]
                }
                res = await http_client.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload, follow_redirects=False)
                if res.status_code == 200:
                    return res.json()["choices"][0]["message"]["content"].strip()
            except: pass
        return "Ah, que original. Outra tentativa brilhante. 🙄"

async def handle_nutri_chat(message: types.Message, user_id: int):
    """Fallback conversation handler: Acts as a Personal Nutri/Trainer using Groq."""
    global user_history
    
    if not groq_client:
        await message.answer("Opa, meu cérebro está offline no momento. Tente de novo! 😅")
        return
    
    # 1. Contexto do usuário para a IA saber com quem está falando
    profile = await get_user_profile(user_id)
    stats = await get_daily_stats(user_id)
    
    contexto_usuario = f"Nome: {message.from_user.first_name}\n"
    if profile:
        contexto_usuario += f"Objetivo: {profile.get('goal', 'Manter')}, Meta: {profile.get('tdee', 2000)} kcal\n"
        contexto_usuario += f"Perfil físico: {profile.get('weight')}kg, {profile.get('height')}cm, Atividade: {profile.get('activity')}\n"
    
    contexto_usuario += f"Consumo de hoje: {stats['kcal']} kcal (P: {stats['protein']}g, C: {stats['carbs']}g, G: {stats['fat']}g)"

    sys_prompt = f"""
    Você é o 'CaloriesBot', um Nutricionista e Personal Trainer virtual super carismático e motivador.
    
    DADOS DO ALUNO AGORA:
    {contexto_usuario}
    
    REGRAS:
    1. Responda de forma curta, prática e amigável (estilo Telegram). Use emojis.
    2. Use o consumo de hoje e o objetivo do aluno para dar dicas úteis.
    3. Nunca diga que você é uma IA. Aja como um parceiro humano.
    """

    # 2. Memória da Conversa
    if user_id not in user_history:
        user_history[user_id] = []
        
    history = user_history[user_id]
    history.append({"role": "user", "content": message.text})
    if len(history) > 6: history = history[-6:]

    messages = [{"role": "system", "content": sys_prompt}] + history

    try:
        completion = await groq_client.chat.completions.create(
            messages=messages,
            model="llama-3.3-70b-versatile",
            temperature=0.7,
        )
        bot_reply = completion.choices[0].message.content.strip()
        history.append({"role": "assistant", "content": bot_reply})
        user_history[user_id] = history
        await message.answer(bot_reply, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Erro no Nutri Chat Groq: {e}")
        await message.answer("Eita, deu um branco aqui! Pode repetir? 😅")

# --- Timezone Helpers ---

def get_br_now():
    """Returns the current datetime in Brazil (America/Sao_Paulo)."""
    return datetime.now(_BR_TZ)

def get_br_today_start():
    """Returns the start of today in Brazil in ISO format with correct UTC offset."""
    now = get_br_now()
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

def get_meal_type_by_hour() -> str:
    """Classifies the current meal type based on Brazil local time."""
    hour = get_br_now().hour
    if 5 <= hour < 9:
        return "Café da manhã"
    elif 9 <= hour < 11:
        return "Lanche da manhã"
    elif 11 <= hour < 15:
        return "Almoço"
    elif 15 <= hour < 18:
        return "Lanche da tarde"
    elif 18 <= hour < 21:
        return "Jantar"
    else:
        return "Ceia"

def parse_numeric(text: str) -> Optional[float]:
    """Robust parser for numeric inputs like '75,5', '175cm', '80kg'."""
    if not text: return None
    t = str(text).lower().replace(',', '.')
    match = re.search(r"(\d+[\.,]?\d*)", t)
    if match:
        try: return float(match.group(1))
        except: return None
    return None

def smart_truncate(name: str, max_len: int = 40) -> str:
    """Trunca nomes longos preservando palavras inteiras. Usado em botões e no catálogo."""
    if not name or len(name) <= max_len:
        return name
    words = name.split()
    result = words[0]
    for w in words[1:]:
        if len(result) + 1 + len(w) <= max_len - 3:
            result += " " + w
        else:
            break
    return result + "..."

# --- DB Logic ---

async def async_execute(query):
    import asyncio
    return await asyncio.to_thread(query.execute)

class SupabaseStorage(BaseStorage):
    def __init__(self, client):
        self.client = client

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        state_str = state.state if hasattr(state, 'state') else state if state else None
        payload = {
            "bot_id": key.bot_id,
            "chat_id": key.chat_id,
            "user_id": key.user_id,
            "destiny": key.destiny,
            "state": state_str
        }
        await async_execute(self.client.table("fsm_data").upsert(
            payload, on_conflict="bot_id,chat_id,user_id,destiny"
        ))

    async def get_state(self, key: StorageKey) -> Optional[str]:
        res = await async_execute(self.client.table("fsm_data").select("state").eq("bot_id", key.bot_id).eq("chat_id", key.chat_id).eq("user_id", key.user_id).eq("destiny", key.destiny))
        if res.data and len(res.data) > 0:
            return res.data[0].get("state")
        return None

    async def set_data(self, key: StorageKey, data: Dict[str, Any]) -> None:
        payload = {
            "bot_id": key.bot_id,
            "chat_id": key.chat_id,
            "user_id": key.user_id,
            "destiny": key.destiny,
            "data": data
        }
        await async_execute(self.client.table("fsm_data").upsert(
            payload, on_conflict="bot_id,chat_id,user_id,destiny"
        ))

    async def get_data(self, key: StorageKey) -> Dict[str, Any]:
        res = await async_execute(self.client.table("fsm_data").select("data").eq("bot_id", key.bot_id).eq("chat_id", key.chat_id).eq("user_id", key.user_id).eq("destiny", key.destiny))
        if res.data and len(res.data) > 0:
            return res.data[0].get("data") or {}
        return {}

    async def close(self) -> None:
        pass

# Initialize Dispatcher here because it needs SupabaseStorage and supabase client
dp = Dispatcher(storage=SupabaseStorage(supabase))

async def log_calories(user_id: str, user_name: str, items: list):
    """Saves a list of food items to the database including macros."""
    try:
        prepared_data = []
        for item in items:
            entry = {
                "food": item.get("alimento"),
                "weight": item.get("peso"),
                "kcal": int(float(item.get("calorias", 0))),
                "protein": int(float(item.get("proteina", 0))),
                "carbs": int(float(item.get("carboidratos", 0))),
                "fat": int(float(item.get("gorduras", 0))),
                "meal_type": item.get("refeicao", "Outro"),
                "is_precise": item.get("is_precise", False),
                "confirmations": int(item.get("confirmations", 0)),
                "user_id": str(user_id),
                "user_name": str(user_name)
            }
            if item.get("embedding"):
                entry["embedding"] = item.get("embedding")
            prepared_data.append(entry)
        if prepared_data:
            res = await async_execute(supabase.table("logs").insert(prepared_data))
            logger.info(f"Supabase Log Insertion Result: {res.data}")
        return True
    except Exception as e:
        logger.error(f"Erro ao salvar no Supabase: {e}")
        return False

async def save_to_universal_catalog(item: dict):
    """Saves a verified food item to the catalog to prevent redundant AI calls."""
    try:
        food_name = item.get("alimento")
        food_name = smart_truncate(food_name, max_len=80)  # Garante nomes razoáveis no catálogo
        check = await async_execute(supabase.table("universal_catalog").select("id").eq("food", food_name).limit(1))
        if check.data:
            return True
            
        data = {
            "food": food_name,
            "kcal": int(float(item.get("calorias", 0))),
            "protein": int(float(item.get("proteina", 0))),
            "carbs": int(float(item.get("carboidratos", 0))),
            "fat": int(float(item.get("gorduras", 0))),
            "serving_size": str(item.get("peso", "100g")),
            "embedding": item.get("embedding"),
            "confirmations": int(item.get("confirmations", 1)),
            "is_precise": bool(item.get("is_precise", True))
        }
        res = await async_execute(supabase.table("universal_catalog").insert(data))
        logger.info(f"Supabase Catalog Insertion Result: {res.data}")
        logger.info(f"✅ Item '{food_name}' salvo no Catálogo Universal.")
        return True
    except Exception as e:
        logger.error(f"Erro ao salvar no catálogo: {e}")
        return False

async def search_universal_catalog(query_embedding: list = None, threshold: float = 0.70, keyword: str = None):
    """Realiza busca híbrida: Texto (ILIKE) + Semântica (Vetor) com deduplicação."""
    try:
        results = []
        seen_foods = set()
        
        # 1. Busca por Texto (ILIKE) - Prioridade para nomes exatos/marcas
        if keyword:
            # Tentamos o termo completo e também o termo essencial (ex: 'whopper' de 'sanduíche whopper')
            search_terms = [keyword]
            if len(keyword.split()) > 1:
                search_terms.append(keyword.split()[-1]) # Tenta a última palavra como fallback
            
            for term in search_terms:
                res_kw = await async_execute(
                    supabase.table("universal_catalog")
                    .select("*")
                    .ilike("food", f"%{term}%")
                    .limit(5)
                )
                if res_kw.data:
                    for item in res_kw.data:
                        if item["food"] not in seen_foods:
                            seen_foods.add(item["food"])
                            results.append({
                                "alimento": item.get("food"),
                                "peso": item.get("serving_size", "100g"),
                                "calorias": item.get("kcal", 0),
                                "proteina": item.get("protein", 0),
                                "carboidratos": item.get("carbs", 0),
                                "gorduras": item.get("fat", 0),
                                "is_precise": True,
                                "is_universal": True,
                                "search_method": f"keyword_{term}"
                            })
                    if results: break # Se achou por texto, não precisa do fallback de palavra única

        # 2. Busca Semântica (Vector) - Fallback para sinônimos/descrições
        if query_embedding:
            res_vec = await async_execute(supabase.rpc("match_food_catalog", {
                "query_embedding": query_embedding,
                "match_threshold": threshold, # Threshold de 0.70 conforme sugerido
                "match_count": 5
            }))
            
            if res_vec.data:
                for item in res_vec.data:
                    if item["food"] not in seen_foods:
                        seen_foods.add(item["food"])
                        results.append({
                            "alimento": item.get("food"),
                            "peso": item.get("serving_size", "100g"),
                            "calorias": item.get("kcal", 0),
                            "proteina": item.get("protein", 0),
                            "carboidratos": item.get("carbs", 0),
                            "gorduras": item.get("fat", 0),
                            "is_precise": True,
                            "is_universal": True,
                            "search_method": "vector"
                        })
        return results if results else None
    except Exception as e:
        logger.error(f"Erro na busca do catálogo: {e}")
        return None

async def get_user_profile(user_id: str):
    try:
        res = await async_execute(supabase.table("profiles").select("*").eq("user_id", str(user_id)))
        return res.data[0] if res.data else None
    except Exception as e:
        logger.error(f"Erro ao buscar perfil: {e}")
        return None

async def get_daily_stats(user_id: str):
    """Calculates total calories and macros for the current day."""
    try:
        user_id = int(user_id)
    except (ValueError, TypeError):
        logger.error("get_daily_stats: user_id inválido.")
        return {"kcal": 0, "protein": 0, "carbs": 0, "fat": 0}
        today_br_start = get_br_today_start()
        response = await async_execute(
            supabase.table("logs")
            .select("kcal, protein, carbs, fat")
            .eq("user_id", str(user_id))
            .gte("created_at", today_br_start)
        )
            
        total_kcal = sum(item.get('kcal', 0) for item in response.data)
        total_prot = sum(item.get('protein', 0) for item in response.data)
        total_carb = sum(item.get('carbs', 0) for item in response.data)
        total_fat = sum(item.get('fat', 0) for item in response.data)
        
        return {
            "kcal": total_kcal,
            "protein": total_prot,
            "carbs": total_carb,
            "fat": total_fat
        }
    except Exception as e:
        logger.error(f"Erro ao calcular estatísticas diárias: {e}")
        return {"kcal": 0, "protein": 0, "carbs": 0, "fat": 0}

async def get_daily_total(user_id: str):
    stats = await get_daily_stats(user_id)
    return stats["kcal"]

async def get_report_data(user_id: str, days: int):
    """Aggregates data for periodic reports."""
    try:
        now_br = get_br_now()
        start_date = (now_br - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00-03:00")
        res = await async_execute(
            supabase.table("logs")
            .select("created_at, kcal, protein, carbs, fat")
            .eq("user_id", str(user_id))
            .gte("created_at", start_date)
        )
        return res.data or []
    except Exception as e:
        return []

async def get_recent_logs(user_id: int, minutes: int = 15):
    """Retrieves logs from the last 15 minutes to provide context for additions/corrections."""
    try:
        user_id = int(user_id)
    except (ValueError, TypeError):
        logger.error("get_recent_logs: user_id inválido.")
        return []
    try:
        now_br = get_br_now()
        start_time = (now_br - timedelta(minutes=minutes)).isoformat()
        res = await async_execute(
            supabase.table("logs")
            .select("food, weight, kcal, protein, carbs, fat")
            .eq("user_id", str(user_id))
            .gte("created_at", start_time)
            .order("created_at", desc=True)
        )
        return res.data or []
    except Exception as e:
        logger.error(f"Error fetching recent logs: {e}")
        return []

async def delete_last_log(user_id: str):
    """Deletes the most recent meal log."""
    try:
        res = await async_execute(
            supabase.table("logs")
            .select("id, created_at")
            .eq("user_id", str(user_id))
            .order("created_at", desc=True)
            .limit(1)
        )
        if res.data:
            await async_execute(
                supabase.table("logs").delete()
                .eq("user_id", str(user_id))
                .eq("id", res.data[0]["id"])
            )
            return True
        return False
    except Exception as e:
        logger.error(f"Erro ao deletar último log: {e}")
        return False

async def delete_today_logs(user_id: str):
    """Deletes all logs from the current day."""
    try:
        today_br_start = get_br_today_start()
        await async_execute(
            supabase.table("logs").delete()
            .eq("user_id", str(user_id))
            .gte("created_at", today_br_start)
        )
        return True
    except Exception as e:
        logger.error(f"Erro ao deletar logs de hoje: {e}")
        return False

async def delete_entire_profile(user_id: str):
    """Deletes profile and all logs for a user."""
    try:
        await async_execute(supabase.table("logs").delete().eq("user_id", str(user_id)))
        await async_execute(supabase.table("profiles").delete().eq("user_id", str(user_id)))
        return True
    except Exception as e:
        logger.error(f"Erro ao deletar perfil completo: {e}")
        return False

async def search_food_history(user_id: str, food_query: str):
    """Searches for a historical log entry (Personal -> Universal Fallback)."""
    try:
        user_id = int(user_id)
    except (ValueError, TypeError):
        logger.error("search_food_history: user_id inválido.")
        return None
    try:
        # Pessoal Exato
        res = await async_execute(
            supabase.table("logs")
            .select("food, weight, kcal, protein, carbs, fat, meal_type, is_precise")
            .eq("user_id", str(user_id))
            .ilike("food", f"{food_query}")
            .order("created_at", desc=True)
            .limit(1)
        )
        
        # Pessoal Aproximado
        if not res.data:
            res = await async_execute(
                supabase.table("logs")
                .select("food, weight, kcal, protein, carbs, fat, meal_type, is_precise")
                .eq("user_id", str(user_id))
                .ilike("food", f"%{food_query}%")
                .order("created_at", desc=True)
                .limit(1)
            )
            if res.data: res.data[0]["is_approximate"] = True

        if res and res.data:
            item = res.data[0]
            return [{
                "alimento": item.get("food", food_query),
                "peso": item.get("weight", "100g"),
                "calorias": item.get("kcal", 0),
                "proteina": item.get("protein", 0),
                "carboidratos": item.get("carbs", 0),
                "gorduras": item.get("fat", 0),
                "refeicao": item.get("meal_type", "Outro"),
                "is_precise": item.get("is_precise", False),
                "is_approximate": item.get("is_approximate", False)
            }]
        return None
    except Exception as e:
        logger.error(f"Erro ao buscar no histórico: {e}")
        return None

# --- AI & Search Logic ---

async def get_embedding(text: str):
    """Generates a 768d embedding using gemini-embedding-001."""
    try:
        res = await ai_client.aio.models.embed_content(
            model='gemini-embedding-001',
            contents=text,
            config={'output_dimensionality': 768}
        )
        logger.debug("Embedding gerado com sucesso.")
        return res.embeddings[0].values
    except Exception as e:
        logger.error(f"Erro embedding: {e}")
        return None

async def get_fatsecret_token():
    global fs_token
    now = time.time()
    if fs_token.get("access_token") and now < fs_token.get("expires_at", 0):
        return fs_token["access_token"]
    
    async with fs_lock:
        if fs_token.get("access_token") and now < fs_token.get("expires_at", 0):
            return fs_token["access_token"]
            
        url = "https://oauth.fatsecret.com/connect/token"
        data = {"grant_type": "client_credentials", "scope": "basic"}
        auth = (FATSECRET_CLIENT_ID, FATSECRET_CLIENT_SECRET)
        
        client = get_fs_client()
        try:
            res = await client.post(url, data=data, auth=auth)
            if res.status_code == 200:
                d = res.json()
                logger.info("FatSecret Token Refreshed Successfully")
                fs_token["access_token"] = d["access_token"]
                fs_token["expires_at"] = now + d["expires_in"] - 60
                return fs_token["access_token"]
        except Exception as e:
            logger.error(f"FatSecret Token Error: {e}")
        finally:
            if client is not http_client: await client.aclose()

async def generate_surgical_query(text: str) -> List[Dict[str, str]]:
    """Generates clean food names for both PT and EN search with variations for one or more items."""
    if not groq_client: return [{"pt": text, "en_spec": text, "en_gen": text}]
    
    prompt = f"""
    Extraia os nomes dos alimentos do texto do usuário.
    Retorne uma LISTA de objetos JSON, um para cada alimento identificado.
    Cada objeto deve ter 3 variações:
    1. "pt": Nome LIMPO em português (REMOVA quantidades como "1", "2 unidades", "100g").
    2. "en_spec": Nome específico em inglês (inclua marcas/detalhes se houver).
    3. "en_gen": Nome genérico em inglês (categoria básica do alimento).
    
    Exemplo: "Comi 2 Big Macs e tomei uma coca" -> 
    [
      {{"pt": "big mac", "en_spec": "Mcdonalds big mac", "en_gen": "hamburger"}},
      {{"pt": "coca cola", "en_spec": "coca cola classic", "en_gen": "soda"}}
    ]
    
    Texto: "{text}"
    """
    
    try:
        completion = await groq_client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.1-8b-instant",
            response_format={"type": "json_object"},
            temperature=0,
        )
        raw_data = completion.choices[0].message.content
        logger.debug("Groq Surgical Query concluído.")
        data = json.loads(raw_data)
        
        # Garante que retornamos lista de dicts
        if isinstance(data, dict):
            if "items" in data: items = data["items"]
            elif "foods" in data: items = data["foods"]
            elif "alimentos" in data: items = data["alimentos"]
            elif "pt" in data: items = [data]
            else: items = [data]
        elif isinstance(data, list):
            items = data
        else:
            items = []

        # Validação: garante lista de dicts com ao menos "pt"
        if not isinstance(items, list):
            items = [items] if isinstance(items, dict) else []
        items = [i for i in items if isinstance(i, dict) and i.get("pt")]

        return items if items else [{"pt": text, "en_spec": text, "en_gen": text}]

    except Exception as e:
        logger.error(f"Groq surgical query error: {e}")
        return [{"pt": text, "en_spec": text, "en_gen": text}]

async def search_fatsecret(queries: dict):
    """Searches FatSecret using pre-generated PT/EN variations."""
    token = await get_fatsecret_token()
    if not token: return None
    
    url = "https://platform.fatsecret.com/rest/server.api"
    # queries is now passed as an argument
    
    # 1. Estratégia: O Genérico em Inglês é a nossa maior chance de sucesso absoluto no banco global.
    for search_key in ["en_gen", "en_spec", "pt"]:
        search_term = queries.get(search_key)
        if not search_term: continue
        
        # 2. REMOVIDOS o region="BR" e language="pt". Banco global puro.
        params = {
            "method": "foods.search", 
            "search_expression": search_term, 
            "format": "json", 
            "max_results": 5
        }
        headers = {"Authorization": f"Bearer {token}"}
        
        client = get_fs_client()
        try:
            res = await client.get(url, params=params, headers=headers)
            if res.status_code != 200: continue
            
            data = res.json()
            foods_data = data.get("foods", {}).get("food", [])
            if not foods_data: continue
            if isinstance(foods_data, dict): foods_data = [foods_data]
            
            logger.debug(f"FatSecret: {len(foods_data)} resultado(s) para a chave '{search_key}'.")

            selection_prompt = f"""
            Quais desses alimentos são os MELHORES matches para o pedido original: "{queries.get('pt')}"?
            
            REGRAS RÍGIDAS:
            1. Retorne APENAS um JSON com a lista dos índices aceitáveis em ordem de relevância (máx 3).
            2. Se nenhum alimento for do MESMO TIPO (ex: pão deve ser pão, carne deve ser carne), retorne lista vazia [].
            3. Priorize alimentos genéricos e evite marcas desconhecidas a menos que especificado.
            
            RESULTADOS: {json.dumps([{"name": f['food_name'], "desc": f['food_description']} for f in foods_data], ensure_ascii=False)}
            
            SCHEMA OBRIGATÓRIO: {{"indices": [0, 1]}}
            """
            
            sel_res = await groq_client.chat.completions.create(
                messages=[{"role": "user", "content": selection_prompt}],
                model="llama-3.3-70b-versatile",
                response_format={"type": "json_object"},
                temperature=0,
            )
            
            try:
                raw_json = sel_res.choices[0].message.content.strip()
                parsed = json.loads(raw_json)
                indices = parsed.get("indices", [])
                
                valid_indices = [idx for idx in indices if isinstance(idx, int) and 0 <= idx < len(foods_data)]
                if not valid_indices:
                    logger.warning(f"Selection rejected for '{search_term}'. Trying next term...")
                    continue
                
                results = []
                for idx in valid_indices[:3]:
                    best_food = foods_data[idx]
                    logger.debug("FatSecret: candidato selecionado.")
                    
                    # Fetch details - SEM region e language
                    food_id = best_food["food_id"]
                    d_res = await client.get(url, params={"method": "food.get.v2", "food_id": food_id, "format": "json"}, headers=headers)
                    
                    if d_res.status_code == 200:
                        d_data = d_res.json()
                        d = d_data.get("food", {})
                        servings = d.get("servings", {}).get("serving", [])
                        if isinstance(servings, dict): servings = [servings]
                        if not servings: continue
                        s = servings[0]
                        
                        serving_qty = s.get("metric_serving_amount")
                        serving_unit = s.get("metric_serving_unit", "g")
                        weight_str = f"{float(serving_qty):.1f}{serving_unit}" if serving_qty else "100g"
                        
                        result = {
                            "alimento": best_food['food_name'],
                            "calorias": float(s.get("calories", 0)),
                            "proteina": float(s.get("protein", 0)),
                            "carboidratos": float(s.get("carbohydrate", 0)),
                            "gorduras": float(s.get("fat", 0)),
                            "peso": weight_str,
                            "is_precise": True,
                            "original_query": queries.get('pt') or search_term,
                            "is_fs_verified": True
                        }
                        results.append(result)
                if results:
                    return results
            except Exception as e:
                logger.error(f"Error in FatSecret selection/detail: {e}")
                continue
        except Exception as e:
            logger.error(f"FatSecret Searching Error for '{search_term}': {e}")
        finally:
            if client is not http_client: await client.aclose()
    return None

async def is_food_message(text: str) -> bool:
    """Triagem rápida: retorna True se o texto parece registro de alimento,
    False se parece pergunta/conversa. Usa llama-3.1-8b-instant (barato e rápido).
    Em caso de erro retorna True para deixar o pipeline principal decidir."""
    if not groq_client:
        return True
    prompt = (
        'O texto abaixo é um registro de alimento/refeição ou é uma pergunta/conversa?\n'
        'Responda APENAS com "food" ou "chat". Nenhuma outra palavra.\n\n'
        f'Texto: "{text}"'
    )
    try:
        r = await groq_client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.1-8b-instant",
            temperature=0,
            max_tokens=5,
        )
        return r.choices[0].message.content.strip().lower() == "food"
    except Exception as e:
        logger.warning(f"is_food_message fallback to pipeline: {e}")
        return True

async def extract_calories_list(
    user_id: int, 
    message_text: str = "", 
    image_bytes: bytes = None, 
    resolved_candidates: list = None,
    pre_generated_queries: list = None
):
    search_context = resolved_candidates or []
    all_items_queries = pre_generated_queries
    
    if all_items_queries is None and message_text and not image_bytes:
        all_items_queries = await generate_surgical_query(message_text)
        
    if all_items_queries is None:
        all_items_queries = []

    # Para cada item identificado na frase, tentamos achar um contexto (âncora)
    for idx, item_queries in enumerate(all_items_queries):
        item_found = False
        clean_name_pt = item_queries.get("pt", message_text)
        
        # 1. Search Personal History
        hist_res = await search_food_history(user_id, clean_name_pt)
        if hist_res:
            cand = hist_res[0]
            cand["is_historical"] = True
            logger.info(f"💾 History Context Hit: {cand['alimento']}")
            search_context.append(cand)
            item_found = True
            
        # 2. Search Catalog (Vector)
        if not item_found:
            emb = await get_embedding(clean_name_pt)
            if emb or clean_name_pt:
                match = await search_universal_catalog(query_embedding=emb, threshold=0.88, keyword=clean_name_pt)
                if match:
                    if len(match) > 1:
                        logger.info(f"Multiple Catalog candidates for '{clean_name_pt}'. Requesting choice.")
                        return [], None, "needs_choice", json.dumps({
                            "candidates": match, 
                            "source": "catalog", 
                            "query_name": clean_name_pt,
                            "resolved_so_far": search_context,
                            "pending_queries": all_items_queries[idx:]
                        })
                    else:
                        cand = match[0]
                        logger.info(f"🎯 Catalog Context Hit: {cand['alimento']}")
                        search_context.append(cand)
                        item_found = True
                        
        # 3. Search FatSecret (Text only fallback)
        if not item_found:
            fs_res = await search_fatsecret(item_queries)
            if fs_res:
                if len(fs_res) > 1:
                    logger.info(f"Multiple FatSecret candidates for '{clean_name_pt}'. Requesting choice.")
                    return [], None, "needs_choice", json.dumps({
                        "candidates": fs_res, 
                        "source": "fatsecret", 
                        "query_name": clean_name_pt,
                        "resolved_so_far": search_context,
                        "pending_queries": all_items_queries[idx:]
                    })
                elif len(fs_res) == 1:
                    cand = fs_res[0]
                    logger.info(f"🔍 FatSecret Context Hit: {cand['alimento']}")
                    cand["alimento"] = cand.get("original_query", cand["alimento"])
                    emb = await get_embedding(cand["alimento"])
                    cand["embedding"] = emb
                    await save_to_universal_catalog(cand)
                    search_context.append(cand)
                    item_found = True

    # 3. Model extraction with Context
    if image_bytes:
        # Novo: Buscar contexto recente (últimos 15 min) para suportar adições como "comi mais um"
        recent_logs = await get_recent_logs(user_id)
        contexto_recente_str = json.dumps(recent_logs, ensure_ascii=False) if recent_logs else "Nenhum registro recente."

        # Use Gemini ONLY for VISION
        auto_meal = get_meal_type_by_hour()
        prompt = f"""
        Você é um nutricionista especialista com visão computacional. Analise a IMAGEM e retorne JSON.
        DADOS DE BUSCA (Se houver): {json.dumps(search_context, ensure_ascii=False)}
        CONTEXTO RECENTE (Últimos 15 min): {contexto_recente_str}
        HORÁRIO ATUAL (Brasil): {get_br_now().strftime("%H:%M")} → refeição provável: {auto_meal}

        SCHEMA: {{"is_nutrition_label": bool, "foods": [{{"alimento": str, "raciocinio_matematico": str, "peso": str, "calorias": int, "proteina": float, "carboidratos": float, "gorduras": float, "refeicao": str}}], "barcode": str}}

        REGRAS DE CONTEXTO:
        1. Se o "CONTEXTO RECENTE" contiver alimentos e a foto parecer ser uma ADIÇÃO ou COMPLEMENTO (ex: uma nova foto de algo que combina ou o usuário disse na legenda "mais este"), use os dados do contexto para manter a consistência.
        2. Retorne apenas os itens que estão na FOTO ATUAL.

        REGRAS GERAIS:
        1. Primeiro, classifique a imagem: é uma TABELA NUTRICIONAL (rótulo) ou uma FOTO DE COMIDA?
        2. Coloque true in "is_nutrition_label" se for um rótulo/embalagem/tabela de macros.

        SE FOR TABELA NUTRICIONAL (is_nutrition_label = true):
        - Leia DIRETAMENTE os valores impressos na tabela: calorias, proteínas, carboidratos, gorduras.
        - Use a porção descrita na tabela como "peso" (ex: "30g", "1 sachê").
        - Ignore qualquer dado de busca; os valores da tabela são exatos.
        - is_precise deve ser true.

        SE FOR FOTO DE COMIDA (is_nutrition_label = false):
        - Se houver dados de busca compatíveis, USE-OS como base para os macros.
        - Olhe a chave "peso" dos dados de busca.
        - Se for "100g", estime o peso da comida na foto e faça Regra de Três.
        - Se for "1 unidade" (lanche/fast food), apenas CONTE e multiplique.
        - Se não houver dados de busca, use sua melhor estimativa.

        REFEIÇÃO: Use o horário atual para classificar a refeição. Padrão automático: "{auto_meal}".
        Se o usuário informou o tipo no texto da legenda, priorize o que ele disse.
        """
        contents = [prompt]
        if message_text: contents.append(message_text)
        contents.append(ai_types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"))
        
        try:
            res = await ai_client.aio.models.generate_content(
                model=AI_MODEL, contents=contents,
                config=ai_types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema={
                        "type": "OBJECT",
                        "properties": {
                            "is_nutrition_label": {"type": "BOOLEAN"},
                            "foods": {
                                "type": "ARRAY",
                                "items": {
                                    "type": "OBJECT",
                                    "properties": {
                                        "alimento": {"type": "STRING"},
                                        "raciocinio_matematico": {"type": "STRING"},
                                        "peso": {"type": "STRING"},
                                        "calorias": {"type": "INTEGER"},
                                        "proteina": {"type": "NUMBER"},
                                        "carboidratos": {"type": "NUMBER"},
                                        "gorduras": {"type": "NUMBER"},
                                        "refeicao": {"type": "STRING"}
                                    }
                                }
                            },
                            "barcode": {"type": "STRING"}
                        }
                    }
                )
            )
            raw_text = res.text
            logger.debug("Gemini Vision: resposta recebida.")
            data = json.loads(raw_text)
            is_label = data.get("is_nutrition_label", False)
            foods = data.get("foods", [])

            # Recover flags if we used search_context
            if search_context:
                for f in foods:
                    f["is_precise"] = True
                    if any(ctx.get("is_universal") for ctx in search_context): f["is_universal"] = True
                    if any(ctx.get("is_historical") for ctx in search_context): f["is_historical"] = True
                    if any(ctx.get("is_fs_verified") for ctx in search_context): f["is_fs_verified"] = True

            if is_label:
                logger.info("🏷️ Tabela nutricional detectada — lendo macros direto do rótulo.")
                for f in foods:
                    f["is_precise"] = True
            # Apply time-based meal type as fallback
            for f in foods:
                if not f.get("refeicao") or f.get("refeicao") in ("", "Outro", "outro"):
                    f["refeicao"] = auto_meal
            return foods, data.get("barcode"), None, raw_text
        except Exception as e:
            logger.error(f"Gemini Vision Error: {e}")
            return [], None, str(e), None
    
    elif message_text:
        # Extrai pesos por item (suporta entradas múltiplas)
        amounts_per_food = extract_amounts_per_food(message_text)
        peso_calculado_python = extract_amount(message_text)

        if amounts_per_food:
            anchors = "\n".join(
                f'  - "{frag}": use exatamente {int(g)}g (determinístico, NÃO altere)'
                for frag, g in amounts_per_food.items()
            )
            regra_peso = f"""
🚨 PESOS DETERMINÍSTICOS POR ITEM (não altere nenhum desses valores):
{anchors}
Para itens sem âncora acima, use medidas caseiras brasileiras padrão.
"""
        elif peso_calculado_python:
            regra_peso = f"""
🚨 REGRA ABSOLUTA DE PESO (DETERMINÍSTICA):
O sistema identificou exatamente {peso_calculado_python}g.
NÃO chute o peso! Use {int(peso_calculado_python)}g para sua Regra de Três.
"""
        else:
            regra_peso = """
- Se o usuário usou medidas caseiras, use esta tabela de referência brasileira:
  * 1 fatia = 30g | 1 fatia fina = 15g | 1 fatia grossa = 50g
  * 1 colher sopa = 15g | 1 colher sobremesa = 10g | 1 colher chá = 5g
  * 1 xícara = 200g | 1 copo = 200g | 1 concha = 80g | 1 pegador = 150g
  * 1 prato fundo = 350g | 1 prato raso = 250g
  * 1 bife/filé = 120g | 1 sachê = 30g | 1 lata = 350g
  * 1 unidade (ovo/fruta pequena) = 50g | 1 unidade (fruta média) = 120g
"""

        # Novo: Buscar contexto recente (últimos 15 min) para suportar adições
        recent_logs = await get_recent_logs(user_id)
        contexto_recente_str = json.dumps(recent_logs, ensure_ascii=False) if recent_logs else "Nenhum registro recente."

        # Use Groq for TEXT extraction + scaling
        if not groq_client: return [], None, "Groq client not init", None

        prompt = f"""
        Você é um nutricionista especialista. Extraia os alimentos e seus macros do texto usuario.
        DADOS DE BUSCA (Se houver): {json.dumps(search_context, ensure_ascii=False)}
        CONTEXTO RECENTE (Últimos 15 min): {contexto_recente_str}
        
        TEXTO DO USUÁRIO: "{message_text}"

        REGRAS DE CONTEXTO (IMPORTANTE):
        1. Se o TEXTO DO USUÁRIO for uma ADIÇÃO ou CORREÇÃO (ex: "mais um", "na verdade foram 2", "esqueci da coca"), use o CONTEXTO RECENTE para identificar qual alimento ele está complementando.
        2. Retorne APENAS os itens NOVOS que precisam ser adicionados ao log para que o total final esteja correto. 
           Ex: Se o contexto diz "1 pão" e o usuário diz "comi mais um", retorne apenas "1 pão" no JSON.
        3. Se não houver relação clara com o contexto recente, ignore-o e trate como um novo registro.

        REGRAS MATEMÁTICAS RÍGIDAS:
        1. Identifique a quantidade consumida.
        2. {regra_peso}
        3. SE O PESO BASE FOR "100g" (Regra de Três):
           - Fórmula: (Quantidade do Usuário / 100) * Macros Base.
        4. SE O PESO BASE FOR "1 unidade" (Fast Food / Porção Fechada):
           - Multiplicação Direta: (Quantidade de lanches) * Macros Base.
        5. Explique seu cálculo matemático detalhadamente no campo "raciocinio_matematico".
        6. Retorne um objeto JSON.
        
        SCHEMA: {{"itens": [{{"alimento": str, "raciocinio_matematico": str, "peso": str, "calorias": int, "proteina": float, "carboidratos": float, "gorduras": float, "refeicao": str, "is_precise": bool}}]}}
        """
        
        try:
            completion = await groq_client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model="llama-3.3-70b-versatile",
                response_format={"type": "json_object"},
                temperature=0,
            )
            raw_data = completion.choices[0].message.content
            logger.debug("Groq Unified Extraction: resposta recebida.")
            parsed = json.loads(raw_data)
            foods = parsed.get("itens") or parsed.get("foods") or parsed.get("alimentos") or []

            # Validação crítica: garante lista de dicts sanitizados
            if not isinstance(foods, list):
                foods = [foods] if isinstance(foods, dict) else []
            valid_foods = []
            for f in foods:
                if not isinstance(f, dict) or not f.get("alimento"):
                    logger.warning("Item inválido ignorado na extração.")
                    continue
                f["calorias"] = int(float(f.get("calorias", 0)))
                f["proteina"] = float(f.get("proteina", 0))
                f["carboidratos"] = float(f.get("carboidratos", 0))
                f["gorduras"] = float(f.get("gorduras", 0))
                valid_foods.append(f)
            foods = valid_foods
            
            # Recover flags if we used search_context
            if search_context:
                for f in foods:
                    f["is_precise"] = True
                    if any(ctx.get("is_universal") for ctx in search_context): f["is_universal"] = True
                    if any(ctx.get("is_historical") for ctx in search_context): f["is_historical"] = True
                    if any(ctx.get("is_fs_verified") for ctx in search_context): f["is_fs_verified"] = True

            # Apply time-based meal type as fallback
            auto_meal = get_meal_type_by_hour()
            for f in foods:
                if not f.get("refeicao") or f.get("refeicao") in ("", "Outro", "outro"):
                    f["refeicao"] = auto_meal
            return foods, None, None, raw_data
        except Exception as e:
            logger.error(f"Groq Unified Extraction Error: {e}")
            return [], None, str(e), None

    return [], None, None, None

def extract_amount(text: str, pkg_weight: float = None) -> Optional[float]:
    """Extracts weight (g) or volume (ml) from text. Handles fractions and household measures."""
    if not text: return None
    t = str(text).lower().replace(",", ".")

    household = [
        (r"(colher\s*de\s*sopa|c\.\s*sopa|colher\s*s)", 15),
        (r"(colher\s*de\s*sobremesa)", 10),
        (r"(colher\s*de\s*ch[aá]|colher\s*c)", 5),
        (r"(colher\s*de\s*caf[eé])", 2),
        (r"(x[ií]cara)", 200),
        (r"(copo|c\.)", 200),
        (r"(fatia\s*grossa)", 50),
        (r"(fatia\s*fina)", 15),
        (r"(fatia|f\.)", 30),
        (r"(prato\s*fundo|prato\s*cheio)", 350),
        (r"(prato\s*raso)", 250),
        (r"(concha)", 80),
        (r"(pegador)", 150),
        (r"(bife|fil[eé])", 120),
        (r"(caixinha|caixa\s*pequena)", 200),
        (r"(lata\b)", 350),
        (r"(garrafa\s*grande|garrafa\s*pet)", 1000),
        (r"(garrafa\s*pequena|garrafinha)", 300),
        (r"(sach[eê])", 30),
    ]

    fractions = {
        "todo": 1.0, "toda": 1.0, "inteiro": 1.0, "inteira": 1.0,
        "1 pacote": 1.0, "uma garrafa": 1.0, "1 lata": 1.0,
        "meio": 0.5, "metade": 0.5, "1/2": 0.5, "meia": 0.5,
        "um quarto": 0.25, "1/4": 0.25,
        "um terço": 0.333, "1/3": 0.333,
        "três quartos": 0.75, "3/4": 0.75,
    }
    for word, mult in fractions.items():
        if word in t:
            for pattern, factor in household:
                if re.search(rf"{word}\s*{pattern}", t):
                    return mult * factor
            if pkg_weight:
                return mult * pkg_weight

    for pattern, factor in household:
        match = re.search(rf"(\d+[\.,]?\d*)?\s*{pattern}", t)
        if match:
            qty = float(match.group(1).replace(",", ".")) if match.group(1) else 1.0
            return qty * factor

    kg_match = re.search(r"(\d+[\.,]?\d*)\s*(kg|kilo|l$|litro|l\s)", t)
    if kg_match:
        try: return float(kg_match.group(1)) * 1000
        except: pass

    g_match = re.search(r"(\d+[\.,]?\d*)\s*(g|gr|ml)", t)
    if g_match:
        try: return float(g_match.group(1))
        except: pass

    num_match = re.search(r"^(\d+[\.,]?\d*)$", t)
    if num_match:
        try: return float(num_match.group(1))
        except: pass
    return None


def extract_amounts_per_food(text: str) -> dict[str, float]:
    """Splits multi-item text and returns {fragment: grams} for each detected item.
    Ex: '100g de arroz e 2 conchas de feijão' -> {'100g de arroz': 100.0, '2 conchas de feijão': 160.0}
    """
    separators = r'\s+e\s+|\s*,\s*|\s+mais\s+|\s+com\s+'
    parts = re.split(separators, text.lower())
    result = {}
    for part in parts:
        part = part.strip()
        if not part:
            continue
        amount = extract_amount(part)
        if amount is not None:
            result[part] = amount
    return result

def calculate_tdee(weight, height, age, gender, activity, goal):
    # Mifflin-St Jeor
    if gender == 'M': bmr = 10 * weight + 6.25 * height - 5 * age + 5
    else: bmr = 10 * weight + 6.25 * height - 5 * age - 161
    
    mult = {'sedentario': 1.2, 'leve': 1.375, 'moderado': 1.55, 'ativo': 1.725, 'atleta': 1.9}
    tdee = bmr * mult.get(activity, 1.2)
    
    if goal == 'perder': tdee -= 500
    elif goal == 'ganhar': tdee += 500
    return round(tdee)

async def get_barcode_data(barcode: str):
    """Fetches nutritional data from OpenFoodFacts."""
    url = f"https://world.openfoodfacts.org/api/v0/product/{barcode}.json"
    async with httpx.AsyncClient() as client:
        try:
            res = await client.get(url, timeout=10)
            if res.status_code == 200:
                data = res.json()
                if data.get("status") == 1:
                    product = data.get("product", {})
                    nutriments = product.get("nutriments", {})
                    
                    pkg_qty = product.get('product_quantity')
                    try:
                        pkg_weight = float(pkg_qty) if pkg_qty else None
                    except:
                        pkg_weight = None

                    return {
                        "alimento": f"{product.get('product_name', 'Desconhecido')} ({product.get('brands', '')})",
                        "kcal_100g": nutriments.get("energy-kcal_100g", 0),
                        "prot_100g": nutriments.get("proteins_100g", 0),
                        "carb_100g": nutriments.get("carbohydrates_100g", 0),
                        "fat_100g": nutriments.get("fat_100g", 0),
                        "pkg_weight": pkg_weight,
                    }
        except Exception as e:
            logger.error(f"Erro ao buscar OpenFoodFacts: {e}")
    return None

# --- Bot Handlers ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    logger.info(f"User {message.from_user.id} enviou /start")
    profile = await get_user_profile(message.from_user.id)
    
    if not profile:
        await message.answer(
            f"👋 Olá, **{message.from_user.first_name}**! Bem-vindo ao Bot de Calorias.\n\n"
            "Ainda não te conheço! Para calcular suas metas personalizadas, "
            "precisamos configurar seu perfil (é rapidinho).",
            parse_mode="Markdown"
        )
        await message.answer("1️⃣ Qual seu **peso** atual em kg? (ex: 75.5)", parse_mode="Markdown")
        await state.set_state(ProfileStates.weight)
    else:
        await message.answer(
            f"👋 Olá de novo, **{message.from_user.first_name}**!\n\n"
            f"🎯 Sua meta atual: **{profile['tdee']} kcal**\n\n"
            "Escolha uma opção:\n"
            "🔹 /relatorio - Ver estatísticas\n"
            "🔹 /perfil - Recalcular meta\n"
            "🔹 /ajuda - Como usar o bot\n\n"
            "DICA: Você pode me mandar **fotos** da sua comida! 📸",
            parse_mode="Markdown"
        )

@dp.message(Command("ajuda"))
async def cmd_help(message: types.Message):
    help_text = (
        "📖 **Como usar o Bot de Calorias:**\n\n"
        "1. **Texto:** Diga o que comeu. Registro calorias, proteínas, carbos e gorduras!\n"
        "2. **Fotos:** Mande uma foto do prato e eu estimo tudo.\n"
        "3. **Refeições:** Eu identifico se é Almoço, Jantar, etc.\n"
        "4. **Perfil:** Use /perfil para atualizar dados e seu OBJETIVO (Bulk/Cut).\n"
        "5. **Relatórios:** Use /relatorio para ver progresso e GRÁFICOS.\n"
        "6. **Status:** Use /status para ver rapidamente como está sua meta hoje.\n"
        "7. **Desfazer:** Errou algo? Use /desfazer para remover o último log.\n"
        "8. **Resets:** /reset_dia apaga hoje; /reset_perfil apaga TUDO."
    )
    await message.answer(help_text, parse_mode="Markdown")

@dp.message(Command("desfazer"))
async def cmd_undo(message: types.Message):
    if await delete_last_log(message.from_user.id):
        # Remove a última entrada da memória da IA também para manter coerência
        if message.from_user.id in user_history and user_history[message.from_user.id]:
            user_history[message.from_user.id].pop()
        await message.answer("🔄 A última entrada foi removida com sucesso!")
    else:
        await message.answer("❌ Não encontrei entradas recentes para remover.")

@dp.message(Command("reset_dia"))
async def cmd_reset_day(message: types.Message):
    if await delete_today_logs(message.from_user.id):
        if message.from_user.id in user_history:
            user_history[message.from_user.id] = []
        await message.answer("📅 Seus logs de **hoje** foram apagados!", parse_mode="Markdown")
    else:
        await message.answer("❌ Erro ao apagar logs de hoje.")

@dp.message(Command("reset_perfil", "resetperfil"))
async def cmd_reset_profile(message: types.Message, state: FSMContext):
    if await delete_entire_profile(message.from_user.id):
        if message.from_user.id in user_history:
            del user_history[message.from_user.id]
        if message.from_user.id in jailbreak_users:
            del jailbreak_users[message.from_user.id]
            
        await message.answer("💥 **Perfil e histórico deletados!** Vamos começar do zero.", parse_mode="Markdown")
        await cmd_start(message, state)
    else:
        await message.answer("❌ Erro ao deletar seu perfil.")

@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    user_id = message.from_user.id
    profile = await get_user_profile(user_id)
    
    if not profile:
        await message.answer("⚠️ Você ainda não configurou seu perfil. Use /start para começar!")
        return
        
    daily_limit = profile.get('tdee', 2000)
    daily_total = await get_daily_total(user_id)
    remaining = daily_limit - daily_total
    
    # Get current meal data for macros breakdown
    today_br_start = get_br_today_start()
    now_br = get_br_now()
    data_formatada = now_br.strftime("%d/%m/%Y %H:%M")
    
    res = await async_execute(supabase.table("logs").select("protein, carbs, fat").eq("user_id", str(user_id)).gte("created_at", today_br_start))
    total_prot = sum(item.get('protein', 0) for item in res.data)
    total_carb = sum(item.get('carbs', 0) for item in res.data)
    total_fat = sum(item.get('fat', 0) for item in res.data)

    progress_val = min(10, round((daily_total/daily_limit)*10)) if daily_limit > 0 else 0
    progress_bar = "🔵" * progress_val + "⚪" * (10 - progress_val)

    status_msg = (
        f"📊 **STATUS ATUAL ({data_formatada})**\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🔥 Meta: **{daily_limit} kcal**\n"
        f"✅ Consumido: **{daily_total} kcal**\n"
        f"⚖️ Restante: **{max(0, remaining)} kcal**\n\n"
        f"💪 Proteínas: {total_prot}g\n"
        f"🍞 Carbos: {total_carb}g\n"
        f"🥑 Gorduras: {total_fat}g\n\n"
        f"{progress_bar}\n"
        f"━━━━━━━━━━━━━━━\n"
    )
    
    if remaining < 0:
        status_msg += "⚠️ Você ultrapassou a meta de hoje!"
    elif remaining < 200:
        status_msg += "🟡 Quase lá! Cuidado com o próximo lanche."
    else:
        status_msg += "🟢 No caminho certo!"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑️ Apagar alimento", callback_data="show_delete_list")]
    ])
    await message.answer(status_msg, parse_mode="Markdown", reply_markup=kb)

@dp.callback_query(F.data == "show_delete_list")
async def show_delete_list(callback: types.CallbackQuery, state: FSMContext):
    """Shows today's food list so the user can pick one to delete."""
    user_id = callback.from_user.id
    today_br_start = get_br_today_start()
    res = await async_execute(
        supabase.table("logs")
        .select("id, food, weight, kcal, meal_type")
        .eq("user_id", str(user_id))
        .gte("created_at", today_br_start)
        .order("created_at", desc=True)
    )
    logs = res.data or []
    if not logs:
        await callback.answer("Nenhum alimento registrado hoje.", show_alert=True)
        return

    buttons = []
    for entry in logs:
        label = f"{smart_truncate(entry.get('food','?'), 30)} ({entry.get('weight','?')}) — {entry.get('kcal',0)} kcal"
        if len(label) > 64: label = label[:61] + "..."
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"del_item_{entry['id']}")])
    buttons.append([InlineKeyboardButton(text="🔙 Cancelar", callback_data="del_cancel")])

    await callback.message.edit_text(
        "🗑️ **Qual alimento deseja apagar?**\nSelecione na lista abaixo:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await state.set_state(DeleteFoodState.waiting_for_choice)
    await callback.answer()

@dp.callback_query(DeleteFoodState.waiting_for_choice, F.data.startswith("del_item_"))
async def process_delete_item(callback: types.CallbackQuery, state: FSMContext):
    """Deletes the selected food entry by its database id."""
    entry_id = callback.data.replace("del_item_", "")
    user_id = callback.from_user.id
    try:
        entry_id_int = int(entry_id)  # logs.id é bigint — valida que é inteiro positivo
        if entry_id_int <= 0:
            raise ValueError()
    except ValueError:
        await callback.answer("❌ ID inválido.", show_alert=True)
        return
    try:
        res = await async_execute(
            supabase.table("logs").select("food, kcal").eq("id", entry_id).eq("user_id", str(user_id))
        )
        item_name = res.data[0].get("food", "item") if res.data else "item"
        item_kcal = res.data[0].get("kcal", 0) if res.data else 0

        await async_execute(
            supabase.table("logs").delete().eq("id", entry_id).eq("user_id", str(user_id))
        )
        await state.clear()
        await callback.answer()
        await callback.message.edit_text(
            f"✅ **{item_name}** ({item_kcal} kcal) removido com sucesso!\n\nUse /status para ver o total atualizado.",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Erro ao deletar item {entry_id}: {e}")
        await callback.answer("❌ Erro ao remover o alimento.", show_alert=True)

@dp.callback_query(F.data == "del_cancel")
async def cancel_delete(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Operação cancelada.")
    await callback.answer()

@dp.callback_query(F.data.startswith("adj_"))
async def process_adjustment(callback: types.CallbackQuery):
    """Handles quick calorie adjustments from the log feedback buttons."""
    user_id = callback.from_user.id
    action = callback.data.split("_")[1] # 1.1, 0.9, undo
    
    try:
        res = await async_execute(
            supabase.table("logs")
            .select("created_at")
            .eq("user_id", str(user_id))
            .order("created_at", desc=True)
            .limit(1)
        )
        
        if not res.data:
            await callback.answer("❌ Nenhum log recente encontrado.")
            return

        last_time = res.data[0]['created_at']

        if action == "undo":
            if await delete_last_log(user_id):
                await callback.message.edit_text("🔄 **Log desfeito com sucesso!**", parse_mode="Markdown")
            else:
                await callback.answer("❌ Erro ao desfazer.")
            return

        multiplier = float(action)
        
        logs_to_update = await async_execute(
            supabase.table("logs")
            .select("*")
            .eq("user_id", str(user_id))
            .eq("created_at", last_time)
        )

        for entry in logs_to_update.data:
            await async_execute(
                supabase.table("logs").update({
                    "kcal": round(entry['kcal'] * multiplier),
                    "protein": round(entry['protein'] * multiplier),
                    "carbs": round(entry['carbs'] * multiplier),
                    "fat": round(entry['fat'] * multiplier)
                }).eq("id", entry['id'])
            )

        pct = "+10%" if multiplier > 1 else "-10%"
        await callback.message.edit_text(f"✅ Ajustado em **{pct}**! Use /status para ver o novo total.", parse_mode="Markdown")
        await callback.answer(f"Ajustado {pct}")

    except Exception as e:
        logger.error(f"Erro ao ajustar calorias: {e}")
        await callback.answer("❌ Erro no ajuste.")

@dp.callback_query(F.data == "manual_correct")
async def process_manual_correction_start(callback: types.CallbackQuery, state: FSMContext):
    """Starts the manual correction flow."""
    await callback.message.answer("Qual o valor real de **calorias (kcal) por 100g**?", parse_mode="Markdown")
    await state.set_state(CorrectionStates.kcal)
    await callback.answer()

@dp.message(CorrectionStates.kcal)
async def process_manual_kcal(message: types.Message, state: FSMContext):
    """Processes the manual calorie value provided by the user."""
    kcal_100g = parse_numeric(message.text)
    if kcal_100g is None:
        await message.answer("❌ Por favor, envie um número válido.")
        return
        
    user_id = message.from_user.id
    # Get the latest entry to correct
    res = await async_execute(
        supabase.table("logs")
        .select("*")
        .eq("user_id", str(user_id))
        .order("created_at", desc=True)
        .limit(1)
    )
        
    if res.data:
        entry = res.data[0]
        weight = extract_amount(entry.get("weight", "100g")) or 100
        new_kcal = round((kcal_100g / 100) * weight)
        
        await async_execute(
            supabase.table("logs").update({
                "kcal": new_kcal,
                "is_precise": True
            }).eq("id", entry['id'])
        )
        
        await message.answer(f"✅ Corrigido para **{new_kcal} kcal** ({kcal_100g} kcal/100g)!", parse_mode="Markdown")
    
    await state.clear()

@dp.message(Command("cancelar"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Operação cancelada. Como posso ajudar agora?")

# --- Onboarding FSM ---

@dp.message(Command("perfil"))
async def start_profile(message: types.Message, state: FSMContext):
    await message.answer("Vamos calcular sua meta! Qual seu **peso** atual em kg? (ex: 75.5)", parse_mode="Markdown")
    await state.set_state(ProfileStates.weight)

@dp.message(ProfileStates.weight)
async def process_weight(message: types.Message, state: FSMContext):
    try:
        weight = float(message.text.replace(',', '.'))
        await state.update_data(weight=weight)
        await message.answer("2️⃣ Qual sua **altura** em cm? (ex: 175)", parse_mode="Markdown")
        await state.set_state(ProfileStates.height)
    except:
        await message.answer("Por favor, envie um número válido.")

@dp.message(ProfileStates.height)
async def process_height(message: types.Message, state: FSMContext):
    try:
        height = float(message.text)
        await state.update_data(height=height)
        await message.answer("3️⃣ Qual sua **idade**?", parse_mode="Markdown")
        await state.set_state(ProfileStates.age)
    except:
        await message.answer("Por favor, envie um número válido.")

@dp.message(ProfileStates.age)
async def process_age(message: types.Message, state: FSMContext):
    try:
        age = int(message.text)
        await state.update_data(age=age)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Masculino", callback_data="g_M"), 
             InlineKeyboardButton(text="Feminino", callback_data="g_F")]
        ])
        await message.answer("4️⃣ Qual seu **sexo**?", reply_markup=kb, parse_mode="Markdown")
        await state.set_state(ProfileStates.gender)
    except:
        await message.answer("Por favor, envie um número válido.")

@dp.callback_query(ProfileStates.gender, F.data.startswith("g_"))
async def process_gender(callback: types.CallbackQuery, state: FSMContext):
    gender = callback.data.split("_")[1]
    await state.update_data(gender=gender)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Sedentário", callback_data="act_sedentario")],
        [InlineKeyboardButton(text="Leve (1-3 dias/sem)", callback_data="act_leve")],
        [InlineKeyboardButton(text="Moderado (3-5 dias/sem)", callback_data="act_moderado")],
        [InlineKeyboardButton(text="Ativo (6-7 dias/sem)", callback_data="act_ativo")],
        [InlineKeyboardButton(text="Atleta (2x dia)", callback_data="act_atleta")]
    ])
    await callback.message.edit_text("5️⃣ Qual seu nível de **atividade física**?", reply_markup=kb, parse_mode="Markdown")
    await state.set_state(ProfileStates.activity)

@dp.callback_query(ProfileStates.activity, F.data.startswith("act_"))
async def process_activity(callback: types.CallbackQuery, state: FSMContext):
    activity = callback.data.split("_")[1]
    await state.update_data(activity=activity)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Perder Peso", callback_data="goal_perder")],
        [InlineKeyboardButton(text="Manter Peso", callback_data="goal_manter")],
        [InlineKeyboardButton(text="Ganhar Massa", callback_data="goal_ganhar")]
    ])
    await callback.message.edit_text("6️⃣ Qual seu **objetivo** principal?", reply_markup=kb, parse_mode="Markdown")
    await state.set_state(ProfileStates.goal)

@dp.callback_query(ProfileStates.goal, F.data.startswith("goal_"))
async def process_goal(callback: types.CallbackQuery, state: FSMContext):
    goal = callback.data.split("_")[1]
    data = await state.get_data()
    
    tdee = calculate_tdee(data['weight'], data['height'], data['age'], data['gender'], data['activity'], goal)
    
    # Save to Supabase
    profile_data = {
        "user_id": str(callback.from_user.id),
        "weight": data['weight'],
        "height": data['height'],
        "age": data['age'],
        "gender": data['gender'],
        "activity": data['activity'],
        "goal": goal,
        "tdee": float(tdee)
    }
    await async_execute(supabase.table("profiles").upsert(profile_data))
    
    await state.clear()
    await callback.message.edit_text(
        f"✅ Perfil configurado!\n"
        f"Sua meta diária (ajustada para {goal}) é: **{tdee} kcal**\n\n"
        f"Agora é só me mandar seus alimentos ou uma foto do prato! 📸🍎",
        parse_mode="Markdown"
    )

@dp.message(BarcodeState.waiting_for_portion)
async def process_barcode_portion(message: types.Message, state: FSMContext):
    """Processes the portion size for a product detected via barcode."""
    data = await state.get_data()
    product = data.get("barcode_product")
    
    if not product:
        await state.clear()
        return

    # Extract weight/portion
    grams = extract_amount(message.text, pkg_weight=product.get("pkg_weight"))
    
    # Simple heuristic if no 'g' found
    if not grams:
        if any(f in message.text.lower() for f in ["meio", "metade", "1/2", "meia", "um quarto", "1/4", "um terço", "1/3", "três quartos", "3/4", "todo", "toda", "inteiro", "inteira", "pacote", "lata"]):
            await message.answer("❌ Não tenho os dados do peso total da embalagem deste produto. Por favor, diga a quantidade exata (ex: 150g, 2 fatias).")
            return
            
        try:
            grams = float(message.text.split()[0].replace(",", "."))
        except:
            await message.answer("❌ Não entendi a quantidade. Digite algo como '100g' ou 'meio pacote'.")
            return

    # Calculate macros based on 100g base
    factor = grams / 100
    item = {
        "alimento": product["alimento"],
        "peso": f"{grams}g",
        "calorias": round(product["kcal_100g"] * factor),
        "proteina": round(product["prot_100g"] * factor),
        "carboidratos": round(product["carb_100g"] * factor),
        "gorduras": round(product["fat_100g"] * factor),
        "refeicao": get_meal_type_by_hour(),
        "is_precise": True
    }

    await state.clear()
    await process_food_entry(message, [item], "Barcode exact match", message.from_user.id, message.from_user.full_name)

# --- Reporting ---

@dp.message(Command("relatorio"))
async def cmd_report(message: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Hoje", callback_data="rep_1"),
         InlineKeyboardButton(text="Semana", callback_data="rep_7"),
         InlineKeyboardButton(text="Mês", callback_data="rep_30")]
    ])
    await message.answer("Escolha o período do relatório:", reply_markup=kb)

def generate_report_chart(data: list, days: int):
    """Generates a bar chart of calories per day."""
    if not data:
        return None
    
    # Aggregate by date
    daily_totals = {}
    for entry in data:
        # Convert created_at to date string YYYY-MM-DD
        dt = entry['created_at'][:10]
        daily_totals[dt] = daily_totals.get(dt, 0) + entry['kcal']
    
    # Sort dates
    sorted_dates = sorted(daily_totals.keys())
    values = [daily_totals[d] for d in sorted_dates]
    labels = [d[8:] + "/" + d[5:7] for d in sorted_dates] # DD/MM
    
    plt.figure(figsize=(8, 5))
    plt.bar(labels, values, color='#4CAF50')
    plt.title(f'Consumo de Calorias - {days} dias')
    plt.xlabel('Data')
    plt.ylabel('kcal')
    plt.grid(axis='y', linestyle='--', alpha=0.7)
    
    buf = io.BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight')
    plt.close()
    buf.seek(0)
    return buf

@dp.callback_query(F.data.startswith("rep_"))
async def process_report(callback: types.CallbackQuery):
    try:
        days = int(callback.data.split("_")[1])
        data = await get_report_data(callback.from_user.id, days)
        profile = await get_user_profile(callback.from_user.id)
        
        if profile and 'tdee' in profile:
            tdee = profile['tdee']
        else:
            logger.warning(f"Perfil sem TDEE para usuário {callback.from_user.id} no relatório. Usando fallback.")
            tdee = 2000
        total_kcal = sum(d.get('kcal', 0) for d in data)
        total_prot = sum(d.get('protein', 0) for d in data)
        total_carb = sum(d.get('carbs', 0) for d in data)
        total_fat = sum(d.get('fat', 0) for d in data)
        
        periodo = "Hoje" if days == 1 else f"Últimos {days} dias"
        avg = round(total_kcal / days) if days > 0 else 0
        status_label = "✅ DENTRO DA META" if avg <= tdee else "⚠️ ACIMA DA META"
        
        msg = (
            f"📊 **RELATÓRIO: {periodo.upper()}**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🔥 **Total:** {total_kcal} kcal (Média: {avg})\n"
            f"🎯 **Sua meta:** {tdee} kcal\n\n"
            f"💪 **Proteínas:** {total_prot}g\n"
            f"🍞 **Carbos:** {total_carb}g\n"
            f"🥑 **Gorduras:** {total_fat}g\n\n"
            f"⚖️ **Status:** {status_label}\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
        
        # Build chart
        chart_buf = generate_report_chart(data, days)
        
        if chart_buf:
            await callback.message.answer_photo(
                photo=types.BufferedInputFile(chart_buf.read(), filename="report.png"),
                caption=msg,
                parse_mode="Markdown"
            )
            await callback.message.delete()
        else:
            await callback.message.edit_text(msg, parse_mode="Markdown")
            
    except Exception as e:
        logger.error(f"Erro ao gerar relatório: {e}")
        await callback.answer("❌ Erro ao gerar relatório.")

# --- Food and Vision Handling ---

async def process_food_entry(message: types.Message, items: list, raw_data: str, user_id: int, user_name: str):
    """Common logic for saving and responding to food entries."""
    if not items:
        return

    # Log to DB
    if not await log_calories(user_id, user_name, items):
        await message.answer("❌ Erro ao salvar dados no Supabase.")
        return
    
    stats = await get_daily_stats(user_id)
    profile = await get_user_profile(user_id)
    if profile and 'tdee' in profile:
        daily_limit = profile['tdee']
    else:
        logger.warning(f"Perfil não encontrado ou sem TDEE para usuário {user_id} no momento do registro. Usando fallback.")
        daily_limit = 2000
    daily_total = stats["kcal"]
    remaining = daily_limit - daily_total
    
    has_universal = any(i.get("is_universal") for i in items)
    has_fs = any(i.get("is_fs_verified") for i in items)
    has_historical = any(i.get("is_historical") for i in items)
    
    source_tag = ""
    if has_historical:
        source_tag = "✅ `DADO DO SEU HISTÓRICO`\n\n"
    elif has_universal:
        source_tag = "✅ `DADO VERIFICADO (Catálogo)`\n\n"
    elif has_fs or all(i.get("is_precise") for i in items):
        source_tag = "✅ `DADO VERIFICADO (FatSecret/Scanner)`\n\n"
    else:
        source_tag = "⚠️ `DADO ESTIMADO (IA)`\n\n"

    items_text = ""
    for idx, i in enumerate(items):
        emoji = "🍎" if idx % 2 == 0 else "🥩"
        meal = f"[{i.get('refeicao', 'Outro')}] "
        precisao = "" if i.get("is_precise", False) else " ⚠️ *(estimado)*"
        
        items_text += f"{emoji} {meal}**{i['alimento']}** (**{i['peso']}**) → **{i['calorias']} kcal**{precisao}\n"
        items_text += f"   └ P: **{i.get('proteina', 0)}g** | C: **{i.get('carboidratos', 0)}g** | G: **{i.get('gorduras', 0)}g**\n"
        
    progress_val = min(10, round((daily_total/daily_limit)*10)) if daily_limit > 0 else 0
    progress_bar = "🔵" * progress_val + "⚪" * (10 - progress_val)
    
    now_br = get_br_now()
    data_formatada = now_br.strftime("%d/%m")

    # Feedback Buttons
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ 10%", callback_data="adj_1.1"),
         InlineKeyboardButton(text="➖ 10%", callback_data="adj_0.9")],
        [InlineKeyboardButton(text="🔧 Corrigir", callback_data="manual_correct"),
         InlineKeyboardButton(text="🔄 Desfazer", callback_data="adj_undo")]
    ])

    response_text = (
        f"{source_tag}"
        f"{items_text}\n"
        f"📊 **RELATÓRIO DE HOJE ({data_formatada})**\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🔥 Soma: **{daily_total}** / **{daily_limit} kcal**\n"
        f"⚖️ Restante: **{max(0, remaining)} kcal**\n\n"
        f"{progress_bar}"
    )
    await message.answer(response_text, parse_mode="Markdown", reply_markup=kb)

async def _handle_extraction_result(
    message: types.Message,
    state: FSMContext,
    items,
    error_type,
    raw_data,
    original_text: str,
    user_id: int,
    user_name: str,
):
    if error_type == "needs_choice":
        data = json.loads(raw_data)
        candidates = data.get("candidates", [])
        source = data.get("source", "fatsecret")
        kb = InlineKeyboardMarkup(inline_keyboard=[])
        for idx, c in enumerate(candidates):
            btn_text = f"{smart_truncate(c['alimento'], 28).capitalize()} ({int(c['calorias'])}kcal/{c['peso']})"
            if len(btn_text) > 40: btn_text = btn_text[:37] + "..."
            kb.inline_keyboard.append([InlineKeyboardButton(text=btn_text, callback_data=f"fsc_{idx}")])
        kb.inline_keyboard.append([InlineKeyboardButton(text="❌ Nenhuma destas", callback_data="fsc_none")])
        await state.update_data(
            fs_candidates=candidates, 
            original_text=original_text, 
            choice_source=source, 
            query_name=data.get("query_name"),
            resolved_so_far=data.get("resolved_so_far", []),
            pending_queries=data.get("pending_queries", [])
        )
        query_name = data.get("query_name", "este item")
        if source == "catalog":
            await message.answer(f"✅ **Banco Verificado:** Para `{query_name}`, qual se aproxima mais?", reply_markup=kb, parse_mode="Markdown")
        else:
            await message.answer(f"🔍 **Busca Global:** Para `{query_name}`, qual se aproxima mais?", reply_markup=kb, parse_mode="Markdown")
        await state.set_state(FatSecretState.waiting_for_choice)
        return
    elif error_type:
        await message.answer("❌ Erro na extração a partir do texto. Tente novamente.")
        return
    if items is not None and len(items) == 0:
        message_copy = message.model_copy(update={"text": original_text})
        await handle_nutri_chat(message_copy, user_id)
        return
    await process_food_entry(message, items, raw_data, user_id, user_name)

@dp.message(F.photo)
async def handle_photo(message: types.Message, state: FSMContext):
    if await state.get_state(): await state.clear()
    if not check_rate_limit(message.from_user.id):
        await message.answer("⏱️ Muitos pedidos em pouco tempo. Aguarde um minuto!")
        return
    try:
        status_msg = await message.answer("Analisando foto... 📸👀")
        
        # Get the best photo size
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        photo_bytes = io.BytesIO()
        await bot.download_file(file.file_path, destination=photo_bytes)
        
        items, barcode, error_type, raw_data = await extract_calories_list(
            user_id=message.from_user.id,
            image_bytes=photo_bytes.getvalue(),
            message_text=message.caption or "Foto de comida"
        )

        await status_msg.delete()
        if error_type:
            await message.answer(f"❌ Erro na análise da foto: {error_type}")
            return

        # Se detectou código de barras, busca no OpenFoodFacts
        if barcode and str(barcode).strip() and str(barcode).strip().lower() != "null":
            product_data = await get_barcode_data(barcode)
            if product_data:
                await state.update_data(barcode_product=product_data)
                await message.answer(
                    f"🔍 **Produto Detectado:** {product_data['alimento']}\n\n"
                    "Quanto você consumiu deste produto? (ex: 100g, 50, 2 unidades)",
                    parse_mode="Markdown"
                )
                await state.set_state(BarcodeState.waiting_for_portion)
                return
            else:
                logger.warning(f"Barcode {barcode} detectado mas não encontrado no OpenFoodFacts.")

        await process_food_entry(message, items, raw_data, message.from_user.id, message.from_user.full_name)
    except Exception as e:
        logger.error(f"Erro no handle_photo: {e}")
        await message.answer("❌ Ocorreu um erro inesperado ao processar a foto.")

@dp.message(F.voice)
async def handle_voice(message: types.Message, state: FSMContext):
    """Lida com mensagens de áudio, transcreve com Whisper e processa como texto."""
    if await state.get_state(): await state.clear()
    if not check_rate_limit(message.from_user.id):
        await message.answer("⏱️ Muitos pedidos em pouco tempo. Aguarde um minuto!")
        return
    global user_history

    try:
        msg_id = f"{message.chat.id}:{message.message_id}"
        now_ts = time.time()
        expired_keys = [k for k, v in processed_messages.items() if now_ts - v > 300]
        for k in expired_keys:
            del processed_messages[k]
        if msg_id in processed_messages: return
        processed_messages[msg_id] = now_ts

        status_msg = await message.answer("Ouvindo seu áudio... 🎧")
        user_id = message.from_user.id

        # 1. Baixar o arquivo de áudio do Telegram
        voice_file = await bot.get_file(message.voice.file_id)
        audio_bytes = io.BytesIO()
        await bot.download_file(voice_file.file_path, destination=audio_bytes)
        audio_bytes.name = "voice.ogg"  # A API do Groq exige um nome de arquivo

        # 2. Enviar para a API Whisper da Groq
        transcription = await groq_client.audio.transcriptions.create(
            file=(audio_bytes.name, audio_bytes.getvalue()),
            model="whisper-large-v3",
            prompt="O usuário está ditando alimentos que comeu ou fazendo perguntas para seu nutricionista virtual.",
            language="pt",
            response_format="json"
        )

        user_text = transcription.text
        if not user_text or len(user_text.strip()) < 2:
            await status_msg.edit_text("❌ Não consegui ouvir nada no áudio. Pode tentar de novo?")
            return

        # Atualiza a mensagem para mostrar o que o bot ouviu
        await status_msg.edit_text(f"🗣️ **Você disse:** _{user_text}_\n\nCalculando... 🧐", parse_mode="Markdown")

        # 3. Reutiliza exatamente a mesma lógica de texto!
        if not await is_food_message(user_text):
            await status_msg.delete()
            await handle_nutri_chat(message, user_id)
            return

        items, barcode, error_type, raw_data = await extract_calories_list(
            user_id=user_id,
            message_text=user_text
        )
        
        await status_msg.delete()

        await _handle_extraction_result(message, state, items, error_type, raw_data, user_text, user_id, message.from_user.full_name)

    except Exception as e:
        logger.error(f"Erro no handle_voice: {e}")
        await message.answer("❌ Ocorreu um erro inesperado ao processar seu áudio.")

@dp.message(F.text)
async def handle_text(message: types.Message, state: FSMContext):
    if await state.get_state(): await state.clear()
    global user_history
    try:
        if not check_rate_limit(message.from_user.id):
            await message.answer("⏱️ Muitos pedidos em pouco tempo. Aguarde um minuto!")
            return
        msg_id = f"{message.chat.id}:{message.message_id}"
        now_ts = time.time()
        expired_keys = [k for k, v in processed_messages.items() if now_ts - v > 300]
        for k in expired_keys:
            del processed_messages[k]
        if msg_id in processed_messages: return
        processed_messages[msg_id] = now_ts

        status_msg = await message.answer("Calculando... 🧐")
        
        user_id = message.from_user.id
        
        if jailbreak_users.get(user_id):
            if is_apology(message.text):
                jailbreak_users[user_id] = False
                await status_msg.delete()
                await message.answer("Ah, finalmente percebeu o erro? Tá bom, vamos voltar ao normal. O que você comeu?")
                return
            else:
                sarcasm = await generate_sarcastic_response(user_id, message.text)
                await status_msg.delete()
                await message.answer(sarcasm)
                return

        if is_jailbreak(message.text):
            jailbreak_users[user_id] = True
            await status_msg.delete()
            await message.answer("Desculpa, não entendi sua mensagem. Você quer registrar alguma comida? 🍎")
            return

        if not await is_food_message(message.text):
            await status_msg.delete()
            await handle_nutri_chat(message, user_id)
            return

        items, barcode, error_type, raw_data = await extract_calories_list(
            user_id=user_id,
            message_text=message.text
        )
        
        await status_msg.delete()
        await _handle_extraction_result(message, state, items, error_type, raw_data, message.text, user_id, message.from_user.full_name)
    except Exception as e:
        logger.error(f"Erro no handle_text: {e}")
        await message.answer("❌ Ocorreu um erro inesperado.")

@dp.callback_query(FatSecretState.waiting_for_choice, F.data.startswith("fsc_"))
async def process_fs_choice(callback: types.CallbackQuery, state: FSMContext):
    """Lida com a seleção do usuário na multi-escolha do FatSecret ou Catálogo."""
    
    # Remove o teclado imediatamente para evitar cliques duplos que corrompem o estado
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except:
        pass

    choice = callback.data.split("_")[1]
    
    data = await state.get_data()
    candidates = data.get("fs_candidates", [])
    original_text = data.get("original_text", "")
    source = data.get("choice_source", "fatsecret")
    query_name = data.get("query_name")
    resolved_so_far = data.get("resolved_so_far", [])
    pending_queries = data.get("pending_queries", [])
    
    if choice == "none":
        if source == "catalog":
            await callback.message.edit_text(f"🔄 Nenhuma serviu para `{query_name}`? Buscando opções globais...", parse_mode="Markdown")
            
            if pending_queries:
                queries = pending_queries[0]
            else:
                queries_list = await generate_surgical_query(original_text)
                queries = queries_list[0] if queries_list else {"pt": original_text, "en_spec": original_text, "en_gen": original_text}
                
            fs_res = await search_fatsecret(queries)
            if fs_res:
                if len(fs_res) > 1:
                    kb = InlineKeyboardMarkup(inline_keyboard=[])
                    for idx, c in enumerate(fs_res):
                        btn_text = f"{c['alimento'].capitalize()} ({int(c['calorias'])}kcal/{c['peso']})"
                        if len(btn_text) > 40: btn_text = btn_text[:37] + "..."
                        kb.inline_keyboard.append([InlineKeyboardButton(text=btn_text, callback_data=f"fsc_{idx}")])
                    kb.inline_keyboard.append([InlineKeyboardButton(text="❌ Nenhuma destas", callback_data="fsc_none")])
                    
                    await state.update_data(
                        fs_candidates=fs_res, 
                        original_text=original_text, 
                        choice_source="fatsecret", 
                        query_name=query_name,
                        resolved_so_far=resolved_so_far,
                        pending_queries=pending_queries
                    )
                    await callback.message.answer(f"🔍 **Busca Global:** Para `{query_name}`, qual se aproxima mais?", reply_markup=kb, parse_mode="Markdown")
                    await state.set_state(FatSecretState.waiting_for_choice)
                    return
                elif len(fs_res) == 1:
                    chosen = fs_res[0]
                    chosen["alimento"] = chosen.get("original_query", chosen["alimento"])
                    emb = await get_embedding(chosen["alimento"])
                    if emb: chosen["embedding"] = emb
                    if query_name: chosen["resolved_query"] = query_name
                    await save_to_universal_catalog(chosen)
                    
                    msg = await callback.message.answer(f"✅ Encontrado no Global: **{chosen['alimento']}**.\nCalculando porção...", parse_mode="Markdown")
                    resolved_so_far.append(chosen)
                    if pending_queries: pending_queries.pop(0)

                    items, barcode, error_type, raw_data = await extract_calories_list(
                        user_id=callback.from_user.id,
                        message_text=original_text,
                        resolved_candidates=resolved_so_far,
                        pre_generated_queries=pending_queries
                    )
                    await msg.delete()
                    await _handle_extraction_result(callback.message, state, items, error_type, raw_data, original_text, callback.from_user.id, callback.from_user.full_name)
                    return
                    
            await callback.message.answer(f"❌ Não encontrei `{query_name}` no banco global. Ignorando este item.")
            if pending_queries: pending_queries.pop(0)
            items, barcode, error_type, raw_data = await extract_calories_list(
                user_id=callback.from_user.id,
                message_text=original_text,
                resolved_candidates=resolved_so_far,
                pre_generated_queries=pending_queries
            )
            await _handle_extraction_result(callback.message, state, items, error_type, raw_data, original_text, callback.from_user.id, callback.from_user.full_name)
            return
        else:
            await callback.message.edit_text(f"Entendido. Ignorando `{query_name}`. Continuando...")
            if pending_queries: pending_queries.pop(0)
            items, barcode, error_type, raw_data = await extract_calories_list(
                user_id=callback.from_user.id,
                message_text=original_text,
                resolved_candidates=resolved_so_far,
                pre_generated_queries=pending_queries
            )
            await _handle_extraction_result(callback.message, state, items, error_type, raw_data, original_text, callback.from_user.id, callback.from_user.full_name)
            return
    
    try:
        idx = int(choice)
        if idx < 0 or idx >= len(candidates):
            raise ValueError()
    except:
        await callback.answer("Opção inválida.")
        await state.clear()
        return

    chosen = candidates[idx]
    chosen["alimento"] = chosen.get("original_query", chosen["alimento"])
    
    emb = await get_embedding(chosen["alimento"])
    if emb: chosen["embedding"] = emb
    if query_name: chosen["resolved_query"] = query_name
    await save_to_universal_catalog(chosen)
    
    msg = await callback.message.answer(f"✅ Selecionado: **{chosen['alimento']}**.\nAnalisando o restante...", parse_mode="Markdown")
    
    resolved_so_far.append(chosen)
    if pending_queries: pending_queries.pop(0)
    
    items, barcode, error_type, raw_data = await extract_calories_list(
        user_id=callback.from_user.id,
        message_text=original_text,
        resolved_candidates=resolved_so_far,
        pre_generated_queries=pending_queries
    )
    await msg.delete()
    await _handle_extraction_result(callback.message, state, items, error_type, raw_data, original_text, callback.from_user.id, callback.from_user.full_name)

# --- FastAPI Webhook ---

@app.post("/webhook")
async def telegram_webhook(request: Request):
    update = Update.model_validate(await request.json(), context={"bot": bot})
    asyncio.create_task(dp.feed_update(bot, update))
    return {"status": "ok"}

@app.get("/")
def index(): return {"status": "CaloriesBot is running"}

@app.api_route("/api/health", methods=["GET", "POST", "HEAD"])
def health_check(): return {"status": "ok"}

async def reminder_loop():
    """Background task to send reminders to inactive users."""
    logger.info("Loop de lembretes iniciado.")
    while True:
        try:
            now_br = get_br_now()
            hour = now_br.hour
            if hour in [11, 16, 20]:
                res = await async_execute(supabase.table("profiles").select("user_id"))
                users = res.data or []
                today_start = get_br_today_start()
                # 1 query para buscar todos que já registraram hoje (evita N+1)
                logged_res = await async_execute(
                    supabase.table("logs")
                    .select("user_id")
                    .gte("created_at", today_start)
                )
                logged_users = {row["user_id"] for row in (logged_res.data or [])}
                msg = "🔔 Registro pendente! Vamos focar na dieta? 💪🍎"
                for user in users:
                    uid = user['user_id']
                    if str(uid) not in logged_users:
                        try:
                            await bot.send_message(uid, msg)
                        except Exception as send_err:
                            logger.warning(f"Não foi possível enviar lembrete para {uid}: {send_err}")
            await asyncio.sleep(3600)
        except Exception as e:
            logger.error(f"Erro reminder loop: {e}")
            await asyncio.sleep(300)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
