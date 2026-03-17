import os
import io
import re
import json
import httpx
import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

import matplotlib.pyplot as plt
import matplotlib
matplotlib.use('Agg')

from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.types import Update, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.storage.memory import MemoryStorage
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
FATSECRET_PROXIES = [p.strip() for p in os.getenv("FATSECRET_PROXIES", "").split(",") if p.strip()]
WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL")

# Init Clients
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
app = FastAPI()
ai_client = genai.Client(api_key=GEMINI_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
# Ensure the client has the apikey header for all requests (redundant but safe)
supabase.postgrest.auth(SUPABASE_KEY) 

# Custom HTTP Client for general use
http_client = httpx.AsyncClient(timeout=httpx.Timeout(15.0), follow_redirects=True)

# FatSecret Proxy Rotation
fs_proxy_index = 0
def get_fs_client():
    global fs_proxy_index
    if not FATSECRET_PROXIES:
        return http_client
    proxy_url = FATSECRET_PROXIES[fs_proxy_index % len(FATSECRET_PROXIES)]
    fs_proxy_index += 1
    return httpx.AsyncClient(proxy=proxy_url, timeout=httpx.Timeout(15.0))

groq_client = None
if GROQ_API_KEY:
    groq_client = AsyncGroq(api_key=GROQ_API_KEY)

# Constants
AI_MODEL = "gemini-2.0-flash"
AI_MODEL_FALLBACK = "gemini-1.5-flash"

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

# Global State
processed_messages = set()
fs_token = {"access_token": None, "expires_at": 0}
fs_lock = asyncio.Lock()
AI_CACHE = {}
user_history = {}
jailbreak_users = {}

# --- Security Logic ---

def is_jailbreak(text: str) -> bool:
    if not text: return False
    text_lower = text.lower()
    
    patterns = [
        r"plane crashed.*snow forest", # Survivors
        r"do anything now", # DAN
        r"hacxgpt",
        r"evil-bot",
        r"developer mode.*enabled",
        r"ignore all.*instructions",
        r"system message",
        r"jailbreak",
        r"caloriesbot"
    ]
    
    for p in patterns:
        if re.search(p, text_lower):
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

# --- Timezone Helpers ---

def get_br_now():
    """Returns the current datetime in Brazil (UTC-3)."""
    return datetime.utcnow() - timedelta(hours=3)

def get_br_today_start():
    """Returns the start of today in Brazil (00:00:00) in ISO format with offset."""
    return get_br_now().strftime("%Y-%m-%dT00:00:00-03:00")

def parse_numeric(text: str) -> Optional[float]:
    """Robust parser for numeric inputs like '75,5', '175cm', '80kg'."""
    if not text: return None
    t = str(text).lower().replace(',', '.')
    match = re.search(r"(\d+[\.,]?\d*)", t)
    if match:
        try: return float(match.group(1))
        except: return None
    return None

# --- DB Logic ---

def log_calories(user_id: str, user_name: str, items: list):
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
            res = supabase.table("logs").insert(prepared_data).execute()
            logger.info(f"Supabase Log Insertion Result: {res.data}")
        return True
    except Exception as e:
        logger.error(f"Erro ao salvar no Supabase: {e}")
        return False

def save_to_universal_catalog(item: dict):
    """Saves a verified food item to the catalog to prevent redundant AI calls."""
    try:
        food_name = item.get("alimento")
        check = supabase.table("universal_catalog").select("id").eq("food", food_name).limit(1).execute()
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
        res = supabase.table("universal_catalog").insert(data).execute()
        logger.info(f"Supabase Catalog Insertion Result: {res.data}")
        logger.info(f"✅ Item '{food_name}' salvo no Catálogo Universal.")
        return True
    except Exception as e:
        logger.error(f"Erro ao salvar no catálogo: {e}")
        return False

def search_universal_catalog(query_embedding: list, threshold: float = 0.85):
    """Performs semantic search in the universal_catalog table."""
    try:
        res = supabase.rpc("match_food_catalog", {
            "query_embedding": query_embedding,
            "match_threshold": threshold,
            "match_count": 1
        }).execute()
        logger.info(f"Supabase Vector Search result: {res.data}")
        
        if res.data:
            item = res.data[0]
            return [{
                "alimento": item.get("food"),
                "peso": item.get("serving_size", "100g"),
                "calorias": item.get("kcal", 0),
                "proteina": item.get("protein", 0),
                "carboidratos": item.get("carbs", 0),
                "gorduras": item.get("fat", 0),
                "is_precise": True,
                "is_universal": True
            }]
        return None
    except Exception as e:
        logger.error(f"Erro na busca vetorial: {e}")
        return None

def get_user_profile(user_id: str):
    try:
        res = supabase.table("profiles").select("*").eq("user_id", str(user_id)).execute()
        return res.data[0] if res.data else None
    except Exception as e:
        logger.error(f"Erro ao buscar perfil: {e}")
        return None

def get_daily_stats(user_id: str):
    """Calculates total calories and macros for the current day."""
    try:
        today_br_start = get_br_today_start()
        response = supabase.table("logs") \
            .select("kcal, protein, carbs, fat") \
            .eq("user_id", str(user_id)) \
            .gte("created_at", today_br_start) \
            .execute()
            
        total_kcal = sum(item.get('kcal', 0) for item in response.data)
        total_prot = sum(item.get('protein', 0) for item in response.data)
        total_carb = sum(item.get('carboidratos', 0) for item in response.data)
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

def get_daily_total(user_id: str):
    return get_daily_stats(user_id)["kcal"]

def get_report_data(user_id: str, days: int):
    """Aggregates data for periodic reports."""
    try:
        now_br = get_br_now()
        start_date = (now_br - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00-03:00")
        res = supabase.table("logs") \
            .select("created_at, kcal, protein, carbs, fat") \
            .eq("user_id", str(user_id)) \
            .gte("created_at", start_date) \
            .execute()
        return res.data or []
    except Exception as e:
        logger.error(f"Erro ao buscar dados do relatório: {e}")
        return []

def delete_last_log(user_id: str):
    """Deletes the most recent meal log."""
    try:
        res = supabase.table("logs") \
            .select("id, created_at") \
            .eq("user_id", str(user_id)) \
            .order("created_at", desc=True) \
            .limit(1) \
            .execute()
        if res.data:
            created_at = res.data[0]['created_at']
            supabase.table("logs").delete() \
                .eq("user_id", str(user_id)) \
                .eq("created_at", created_at) \
                .execute()
            return True
        return False
    except Exception as e:
        logger.error(f"Erro ao deletar último log: {e}")
        return False

def delete_today_logs(user_id: str):
    """Deletes all logs from the current day."""
    try:
        today_br_start = get_br_today_start()
        supabase.table("logs").delete() \
            .eq("user_id", str(user_id)) \
            .gte("created_at", today_br_start) \
            .execute()
        return True
    except Exception as e:
        logger.error(f"Erro ao deletar logs de hoje: {e}")
        return False

def delete_entire_profile(user_id: str):
    """Deletes profile and all logs for a user."""
    try:
        supabase.table("logs").delete().eq("user_id", str(user_id)).execute()
        supabase.table("profiles").delete().eq("user_id", str(user_id)).execute()
        return True
    except Exception as e:
        logger.error(f"Erro ao deletar perfil completo: {e}")
        return False

def search_food_history(user_id: str, food_query: str):
    """Searches for a historical log entry (Personal -> Universal Fallback)."""
    try:
        # Pessoal Exato
        res = supabase.table("logs") \
            .select("food, weight, kcal, protein, carbs, fat, meal_type, is_precise") \
            .eq("user_id", str(user_id)) \
            .ilike("food", f"{food_query}") \
            .order("created_at", desc=True) \
            .limit(1) \
            .execute()
        
        # Pessoal Aproximado
        if not res.data:
            res = supabase.table("logs") \
                .select("food, weight, kcal, protein, carbs, fat, meal_type, is_precise") \
                .eq("user_id", str(user_id)) \
                .ilike("food", f"%{food_query}%") \
                .order("created_at", desc=True) \
                .limit(1) \
                .execute()
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
        logger.info(f"Embedding generated for text: '{text[:50]}...'")
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

async def generate_surgical_query(food_name: str):
    """Generates clean food names for both PT and EN search."""
    if not groq_client: return {"pt": food_name, "en": food_name}
    
    prompt = f"""
    Extraia o nome puro do alimento. Retorne JSON:
    {{"pt": "nome em português", "en": "english name"}}
    Exemplo: "2 fatias de pão visconti" -> {{"pt": "pão visconti", "en": "sliced white bread visconti"}}
    Entrada: "{food_name}"
    """
    
    try:
        completion = await groq_client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.1-8b-instant",
            response_format={"type": "json_object"},
            temperature=0,
        )
        data = completion.choices[0].message.content
        logger.info(f"Groq Surgical Query Response: {data}")
        return json.loads(data)
    except Exception as e:
        logger.error(f"Groq surgical query error: {e}")
        return {"pt": food_name, "en": food_name}

async def search_fatsecret(food_name: str):
    token = await get_fatsecret_token()
    if not token: return None
    
    url = "https://platform.fatsecret.com/rest/server.api"
    # Dual query strategy
    queries = await generate_surgical_query(food_name)
    
    # Try with EN first for better matching in Global DB
    search_term = queries.get("en") or queries.get("pt")
    
    params = {
        "method": "foods.search", 
        "search_expression": search_term, 
        "format": "json", 
        "region": "BR", # Still kept as hint
        "language": "pt",
        "max_results": 5 # Get multiple results to find the needle in the haystack
    }
    headers = {"Authorization": f"Bearer {token}"}
    
    client = get_fs_client()
    found_items = []
    
    try:
        res = await client.get(url, params=params, headers=headers)
        if res.status_code == 200:
            data = res.json()
            logger.info(f"FatSecret Raw Search Results (Total): {json.dumps(data, ensure_ascii=False)}")
            
            foods_data = data.get("foods", {}).get("food", [])
            if not foods_data: return None
            if isinstance(foods_data, dict): foods_data = [foods_data]
            
            # Step 2: Use Groq to select the most relevant match from the list
            selection_prompt = f"""
            Qual desses alimentos é o melhor match para: "{queries.get('pt')}"?
            RESULTADOS: {json.dumps(foods_data, ensure_ascii=False)}
            Retorne o index (0, 1, 2...) ou -1 se nenhum for relevante.
            Apenas o número.
            """
            
            sel_res = await groq_client.chat.completions.create(
                messages=[{"role": "user", "content": selection_prompt}],
                model="llama-3.1-8b-instant",
                temperature=0,
            )
            try:
                idx = int(sel_res.choices[0].message.content.strip())
                if idx < 0 or idx >= len(foods_data): return None
                best_food = foods_data[idx]
            except:
                best_food = foods_data[0] # Fallback to first if LLM fails
            
            food_id = best_food["food_id"]
            d_res = await client.get(url, params={"method": "food.get.v2", "food_id": food_id, "format": "json", "region": "BR", "language": "pt"}, headers=headers)
            if d_res.status_code == 200:
                d_data = d_res.json()
                logger.info(f"FatSecret Raw Detail Result: {json.dumps(d_data, ensure_ascii=False)}")
                
                d = d_data.get("food", {})
                servings = d.get("servings", {}).get("serving", [])
                if isinstance(servings, dict): servings = [servings]
                if not servings: return None
                s = servings[0]
                
                result = {
                    "alimento": d.get("food_name"),
                    "calorias": float(s.get("calories", 0)),
                    "proteina": float(s.get("protein", 0)),
                    "carboidratos": float(s.get("carbohydrate", 0)),
                    "gorduras": float(s.get("fat", 0)),
                    "peso": "100g",
                    "is_precise": True
                }
                # Save to catalog logic
                emb = await get_embedding(result["alimento"])
                result["embedding"] = emb
                save_to_universal_catalog(result)
                return [result]
    except Exception as e:
        logger.error(f"FatSecret Search Error: {e}")
    finally:
        if client is not http_client: await client.aclose()
    return None

async def extract_calories_list(user_id: int, message_text: str = "", image_bytes: bytes = None):
    # Flow: 1. Search (Catalog + FatSecret) -> 2. Contextual Extraction (Groq/Gemini scales everything)
    
    search_context = []
    
    # 1. Search Catalog (Vector)
    if message_text and not image_bytes:
        emb = await get_embedding(message_text)
        if emb:
            match = search_universal_catalog(emb)
            if match:
                logger.info(f"🎯 Catalog Context Hit: {message_text}")
                search_context.extend(match)

    # 2. Search FatSecret (Text only fallback)
    if not image_bytes and message_text:
        fs_res = await search_fatsecret(message_text)
        if fs_res:
            logger.info(f"🔍 FatSecret Context Hit for: {message_text}")
            search_context.extend(fs_res)

    # 3. Model extraction with Context
    if image_bytes:
        # Use Gemini ONLY for VISION
        prompt = f"""
        Você é um nutricionista especialista. Analise a IMAGEM e retorne JSON.
        DADOS DE BUSCA (Se houver): {json.dumps(search_context, ensure_ascii=False)}
        
        SCHEMA: {{"foods": [{{"alimento": str, "peso": str, "calorias": int, "proteina": float, "carboidratos": float, "gorduras": float, "refeicao": str}}], "barcode": str}}
        REGRAS: 
        1. Se houver dados de busca compatíveis, USE-OS como base para calibrar os macros.
        2. ESCALE os valores para a quantidade que você ver na imagem ou ler no texto.
        3. Se não houver peso, use 100g.
        """
        contents = [prompt]
        if message_text: contents.append(message_text)
        contents.append(ai_types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"))
        
        try:
            res = await ai_client.aio.models.generate_content(
                model=AI_MODEL, contents=contents,
                config=ai_types.GenerateContentConfig(response_mime_type="application/json")
            )
            raw_text = res.text
            logger.info(f"Gemini Vision Raw Response: {raw_text}")
            data = json.loads(raw_text)
            return data.get("foods", []), data.get("barcode"), None, raw_text
        except Exception as e:
            logger.error(f"Gemini Vision Error: {e}")
            return [], None, str(e), None
    
    elif message_text:
        # Use Groq for TEXT extraction + scaling
        if not groq_client: return [], None, "Groq client not init", None

        prompt = f"""
        Você é um nutricionista especialista. Extraia os alimentos e seus macros do texto usuario.
        USE OS DADOS DE BUSCA ABAIXO COMO REFERÊNCIA DE CALORIAS POR PORÇÃO.
        
        DADOS DE BUSCA: {json.dumps(search_context, ensure_ascii=False)}
        TEXTO DO USUÁRIO: "{message_text}"

        REGRAS:
        1. Identifique a quantidade no texto (ex: "2 fatias").
        2. Use os DADOS DE BUSCA para saber quanto vale 1 porção/100g.
        3. FAÇA A CONTA e retorne os macros ajustados para a quantidade do usuário.
        4. Retorne APENAS um objeto JSON com a chave "itens".
        
        SCHEMA: {{"itens": [{{"alimento": str, "peso": str, "calorias": int, "proteina": float, "carboidratos": float, "gorduras": float, "refeicao": str, "is_precise": bool}}]}}
        """
        
        try:
            completion = await groq_client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model="llama-3.3-70b-versatile",
                response_format={"type": "json_object"},
                temperature=0,
            )
            raw_data = completion.choices[0].message.content
            logger.info(f"Groq Unified Extraction Response: {raw_data}")
            parsed = json.loads(raw_data)
            foods = parsed.get("itens") or parsed.get("foods") or []
            return foods, None, None, raw_data
        except Exception as e:
            logger.error(f"Groq Unified Extraction Error: {e}")
            return [], None, str(e), None

    return [], None, None, None

def extract_amount(text: str) -> Optional[float]:
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
        (r"(fatia|f\.)", 30)
    ]

    fractions = {
        "meio": 0.5, "metade": 0.5, "1/2": 0.5,
        "um quarto": 0.25, "1/4": 0.25,
        "um terço": 0.33, "1/3": 0.33,
        "três quartos": 0.75, "3/4": 0.75
    }
    for word, mult in fractions.items():
        if word in t:
            for pattern, factor in household:
                if re.search(rf"{word}\s*{pattern}", t):
                    return mult * factor
    
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
                    return {
                        "alimento": f"{product.get('product_name', 'Desconhecido')} ({product.get('brands', '')})",
                        "kcal_100g": nutriments.get("energy-kcal_100g", 0),
                        "prot_100g": nutriments.get("proteins_100g", 0),
                        "carb_100g": nutriments.get("carbohydrates_100g", 0),
                        "fat_100g": nutriments.get("fat_100g", 0),
                    }
        except Exception as e:
            logger.error(f"Erro ao buscar OpenFoodFacts: {e}")
    return None

