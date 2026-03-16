import os
import logging
import json
import io
from PIL import Image
import re
import asyncio
import httpx
from json import JSONDecodeError
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

import matplotlib.pyplot as plt
import matplotlib
matplotlib.use('Agg') # Non-interactive backend

from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter, CommandObject
from aiogram.types import Update, InlineKeyboardMarkup, InlineKeyboardButton, PhotoSize, ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from google import genai
from google.genai import types as ai_types
from supabase import create_client, Client
from dotenv import load_dotenv
from groq import AsyncGroq
from duckduckgo_search import DDGS

load_dotenv()

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Config
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL")

# Validation
missing_vars = []
if not TELEGRAM_TOKEN: missing_vars.append("TELEGRAM_BOT_TOKEN")
if not GEMINI_KEY: missing_vars.append("GEMINI_API_KEY")
if not SUPABASE_URL: missing_vars.append("SUPABASE_URL")
if not SUPABASE_KEY: missing_vars.append("SUPABASE_KEY")
if not WEBHOOK_URL: missing_vars.append("RENDER_EXTERNAL_URL")

if missing_vars:
    error_msg = f"❌ Faltando variáveis de ambiente: {', '.join(missing_vars)}"
    logger.error(error_msg)
    raise ValueError(error_msg)

SUPABASE_URL = SUPABASE_URL.strip()
if not SUPABASE_URL.startswith("https://"):
    error_msg = f"❌ SUPABASE_URL inválida: {SUPABASE_URL[:10]}..."
    logger.error(error_msg)
    raise ValueError(error_msg)

# Init Aiogram
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
app = FastAPI()

# Init Gemini (New SDK)
ai_client = genai.Client(api_key=GEMINI_KEY)

