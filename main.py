import os
import logging
from datetime import datetime
from typing import Dict, Any

from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import Update
from aiogram.fsm.storage.memory import MemoryStorage
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

# Init Aiogram
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
app = FastAPI()

# Init Gemini
genai.configure(api_key=GEMINI_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

# Init Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# DB Logic
def log_calorie(user_id: str, user_name: str, food: str, weight: str, kcal: int):
    data = {
        "food": food,
        "weight": weight,
        "kcal": kcal,
        "user_id": str(user_id),
        "user_name": user_name
    }
    supabase.table("logs").insert(data).execute()
    return True

def get_daily_total(user_id: str):
    today = datetime.now().strftime("%Y-%m-%d")
    # Using filter for today's date (assuming created_at is timestamptz)
    response = supabase.table("logs") \
        .select("kcal") \
        .eq("user_id", str(user_id)) \
        .gte("created_at", today) \
        .execute()
    
    total = sum(item['kcal'] for item in response.data)
    return total

# AI Logic
async def extract_calories(message_text: str):
    prompt = f"""
    Você é um nutricionista especialista em cálculo calórico.
    Analise a frase do usuário: "{message_text}"
    Extraia o alimento, o peso/quantidade aproximada e calcule as calorias totais.
    Responda APENAS em JSON no formato:
    {{"alimento": "string", "peso": "string", "calorias": integer}}
    """
    response = model.generate_content(prompt)
    try:
        import json
        cleaned_text = response.text.strip().replace('```json', '').replace('```', '')
        return json.loads(cleaned_text)
    except Exception as e:
        logger.error(f"Error parsing Gemini response: {e}")
        return None

# Bot Handlers
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("Olá! Eu sou seu bot de calorias. Me diga o que comeu e quanto (ex: 200g de frango) e eu calculo tudo para você!")

@dp.message(F.text)
async def handle_message(message: types.Message):
    if not message.text:
        return

    status_msg = await message.answer("Calculando... 🧐")
    
    data = await extract_calories(message.text)
    if not data:
        await status_msg.edit_text("Ops, não consegui entender o alimento. Tente dizer algo como '100g de arroz'.")
        return

    food = data.get("alimento")
    weight = data.get("peso")
    kcal = data.get("calorias")
    
    # Log to DB
    log_calorie(message.from_user.id, message.from_user.full_name, food, weight, kcal)
    
    # Get total
    daily_total = get_daily_total(message.from_user.id)
    daily_limit = 2000 
    remaining = daily_limit - daily_total

    response_text = (
        f"✅ **{food}** ({weight})\n"
        f"🔥 Calorias: {kcal} kcal\n\n"
        f"📊 **Resumo de Hoje:**\n"
        f"Soma: {daily_total} kcal\n"
        f"Meta: {daily_limit} kcal\n"
        f"Restante: {max(0, remaining)} kcal"
    )
    
    await status_msg.edit_text(response_text, parse_mode="Markdown")

# FastAPI Webhook
@app.post("/webhook")
async def telegram_webhook(request: Request):
    update = Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"status": "ok"}

@app.get("/")
def index():
    return {"status": "Bot is running"}

@app.on_event("startup")
async def on_startup():
    await bot.set_webhook(url=f"{WEBHOOK_URL}/webhook")
    logger.info(f"Webhook set to {WEBHOOK_URL}/webhook")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