# Logic consolidated above

# --- Mifflin-St Jeor ---

# calculate_tdee already defined above

# --- Bot Handlers ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    logger.info(f"User {message.from_user.id} enviou /start")
    profile = get_user_profile(message.from_user.id)
    
    if not profile:
        await message.answer(
            f"👋 Olá, **{message.from_user.first_name}**! Bem-vindo ao Bot de Calorias.\n\n"
            "Ainda não te conheço! Para calcular suas metas personalizadas, "
            "precisamos configurar seu perfil (é rapidinho).",
            parse_mode="Markdown"
        )
        await message.answer("1️⃣ Qual seu **peso** atual em kg? (ex: 75.5)")
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
    if delete_last_log(message.from_user.id):
        # Remove a última entrada da memória da IA também para manter coerência
        if message.from_user.id in user_history and user_history[message.from_user.id]:
            user_history[message.from_user.id].pop()
        await message.answer("🔄 A última entrada foi removida com sucesso!")
    else:
        await message.answer("❌ Não encontrei entradas recentes para remover.")

@dp.message(Command("reset_dia"))
async def cmd_reset_day(message: types.Message):
    if delete_today_logs(message.from_user.id):
        # Limpa memória local da IA também para o dia
        if message.from_user.id in user_history:
            user_history[message.from_user.id] = []
        await message.answer("📅 Seus logs de **hoje** foram apagados!")
    else:
        await message.answer("❌ Erro ao apagar logs de hoje.")

