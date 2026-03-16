import asyncio
from fastapi import Request
from aiogram.types import Update
from app.config import fastapi_app as app, bot, dp, WEBHOOK_URL, logger, http_client, processed_messages
from app.utils import get_br_now, get_br_today_start
from app.database import supabase
# Import handlers using 'from app import' to avoid package name collision
from app import bot_handlers

@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        body = await request.json()
        update = Update.model_validate(body, context={"bot": bot})
        
        # Deduplicação baseada em update_id
        if update.update_id in processed_messages:
            return {"status": "already_processed"}
            
        processed_messages.add(update.update_id)
        if len(processed_messages) > 1000:
            # Mantém apenas as últimas 1000 IDs
            list_ids = list(processed_messages)
            processed_messages.clear()
            for id in list_ids[-500:]:
                processed_messages.add(id)

        await dp.feed_update(bot, update)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Erro no webhook: {e}")
        return {"status": "error"}

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
