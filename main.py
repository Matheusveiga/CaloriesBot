import os
import logging
import json
from datetime import datetime, timedelta
from typing import Dict, Any, List

from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.types import Update, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
import google.generativeai as genai
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

# Init Gemini
genai.configure(api_key=GEMINI_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

# Init Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# States for Onboarding
class ProfileStates(StatesGroup):
    weight = State()
    height = State()
    age = State()
    gender = State()
    activity = State()

processed_messages = set()

# --- DB Logic ---

def log_calories(user_id: str, user_name: str, items: list):
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

def get_user_profile(user_id: str):
    res = supabase.table("profiles").select("*").eq("user_id", str(user_id)).execute()
    return res.data[0] if res.data else None

def get_daily_total(user_id: str):
    today = datetime.now().strftime("%Y-%m-%d")
    response = supabase.table("logs") \
        .select("kcal") \
        .eq("user_id", str(user_id)) \
        .gte("created_at", today) \
        .execute()
    return sum(item['kcal'] for item in response.data)

def get_report_data(user_id: str, days: int):
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    response = supabase.table("logs") \
        .select("created_at, kcal") \
        .eq("user_id", str(user_id)) \
        .gte("created_at", start_date) \
        .execute()
    return response.data

# --- AI Logic ---

async def extract_calories_list(message_text: str):
    prompt = f"""
    Você é um nutricionista especialista em cálculo calórico. Analise: "{message_text}"
    Extraia alimentos, pesos e calorias. Responda APENAS em JSON LIST:
    [ {{"alimento": "str", "peso": "str", "calorias": int}} ]
    """
    try:
        response = model.generate_content(prompt)
        cleaned_text = response.text.replace('```json', '').replace('```', '').strip()
        return json.loads(cleaned_text)
    except Exception as e:
        logger.error(f"Gemini error: {e}")
        return None

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
    profile = get_user_profile(message.from_user.id)
    
    if not profile:
        await message.answer(
            f"👋 Olá, {message.from_user.first_name}! Bem-vindo ao Bot de Calorias.\n\n"
            "Notei que você ainda não tem um perfil. Para eu calcular suas metas com precisão, "
            "precisamos fazer uma configuração rápida."
        )
        await message.answer("Vamos lá! Qual seu **peso** atual em kg? (ex: 75.5)")
        await state.set_state(ProfileStates.weight)
    else:
        await message.answer(
            f"👋 Olá de novo, {message.from_user.first_name}!\n\n"
            f"Sua meta atual é **{profile['tdee']} kcal**.\n"
            "Comande:\n"
            "/perfil - Atualizar peso/meta\n"
            "/relatorio - Ver estatísticas\n"
            "Ou apenas me diga o que comeu! 🍎"
        )

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
        await message.answer("Qual sua **altura** em cm? (ex: 175)")
        await state.set_state(ProfileStates.height)
    except:
        await message.answer("Por favor, envie um número válido.")

@dp.message(ProfileStates.height)
async def process_height(message: types.Message, state: FSMContext):
    try:
        height = float(message.text)
        await state.update_data(height=height)
        await message.answer("Qual sua **idade**?")
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
        await message.answer("Qual seu **sexo**?", reply_markup=kb)
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
    await callback.message.edit_text("Qual seu nível de **atividade física**?", reply_markup=kb)
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
        f"Agora é só me mandar seus alimentos!"
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
    days = int(callback.data.split("_")[1])
    data = get_report_data(callback.from_user.id, days)
    profile = get_user_profile(callback.from_user.id)
    tdee = profile['tdee'] if profile else 2000
    
    total = sum(d['kcal'] for d in data)
    periodo = "Hoje" if days == 1 else f"Últimos {days} dias"
    
    avg = round(total / days) if days > 1 else total
    status = "dentro da meta" if avg <= tdee else "acima da meta"
    
    msg = (
        f"📊 **Relatório: {periodo}**\n\n"
        f"🔥 Total: {total} kcal\n"
        f"📉 Média: {avg} kcal/dia\n"
        f"🎯 Meta: {tdee} kcal\n"
        f"⚖️ Status: Você está {status}."
    )
    await callback.message.edit_text(msg, parse_mode="Markdown")

# --- Food Handling ---

@dp.message(F.text, StateFilter(None))
async def handle_message(message: types.Message):
    msg_id = f"{message.chat.id}:{message.message_id}"
    if msg_id in processed_messages: return
    processed_messages.add(msg_id)
    if len(processed_messages) > 1000: processed_messages.clear()

    status_msg = await message.answer("Calculando... 🧐")
    items = await extract_calories_list(message.text)
    
    if not items:
        await status_msg.edit_text("❌ Não entendi ou erro na IA.")
        return

    log_calories(message.from_user.id, message.from_user.full_name, items)
    
    profile = get_user_profile(message.from_user.id)
    daily_limit = profile['tdee'] if profile else 2000
    daily_total = get_daily_total(message.from_user.id)
    
    items_text = "\n".join([f"✅ **{i['alimento']}** ({i['peso']}) 🔥 {i['calorias']} kcal" for i in items])
    remaining = daily_limit - daily_total

    await status_msg.edit_text(
        f"{items_text}\n\n"
        f"📊 **Resumo de Hoje:**\n"
        f"Soma: {daily_total} kcal\n"
        f"Meta: {daily_limit} kcal\n"
        f"Restante: {max(0, remaining)} kcal",
        parse_mode="Markdown"
    )

# --- FastAPI Webhook ---

@app.post("/webhook")
async def telegram_webhook(request: Request):
    update = Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"status": "ok"}

@app.get("/")
def index(): return {"status": "Bot is running"}

@app.on_event("startup")
async def on_startup():
    await bot.set_webhook(url=f"{WEBHOOK_URL}/webhook")
    logger.info(f"Webhook set to {WEBHOOK_URL}/webhook")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