@dp.message(Command("reset_perfil"))
async def cmd_reset_profile(message: types.Message, state: FSMContext):
    if delete_entire_profile(message.from_user.id):
        # Limpa tudo
        if message.from_user.id in user_history:
            del user_history[message.from_user.id]
        if message.from_user.id in jailbreak_users:
            del jailbreak_users[message.from_user.id]
            
        await message.answer("💥 **Perfil e histórico deletados!** Vamos começar do zero.")
        # Trigger onboarding again
        await cmd_start(message, state)
    else:
        await message.answer("❌ Erro ao deletar seu perfil.")

@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    user_id = message.from_user.id
    profile = get_user_profile(user_id)
    
    if not profile:
        await message.answer("⚠️ Você ainda não configurou seu perfil. Use /start para começar!")
        return
        
    daily_limit = profile['tdee']
    daily_total = get_daily_total(user_id)
    remaining = daily_limit - daily_total
    
    # Get current meal data for macros breakdown
    today_br_start = get_br_today_start()
    now_br = get_br_now()
    data_formatada = now_br.strftime("%d/%m/%Y %H:%M")
    
    res = supabase.table("logs").select("protein, carbs, fat").eq("user_id", str(user_id)).gte("created_at", today_br_start).execute()
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

    await message.answer(status_msg, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("adj_"))
