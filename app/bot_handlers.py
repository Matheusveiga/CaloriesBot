import asyncio
import re
import io
import os
import json
from datetime import datetime, timedelta, UTC
from typing import Dict, Any, List, Optional
from PIL import Image
import matplotlib.pyplot as plt
from aiogram import types, F
from aiogram.filters import Command, StateFilter, CommandObject
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from app.config import bot, dp, logger, processed_messages, user_history, jailbreak_users, AI_CACHE, supabase
from app.utils import get_br_now, get_br_today_start, is_jailbreak, is_apology, extract_amount, parse_numeric
from app.database import (
    log_calories, get_user_profile, get_daily_stats, get_daily_total, 
    get_report_data, delete_last_log, delete_today_logs, delete_entire_profile, delete_log_by_id
)
from app.services.ai_service import extract_calories_list, calculate_tdee, generate_sarcastic_response
from app.services.search_service import search_openfoodfacts

# States (Duplicating here for localized access in handlers)
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

def get_main_keyboard():
    kb = [
        [KeyboardButton(text="📊 Status"), KeyboardButton(text="🍱 Logar Comida")],
        [KeyboardButton(text="📈 Relatório"), KeyboardButton(text="⚙️ Perfil")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, persistent=True)

# --- Command Handlers ---

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
        await asyncio.sleep(1)
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
        if message.from_user.id in user_history and user_history[message.from_user.id]:
            user_history[message.from_user.id].pop()
        await message.answer("🔄 **A última entrada foi removida com sucesso!**", parse_mode="Markdown")
    else:
        await message.answer("❌ **Não encontrei entradas recentes para remover.**", parse_mode="Markdown")

@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    user_id = message.from_user.id
    profile = get_user_profile(user_id)
    if not profile:
        await message.answer("⚠️ **Ops!** Você ainda não tem um perfil configurado.", parse_mode="Markdown", reply_markup=get_main_keyboard())
        return
    stats = get_daily_stats(user_id)
    daily_total = stats["kcal"]
    daily_limit = profile['tdee']
    remaining = daily_limit - daily_total
    today_br_start = get_br_today_start()
    res = supabase.table("logs").select("*").eq("user_id", str(user_id)).gte("created_at", today_br_start).order("created_at", desc=True).execute()
    items_list_text = "".join([f"• {item['food']} ({item['kcal']} kcal)\n" for item in res.data]) or "_Nenhum alimento logado hoje._\n"
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
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🗑️ Deletar Item Específico", callback_data="list_undo")]])
    await message.answer(status_msg, parse_mode="Markdown", reply_markup=kb)

# --- Keyboard Buttons ---
@dp.message(F.text == "📊 Status")
async def btn_status(message: types.Message): await cmd_status(message)

@dp.message(F.text == "🍱 Logar Comida")
async def btn_log(message: types.Message): 
    await message.answer("Pode mandar! Descreva o que você comeu ou envie uma foto. 📸🥗", parse_mode="Markdown")

@dp.message(F.text == "📈 Relatório")
async def btn_report(message: types.Message): await cmd_report(message)

@dp.message(F.text == "⚙️ Perfil")
async def btn_profile(message: types.Message, state: FSMContext): await start_profile(message, state)

# --- Core Processing ---

async def process_food_entry(message: types.Message, items: list, raw_data: str):
    if not items: return
    for i in items:
        grams = extract_amount(i['peso'])
        if grams and grams > 0:
            factor = grams / 100
            i['calorias'] = round(i['calorias'] * factor)
            i['proteina'] = round(i.get('proteina', 0) * factor)
            i['carboidratos'] = round(i.get('carboidratos', 0) * factor)
            i['gorduras'] = round(i.get('gorduras', 0) * factor)
    if not log_calories(message.from_user.id, message.from_user.full_name, items):
        await message.answer("❌ **Erro ao salvar dados.** Tente novamente.", parse_mode="Markdown")
        return
    profile = get_user_profile(message.from_user.id)
    daily_limit = profile['tdee'] if profile else 2000
    stats = get_daily_stats(message.from_user.id)
    items_text = "".join([f"🍱 **{(i.get('refeicao') or 'Lanche').upper()}**\n{i['alimento']} | {i['peso']} | {i['calorias']} kcal\n" for i in items])
    progress = min(10, round((stats['kcal']/daily_limit)*10)) if daily_limit > 0 else 0
    p_bar = "🔵" * progress + "⚪" * (10 - progress)
    await message.answer(f"✅ **Registro Confirmado!**\n\n{items_text}\n📊 **RESUMO HOJE:** {stats['kcal']} / {daily_limit} kcal\n|{p_bar}|", parse_mode="Markdown")

@dp.message(F.photo)
async def handle_photo(message: types.Message, state: FSMContext):
    status_msg = await message.answer("🔍 **Analisando foto...** 📸👀", parse_mode="Markdown")
    try:
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        photo_bytes = io.BytesIO()
        await bot.download_file(file.file_path, destination=photo_bytes)
        img = Image.open(photo_bytes)
        img.thumbnail((1600, 1600))
        compressed_io = io.BytesIO()
        img.convert("RGB").save(compressed_io, format="JPEG", quality=80)
        final_bytes = compressed_io.getvalue()
        
        items, barcode, is_packaged, error_type, raw_data = await extract_calories_list(message.from_user.id, message.caption or "Foto", final_bytes)
        await status_msg.delete()
        if error_type:
            await message.answer(f"❌ Erro: {error_type}")
            return
        # Barcode logic (simplified for mvp modularization)
        await process_food_entry(message, items, raw_data)
    except Exception as e:
        logger.error(f"Erro foto: {e}")
        await message.answer("❌ Erro ao analisar foto.")

@dp.message(F.text)
async def handle_text(message: types.Message):
    if is_jailbreak(message.text):
        await message.answer("Eae amigão, tentando mandar um Jailbreak?")
        return
    status_msg = await message.answer("🧐 **Calculando...**", parse_mode="Markdown")
    items, barcode, is_packaged, error_type, raw_data = await extract_calories_list(message.from_user.id, message.text)
    await status_msg.delete()
    if items:
        await process_food_entry(message, items, raw_data)
    else:
        await message.answer("🤔 Não entendi muito bem. Tente resumir ou mande uma foto.")

# ... (Additional handlers like cmd_report, callbacks, etc. would go here)