def get_main_keyboard():
    """Returns a persistent ReplyKeyboardMarkup for main navigation."""
    kb = [
        [KeyboardButton(text="📊 Status"), KeyboardButton(text="🍱 Logar Comida")],
        [KeyboardButton(text="📈 Relatório"), KeyboardButton(text="⚙️ Perfil")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, persistent=True)

async def get_embedding(text: str):
    """Generates a vector embedding for a piece of text using Gemini."""
    logger.info("📡 SERVIÇO: Usando Gemini Embedding para busca histórica...")
    try:
        # Usando o nome completo do modelo e verificando se v1 resolve o erro de beta
        res = await ai_client.aio.models.embed_content(
            model='text-embedding-004',
            contents=text
        )
        return res.embeddings[0].values
    except Exception as e:
        logger.error(f"Erro ao gerar embedding: {e}")
        return None
AI_MODEL = "gemini-2.0-flash" 
AI_MODEL_FALLBACK = "gemini-1.5-flash" # Corrigido: gemini-3.1 não existe. 1.5-Flash tem cota maior.

# Init Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Init Groq (Fallback)
groq_client = None
if GROQ_API_KEY:
    groq_client = AsyncGroq(api_key=GROQ_API_KEY)

# States for Onboarding
class ProfileStates(StatesGroup):
    weight = State()
    height = State()
    age = State()
    gender = State()
    activity = State()
    goal = State() # NEW: Objetivo (Perder, Manter, Ganhar)
class BarcodeState(StatesGroup):
    waiting_for_portion = State() 

class CorrectionStates(StatesGroup):
    kcal = State()

# Memory and Duplicate protection
processed_messages = set()
user_history: Dict[int, List[str]] = {}
jailbreak_users: Dict[int, bool] = {}
AI_CACHE: Dict[str, Any] = {} # Simples cache em memória

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
        response = await ai_client.aio.models.generate_content(
            model=AI_MODEL,
            contents=[prompt]
        )
        return response.text.strip()
    except Exception as e:
        logger.error(f"Erro ao gerar sarcasmo: {e}")
        return "Ah, que original. Outra tentativa brilhante. 🙄"

# --- Timezone Helpers ---

def get_br_now():
    """Returns the current datetime in Brazil (UTC-3)."""
    return datetime.utcnow() - timedelta(hours=3)

def get_br_today_start():
    """Returns the start of today in Brazil (00:00:00) in ISO format with offset."""
    return get_br_now().strftime("%Y-%m-%dT00:00:00-03:00")

# --- DB Logic ---

def log_calories(user_id: str, user_name: str, items: list):
    """Saves a list of food items to the database including macros."""
    try:
        prepared_data = []
        for item in items:
            prepared_data.append({
                "food": item.get("alimento"),
                "weight": item.get("peso"),
                "kcal": item.get("calorias"),
                "protein": item.get("proteina", 0),
                "carbs": item.get("carboidratos", 0),
                "fat": item.get("gorduras", 0),
                "meal_type": item.get("refeicao", "Outro"),
                "is_precise": item.get("is_precise", False),
                "confirmations": 0, # Novo campo para crowdsourcing
                "user_id": str(user_id),
                "user_name": user_name
            })
        if prepared_data:
            supabase.table("logs").insert(prepared_data).execute()
        return True
    except Exception as e:
        logger.error(f"Erro ao salvar no Supabase: {e}")
        return False

def get_user_profile(user_id: str):
    """Fetches the user's profile and TDEE."""
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

def get_daily_total(user_id: str):
    """Legacy helper: returns only kcal."""
    return get_daily_stats(user_id)["kcal"]

def get_report_data(user_id: str, days: int):
    """Aggregates data for periodic reports."""
    try:
        now_br = get_br_now()
        start_date = (now_br - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00-03:00")
        
        response = supabase.table("logs") \
            .select("created_at, kcal") \
            .eq("user_id", str(user_id)) \
            .gte("created_at", start_date) \
            .execute()
        return response.data
    except Exception as e:
        logger.error(f"Erro ao buscar dados do relatório: {e}")
        return []

def delete_last_log(user_id: str):
    """Deletes the most recent meal log (all items from last entry message)."""
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
        # Delete logs first (foreign key/consistency)
        supabase.table("logs").delete().eq("user_id", str(user_id)).execute()
        # Delete profile
        supabase.table("profiles").delete().eq("user_id", str(user_id)).execute()
        return True
    except Exception as e:
        logger.error(f"Erro ao deletar perfil completo: {e}")
        return False

def delete_log_by_id(user_id: str, log_id: int):
    """Deletes a specific log entry by its ID."""
    try:
        supabase.table("logs").delete().eq("user_id", str(user_id)).eq("id", log_id).execute()
        return True
    except Exception as e:
        logger.error(f"Erro ao deletar log por ID: {e}")
        return False

def search_food_history(user_id: str, food_query: str):
    """
    Searches for a historical log entry that matches the message text exactly.
    Returns the items list if found, otherwise None.
    """
    try:
        # Busca a última entrada desse usuário com esse exato texto
        # Precisamos buscar entradas que tenham o mesmo texto de entrada na memória da IA ou algo similar
        # Por enquanto, vamos focar em entradas idênticas para máxima precisão.
        
        # Como o food_query pode ser longo, vamos tentar buscar no 'alimento' se for uma palavra só
        # ou ver se temos um log recente (últimos 30 dias) com esse padrão.
        
        # Estratégia: Buscar no Supabase logs onde o nome do alimento bate.
        # Priorizamos a entrada mais recente para refletir a última validação feita.
        
        # Tenta busca exata primeiro
        res = supabase.table("logs") \
            .select("food, weight, kcal, protein, carbs, fat, meal_type, is_precise") \
            .eq("user_id", str(user_id)) \
            .ilike("food", f"{food_query}") \
            .order("created_at", desc=True) \
            .limit(1) \
            .execute()
        
        # Se não achar exato, tenta aproximação (Fuzzy simples)
        if not res.data:
            res = supabase.table("logs") \
                .select("food, weight, kcal, protein, carbs, fat, meal_type, is_precise") \
                .eq("user_id", str(user_id)) \
                .ilike("food", f"%{food_query}%") \
                .order("created_at", desc=True) \
                .limit(1) \
                .execute()
            if res.data:
                res.data[0]["is_approximate"] = True # Marca como aproximado

        # PASSO 2: Se não achou NADA pessoal, busca no catálogo GLOBAL por itens VERIFICADOS
        is_universal = False
        if not res.data:
            # Itens universais: Marcados como precisos OU com muitas confirmações da comunidade
            res = supabase.table("logs") \
                .select("food, weight, kcal, protein, carbs, fat, meal_type, is_precise, confirmations") \
                .ilike("food", f"{food_query}") \
                .or_("is_precise.eq.true,confirmations.gte.5") \
                .order("created_at", desc=True) \
                .limit(1) \
                .execute()
            if res.data:
                is_universal = True
                logger.info(f"Item encontrado no Catálogo Universal: {food_query}")

        if res.data:
            item = res.data[0]
            return [{
                "alimento": item["food"],
                "peso": item["weight"],
                "calorias": item["kcal"],
                "proteina": item["protein"],
                "carboidratos": item["carbs"],
                "gorduras": item["fat"],
                "refeicao": item["meal_type"],
                "is_precise": item.get("is_precise", False),
                "is_approximate": item.get("is_approximate", False),
                "is_universal": is_universal
            }]
        return None
    except Exception as e:
        logger.error(f"Erro ao buscar no histórico: {e}")
        return None


def extract_amount(text: str) -> Optional[float]:
    """Extracts weight (g) or volume (ml) from text. Converts kg/l to g/ml."""
    if not text:
        return None
    
    # Pre-clean: "6.1" (from "6,1")
    t = str(text).lower().replace(",", ".")
    
    # Medidas Caseiras (Conversões aproximadas)
    household = [
        (r"(colher\s*de\s*sopa|c\.\s*sopa|colher\s*s)", 15),
        (r"(colher\s*de\s*sobremesa)", 10),
        (r"(colher\s*de\s*ch[aá]|colher\s*c)", 5),
        (r"(colher\s*de\s*caf[eé])", 2),
        (r"(x[ií]cara)", 200),
        (r"(copo|c\.)", 200),
        (r"(fatia|f\.)", 30)
    ]

    # Frações em texto
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
            qty = float(match.group(1)) if match.group(1) else 1.0
            return qty * factor

    # Check for kg/l
    kg_match = re.search(r"(\d+[\.,]?\d*)\s*(kg|kilo|l$|litro|l\s)", t)
    if kg_match:
        try:
            return float(kg_match.group(1)) * 1000
        except: pass

    # Check for g/ml
    g_match = re.search(r"(\d+[\.,]?\d*)\s*(g|gr|ml)", t)
    if g_match:
        try:
            return float(g_match.group(1))
        except: pass
    
    # Just a number (assumes g/ml)
    num_match = re.search(r"^(\d+[\.,]?\d*)$", t)
    if num_match:
        try:
            return float(num_match.group(1))
        except: pass
        
    return None

def parse_numeric(text: str) -> Optional[float]:
    """Robust parser for numeric inputs like '75,5', '175cm', '80kg'."""
    if not text: return None
    t = text.lower().replace(',', '.')
    match = re.search(r"(\d+[\.,]?\d*)", t)
    if match:
        try:
            return float(match.group(1))
        except:
            return None
    return None



# --- AI Logic ---

async def call_gemini_with_retry(contents, config=None, max_retries=3):
    """Calls Gemini with exponential backoff for 429 errors."""
    for i in range(max_retries):
        try:
            # Use .aio for non-blocking calls
            response = await ai_client.aio.models.generate_content(
                model=AI_MODEL,
                contents=contents,
                config=config
            )
            # Log if tool calls were used (like search)
            if response.candidates:
                for candidate in response.candidates:
                    if candidate.content and candidate.content.parts:
                        for part in candidate.content.parts:
                            if hasattr(part, 'call_queries') and part.call_queries:
                                logger.info(f"🔍 Gemini usou Ferramentas (Search): {part.call_queries}")
            
            return response
        except Exception as e:
            if "429" in str(e) and i < max_retries - 1:
                wait_time = (2 ** i) + 1
                logger.warning(f"Gemini 429 Detectado. Tentando novamente em {wait_time}s... (Tentativa {i+1}/{max_retries})")
                await asyncio.sleep(wait_time)
                continue
            raise e

async def search_duckduckgo(query: str) -> str:
    """Performs a text search via DuckDuckGo as a zero-cost fallback."""
    try:
        logger.info(f"🌐 Iniciando Busca DuckDuckGo: {query}")
        with DDGS() as ddgs:
            results = ddgs.text(query, max_results=3, region="br-pt")
            context = "\n".join([f"- {r['title']}: {r['body']}" for r in results])
            return context if context else "Nenhuma informação adicional encontrada via search."
    except Exception as e:
        logger.warning(f"Erro no DuckDuckGo Search: {e}")
        return "Falha ao realizar busca externa."

async def call_groq_fallback(message_text: str, image_bytes: Optional[bytes] = None, prompt: str = "", search_context: str = ""):
    """Calls Groq (Llama 3.3) as a fallback, optionally with search context."""
    if not groq_client:
        return None
    
    # Se houver contexto de busca (DDGS), injeta no prompt
    if search_context:
        full_prompt = f"DADOS VERIFICADOS (WEB):\n{search_context}\n\n{prompt}"
    else:
        full_prompt = prompt

    try:
        import base64
        model = "llama-3.3-70b-versatile" 
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": full_prompt},
                ]
            }
        ]
        
        if image_bytes:
            base64_image = base64.b64encode(image_bytes).decode('utf-8')
            messages[0]["content"].append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}
            })
            
        completion = await groq_client.chat.completions.create(
            model=model,
            messages=messages,
            response_format={"type": "json_object"}
        )
        return completion.choices[0].message.content
    except Exception as e:
        logger.error(f"Erro no fallback Groq: {e}")
        return None

async def get_barcode_data(barcode: str):
    """Fetches nutritional data from OpenFoodFacts (Brazil)."""
    url = f"https://br.openfoodfacts.org/api/v0/product/{barcode}.json"
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

