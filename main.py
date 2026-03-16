import asyncio
from fastapi import Request
from aiogram.types import Update
from app.config import app, bot, dp, WEBHOOK_URL, logger
from app.utils import get_br_now, get_br_today_start
from app.database import supabase
# Import handlers to ensure they are registered
import app.bot_handlers

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
    uvicorn.run(app, host="0.0.0.0", port=8000)
