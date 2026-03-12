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

# Deep Validation for SUPABASE_URL
SUPABASE_URL = SUPABASE_URL.strip()
if not SUPABASE_URL.startswith("https://"):
    error_msg = f"❌ SUPABASE_URL inválida: Deve começar com https://. Valor detectado: {SUPABASE_URL[:10]}..."
    logger.error(error_msg)
    raise ValueError(error_msg)

if "supabase.co" not in SUPABASE_URL:
    logger.warning("⚠️ SUPABASE_URL não parece ser uma URL padrão do Supabase (.supabase.co)")

# Init Aiogram
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
app = FastAPI()

# Init Gemini
genai.configure(api_key=GEMINI_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

# Init Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Prevent duplicate processing (Simple Memory Cache)
# In production with multiple workers, this would need Redis
processed_messages = set()

# DB Logic
def log_calories(user_id: str, user_name: str, items: list):
    """Logs a list of items to Supabase."""
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

def get_daily_total(user_id: str):
    today = datetime.now().strftime("%Y-%m-%d")
    response = supabase.table("logs") \
        .select("kcal") \
        .eq("user_id", str(user_id)) \
        .gte("created_at", today) \
        .execute()
    
    total = sum(item['kcal'] for item in response.data)
    return total

# AI Logic
async def extract_calories_list(message_text: str):
    prompt = f"""
    Você é um nutricionista especialista em cálculo calórico.
    Analise a frase do usuário: "{message_text}"
    Identifique TODOS os alimentos e seus respectivos pesos/quantidades.
    Calcule as calorias de cada um.
    Responda APENAS em JSON no formato de uma LISTA de objetos:
    [
      {{"alimento": "string", "peso": "string", "calorias": integer}},
      ...
    ]
    Se não encontrar nenhum alimento, responda: []
    """
    try:
        response = model.generate_content(prompt)
        import json
        cleaned_text = response.text.strip().replace('```json', '').replace('```', '')
        return json.loads(cleaned_text)
    except Exception as e:
        logger.error(f"Error calling or parsing Gemini: {e}")
        return None

# Bot Handlers
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("Olá! Eu sou seu bot de calorias. Me diga o que comeu e quanto (ex: 200g de frango) e eu calculo tudo para você!")

@dp.message(F.text)
async def handle_message(message: types.Message):
    if not message.text:
        return

    # Duplicate Check
    msg_id = f"{message.chat.id}:{message.message_id}"
    if msg_id in processed_messages:
        return
    processed_messages.add(msg_id)
    # Simple cleanup to keep memory low
    if len(processed_messages) > 1000:
        processed_messages.clear()

    status_msg = await message.answer("Calculando... 🧐")
    
    items = await extract_calories_list(message.text)
    
    if items is None:
        await status_msg.edit_text("❌ Erro ao processar com a IA. Tente descrever os alimentos de forma mais simples.")
        return
        
    if len(items) == 0:
        await status_msg.edit_text("🤔 Não identifiquei nenhum alimento na sua frase. Tente dizer algo como 'comi 1 pão e 2 ovos'.")
        return

    # Log to DB
    log_calories(message.from_user.id, message.from_user.full_name, items)
    
    # Get total
    daily_total = get_daily_total(message.from_user.id)
    daily_limit = 2000 
    remaining = daily_limit - daily_total

    # Format items output
    items_text = ""
    for item in items:
        items_text += f"✅ **{item['alimento']}** ({item['peso']}) 🔥 {item['calorias']} kcal\n"

    response_text = (
        f"{items_text}\n"
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