async def extract_calories_list(user_id: int, message_text: str = "", image_bytes: Optional[bytes] = None):
    """
    Calls Gemini to extract food items from text or image. 
    Returns (items_list, barcode, is_packaged, error_type, raw_response)
    """
    # Get history
    history = user_history.get(user_id, [])
    history_ctx = "\n".join(history[-5:]) if history else "Sem histórico."
    
    # Horário local Brasil (UTC-3) para inferência de refeição
    now_br = get_br_now()
    hora_local = now_br.strftime("%H:%M")

    # Check Cache for text-only inputs
    cache_key = f"{message_text}_{image_bytes is not None}"
    # --- CACHE & SEMANTIC SEARCH ---
    if not image_bytes:
        # 1. Cache Semântico (pgvector)
        # Tenta encontrar algo similar no histórico pessoal primeiro
        embedding = await get_embedding(message_text)
        if embedding:
            try:
                # Simula chamada RPC no Supabase (usuário deve ter a função match_logs no DB)
                match_res = supabase.rpc("match_logs", {
                    "query_embedding": embedding,
                    "match_threshold": 0.92, # Alta similaridade
                    "match_count": 1,
                    "p_user_id": str(user_id)
                }).execute()
                
                if match_res.data:
                    item = match_res.data[0]
                    logger.info(f"🎯 Cache Semântico: {message_text} -> {item['food']}")
                    db_match = [{
                        "alimento": item["food"],
                        "peso": item["weight"],
                        "calorias": item["kcal"],
                        "proteina": item["protein"],
                        "carboidratos": item["carbs"],
                        "gorduras": item["fat"],
                        "refeicao": item["meal_type"],
                        "is_precise": item["is_precise"] # Mantém a precisão original
                    }]
                    return db_match, None, None, None, "SEMANTIC_CACHE"
            except Exception as e:
                logger.warning(f"Erro na busca semântica: {e}")
                
        # 2. Check In-memory cache
        if cache_key in AI_CACHE:
            logger.info(f"Usando Cache em memória para: {message_text}")
            return AI_CACHE[cache_key], None, None, "MEMORY_CACHE"
        
        # 2. Check DB History (if it's a simple food name)
        if len(message_text.split()) <= 3: # Apenas para buscas simples
            db_match = search_food_history(user_id, message_text.strip())
            # Se for aproximado, não retornamos aqui, deixamos cair na IA ou na pergunta
            if db_match and db_match[0].get("is_approximate"):
                logger.info(f"Achei item próximo no DB: {db_match[0]['alimento']}. Deixando fluir.")
            # SÓ pula a IA se o item do banco for PRECISO (já validado) e EXATO
            elif db_match and db_match[0].get("is_precise"):
                logger.info(f"Usando Cache DB VERIFICADO para: {message_text}")
                AI_CACHE[cache_key] = db_match
                return db_match, None, None, "DB_VERIFIED_CACHE"
            elif db_match:
                logger.info(f"Item no DB é impreciso. Chamando IA para verificação: {message_text}")

    prompt = f"""
    Você é um nutricionista especialista de ELITE. 
    OBJETIVO: Identificar alimentos da "ENTRADA ATUAL", extraindo calorias, macronutrientes e o tipo de refeição.
    
    ESTRATÉGIA DE PESQUISA (CRÍTICO):
    1. **TABELA OFICIAL (PRIORIDADE 1):** Procure prioritariamente por sites de FABRICANTES (.com.br, nestle.com, etc) ou catálogos oficiais de grandes supermercados.
    2. **PESQUISA POR TABELA:** Use termos como "tabela nutricional oficial [produto]" para evitar blogs de terceiros imprecisos.
    3. **QUANDO NÃO ACHAR TABELA (INCIDÊNCIA):** Se não houver fonte oficial, pesquise e use o valor com mais recorrência/incidência estatística na web (consenso).
    4. **MARCAS E PRODUTOS:** Para marcas (ex: Paderrí, Nestlé), **USE O GOOGLE SEARCH**.
    
    PESQUISA DE PORÇÕES DINÂMICA:
    Para itens in natura/granel sem peso (ex: "uma maçã"):
    1. Pesquise o "peso médio usual de uma unidade no Brasil".
    2. Maçã (~130-150g), Banana (~80-100g), Pão Francês (~50g). Na dúvida entre fontes, use a média da pesquisa.
    
    REGRAS DE EXTRAÇÃO:
    5. **PÓ vs PREPARADO:** Para alimentos que exigem preparo, assuma o peso do produto PRONTO.
    6. **MEDIDAS CASEIRAS:** Converta automaticamente (1 colher sopa = 15g, 1 xícara = 200ml, 1 fatia = 30g).
    7. Classifique a REFEIÇÃO ({hora_local}: 05-10:30 Café, 11-14:30 Almoço, 18-23 Jantar, outros: Lanche).
    8. **FRAÇÕES E UNIDADES:** Se o usuário disser "meio" ou "1/2", ou usar unidades do catálogo acima, o campo `peso` deve refletir o cálculo (ex: "meio pão francês" -> 25g). Se o alimento não estiver no catálogo e não houver peso, use 100g como fallback genérico.
    
    OUTPUT JSON:
    {{ 
      "items": [ 
        {{
          "alimento": "Nome Marca/Produto", 
          "peso": "peso_em_g_ou_ml", 
          "calorias": int_base_100, 
          "proteina": int_base_100, 
          "carboidratos": int_base_100, 
          "gorduras": int_base_100, 
          "refeicao": "str", 
          "is_precise": bool,
          "verified_via_search": bool,
          "table_detected": bool
        }} 
      ], 
      "barcode": "string_or_null", 
      "is_packaged": bool 
    }}
    
    is_precise: OBRIGATORIAMENTE false se estiver estimando.
    verified_via_search: true se usou a ferramenta de pesquisa.
    table_detected: true se encontrou e leu valores de uma tabela nutricional oficial (na imagem ou via search).
    
    CONTEXTO: {history_ctx}
    ENTRADA ATUAL: "{message_text}"
    """
    
    raw_text = ""
    # --- IA LOGIC: SPECIALIZED ROUTING ---
    # IMAGE PATH: Gemini 1.5 Flash (Superior Vision) -> Gemini 3.1 Lite (Fallback)
    # TEXT PATH: Groq 70B (Superior Speed) -> Gemini 3.1 Lite (Fallback)
    
    use_vision = image_bytes is not None
    ai_success = False

    if use_vision:
        logger.info("📡 ROTA IMAGEM: Usando Gemini Vision para análise...")
        config = ai_types.GenerateContentConfig(
            tools=[ai_types.Tool(google_search=ai_types.GoogleSearch())],
            temperature=0.1 # Mais determinístico
        )
        contents = [prompt]
        contents.append(ai_types.Part.from_bytes(data=image_bytes, mime_type='image/jpeg'))
        
        try:
            response = await call_gemini_with_retry(contents, config=config)
            raw_text = response.text
            ai_success = True
        except Exception as e:
            logger.warning(f"Gemini Vision falhou: {e}. Indo para Fallback...")
    else:
        # Padrões que sugerem necessidade de pesquisa (marcas, produtos específicos, descrições longas)
        search_keywords = ["marca", "paderri", "nestle", "bauducco", "coca", "sadia", "perdigao", "danone", "heineken", "ambev", "swift", "sear"]
        is_branded = any(k in message_text.lower() for k in search_keywords) or len(message_text.split()) > 5
        
        # TEXTO: Sempre preferimos Groq para economizar Gemini "quente"
        if groq_client:
            ddgs_context = ""
            if is_branded:
                logger.info("📡 ROTA TEXTO (PESQUISA): Usando DuckDuckGo + Groq (Llama 3.3)...")
                ddgs_context = await search_duckduckgo(f"tabela nutricional {message_text}")
            else:
                logger.info("📡 ROTA TEXTO (DIRETA): Usando Groq (Llama 3.3)...")
                
            try:
                raw_text = await call_groq_fallback(message_text, None, prompt, search_context=ddgs_context)
                if raw_text:
                    ai_success = True
                    if ddgs_context: logger.info("Resiliência DuckDuckGo + Groq bem sucedida!")
            except Exception as e:
                logger.warning(f"Groq falhou: {e}")

    # UNIVERSAL FALLBACK: Se o caminho primário falhou e Groq tb não resolveu
    if not ai_success:
        logger.warning("📡 ROTA FALLBACK: Usando Gemini Flash (Cota Alta/Fallback)...")
        config = ai_types.GenerateContentConfig() # Lite não precisa de search para fallback rápido
        contents = [prompt]
        if use_vision:
            contents.append(ai_types.Part.from_bytes(data=image_bytes, mime_type='image/jpeg'))

        try:
            # Usamos explicitamente o modelo Lite para fallback
            response = await ai_client.aio.models.generate_content(
                model=AI_MODEL_FALLBACK,
                contents=contents,
                config=config
            )
            raw_text = response.text
            logger.info("Fallback Gemini 3.1 Flash Lite bem sucedido!")
        except Exception as e2:
            logger.error(f"Apocalipse de IA: Ambas falharam: {e2}")
            return None, None, False, "ai_error", str(e2)

    try:
        # Robust JSON extraction
        cleaned_text = raw_text.strip()
        if "```json" in cleaned_text:
            cleaned_text = cleaned_text.split("```json")[1].split("```")[0]
        elif "```" in cleaned_text:
            cleaned_text = cleaned_text.split("```")[1].split("```")[0]
            
        result_json = json.loads(cleaned_text.strip())
        items = result_json.get("items", [])
        barcode = result_json.get("barcode")
        is_packaged = result_json.get("is_packaged", False)
        
        # Sanitize and force types
        sanitized_items = []
        for item in items:
            if isinstance(item, dict) and item.get("alimento"):
                # Force ints for macros
                item["calorias"] = int(float(item.get("calorias", 0)))
                item["proteina"] = int(float(item.get("proteina", 0)))
                item["carboidratos"] = int(float(item.get("carboidratos", 0)))
                item["gorduras"] = int(float(item.get("gorduras", 0)))
                # Preserve flags
                item["is_precise"] = bool(item.get("is_precise", False))
                item["is_universal"] = bool(item.get("verified_via_search", False))
                item["table_detected"] = bool(item.get("table_detected", False))
                sanitized_items.append(item)

        # Guarda de plausibilidade simples para evitar outliers absurdos
        final_items = []
        for item in sanitized_items:
            grams = extract_amount(item.get("peso", ""))
            kcal = int(float(item.get("calorias", 0)))

            if grams and grams > 0:
                kcal_per_100g = (kcal / grams) * 100
                # Faixa ampla para cobrir diferentes alimentos, mas evita aberrações
                if kcal_per_100g < 15 or kcal_per_100g > 900:
                    logger.warning(
                        "Valor fora da faixa plausível (%s kcal/100g) para item %s. Mantendo extração original.",
                        round(kcal_per_100g, 2),
                        item.get("alimento", "desconhecido")
                    )

            final_items.append(item)

        # Update memory with what was actually extracted
        if final_items:
            if user_id not in user_history: user_history[user_id] = []
            extracted_summary = ", ".join([f"{i['alimento']} ({i.get('peso', '')})" for i in final_items])
            user_history[user_id].append(f"LOGADO ANTERIORMENTE: {extracted_summary}")
            if len(user_history[user_id]) > 10: user_history[user_id] = user_history[user_id][-10:]

        # Store in Cache (Persistent & Memory) if successful and not an image
        if not image_bytes and final_items:
            AI_CACHE[cache_key] = final_items

        return final_items, barcode, is_packaged, None, raw_text
    except JSONDecodeError as e:
        logger.error(f"Erro ao decodificar JSON da IA: {e}")
        return None, None, False, "json_error", raw_text
    except Exception as e:
        logger.error(f"Gemini error: {e}")
        return None, None, False, "ai_error", str(e)

