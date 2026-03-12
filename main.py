import os
import logging
import json
import io
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

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

# Memory and Duplicate protection
processed_messages = set()
user_history: Dict[int, List[str]] = {}

# --- DB Logic ---

def log_calories(user_id: str, user_name: str, items: list):
    """Saves a list of food items to the database."""
    try:
        prepared_data = []
        for item in items:
            prepared_data.append({
                "food": item.get("alimento"),
                "weight": item.get("peso"),
                "kcal": item.get("calorias"),
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
        # Ajuste manual para o fuso do usuário (Brasília UTC-3)
        # Note: In production, consider asking for user's TZ or using a more robust method
        now_utc = datetime.utcnow()
        now_br = now_utc - timedelta(hours=3)
        today_br = now_br.strftime("%Y-%m-%d")
        
        response = supabase.table("logs") \
            .select("kcal") \
            .eq("user_id", str(user_id)) \
            .gte("created_at", today_br) \
            .execute()
        return sum(item.get('kcal', 0) for item in response.data)
    except Exception as e:
        logger.error(f"Erro ao calcular total diário: {e}")
        return 0

def get_report_data(user_id: str, days: int):
    """Aggregates data for periodic reports."""
    try:
        now_utc = datetime.utcnow()
        now_br = now_utc - timedelta(hours=3)
        start_date = (now_br - timedelta(days=days)).strftime("%Y-%m-%d")
        
        response = supabase.table("logs") \
            .select("created_at, kcal") \
            .eq("user_id", str(user_id)) \
            .gte("created_at", start_date) \
            .execute()
        return response.data
    except Exception as e:
        logger.error(f"Erro ao buscar dados do relatório: {e}")
        return []

# --- AI Logic ---

async def extract_calories_list(user_id: int, message_text: str = "", image_bytes: Optional[bytes] = None):
    """
    Calls Gemini to extract food items from text or image. 
    Returns (items_list, error_type, raw_response)
    """
    # Get history
    history = user_history.get(user_id, [])
    history_ctx = "\n".join(history[-5:]) if history else "Sem histórico."

    prompt = f"""
    Você é um nutricionista especialista em análise calórica.
    OBJETIVO: Identificar APENAS OS NOVOS alimentos da "ENTRADA ATUAL".
    
    REGRAS CRÍTICAS:
    1. O JSON retornado deve conter APENAS alimentos que o usuário acabou de mencionar ou enviar na foto agora. 
    2. NUNCA repita alimentos que já aparecem no "CONTEXTO" abaixo.
    3. Use o "CONTEXTO" APENAS para entender referências (ex: "e mais um desse").
    4. Se a "ENTRADA ATUAL" não trouxer nada novo, retorne: []
    5. Responda APENAS com uma lista JSON: [ {{"alimento": "str", "peso": "str", "calorias": int}} ]
    6. Garanta que o campo "calorias" seja sempre um NÚMERO INTEIRO maior que zero.
    
    CONTEXTO (Logs recentes):
    {history_ctx}
    
    ENTRADA ATUAL: "{message_text}"
    """
    
    contents = [prompt]
    if image_bytes:
        contents.append(ai_types.Part.from_bytes(data=image_bytes, mime_type='image/jpeg'))

    raw_text = ""
    try:
        response = ai_client.models.generate_content(
            model=AI_MODEL,
            contents=contents
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
        
        # Update memory with what was actually extracted
        if sanitized_items:
            if user_id not in user_history: user_history[user_id] = []
            extracted_summary = ", ".join([f"{i['alimento']} ({i['peso']})" for i in sanitized_items])
            user_history[user_id].append(f"LOGADO ANTERIORMENTE: {extracted_summary}")
            if len(user_history[user_id]) > 10: user_history[user_id] = user_history[user_id][-10:]

        return sanitized_items, None, raw_text
    except Exception as e:
        logger.error(f"Gemini error: {e}")
        print(f"DEBUG GEMINI RAW: {raw_text}")
        return None, "ai_error", str(e)

# --- Mifflin-St Jeor ---

def calculate_tdee(w, h, a, g, act):
    # BMR
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
    return round(bmr * multipliers.get(act, 1.2))

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
        "1. **Texto:** Diga o que comeu. \n"
        "   Ex: '2 ovos e 1 pão' ou 'repeti o almoço'.\n"
        "2. **Fotos:** Mande uma foto do prato e eu estimo as calorias.\n"
        "3. **Memória:** Eu entendo frases como 'e mais uma coca zero'.\n"
        "4. **Perfil:** Use /perfil para atualizar seus dados.\n"
        "5. **Relatórios:** Use /relatorio para ver seu progresso."
    )
    await message.answer(help_text, parse_mode="Markdown")

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
    data = await state.get_data()
    
    tdee = calculate_tdee(data['weight'], data['height'], data['age'], data['gender'], activity)
    
    # Save to Supabase
    profile_data = {
        "user_id": str(callback.from_user.id),
        "weight": data['weight'],
        "height": data['height'],
        "age": data['age'],
        "gender": data['gender'],
        "tdee": float(tdee)
    }
    supabase.table("profiles").upsert(profile_data).execute()
    
    await state.clear()
    await callback.message.edit_text(
        f"✅ Perfil configurado!\n"
        f"Sua meta diária é: **{tdee} kcal**\n\n"
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

@dp.callback_query(F.data.startswith("rep_"))
async def process_report(callback: types.CallbackQuery):
    try:
        days = int(callback.data.split("_")[1])
        data = get_report_data(callback.from_user.id, days)
        profile = get_user_profile(callback.from_user.id)
        
        tdee = profile['tdee'] if profile else 2000
        total = sum(d['kcal'] for d in data)
        periodo = "Hoje" if days == 1 else f"Últimos {days} dias"
        
        avg = round(total / days) if days > 0 else 0
        status_label = "✅ DENTRO DA META" if avg <= tdee else "⚠️ ACIMA DA META"
        
        msg = (
            f"📊 **RELATÓRIO: {periodo.upper()}**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🔥 **Total acumulado:** {total} kcal\n"
            f"📉 **Média diária:** {avg} kcal/dia\n"
            f"🎯 **Sua meta:** {tdee} kcal\n\n"
            f"⚖️ **Status:** {status_label}\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
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
        items_text += f"{emoji} **{i['alimento']}** ({i['peso']}) → {i['calorias']} kcal\n"
        
    remaining = daily_limit - daily_total
    progress_val = min(10, round((daily_total/daily_limit)*10)) if daily_limit > 0 else 0
    progress_bar = "🔵" * progress_val + "⚪" * (10 - progress_val)

    response_text = (
        f"{items_text}\n"
        f"📊 **CONTAGEM DE HOJE**\n"
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
    
    items, error_type, raw_data = await extract_calories_list(
        user_id=message.from_user.id, 
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