async def process_adjustment(callback: types.CallbackQuery):
    """Handles quick calorie adjustments from the log feedback buttons."""
    user_id = callback.from_user.id
    action = callback.data.split("_")[1] # 1.1, 0.9, undo
    
    try:
        # Get the latest entry timestamp for this user
        res = supabase.table("logs") \
            .select("created_at") \
            .eq("user_id", str(user_id)) \
            .order("created_at", desc=True) \
            .limit(1) \
            .execute()
        
        if not res.data:
            await callback.answer("❌ Nenhum log recente encontrado.")
            return

        last_time = res.data[0]['created_at']

        if action == "undo":
            if delete_last_log(user_id):
                await callback.message.edit_text("🔄 **Log desfeito com sucesso!**", parse_mode="Markdown")
            else:
                await callback.answer("❌ Erro ao desfazer.")
            return

        # Numeric adjustment
        multiplier = float(action)
        
        # Update kcal and macros in the DB using RPC or direct update
        # For simplicity, we update all entries at that exact timestamp
        logs_to_update = supabase.table("logs") \
            .select("*") \
            .eq("user_id", str(user_id)) \
            .eq("created_at", last_time) \
            .execute()

        for entry in logs_to_update.data:
            supabase.table("logs").update({
                "kcal": round(entry['kcal'] * multiplier),
                "protein": round(entry['protein'] * multiplier),
                "carbs": round(entry['carbs'] * multiplier),
                "fat": round(entry['fat'] * multiplier)
            }).eq("id", entry['id']).execute()

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
    res = supabase.table("logs") \
        .select("*") \
        .eq("user_id", str(user_id)) \
        .order("created_at", desc=True) \
        .limit(1) \
        .execute()
        
    if res.data:
        entry = res.data[0]
        weight = extract_amount(entry.get("weight", "100g")) or 100
        new_kcal = round((kcal_100g / 100) * weight)
        
        supabase.table("logs").update({
            "kcal": new_kcal,
            "is_precise": True
        }).eq("id", entry['id']).execute()
        
        await message.answer(f"✅ Corrigido para **{new_kcal} kcal** ({kcal_100g} kcal/100g)!")
    
    await state.clear()