# --- Mifflin-St Jeor ---

def calculate_tdee(w, h, a, g, act, goal="manter"):
    # BMR (Mifflin-St Jeor)
    if g == 'M':
        bmr = (10 * w) + (6.25 * h) - (5 * a) + 5
    else:
        bmr = (10 * w) + (6.25 * h) - (5 * a) - 161
    
    multipliers = {
        "sedentario": 1.2,
        "leve": 1.375,
        "moderado": 1.55,
        "ativo": 1.725,
        "atleta": 1.9
    }
    tdee = bmr * multipliers.get(act, 1.2)
    
    # Adjust based on Goal
    if goal == "perder":
        return round(tdee - 500)
    elif goal == "ganhar":
        return round(tdee + 300)
    return round(tdee)

# --- Bot Handlers ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    logger.info(f"User {message.from_user.id} enviou /start")
    profile = get_user_profile(message.from_user.id)
    
    if not profile:
        await message.answer(
            f"👋 Olá, **{message.from_user.first_name}**! Prazer em te conhecer. 😊\n\n"
            "Eu sou o seu assistente de calorias inteligente! Vou te ajudar a trackear sua dieta de forma simples, usando apenas fotos ou mensagens de texto.\n\n"
            "🚀 **Vamos começar?** Para eu calcular suas metas ideais, preciso criar o seu perfil. É super rápido!",
            parse_mode="Markdown"
        )
        await asyncio.sleep(1) # Pequena pausa para leitura
        await message.answer("⚖️ Primeiro, qual seu **peso** atual em kg?\n*(ex: 75.5)*", parse_mode="Markdown")
        await state.set_state(ProfileStates.weight)
    else:
        await message.answer(
            f"👋 Bem-vindo de volta, **{message.from_user.first_name}**!\n\n"
            f"🎯 Sua meta: **{profile['tdee']} kcal**\n"
            f"💪 Foco: **{profile['goal'].capitalize()}**\n\n"
            "O que vamos registrar agora? Você pode me mandar uma **foto** 📸 ou descrever sua refeição por **texto** 📝.",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
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
        await message.answer("🔄 **A última entrada foi removida com sucesso!**", parse_mode="Markdown")
    else:
        await message.answer("❌ **Não encontrei entradas recentes para remover.**", parse_mode="Markdown")

@dp.message(Command("reset_dia"))
async def cmd_reset_day(message: types.Message):
    if delete_today_logs(message.from_user.id):
        # Limpa memória local da IA também para o dia
        if message.from_user.id in user_history:
            user_history[message.from_user.id] = []
        await message.answer("📅 Seus logs de **hoje** foram apagados!", parse_mode="Markdown")
    else:
        await message.answer("❌ **Erro ao apagar logs de hoje.**", parse_mode="Markdown")

@dp.message(Command("reset_perfil"))
async def cmd_reset_profile(message: types.Message, state: FSMContext):
    if delete_entire_profile(message.from_user.id):
        # Limpa tudo
        if message.from_user.id in user_history:
            del user_history[message.from_user.id]
        if message.from_user.id in jailbreak_users:
            del jailbreak_users[message.from_user.id]
            
        await message.answer("💥 **Perfil e histórico deletados!** Vamos começar do zero.", parse_mode="Markdown")
        # Trigger onboarding again
        await cmd_start(message, state)
    else:
        await message.answer("❌ **Erro ao deletar seu perfil.**", parse_mode="Markdown")

@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    user_id = message.from_user.id
    profile = get_user_profile(user_id)
    
    if not profile:
        await message.answer("⚠️ **Ops!** Você ainda não tem um perfil configurado.\nClique em **⚙️ Perfil** ou use /perfil para começar!", parse_mode="Markdown", reply_markup=get_main_keyboard())
        return
        
    stats = get_daily_stats(user_id)
    daily_total = stats["kcal"]
    daily_limit = profile['tdee']
    remaining = daily_limit - daily_total
    
    today_br_start = get_br_today_start()
    res = supabase.table("logs").select("*").eq("user_id", str(user_id)).gte("created_at", today_br_start).order("created_at", desc=True).execute()
    
    items_list_text = ""
    for item in res.data:
        items_list_text += f"• {item['food']} ({item['kcal']} kcal)\n"
    
    if not items_list_text:
        items_list_text = "_Nenhum alimento logado hoje._\n"

    progress_val = min(10, round((daily_total/daily_limit)*10)) if daily_limit > 0 else 0
    progress_bar = "🔵" * progress_val + "⚪" * (10 - progress_val)

    status_msg = (
        f"📊 **STATUS ATUAL**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 **Meta:** {daily_limit} kcal\n"
        f"🔥 **Consumo:** {daily_total} kcal\n"
        f"⚖️ **Restante:** {max(0, remaining)} kcal\n\n"
        f"📝 **Itens de hoje:**\n{items_list_text}\n"
        f"💪 **P:** {stats['protein']}g | 🍞 **C:** {stats['carbs']}g | 🥑 **G:** {stats['fat']}g\n\n"
        f"|{progress_bar}|"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑️ Deletar Item Específico", callback_data="list_undo")]
    ])

    await message.answer(status_msg, parse_mode="Markdown", reply_markup=kb)

