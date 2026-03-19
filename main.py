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
FATSECRET_PROXIES = [p.strip() for p in os.getenv("FATSECRET_PROXIES", "").split(",") if p.strip()]
WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL")

# Init Clients
bot = Bot(token=TELEGRAM_TOKEN)
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

class FatSecretState(StatesGroup):
    waiting_for_choice = State()

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
    """Returns the current datetime in Brazil (America/Sao_Paulo)."""
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/Sao_Paulo"))

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
        today_br_start = get_br_today_start()
        response = await async_execute(
            supabase.table("logs")
            .select("kcal, protein, carbs, fat")
            .eq("user_id", str(user_id))
            .gte("created_at", today_br_start)
        )
            
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
        logger.error(f"Erro ao buscar dados do relatório: {e}")
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
            created_at = res.data[0]['created_at']
            await async_execute(
                supabase.table("logs").delete()
                .eq("user_id", str(user_id))
                .eq("created_at", created_at)
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
    """Generates clean food names for both PT and EN search with variations."""
    if not groq_client: return {"pt": food_name, "en_spec": food_name, "en_gen": food_name}
    
    prompt = f"""
    Extraia o nome do alimento do texto do usuário.
    Retorne JSON com 3 variações:
    1. "pt": Nome LIMPO em português (REMOVA quantidades como "1", "2 unidades", "100g").
    2. "en_spec": Nome específico em inglês (com marca/tipo).
    3. "en_gen": Nome genérico em inglês (apenas o tipo de alimento).
    
    Exemplo: "2 Big Macs" -> 
    {{"pt": "big mac", "en_spec": "Mcdonalds big mac", "en_gen": "hamburger"}}
    
    Exemplo: "1 whopper" ->
    {{"pt": "whopper", "en_spec": "burger king whopper", "en_gen": "hamburger"}}
    
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
        return {"pt": food_name, "en_spec": food_name, "en_gen": food_name}

async def search_fatsecret(food_name: str):
    token = await get_fatsecret_token()
    if not token: return None
    
    url = "https://platform.fatsecret.com/rest/server.api"
    queries = await generate_surgical_query(food_name)
    
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
            
            logger.info(f"FatSecret Searching with '{search_term}' ({search_key}). Results: {len(foods_data)}")

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
                    logger.info(f"🎯 FatSecret Selected Candidate: {best_food['food_name']}")
                    
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
                            "original_query": queries.get('pt') or food_name,
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

async def extract_calories_list(user_id: int, message_text: str = "", image_bytes: bytes = None, fs_chosen_candidate: dict = None):
    # Flow: 1. Search (Catalog + FatSecret) -> 2. Contextual Extraction (Groq/Gemini scales everything)
    
    search_context = []
    
    if fs_chosen_candidate:
        search_context.append(fs_chosen_candidate)
    else:
        # 1. Search Catalog (Vector)
        if message_text and not image_bytes:
            # Novo: Extrair o nome limpo em PT antes do embedding para não sujar com as quantidades/marcas
            queries = await generate_surgical_query(message_text)
            clean_name_pt = queries.get("pt", message_text)
            
            emb = await get_embedding(clean_name_pt)
            if emb or clean_name_pt:
                match = await search_universal_catalog(query_embedding=emb, threshold=0.75, keyword=clean_name_pt)
                if match:
                    if len(match) > 1:
                        logger.info("Multiple Catalog candidates found. Requesting user choice.")
                        return [], None, "needs_choice", json.dumps({"candidates": match, "source": "catalog"})
                    else:
                        cand = match[0]
                        logger.info(f"🎯 Catalog Single Context Hit: {cand['alimento']}")
                        search_context.append(cand)

        # 2. Search FatSecret (Text only fallback)
        if not image_bytes and message_text and not search_context:
            fs_res = await search_fatsecret(message_text)
            if fs_res:
                if len(fs_res) > 1:
                    logger.info("Multiple FatSecret candidates found. Requesting user choice.")
                    return [], None, "needs_choice", json.dumps({"candidates": fs_res, "source": "fatsecret"})
                elif len(fs_res) == 1:
                    cand = fs_res[0]
                    logger.info(f"🔍 FatSecret Single Context Hit: {cand['alimento']}")
                    cand["alimento"] = cand.get("original_query", cand["alimento"])
                    emb = await get_embedding(cand["alimento"])
                    cand["embedding"] = emb
                    await save_to_universal_catalog(cand)
                    search_context.append(cand)

    # 3. Model extraction with Context
    if image_bytes:
        # Use Gemini ONLY for VISION
        prompt = f"""
        Você é um nutricionista especialista. Analise a IMAGEM e retorne JSON.
        DADOS DE BUSCA (Se houver): {json.dumps(search_context, ensure_ascii=False)}
        
        SCHEMA: {{"foods": [{{"alimento": str, "peso": str, "calorias": int, "proteina": float, "carboidratos": float, "gorduras": float, "refeicao": str}}], "barcode": str}}
        
        REGRAS: 
        1. Se houver dados de busca compatíveis, USE-OS como base para os macros.
        2. Olhe a chave "peso" dos dados de busca.
        3. Se for "100g", estime o peso da comida na foto em gramas e faça Regra de Três.
        4. Se for "1 unidade" (lanche/fast food), apenas CONTE quantos lanches tem na foto e multiplique diretamente.
        5. Se não houver dados de busca, use sua melhor estimativa.
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
        USE OS DADOS DE BUSCA ABAIXO COMO REFERÊNCIA DE CALORIAS E PORÇÕES.
        
        DADOS DE BUSCA: {json.dumps(search_context, ensure_ascii=False)}
        TEXTO DO USUÁRIO: "{message_text}"

        REGRAS MATEMÁTICAS RÍGIDAS:
        1. Identifique a quantidade que o usuário consumiu no texto.
        2. Olhe a chave "peso" do alimento correspondente nos DADOS DE BUSCA.
        3. SE O PESO BASE FOR "100g" (Regra de Três):
           - Fórmula: (Quantidade do Usuário em gramas / 100) * Calorias.
           - Se o usuário falou em medidas caseiras (fatia, colher), estime o peso em gramas antes de calcular.
        4. SE O PESO BASE FOR "1 unidade" (Fast Food / Porção Fechada):
           - Multiplicação Direta: (Quantidade de lanches) * Calorias.
           - Exemplo: Se os dados dizem "1 unidade = 550kcal" e o usuário comeu "2", retorne 1100kcal. 
           - Ignore tentativas de calcular em gramas se for um lanche fechado.
        5. Pondere TODOS os outros macros (proteina, carboidratos, gorduras) usando a mesma regra que usou para as calorias. Arredonde para números inteiros.
        6. No campo "peso", descreva o que foi calculado (Ex: "2 unidades", ou "150g", ou "2 fatias (50g)").
        7. Retorne APENAS um objeto JSON com a chave "itens".
        
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
        (r"(fatia|f\.)", 30)
    ]

    fractions = {
        "todo": 1.0, "toda": 1.0, "inteiro": 1.0, "inteira": 1.0, "1 pacote": 1.0, "uma garrafa": 1.0, "1 lata": 1.0,
        "meio": 0.5, "metade": 0.5, "1/2": 0.5, "meia": 0.5,
        "um quarto": 0.25, "1/4": 0.25,
        "um terço": 0.333, "1/3": 0.333,
        "três quartos": 0.75, "3/4": 0.75
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

# Logic consolidated above

# --- Mifflin-St Jeor ---

# calculate_tdee already defined above

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

@dp.message(Command("reset_perfil"))
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
        
    daily_limit = profile['tdee']
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

    await message.answer(status_msg, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("adj_"))
async def process_adjustment(callback: types.CallbackQuery):
    """Handles quick calorie adjustments from the log feedback buttons."""
    user_id = callback.from_user.id
    action = callback.data.split("_")[1] # 1.1, 0.9, undo
    
    try:
        # Get the latest entry timestamp for this user
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

        # Numeric adjustment
        multiplier = float(action)
        
        # Update kcal and macros in the DB using RPC or direct update
        # For simplicity, we update all entries at that exact timestamp
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
        data = await get_report_data(callback.from_user.id, days)
        profile = await get_user_profile(callback.from_user.id)
        
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
    if not await log_calories(message.from_user.id, message.from_user.full_name, items):
        await message.answer("❌ Erro ao salvar dados no Supabase.")
        return
    
    stats = await get_daily_stats(message.from_user.id)
    profile = await get_user_profile(message.from_user.id)
    daily_limit = profile['tdee'] if profile else 2000
    daily_total = stats["kcal"]
    remaining = daily_limit - daily_total
    
    has_universal = any(i.get("is_universal") for i in items)
    has_fs = any(i.get("is_fs_verified") for i in items)
    
    source_tag = ""
    if has_universal:
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

@dp.message(F.photo)
async def handle_photo(message: types.Message, state: FSMContext):
    if await state.get_state(): await state.clear()
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

@dp.message(F.text)
async def handle_text(message: types.Message, state: FSMContext):
    if await state.get_state(): await state.clear()
    global user_history
    try:
        msg_id = f"{message.chat.id}:{message.message_id}"
        if msg_id in processed_messages: return
        processed_messages.add(msg_id)
        if len(processed_messages) > 1000: processed_messages.clear()

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
            await message.answer("Eae amigão, ta tentando mandar um Jailbreak para CaloriesBot? Não vai conseguir.")
            return

        if len(message.text.split()) <= 3:
            db_match = await search_food_history(user_id, message.text.strip())
            if db_match and db_match[0].get("is_approximate"):
                pass

        items, barcode, error_type, raw_data = await extract_calories_list(
            user_id=user_id, 
            message_text=message.text
        )
        
        await status_msg.delete()
        if error_type == "needs_choice":
            data = json.loads(raw_data)
            candidates = data.get("candidates", [])
            source = data.get("source", "fatsecret")
            
            kb = InlineKeyboardMarkup(inline_keyboard=[])
            for idx, c in enumerate(candidates):
                btn_text = f"{c['alimento'].capitalize()} ({int(c['calorias'])}kcal/{c['peso']})"
                if len(btn_text) > 40: btn_text = btn_text[:37] + "..."
                kb.inline_keyboard.append([InlineKeyboardButton(text=btn_text, callback_data=f"fsc_{idx}")])
            
            kb.inline_keyboard.append([InlineKeyboardButton(text="❌ Nenhuma destas", callback_data="fsc_none")])
            
            await state.update_data(fs_candidates=candidates, original_text=message.text, choice_source=source)
            if source == "catalog":
                await message.answer("✅ **Encontrei no nosso banco verificado.** Qual se aproxima mais do que você consumiu?", reply_markup=kb, parse_mode="Markdown")
            else:
                await message.answer("🔍 **Encontrei opções globais.** Qual se aproxima mais do que você consumiu?", reply_markup=kb, parse_mode="Markdown")
            await state.set_state(FatSecretState.waiting_for_choice)
            return
            
        elif error_type:
            await message.answer(f"❌ Erro na extração a partir do texto. Tente novamente.")
            return
            
        if items is not None and len(items) == 0:
            await message.answer("🤔 Não identifiquei alimentos. Pode ser mais específico?")
            return

        await process_food_entry(message, items, raw_data)
    except Exception as e:
        logger.error(f"Erro no handle_text: {e}")
        await message.answer("❌ Ocorreu um erro inesperado.")

@dp.callback_query(FatSecretState.waiting_for_choice, F.data.startswith("fsc_"))
async def process_fs_choice(callback: types.CallbackQuery, state: FSMContext):
    """Lida com a seleção do usuário na multi-escolha do FatSecret ou Catálogo."""
    choice = callback.data.split("_")[1]
    
    data = await state.get_data()
    candidates = data.get("fs_candidates", [])
    original_text = data.get("original_text", "")
    source = data.get("choice_source", "fatsecret")
    
    if choice == "none":
        if source == "catalog":
            await callback.message.edit_text("🔄 Nenhuma serviu? Buscando opções globais (FatSecret)...", parse_mode="Markdown")
            await state.clear()
            
            fs_res = await search_fatsecret(original_text)
            if fs_res:
                if len(fs_res) > 1:
                    kb = InlineKeyboardMarkup(inline_keyboard=[])
                    for idx, c in enumerate(fs_res):
                        btn_text = f"{c['alimento'].capitalize()} ({int(c['calorias'])}kcal/{c['peso']})"
                        if len(btn_text) > 40: btn_text = btn_text[:37] + "..."
                        kb.inline_keyboard.append([InlineKeyboardButton(text=btn_text, callback_data=f"fsc_{idx}")])
                    kb.inline_keyboard.append([InlineKeyboardButton(text="❌ Nenhuma destas", callback_data="fsc_none")])
                    
                    await state.update_data(fs_candidates=fs_res, original_text=original_text, choice_source="fatsecret")
                    await callback.message.answer("🔍 **Pesquisa Global.** Qual se aproxima mais?", reply_markup=kb, parse_mode="Markdown")
                    await state.set_state(FatSecretState.waiting_for_choice)
                    return
                elif len(fs_res) == 1:
                    chosen = fs_res[0]
                    chosen["alimento"] = chosen.get("original_query", chosen["alimento"])
                    emb = await get_embedding(chosen["alimento"])
                    if emb: chosen["embedding"] = emb
                    await save_to_universal_catalog(chosen)
                    
                    msg = await callback.message.answer(f"✅ Encontrado no Global: **{chosen['alimento']}**.\nCalculando porção...", parse_mode="Markdown")
                    items, barcode, error_type, raw_data = await extract_calories_list(
                        user_id=callback.from_user.id,
                        message_text=original_text,
                        fs_chosen_candidate=chosen
                    )
                    if error_type:
                        await msg.edit_text("❌ Erro na extração. Tente novamentee.")
                    elif items is not None and len(items) == 0:
                        await msg.edit_text("🤔 A opção selecionada não gerou nenhum alimento válido.")
                    else:
                        await msg.delete() # Remove loading
                        await process_food_entry(callback.message, items, raw_data)
                    return
            await callback.message.answer("❌ Não encontrei nada nem no banco global. Tente descrever com outras palavras!")
            return
        else:
            await callback.message.edit_text("Entendido. Opções ignoradas. Tente descrever com outras palavras ou mande uma foto do rótulo!")
            await state.clear()
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
    await save_to_universal_catalog(chosen)
    
    await callback.message.edit_text(f"✅ Você escolheu: **{chosen['alimento']}**.\nCalculando porção...", parse_mode="Markdown")
    
    # Prossegue com text extraction usando o candidato selecionado como base
    items, barcode, error_type, raw_data = await extract_calories_list(
        user_id=callback.from_user.id,
        message_text=original_text,
        fs_chosen_candidate=chosen
    )
    
    await state.clear()
    
    if error_type:
        await callback.message.answer(f"❌ Erro na extração. Tente novamentee.")
        return
        
    if items is not None and len(items) == 0:
        await callback.message.answer("🤔 Opcão selecionada não gerou nenhum alimento válido na porção descrita.")
        return
        
    await process_food_entry(callback.message, items, raw_data)

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
                res = await async_execute(supabase.table("profiles").select("user_id"))
                users = res.data or []
                today_start = get_br_today_start()
                for user in users:
                    uid = user['user_id']
                    log_res = await async_execute(supabase.table("logs").select("id").eq("user_id", str(uid)).gte("created_at", today_start).limit(1))
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