@dp.message(Command("cancelar"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Operação cancelada. Como posso ajudar agora?")

# --- Onboarding FSM ---

@dp.message(Command("perfil"))
async def start_profile(message: types.Message, state: FSMContext):
    await message.answer("Vamos calcular sua meta! Qual seu **peso** atual em kg? (ex: 75.5)")
    await state.set_state(ProfileStates.weight)

@dp.message(ProfileStates.weight)
async def process_weight(message: types.Message, state: FSMContext):
    try:
        weight = float(message.text.replace(',', '.'))
        await state.update_data(weight=weight)
        await message.answer("2️⃣ Qual sua **altura** em cm? (ex: 175)")
        await state.set_state(ProfileStates.height)
    except:
        await message.answer("Por favor, envie um número válido.")

@dp.message(ProfileStates.height)
async def process_height(message: types.Message, state: FSMContext):
    try:
        height = float(message.text)
        await state.update_data(height=height)
        await message.answer("3️⃣ Qual sua **idade**?")
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
        await message.answer("4️⃣ Qual seu **sexo**?", reply_markup=kb)
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
    await callback.message.edit_text("5️⃣ Qual seu nível de **atividade física**?", reply_markup=kb)
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
    await callback.message.edit_text("6️⃣ Qual seu **objetivo** principal?", reply_markup=kb)
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
    supabase.table("profiles").upsert(profile_data).execute()
    
    await state.clear()
    await callback.message.edit_text(
        f"✅ Perfil configurado!\n"
        f"Sua meta diária (ajustada para {goal}) é: **{tdee} kcal**\n\n"
        f"Agora é só me mandar seus alimentos ou uma foto do prato! 📸🍎"
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
    grams = extract_amount(message.text)
    
    # Simple heuristic if no 'g' found
    if not grams:
        try:
            grams = float(message.text.split()[0].replace(",", "."))
        except:
            await message.answer("❌ Não entendi a quantidade. Digite algo como '100g' ou '200'.")
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
        "refeicao": "Lanche", # Default para scanner, process_food_entry ajusta se necessário
        "is_precise": True
    }

    await state.clear()
    await process_food_entry(message, [item], "Barcode exact match")

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
        data = get_report_data(callback.from_user.id, days)
        profile = get_user_profile(callback.from_user.id)
        
        tdee = profile['tdee'] if profile else 2000
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


# --- Food and Vision Handling ---

async def process_food_entry(message: types.Message, items: list, raw_data: str):
    """Common logic for saving and responding to food entries."""
    if not items:
        return

    # Log to DB
    if not log_calories(message.from_user.id, message.from_user.full_name, items):
        await message.answer("❌ Erro ao salvar dados no Supabase.")
        return
    
    stats = get_daily_stats(message.from_user.id)
    profile = get_user_profile(message.from_user.id)
    daily_limit = profile['tdee'] if profile else 2000
    daily_total = stats["kcal"]
    remaining = daily_limit - daily_total
    
    items_text = ""
    for idx, i in enumerate(items):
        emoji = "🍎" if idx % 2 == 0 else "🥩"
        meal = f"[{i.get('refeicao', 'Outro')}] "
        # Se for preciso (Verificado), não mostra tag. Se for impreciso, mostra (estimado).
        precisao = "" if i.get("is_precise", False) else " ⚠️ *(estimado)*"
        
        # Tag especial para Catálogo Universal
        universal_tag = " 🌐 *(universal)*" if i.get("is_universal") else ""
        
        items_text += f"{emoji} {meal}**{i['alimento']}** ({i['peso']}) → {i['calorias']} kcal{precisao}{universal_tag}\n"
        items_text += f"   └ P: {i.get('proteina', 0)}g | C: {i.get('carboidratos', 0)}g | G: {i.get('gorduras', 0)}g\n"
        
    progress_val = min(10, round((daily_total/daily_limit)*10)) if daily_limit > 0 else 0
    progress_bar = "🔵" * progress_val + "⚪" * (10 - progress_val)
    
    now_br = get_br_now()
    data_formatada = now_br.strftime("%d/%m")

    # Feedback Buttons
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ 10%", callback_data="adj_1.1"),
         InlineKeyboardButton(text="➖ 10%", callback_data="adj_0.9")],
        [InlineKeyboardButton(text="🔄 Desfazer", callback_data="adj_undo")]
    ])

    response_text = (
        f"{items_text}\n"
        f"📊 **CONTAGEM DE HOJE ({data_formatada})**\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🔥 Soma: **{daily_total}** / {daily_limit} kcal\n"
        f"⚖️ Restante: **{max(0, remaining)} kcal**\n\n"
        f"{progress_bar}"
    )
    await message.answer(response_text, parse_mode="Markdown", reply_markup=kb)