# --- Keyboard Button Handlers ---
@dp.message(F.text == "📊 Status")
async def btn_status(message: types.Message):
    await cmd_status(message)

@dp.message(F.text == "🍱 Logar Comida")
async def btn_log(message: types.Message):
    await message.answer("Pode mandar! Descreva o que você comeu ou envie uma foto. 📸🥗", parse_mode="Markdown")

@dp.message(F.text == "📈 Relatório")
async def btn_report(message: types.Message):
    await cmd_report(message)

@dp.message(F.text == "⚙️ Perfil")
async def btn_profile(message: types.Message, state: FSMContext):
    await start_profile(message, state)

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

@dp.callback_query(F.data == "list_undo")
async def process_list_undo(callback: types.CallbackQuery):
    """Shows a list of today's items to delete."""
    user_id = callback.from_user.id
    today_br_start = get_br_today_start()
    
    res = supabase.table("logs") \
        .select("id, food, kcal") \
        .eq("user_id", str(user_id)) \
        .gte("created_at", today_br_start) \
        .order("created_at", desc=True) \
        .execute()
    
    if not res.data:
        await callback.answer("❌ Nenhum item para deletar hoje.")
        return

    buttons = []
    for item in res.data:
        # Label encurtada para caber no botão
        label = f"🗑️ {item['food'][:15]}... ({item['kcal']} kcal)"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"del_{item['id']}")])
    
    buttons.append([InlineKeyboardButton(text="⬅️ Voltar", callback_data="status_back")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    await callback.message.edit_text("🎯 Selecione o item que deseja **remover**:", reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("del_"))
async def process_delete_specific(callback: types.CallbackQuery):
    """Deletes a specific log item."""
    user_id = callback.from_user.id
    log_id = int(callback.data.split("_")[1])
    
    if delete_log_by_id(user_id, log_id):
        await callback.answer("✅ Item removido!")
        # Atualiza a lista (volta para o status ou mostra lista de novo)
        await process_list_undo(callback)
    else:
        await callback.answer("❌ Erro ao deletar.")

@dp.callback_query(F.data == "confirm_precise")
async def process_confirm_precise(callback: types.CallbackQuery):
    """Increments the confirmation counter for the latest entry."""
    user_id = callback.from_user.id
    try:
        # Get the latest entry
        res = supabase.table("logs") \
            .select("id, confirmations") \
            .eq("user_id", str(user_id)) \
            .order("created_at", desc=True) \
            .limit(1) \
            .execute()
        
        if not res.data:
            await callback.answer("❌ Nenhum log encontrado.")
            return

        last_id = res.data[0]['id']
        current_conf = res.data[0].get('confirmations', 0)
        
        # Increment confirmations
        supabase.table("logs").update({"confirmations": current_conf + 1}) \
            .eq("id", last_id) \
            .execute()
            
        await callback.answer("🗳️ Voto registrado! Obrigado por ajudar a comunidade.")
        
        # Remove o botão da mensagem original
        new_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ 10%", callback_data="adj_1.1"),
             InlineKeyboardButton(text="➖ 10%", callback_data="adj_0.9")],
            [InlineKeyboardButton(text="🔄 Desfazer", callback_data="adj_undo")]
        ])
        await callback.message.edit_reply_markup(reply_markup=new_kb)
        
    except Exception as e:
        logger.error(f"Erro ao votar precisão: {e}")
        await callback.answer("❌ Erro ao processar voto.")

@dp.callback_query(F.data == "manual_correct")
async def process_manual_correction_start(callback: types.CallbackQuery, state: FSMContext):
    """Starts the manual kcal correction flow."""
    await callback.answer()
    await callback.message.answer(
        "📝 **Correção Manual**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Qual o valor real de **calorias (kcal) para cada 100g** desse alimento?\n\n"
        "*(Basta digitar o número, ex: 350. Eu calcularei o total baseado no peso do log original)*",
        parse_mode="Markdown"
    )
    await state.set_state(CorrectionStates.kcal)

@dp.message(CorrectionStates.kcal)
async def process_manual_kcal(message: types.Message, state: FSMContext):
    """Processes the manual kcal input and updates the last log."""
    kcal_100g = parse_numeric(message.text)
    if kcal_100g is None:
        await message.answer("❌ **Valor inválido.** Por favor, digite apenas o número das calorias por 100g.")
        return

    user_id = message.from_user.id
    try:
        # Busca o último log para pegar o peso
        res = supabase.table("logs") \
            .select("*") \
            .eq("user_id", str(user_id)) \
            .order("created_at", desc=True) \
            .limit(1) \
            .execute()
        
        if not res.data:
            await message.answer("❌ Não encontrei registros recentes para corrigir.")
            await state.clear()
            return
            
        last_item = res.data[0]
        weight_str = last_item.get("weight", "100g")
        grams = extract_amount(weight_str) or 100
        
        # Recalcula kcal total baseado na base 100 informada
        new_kcal = round((kcal_100g / 100) * grams)
        
        # Atualiza no DB
        supabase.table("logs").update({
            "kcal": new_kcal,
            "is_precise": True # Agora é preciso pois o usuário informou
        }).eq("id", last_item["id"]).execute()
        
        await state.clear()
        
        # Feedback de sucesso e resumo atualizado
        stats = get_daily_stats(user_id)
        profile = get_user_profile(user_id)
        limit = profile['tdee'] if profile else 2000
        progress = min(10, round((stats['kcal']/limit)*10)) if limit > 0 else 0
        p_bar = "🔵" * progress + "⚪" * (10 - progress)

        await message.answer(
            f"✅ **Valor corrigido com sucesso!**\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🍎 **{last_item['food']}**\n"
            f"⚖️ {weight_str} | 🔥 **{new_kcal} kcal** (atualizado)\n\n"
            f"🔥 **Consumo Total Hoje:** {stats['kcal']} / {limit} kcal\n"
            f"|{p_bar}|",
            parse_mode="Markdown"
        )
        
    except Exception as e:
        logger.error(f"Erro na correção manual: {e}")
        await message.answer("❌ Erro ao processar a correção.")
        await state.clear()

@dp.callback_query(F.data.startswith("err_"))
async def process_error_actions(callback: types.CallbackQuery):
    action = callback.data.split("_")[1]
    if action == "text":
        await callback.message.answer("Pode digitar! Estou pronto para processar seu texto. 📝")
    elif action == "photo":
        await callback.message.answer("Mande a foto! Vou tentar analisar aqui. 📸")
    elif action == "examples":
        examples = (
            "📖 **Dicas para o registro:**\n\n"
            "✅ **Certo:** 'Arroz 100g, feijão 1 concha, 1 bife de frango'\n"
            "✅ **Certo:** 'Mande uma foto clara do seu prato de cima'\n"
            "❌ **Errado:** 'Comi muito no almoço hoje'\n\n"
            "Quanto mais específico você for (gramas, unidades, colheres), melhor eu calculo!"
        )
        await callback.message.answer(examples, parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "status_back")
