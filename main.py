import os
import logging
import json
import io
import re
from json import JSONDecodeError
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

import matplotlib.pyplot as plt
import matplotlib
matplotlib.use('Agg') # Non-interactive backend

from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter, CommandObject
from aiogram.types import Update, InlineKeyboardMarkup, InlineKeyboardButton, PhotoSize
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from google import genai
from google.genai import types as ai_types
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Config
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
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
AI_MODEL = "gemini-2.5-flash-lite"

# Init Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# States for Onboarding
class ProfileStates(StatesGroup):
    weight = State()
    height = State()
    age = State()
    gender = State()
    activity = State()
    goal = State() # NEW: Objetivo (Perder, Manter, Ganhar)

# Memory and Duplicate protection
processed_messages = set()
user_history: Dict[int, List[str]] = {}
jailbreak_users: Dict[int, bool] = {}

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
        response = ai_client.models.generate_content(
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

def get_daily_total(user_id: str):
    """Calculates the total calories for the current day using Brazil Time (UTC-3)."""
    try:
        today_br_start = get_br_today_start()
        
        response = supabase.table("logs") \
            .select("kcal") \
            .eq("user_id", str(user_id)) \
            .gte("created_at", today_br_start) \
            .execute()
        return sum(item.get('kcal', 0) for item in response.data)
    except Exception as e:
        logger.error(f"Erro ao calcular total diário: {e}")
        return 0

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


def extract_weight_in_grams(weight_text: str) -> Optional[float]:
    """Extracts weight in grams from a free text field like '200g' or '1 porção (150 g)'."""
    if not weight_text:
        return None

    match = re.search(r"(\d+[\.,]?\d*)\s*g", str(weight_text).lower())
    if not match:
        return None

    try:
        return float(match.group(1).replace(",", "."))
    except ValueError:
        return None


async def enrich_items_with_google_search(items: list):
    """Uses Google Search tool to validate kcal values and correct obvious distortions."""
    if not items:
        return items

    prompt = f"""
    Você é um nutricionista e deve validar calorias e MACRONUTRIENTES com pesquisa web confiável.
    Para cada item no JSON abaixo, pesquise valor calórico médio (kcal) e macros (proteína, carbos, gorduras) por 100g ou unidade e recalcule proporcionalmente ao peso.

    REGRAS:
    1) Use Google Search para valores reais.
    2) Retorne SOMENTE JSON no formato:
       [{{"alimento":"str","peso":"str","calorias":int,"proteina":int,"carboidratos":int,"gorduras":int}}]
    3) Macros devem ser inteiros por grama.
    4) Mantenha a ordem original.

    ITENS:
    {json.dumps(items, ensure_ascii=False)}
    """

    config = ai_types.GenerateContentConfig(
        tools=[ai_types.Tool(google_search=ai_types.GoogleSearch())]
    )

    try:
        response = ai_client.models.generate_content(
            model=AI_MODEL,
            contents=[prompt],
            config=config
        )
        cleaned_text = response.text.strip()
        if "```json" in cleaned_text:
            cleaned_text = cleaned_text.split("```json")[1].split("```")[0]
        elif "```" in cleaned_text:
            cleaned_text = cleaned_text.split("```")[1].split("```")[0]

        validated_items = json.loads(cleaned_text.strip())
        if not isinstance(validated_items, list):
            return items

        sanitized_items = []
        for idx, original in enumerate(items):
            validated = validated_items[idx] if idx < len(validated_items) else None
            if not isinstance(validated, dict) or not validated.get("alimento"):
                sanitized_items.append(original)
                continue

            kcal = int(float(validated.get("calorias", 0)))
            if kcal <= 0:
                sanitized_items.append(original)
                continue

            validated["calorias"] = kcal
            validated.setdefault("peso", original.get("peso", ""))
            sanitized_items.append(validated)

        return sanitized_items if sanitized_items else items
    except Exception as e:
        logger.warning(f"Falha ao validar calorias por pesquisa web: {e}")
        return items

# --- AI Logic ---

async def extract_calories_list(user_id: int, message_text: str = "", image_bytes: Optional[bytes] = None):
    """
    Calls Gemini to extract food items from text or image. 
    Returns (items_list, error_type, raw_response)
    """
    # Get history
    history = user_history.get(user_id, [])
    history_ctx = "\n".join(history[-5:]) if history else "Sem histórico."
    
    # Horário local Brasil (UTC-3) para inferência de refeição
    now_br = get_br_now()
    hora_local = now_br.strftime("%H:%M")

    prompt = f"""
    Você é um nutricionista especialista com ACESSO À PESQUISA GOOGLE.
    OBJETIVO: Identificar APENAS OS NOVOS alimentos da "ENTRADA ATUAL", extraindo calorias, macronutrientes e o tipo de refeição.
    
    REGRAS:
    1. Identifique: Nome do alimento, peso (estimado ou real), calorias (kcal), proteínas (g), carboidratos (g) e gorduras (g).
    2. Classifique a REFEIÇÃO em: "Café da Manhã", "Almoço", "Jantar", "Lanche" ou "Outro".
    3. IMPORTANTE (HORÁRIO): Agora são {hora_local} no horário do usuário. Se ele não especificar a refeição, use o horário para inferir EX:
       - 05:00-10:30: Café da Manhã
       - 11:00-14:30: Almoço
       - 18:00-23:00: Jantar
       - Outros horários: Lanche / Outro
    4. Retorne APENAS alimentos novos em JSON: 
       [ {{"alimento": "str", "peso": "str", "calorias": int, "proteina": int, "carboidratos": int, "gorduras": int, "refeicao": "str"}} ]
    5. Se o peso for omitido, chute um valor plausível baseado no contexto ou imagem.
    
    CONTEXTO (Logs recentes):
    {history_ctx}
    
    ENTRADA ATUAL: "{message_text}"
    """
    
    # Configurar ferramentas (Pesquisa Google)
    config = ai_types.GenerateContentConfig(
        tools=[ai_types.Tool(google_search=ai_types.GoogleSearch())]
    )

    contents = [prompt]
    if image_bytes:
        contents.append(ai_types.Part.from_bytes(data=image_bytes, mime_type='image/jpeg'))

    raw_text = ""
    try:
        response = ai_client.models.generate_content(
            model=AI_MODEL,
            contents=contents,
            config=config # Habilita o Google Search
        )
        raw_text = response.text
        
        # Robust JSON extraction
        cleaned_text = raw_text.strip()
        if "```json" in cleaned_text:
            cleaned_text = cleaned_text.split("```json")[1].split("```")[0]
        elif "```" in cleaned_text:
            cleaned_text = cleaned_text.split("```")[1].split("```")[0]
            
        items = json.loads(cleaned_text.strip())
        
        # Sanitize and force types
        sanitized_items = []
        for item in items:
            if isinstance(item, dict) and item.get("alimento"):
                item["calorias"] = int(float(item.get("calorias", 0)))
                sanitized_items.append(item)
        
        # Segunda validação para reduzir superestimativas
        sanitized_items = await enrich_items_with_google_search(sanitized_items)

        # Guarda de plausibilidade simples para evitar outliers absurdos
        final_items = []
        for item in sanitized_items:
            grams = extract_weight_in_grams(item.get("peso", ""))
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

        return final_items, None, raw_text
    except JSONDecodeError as e:
        logger.error(f"Erro ao decodificar JSON da IA: {e}")
        print(f"DEBUG GEMINI RAW: {raw_text}")
        return None, "json_error", raw_text
    except Exception as e:
        logger.error(f"Gemini error: {e}")
        print(f"DEBUG GEMINI RAW: {raw_text}")
        return None, "ai_error", str(e)

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

async def process_food_entry(message: types.Message, items: list, raw_data: str):
    """Common logic for saving and responding to food entries."""
    if not items:
        return

    # Log to DB
    if not log_calories(message.from_user.id, message.from_user.full_name, items):
        await message.answer("❌ Erro ao salvar dados no Supabase.")
        return
    
    profile = get_user_profile(message.from_user.id)
    daily_limit = profile['tdee'] if profile else 2000
    daily_total = get_daily_total(message.from_user.id)
    
    items_text = ""
    for idx, i in enumerate(items):
        emoji = "🍎" if idx % 2 == 0 else "🥩"
        meal = f"[{i.get('refeicao', 'Outro')}] "
        items_text += f"{emoji} {meal}**{i['alimento']}** ({i['peso']}) → {i['calorias']} kcal\n"
        items_text += f"   └ P: {i.get('proteina', 0)}g | C: {i.get('carboidratos', 0)}g | G: {i.get('gorduras', 0)}g\n"
        
    remaining = daily_limit - daily_total
    progress_val = min(10, round((daily_total/daily_limit)*10)) if daily_limit > 0 else 0
    progress_bar = "🔵" * progress_val + "⚪" * (10 - progress_val)
    
    now_br = get_br_now()
    data_formatada = now_br.strftime("%d/%m")

    response_text = (
        f"{items_text}\n"
        f"📊 **CONTAGEM DE HOJE ({data_formatada})**\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🔥 Soma: **{daily_total}** / {daily_limit} kcal\n"
        f"⚖️ Restante: **{max(0, remaining)} kcal**\n\n"
        f"{progress_bar}"
    )
    await message.answer(response_text, parse_mode="Markdown")

@dp.message(F.photo, StateFilter(None))
async def handle_photo(message: types.Message):
    status_msg = await message.answer("Analisando foto... 📸👀")
    
    # Get the best photo size
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    photo_bytes = io.BytesIO()
    await bot.download_file(file.file_path, destination=photo_bytes)
    
    items, error_type, raw_data = await extract_calories_list(
        user_id=message.from_user.id,
        image_bytes=photo_bytes.getvalue(),
        message_text=message.caption or "Foto de comida"
    )

    await status_msg.delete()
    if error_type:
        await message.answer(f"❌ Erro na análise da foto: {error_type}")
        return

    await process_food_entry(message, items, raw_data)

@dp.message(F.text, StateFilter(None))
async def handle_text(message: types.Message):
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

    items, error_type, raw_data = await extract_calories_list(
        user_id=user_id, 
        message_text=message.text
    )
    
    await status_msg.delete()
    if error_type == "ai_error":
        await message.answer(f"❌ Erro na IA. Verifique os logs.")
        return
    elif error_type == "json_error":
        await message.answer("❌ Não entendi o formato. Tente simplificar.")
        return
        
    if items is not None and len(items) == 0:
        await message.answer("🤔 Não identifiquei alimentos.")
        return

    await process_food_entry(message, items, raw_data)

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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