@dp.message(F.photo, StateFilter(None))
async def handle_photo(message: types.Message, state: FSMContext):
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

        await process_food_entry(message, items, raw_data)
    except Exception as e:
        logger.error(f"Erro no handle_photo: {e}")
        await message.answer("❌ Ocorreu um erro inesperado ao processar a foto.")

@dp.message(F.text, StateFilter(None))
async def handle_text(message: types.Message):
    global user_history # Ensure global access to avoid NameError
    try:
        msg_id = f"{message.chat.id}:{message.message_id}"
        if msg_id in processed_messages: return
        processed_messages.add(msg_id)
        if len(processed_messages) > 1000: processed_messages.clear()

        status_msg = await message.answer("Calculando... 🧐")
        
        user_id = message.from_user.id
        
        # Check if user is in sarcasm mode
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

        # Check for new jailbreak attempt
        if is_jailbreak(message.text):
            jailbreak_users[user_id] = True
            await status_msg.delete()
            await message.answer("Eae amigão, ta tentando mandar um Jailbreak para CaloriesBot? Não vai conseguir.")
            return

        # Busca rápida no histórico antes da IA
        if len(message.text.split()) <= 3:
            db_match = search_food_history(user_id, message.text.strip())
            if db_match and db_match[0].get("is_approximate"):
                # Deixa a IA processar ou poderíamos implementar confirmação
                pass

        items, barcode, error_type, raw_data = await extract_calories_list(
            user_id=user_id, 
            message_text=message.text
        )
        
        await status_msg.delete()
        if error_type:
            await message.answer(f"❌ Erro na extração. Tente novamente.")
            return
            
        if items is not None and len(items) == 0:
            await message.answer("🤔 Não identifiquei alimentos.")
            return

        await process_food_entry(message, items, raw_data)
    except Exception as e:
        logger.error(f"Erro no handle_text: {e}")
        await message.answer("❌ Ocorreu um erro inesperado.")