async def process_status_back(callback: types.CallbackQuery):
    """Returns to the main status view."""
    # Simula o comando /status editando a mensagem atual
    # Precisamos de um profile para o cálculo
    user_id = callback.from_user.id
    profile = get_user_profile(user_id)
    if not profile: return
    
    daily_limit = profile['tdee']
    daily_total = get_daily_total(user_id)
    remaining = daily_limit - daily_total
    today_br_start = get_br_today_start()
    res = supabase.table("logs").select("*").eq("user_id", str(user_id)).gte("created_at", today_br_start).order("created_at", desc=True).execute()
    
    items_list_text = "".join([f"• {item['food']} ({item['kcal']} kcal)\n" for item in res.data]) or "Nenhum lanche logado.\n"
    total_prot = sum(item.get('protein', 0) for item in res.data)
    total_carb = sum(item.get('carbs', 0) for item in res.data)
    total_fat = sum(item.get('fat', 0) for item in res.data)
    progress_val = min(10, round((daily_total/daily_limit)*10)) if daily_limit > 0 else 0
    progress_bar = "🔵" * progress_val + "⚪" * (10 - progress_val)

    status_msg = (
        f"📊 **STATUS ATUAL**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 **Meta:** {daily_limit} kcal\n"
        f"🔥 **Consumo:** {daily_total} kcal\n"
        f"⚖️ **Restante:** {max(0, daily_limit - daily_total)} kcal\n\n"
        f"📝 **Itens de hoje:**\n{items_list_text}\n"
        f"💪 **P:** {total_prot}g | 🍞 **C:** {total_carb}g | 🥑 **G:** {total_fat}g\n\n"
        f"|{progress_bar}|"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑️ Deletar Item Específico", callback_data="list_undo")]
    ])
    await callback.message.edit_text(status_msg, parse_mode="Markdown", reply_markup=kb)

@dp.message(Command("cancelar"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("🔄 **Operação cancelada.** Como posso ajudar agora?", parse_mode="Markdown")

# --- Onboarding FSM ---

@dp.message(Command("perfil"))
async def start_profile(message: types.Message, state: FSMContext):
    await message.answer(
        "⚙️ **Configuração de Perfil**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Vamos descobrir suas necessidades diárias! Para isso, preciso de alguns dados básicos.\n\n"
        "💪 **Qual seu peso atual em kg?**\n*(ex: 75,5)*", 
        parse_mode="Markdown"
    )
    await state.set_state(ProfileStates.weight)

@dp.message(ProfileStates.weight)
async def process_weight(message: types.Message, state: FSMContext):
    val = parse_numeric(message.text)
    if val is not None:
        await state.update_data(weight=val)
        await message.answer("2️⃣ Qual sua **altura** em cm? (ex: `175`)", parse_mode="Markdown")
        await state.set_state(ProfileStates.height)
    else:
        await message.answer("⚠️ **Ops!** Não consegui entender o peso. Tente enviar apenas o número (ex: `75,5` ou `75.5`).", parse_mode="Markdown")

@dp.message(ProfileStates.height)
async def process_height(message: types.Message, state: FSMContext):
    val = parse_numeric(message.text)
    if val is not None:
        await state.update_data(height=val)
        await message.answer("3️⃣ Qual sua **idade**?", parse_mode="Markdown")
        await state.set_state(ProfileStates.age)
    else:
        await message.answer("⚠️ **Ops!** Não entendi a altura. Tente mandar apenas os cm (ex: `175`).", parse_mode="Markdown")

@dp.message(ProfileStates.age)
async def process_age(message: types.Message, state: FSMContext):
    val = parse_numeric(message.text)
    if val is not None:
        await state.update_data(age=int(val))
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="♂️ Masculino", callback_data="g_M"), 
             InlineKeyboardButton(text="♀️ Feminino", callback_data="g_F")]
        ])
        await message.answer("4️⃣ Qual seu **sexo**?", reply_markup=kb, parse_mode="Markdown")
        await state.set_state(ProfileStates.gender)
    else:
        await message.answer("⚠️ **Ops!** Não entendi a idade. Tente mandar apenas o número (ex: `25`).", parse_mode="Markdown")