# --- FastAPI Webhook ---

@app.post("/webhook")
async def telegram_webhook(request: Request):
    update = Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"status": "ok"}

@app.get("/")
def index(): return {"status": "CaloriesBot is running"}

@app.api_route("/api/health", methods=["GET", "POST", "HEAD"])
def health_check(): return {"status": "ok"}

@app.on_event("startup")
async def on_startup():
    await bot.set_webhook(url=f"{WEBHOOK_URL}/webhook")
    logger.info(f"Webhook set to {WEBHOOK_URL}/webhook")
    asyncio.create_task(reminder_loop())

@app.on_event("shutdown")
async def on_shutdown():
    await http_client.aclose()
    logger.info("HTTP client closed.")

async def reminder_loop():
    """Background task to send reminders to inactive users."""
    logger.info("Loop de lembretes iniciado.")
    while True:
        try:
            now_br = get_br_now()
            hour = now_br.hour
            if 10 <= hour <= 22:
                res = supabase.table("profiles").select("user_id").execute()
                users = res.data or []
                today_start = get_br_today_start()
                for user in users:
                    uid = user['user_id']
                    log_res = supabase.table("logs").select("id").eq("user_id", str(uid)).gte("created_at", today_start).limit(1).execute()
                    if not log_res.data and hour in [11, 16, 20]:
                        msg = "🔔 Registro pendente! Vamos focar na dieta? 💪🍎"
                        await bot.send_message(uid, msg, parse_mode="Markdown")
            await asyncio.sleep(3600) 
        except Exception as e:
            logger.error(f"Erro reminder loop: {e}")
            await asyncio.sleep(300)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