@dp.callback_query(ProfileStates.gender, F.data.startswith("g_"))
async def process_gender(callback: types.CallbackQuery, state: FSMContext):
    gender = callback.data.split("_")[1]
    await state.update_data(gender=gender)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Sedentário", callback_data="act_sedentario")],
        [InlineKeyboardButton(text="Leve (1-3 dias/sem)", callback_data="act_leve")],
        [InlineKeyboardButton(text="Moderado (3-5 dias/sem)", callback_data="act_moderado")],
        [InlineKeyboardButton(text="🏃 Ativo (6-7 dias/sem)", callback_data="act_ativo")],
        [InlineKeyboardButton(text="🏆 Atleta (2x dia)", callback_data="act_atleta")]
    ])
    await callback.message.edit_text("5️⃣ Qual seu nível de **atividade física**?", reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(ProfileStates.activity, F.data.startswith("act_"))
async def process_activity(callback: types.CallbackQuery, state: FSMContext):
    activity = callback.data.split("_")[1]
    await state.update_data(activity=activity)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📉 Perder Peso", callback_data="goal_perder")],
        [InlineKeyboardButton(text="⚖️ Manter Peso", callback_data="goal_manter")],
        [InlineKeyboardButton(text="📈 Ganhar Massa", callback_data="goal_ganhar")]
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
    supabase.table("profiles").upsert(profile_data).execute()
    
    await state.clear()
    await callback.message.edit_text(
        f"✨ **Perfil configurado!**\n\n"
        f"🎯 Sua meta diária (ajustada para **{goal}**) é: **{tdee} kcal**\n\n"
        f"Agora é só me mandar seus alimentos ou uma foto do prato! 📸🍎",
        parse_mode="Markdown"
    )
    # Envia o teclado principal em uma nova mensagem para garantir que ele apareça
    await callback.message.answer("O que vamos registrar agora?", reply_markup=get_main_keyboard())

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
    
    if not grams:
        await message.answer("❌ **Não entendi a quantidade.** Digite algo como `100ml` ou `200`.", parse_mode="Markdown")
        return

    # Calculate factor based on 100g base (IA agora garante base 100)
    factor = grams / 100
    
    # Se o usuário escreveu um texto longo (ex: 'Suco Maguary 200ml'), tentamos extrair o nome
    # Removemos apenas a parte numérica e unidades conhecidas
    name_clean = re.sub(r"\d+[\.,]?\d*\s*(ml|g|gr|kg|l|copo|unidade|unid|xicara|unidades)?", "", message.text, flags=re.IGNORECASE).strip()
    # Limpa caracteres extras como parênteses ou traços sobrando
    name_clean = re.sub(r"^[^\w]+|[^\w]+$", "", name_clean)
    
    display_name = name_clean if len(name_clean) > 3 else product["alimento"]

    item = {
        "alimento": display_name,
        "peso": f"{grams}ml" if "ml" in message.text.lower() or "l" in message.text.lower() else f"{grams}g",
        "calorias": round(product["kcal_100g"] * factor),
        "proteina": round(product["prot_100g"] * factor),
        "carboidratos": round(product["carb_100g"] * factor),
        "gorduras": round(product["fat_100g"] * factor),
        "refeicao": "Lanche",
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
    await message.answer("📊 Escolha o período do relatório:", reply_markup=kb, parse_mode="Markdown")

def generate_report_chart(data: list, days: int):
    """Generates a pie chart of macro distribution using Agg backend."""
    if not data:
        return None
    
    total_prot = sum(d.get('protein', 0) for d in data)
    total_carb = sum(d.get('carbs', 0) for d in data)
    total_fat = sum(d.get('fat', 0) for d in data)
    
    if total_prot == 0 and total_carb == 0 and total_fat == 0:
        return None

    labels = ['Proteínas', 'Carbos', 'Gorduras']
    sizes = [total_prot, total_carb, total_fat]
    colors = ['#FF4B4B', '#FFD700', '#4CAF50']
    
    # IMPORTANTE: Usar backend offline para evitar erros de GUI
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    
    plt.figure(figsize=(6, 6))
    plt.pie(sizes, labels=labels, autopct='%1.1f%%', colors=colors, startangle=140, textprops={'color':"white"})
    plt.title(f'Macronutrientes ({days} dias)', color='white')
    
    # Estética premium: Fundo transparente/escuro
    plt.gcf().patch.set_facecolor('#1A1A1A')
    
    buf = io.BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight', facecolor='#1A1A1A')
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
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔥 **Total:** {total_kcal} kcal\n"
            f"🎯 **Meta:** {tdee} kcal\n"
            f"Média: {avg} kcal/dia\n\n"
            f"💪 **P:** {total_prot}g | 🍞 **C:** {total_carb}g | 🥑 **G:** {total_fat}g\n\n"
            f"⚖️ **Status:** {status_label}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
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

@dp.message(Command("exportar"))
async def cmd_export(message: types.Message):
    """Exports the user's food history to a CSV file."""
    try:
        user_id = message.from_user.id
        # Fetch all logs for this user
        res = supabase.table("logs").select("*").eq("user_id", str(user_id)).order("created_at", desc=True).execute()
        
        if not res.data:
            await message.answer("ℹ️ **Você ainda não tem alimentos logados.**", parse_mode="Markdown")
            return
            
        import csv
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=["created_at", "food", "weight", "kcal", "protein", "carbs", "fat", "meal_type"])
        writer.writeheader()
        
        for row in res.data:
            writer.writerow({
                "created_at": row["created_at"],
                "food": row["food"],
                "weight": row["weight"],
                "kcal": row["kcal"],
                "protein": row["protein"],
                "carbs": row["carbs"],
                "fat": row["fat"],
                "meal_type": row["meal_type"]
            })
            
        output.seek(0)
        csv_bytes = output.getvalue().encode('utf-8')
        
        await message.answer_document(
            document=types.BufferedInputFile(csv_bytes, filename=f"log_calorias_{datetime.now().strftime('%Y%m%d')}.csv"),
            caption="📂 **Aqui está seu histórico completo de alimentos!**",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Erro ao exportar: {e}")
        await message.answer("❌ **Ocorreu um erro ao exportar seus dados.**", parse_mode="Markdown")

# --- Food and Vision Handling ---

async def process_food_entry(message: types.Message, items: list, raw_data: str):
    """Common logic for saving and responding to food entries."""
    if not items:
        return

    # Corrigir calorias e macros baseado no peso (IA retorna base 100)
    for i in items:
        grams = extract_amount(i['peso'])
        if grams is not None and grams > 0:
            factor = grams / 100
            i['calorias'] = round(i['calorias'] * factor)
            i['proteina'] = round(i.get('proteina', 0) * factor)
            i['carboidratos'] = round(i.get('carboidratos', 0) * factor)
            i['gorduras'] = round(i.get('gorduras', 0) * factor)

    # Log to DB
    if not log_calories(message.from_user.id, message.from_user.full_name, items):
        await message.answer("❌ **Erro ao salvar dados.** Tente novamente.", parse_mode="Markdown")
        return
    
    profile = get_user_profile(message.from_user.id)
    daily_limit = profile['tdee'] if profile else 2000
    stats = get_daily_stats(message.from_user.id)
    daily_total = stats["kcal"]
    
    table_detected = any(i.get("table_detected", False) for i in items)
    prefix = "✅ **Tabela Nutricional Detectada**\n" if table_detected else ""
    
    msg = f"{prefix}✅ **Registro Confirmado!**\n"
    msg += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    
    items_text = ""
    for idx, i in enumerate(items):
        meal_header = i.get('refeicao', 'Lanche').upper()
        emoji_food = "🍎" if idx % 2 == 0 else "🥩"
        precisao = " ⚠️" if not i.get("is_precise", False) else ""
        universal_tag = " 🌐" if i.get("is_universal") else ""
        
        items_text += f"🍱 **{meal_header}**\n"
        items_text += f"{emoji_food} **{i['alimento']}**\n"
        items_text += f"⚖️ {i['peso']}  |  🔥 **{i['calorias']} kcal**{precisao}{universal_tag}\n"
        items_text += f"   └ 💪 **P:** {i.get('proteina', 0)}g | 🍞 **C:** {i.get('carboidratos', 0)}g | 🥑 **G:** {i.get('gorduras', 0)}g\n\n"
        
    remaining = daily_limit - daily_total
    progress_val = min(10, round((daily_total/daily_limit)*10)) if daily_limit > 0 else 0
    progress_bar = "🔵" * progress_val + "⚪" * (10 - progress_val)
    
    now_br = get_br_now()
    data_formatada = now_br.strftime("%d/%m")

    # Feedback Buttons
    buttons = [
        [InlineKeyboardButton(text="➕ 10%", callback_data="adj_1.1"),
         InlineKeyboardButton(text="➖ 10%", callback_data="adj_0.9")],
        [InlineKeyboardButton(text="🔄 Desfazer", callback_data="adj_undo")]
    ]
    
    # Se houver itens estimados, oferece o botão de votar e o de corrigir
    if any(not i.get("is_precise", False) for i in items):
        row = [InlineKeyboardButton(text="🗳️ Votar Correto", callback_data="confirm_precise")]
        # Só permite corrigir se NÃO detectou tabela (tabela é absoluto)
        if not table_detected:
            row.append(InlineKeyboardButton(text="🎨 Corrigir", callback_data="manual_correct"))
        buttons.insert(0, row)
        
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    response_text = (
        f"✅ **Registro Confirmado!**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{items_text}"
        f"📊 **RESUMO DO DIA ({data_formatada})**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔥 **Consumo:** {daily_total} / {daily_limit} kcal\n"
        f"⚖️ **Restante:** **{max(0, remaining)} kcal**\n\n"
        f"💪 **P:** {stats['protein']}g | 🍞 **C:** {stats['carbs']}g | 🥑 **G:** {stats['fat']}g\n\n"
        f"|{progress_bar}|"
    )
    await message.answer(response_text, parse_mode="Markdown", reply_markup=kb)

@dp.message(F.photo, StateFilter(None))
async def handle_photo(message: types.Message, state: FSMContext):
    try:
        status_msg = await message.answer("🔍 **Analisando foto...** 📸👀", parse_mode="Markdown")
        
        # Get the best photo size
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        photo_bytes = io.BytesIO()
        await bot.download_file(file.file_path, destination=photo_bytes)
        
        # OTIMIZAÇÃO: Comprimir imagem antes de enviar para Gemini
        img = Image.open(photo_bytes)
        # Redimensiona mantendo proporção (max 1600px largura/altura)
        img.thumbnail((1600, 1600))
        compressed_io = io.BytesIO()
        img.convert("RGB").save(compressed_io, format="JPEG", quality=80, optimize=True)
        final_bytes = compressed_io.getvalue()
        
        items, barcode, is_packaged, error_type, raw_data = await extract_calories_list(
            user_id=message.from_user.id,
            image_bytes=final_bytes,
            message_text=message.caption or "Foto de comida"
        )

        await status_msg.delete()
        if error_type:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📝 Descrever por Texto", callback_data="err_text")],
                [InlineKeyboardButton(text="📖 Ver Exemplos", callback_data="err_examples")]
            ])
            await message.answer(
                f"❌ **Ops! Não consegui analisar essa foto.**\n"
                f"Motivo: {error_type}\n\n"
                f"Tente tirar uma foto mais clara ou descreva o que você comeu clicando abaixo:", 
                parse_mode="Markdown", 
                reply_markup=kb
            )
            return

        # ROTA 1: Código de Barras (Zera tudo e usa base oficial)
        if barcode and str(barcode).strip() and str(barcode).strip().lower() != "null":
            product_data = await get_barcode_data(barcode)
            if product_data:
                await state.update_data(barcode_product=product_data)
                await message.answer(
                    f"🔍 **Produto Detectado (Barcode):** {product_data['alimento']}\n\n"
                    "Quanto você consumiu deste produto? (ex: 100g, 50, 1 unidade)",
                    parse_mode="Markdown"
                )
                await state.set_state(BarcodeState.waiting_for_portion)
                return
            else:
                logger.warning(f"Barcode {barcode} não encontrado. Seguindo para análise visual.")

        # ROTA 2: Produto Industrializado Sem Barcode (IA identificou como embalagem)
        if is_packaged and items:
            # Transformamos o item da IA em um 'barcode_product' fake para reutilizar o flow de porção
            # mas baseamos nos valores que a IA leu da TABELA NUTRICIONAL (que agora é prioridade)
            main_item = items[0]
            # Com o novo prompt, a IA JÁ retorna valores em base 100.
            # Não precisamos mais calcular fatores complexos aqui, apenas confiar na base 100 da IA.
            product_data = {
                "alimento": main_item["alimento"],
                "kcal_100g": main_item["calorias"],
                "prot_100g": main_item.get("proteina", 0),
                "carb_100g": main_item.get("carboidratos", 0),
                "fat_100g": main_item.get("gorduras", 0)
            }
            await state.update_data(barcode_product=product_data)
            await message.answer(
                f"📦 **Embalagem Detectada:** {main_item['alimento']}\n\n"
                "Para ser mais preciso, quanto você consumiu? (ex: 100g, 1 copo, 200ml)",
                parse_mode="Markdown"
            )
            await state.set_state(BarcodeState.waiting_for_portion)
            return

        await process_food_entry(message, items, raw_data)
    except Exception as e:
        logger.error(f"Erro no handle_photo: {e}")
        await message.answer("❌ **Ocorreu um erro inesperado** ao processar a foto.", parse_mode="Markdown")

@dp.message(F.text, StateFilter(None))
async def handle_text(message: types.Message):
    try:
        msg_id = f"{message.chat.id}:{message.message_id}"
        if msg_id in processed_messages: return
        processed_messages.add(msg_id)
        if len(processed_messages) > 1000: processed_messages.clear()

        status_msg = await message.answer("🧐 **Calculando...**", parse_mode="Markdown")
        
        user_id = message.from_user.id
        
        # Check if user is in sarcasm mode
        if jailbreak_users.get(user_id):
            if is_apology(message.text):
                jailbreak_users[user_id] = False
                await status_msg.delete()
                await message.answer("😇 Ah, finalmente percebeu o erro? Tá bom, vamos voltar ao normal. O que você comeu?")
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

        items, barcode, is_packaged, error_type, raw_data = await extract_calories_list(
            user_id=user_id, 
            message_text=message.text
        )
        
        await status_msg.delete()
        if error_type:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📖 Ver Exemplos", callback_data="err_examples")],
                [InlineKeyboardButton(text="📸 Tentar Foto", callback_data="err_photo")]
            ])
            await message.answer(
                f"❌ **Não entendi muito bem...**\n"
                f"Tente resumir o que você comeu (ex: 'Arroz, feijão e frango 300g').\n\n"
                f"Se preferir, me mande uma foto do prato!", 
                parse_mode="Markdown",
                reply_markup=kb
            )
            return
            
        if items is not None and len(items) == 0:
            await message.answer("🤔 Não identifiquei alimentos. Pode tentar descrever de outra forma?")
            return

        await process_food_entry(message, items, raw_data)
    except Exception as e:
        logger.error(f"Erro no handle_text: {e}")
        await message.answer("❌ **Ocorreu um erro inesperado.**", parse_mode="Markdown")

# --- FastAPI Webhook ---

@app.post("/webhook")
async def telegram_webhook(request: Request):
    update = Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"status": "ok"}

@app.get("/")
def index(): return {"status": "Bot is running"}

@app.api_route("/api/health", methods=["GET", "POST", "HEAD"])
def health_check(): return {"status": "ok"}

@app.on_event("startup")
async def on_startup():
    await bot.set_webhook(url=f"{WEBHOOK_URL}/webhook")
    logger.info(f"Webhook set to {WEBHOOK_URL}/webhook")
    # Inicia o loop de notificações em background
    asyncio.create_task(reminder_loop())

async def reminder_loop():
    """Background task to send reminders to inactive users."""
    logger.info("Loop de lembretes iniciado.")
    while True:
        try:
            now_br = get_br_now()
            hour = now_br.hour
            
            # Só manda lembretes entre 10h e 22h BR
            if 10 <= hour <= 22:
                # 1. Busca todos os usuários
                res = supabase.table("profiles").select("user_id").execute()
                users = res.data or []
                
                today_start = get_br_today_start()
                
                for user in users:
                    uid = user['user_id']
                    name = "usuário" # Fallback since name is not in profiles
                    
                    # 2. Verifica se o usuário logou hoje
                    log_res = supabase.table("logs") \
                        .select("id") \
                        .eq("user_id", str(uid)) \
                        .gte("created_at", today_start) \
                        .limit(1) \
                        .execute()
                    
                    if not log_res.data:
                        # Usuário não logou nada hoje.
                        # Vamos mandar lembrete apenas em horários específicos para não flooder
                        # 11h (Almoço), 16h (Lanche), 20h (Jantar)
                        if hour in [11, 16, 20]:
                            try:
                                msg = f"🔔 **Opa {name}!** Vi que você ainda não registrou nada hoje. Como está o foco na dieta? 💪🍎"
                                if hour == 11: msg = f"🥗 **Hora do Almoço, {name}!** Não esquece de registrar para não perder a conta! 🧐"
                                elif hour == 20: msg = f"🌙 **O dia está acabando, {name}!** Registre suas últimas refeições para fechar o relatório de hoje. 🔥"
                                
                                await bot.send_message(uid, msg, parse_mode="Markdown")
                                logger.info(f"Lembrete enviado para {uid}")
                            except Exception as send_err:
                                logger.warning(f"Não consegui mandar reminder para {uid}: {send_err}")
                                
            # Espera 1 hora antes da próxima verificação
            await asyncio.sleep(3600) 
        except Exception as e:
            logger.error(f"Erro no loop de lembretes: {e}")
            await asyncio.sleep(300) # Se der erro, espera 5 min e tenta de novo

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
