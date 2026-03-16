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

@dp.message(Command("reset_dia"))
async def cmd_reset_day(message: types.Message):
    if delete_today_logs(message.from_user.id):
        if message.from_user.id in user_history:
            user_history[message.from_user.id] = []
        await message.answer("📅 Seus logs de **hoje** foram apagados!", parse_mode="Markdown")
    else:
        await message.answer("❌ **Erro ao apagar logs de hoje.**", parse_mode="Markdown")

@dp.message(Command("reset_perfil"))
async def cmd_reset_profile(message: types.Message, state: FSMContext):
    if delete_entire_profile(message.from_user.id):
        if message.from_user.id in user_history:
            del user_history[message.from_user.id]
        if message.from_user.id in jailbreak_users:
            del jailbreak_users[message.from_user.id]
        await message.answer("💥 **Perfil e histórico deletados!** Vamos começar do zero.", parse_mode="Markdown")
        await cmd_start(message, state)
    else:
        await message.answer("❌ **Erro ao deletar seu perfil.**", parse_mode="Markdown")

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

# --- Callbacks ---

@dp.callback_query(F.data.startswith("adj_"))
async def process_adjustment(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    action = callback.data.split("_")[1]
    try:
        if action == "undo":
            if delete_last_log(user_id):
                await callback.message.edit_text("🔄 **Log desfeito com sucesso!**", parse_mode="Markdown")
            else:
                await callback.answer("❌ Erro ao desfazer.")
            return
        multiplier = float(action)
        res = supabase.table("logs").select("*").eq("user_id", str(user_id)).order("created_at", desc=True).limit(1).execute()
        if not res.data: return
        last_time = res.data[0]['created_at']
        logs_to_update = supabase.table("logs").select("*").eq("user_id", str(user_id)).eq("created_at", last_time).execute()
        for entry in logs_to_update.data:
            supabase.table("logs").update({
                "kcal": round(entry['kcal'] * multiplier),
                "protein": round(entry['protein'] * multiplier),
                "carbs": round(entry['carbs'] * multiplier),
                "fat": round(entry['fat'] * multiplier)
            }).eq("id", entry['id']).execute()
        pct = "+10%" if multiplier > 1 else "-10%"
        await callback.message.edit_text(f"✅ Ajustado em **{pct}**!", parse_mode="Markdown")
        await callback.answer(f"Ajustado {pct}")
    except Exception as e:
        logger.error(f"Erro ajuste: {e}")
        await callback.answer("❌ Erro.")

@dp.callback_query(F.data == "list_undo")
async def process_list_undo(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    today_br_start = get_br_today_start()
    res = supabase.table("logs").select("id, food, kcal").eq("user_id", str(user_id)).gte("created_at", today_br_start).order("created_at", desc=True).execute()
    if not res.data:
        await callback.answer("Nenhum item.")
        return
    buttons = [[InlineKeyboardButton(text=f"🗑️ {i['food'][:15]} ({i['kcal']} kcal)", callback_data=f"del_{i['id']}")] for i in res.data]
    buttons.append([InlineKeyboardButton(text="⬅️ Voltar", callback_data="status_back")])
    await callback.message.edit_text("🎯 Selecione para remover:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.callback_query(F.data.startswith("del_"))
async def process_delete_specific(callback: types.CallbackQuery):
    log_id = int(callback.data.split("_")[1])
    if delete_log_by_id(callback.from_user.id, log_id):
        await callback.answer("✅ Removido!")
        await process_list_undo(callback)
    else:
        await callback.answer("❌ Erro.")

@dp.callback_query(F.data == "confirm_precise")
async def process_confirm_precise(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    res = supabase.table("logs").select("id, confirmations").eq("user_id", str(user_id)).order("created_at", desc=True).limit(1).execute()
    if res.data:
        supabase.table("logs").update({"confirmations": (res.data[0].get('confirmations', 0) or 0) + 1}).eq("id", res.data[0]['id']).execute()
        await callback.answer("🗳️ Voto registrado!")
        await callback.message.edit_reply_markup(reply_markup=None)

@dp.callback_query(F.data == "manual_correct")
async def process_manual_correction_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("Qual o valor real de **calorias (kcal) por 100g**?", parse_mode="Markdown")
    await state.set_state(CorrectionStates.kcal)
    await callback.answer()

@dp.message(CorrectionStates.kcal)
async def process_manual_kcal(message: types.Message, state: FSMContext):
    kcal_100g = parse_numeric(message.text)
    if kcal_100g is None: return
    res = supabase.table("logs").select("*").eq("user_id", str(message.from_user.id)).order("created_at", desc=True).limit(1).execute()
    if res.data:
        weight = extract_amount(res.data[0].get("weight", "100g")) or 100
        new_kcal = round((kcal_100g / 100) * weight)
        supabase.table("logs").update({"kcal": new_kcal, "is_precise": True}).eq("id", res.data[0]['id']).execute()
        await message.answer(f"✅ Corrigido para {new_kcal} kcal!")
    await state.clear()

@dp.callback_query(F.data.startswith("err_"))
async def process_error_actions(callback: types.CallbackQuery):
    action = callback.data.split("_")[1]
    if action == "text": await callback.message.answer("Pode digitar!")
    elif action == "photo": await callback.message.answer("Mande a foto!")
    await callback.answer()

@dp.callback_query(F.data == "status_back")
async def process_status_back(callback: types.CallbackQuery):
    await callback.message.delete()
    await cmd_status(callback.message)

# --- Reporting ---

@dp.message(Command("relatorio"))
async def cmd_report(message: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Hoje", callback_data="rep_1"), InlineKeyboardButton(text="Semana", callback_data="rep_7"), InlineKeyboardButton(text="Mês", callback_data="rep_30")]])
    await message.answer("📊 Escolha o período:", reply_markup=kb)

def generate_report_chart(data: list, days: int):
    if not data: return None
    total_prot = sum(d.get('protein', 0) for d in data)
    total_carb = sum(d.get('carbs', 0) for d in data)
    total_fat = sum(d.get('fat', 0) for d in data)
    if total_prot == 0 and total_carb == 0 and total_fat == 0: return None
    plt.figure(figsize=(6, 6))
    plt.pie([total_prot, total_carb, total_fat], labels=['Proteínas', 'Carbos', 'Gorduras'], autopct='%1.1f%%', colors=['#FF4B4B', '#FFD700', '#4CAF50'], startangle=140)
    plt.title(f'Macros ({days} dias)')
    buf = io.BytesIO()
    plt.savefig(buf, format='png')
    plt.close()
    buf.seek(0)
    return buf

@dp.callback_query(F.data.startswith("rep_"))
async def process_report(callback: types.CallbackQuery):
    days = int(callback.data.split("_")[1])
    data = get_report_data(callback.from_user.id, days)
    total_kcal = sum(d.get('kcal', 0) for d in data)
    msg = f"📊 **RELATÓRIO ({days} dias)**\n🔥 Total: {total_kcal} kcal"
    chart = generate_report_chart(data, days)
    if chart:
        await callback.message.answer_photo(photo=types.BufferedInputFile(chart.read(), filename="report.png"), caption=msg, parse_mode="Markdown")
        await callback.message.delete()
    else:
        await callback.message.edit_text(msg, parse_mode="Markdown")

@dp.message(Command("exportar"))
async def cmd_export(message: types.Message):
    res = supabase.table("logs").select("*").eq("user_id", str(message.from_user.id)).order("created_at", desc=True).execute()
    if not res.data: return
    import csv
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["created_at", "food", "weight", "kcal", "protein", "carbs", "fat", "meal_type"])
    writer.writeheader()
    writer.writerows(res.data)
    output.seek(0)
    await message.answer_document(document=types.BufferedInputFile(output.getvalue().encode('utf-8'), filename="historico.csv"))

# --- Profile FSM ---

@dp.message(Command("perfil"))
async def start_profile(message: types.Message, state: FSMContext):
    await message.answer("⚖️ Qual seu **peso** (kg)?", parse_mode="Markdown")
    await state.set_state(ProfileStates.weight)

@dp.message(ProfileStates.weight)
async def process_weight(message: types.Message, state: FSMContext):
    val = parse_numeric(message.text)
    if val:
        await state.update_data(weight=val)
        await message.answer("📏 Altura (cm):")
        await state.set_state(ProfileStates.height)

@dp.message(ProfileStates.height)
async def process_height(message: types.Message, state: FSMContext):
    val = parse_numeric(message.text)
    if val:
        await state.update_data(height=val)
        await message.answer("🎂 Idade:")
        await state.set_state(ProfileStates.age)

@dp.message(ProfileStates.age)
async def process_age(message: types.Message, state: FSMContext):
    val = parse_numeric(message.text)
    if val:
        await state.update_data(age=int(val))
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="♂️ M", callback_data="g_M"), InlineKeyboardButton(text="♀️ F", callback_data="g_F")]])
        await message.answer("🧬 Sexo:", reply_markup=kb)
        await state.set_state(ProfileStates.gender)

@dp.callback_query(ProfileStates.gender, F.data.startswith("g_"))
async def process_gender(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(gender=callback.data.split("_")[1])
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Sedentário", callback_data="act_sedentario")], [InlineKeyboardButton(text="Moderado", callback_data="act_moderado")], [InlineKeyboardButton(text="Ativo", callback_data="act_ativo")]])
    await callback.message.edit_text("🏃 Atividade:", reply_markup=kb)
    await state.set_state(ProfileStates.activity)

@dp.callback_query(ProfileStates.activity, F.data.startswith("act_"))
async def process_activity(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(activity=callback.data.split("_")[1])
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📉 Perder", callback_data="goal_perder"), InlineKeyboardButton(text="⚖️ Manter", callback_data="goal_manter"), InlineKeyboardButton(text="📈 Ganhar", callback_data="goal_ganhar")]])
    await callback.message.edit_text("🎯 Objetivo:", reply_markup=kb)
    await state.set_state(ProfileStates.goal)

@dp.callback_query(ProfileStates.goal, F.data.startswith("goal_"))
async def process_goal(callback: types.CallbackQuery, state: FSMContext):
    goal = callback.data.split("_")[1]
    data = await state.get_data()
    tdee = calculate_tdee(data['weight'], data['height'], data['age'], data['gender'], data['activity'], goal)
    supabase.table("profiles").upsert({"user_id": str(callback.from_user.id), **data, "goal": goal, "tdee": tdee}).execute()
    await state.clear()
    await callback.message.edit_text(f"✨ Perfil pronto! Meta: {tdee} kcal")

@dp.message(Command("cancelar"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Cancelado.")

# --- Barcode ---

@dp.message(BarcodeState.waiting_for_portion)
async def process_barcode_portion(message: types.Message, state: FSMContext):
    data = await state.get_data()
    product = data.get("barcode_product")
    grams = extract_amount(message.text)
    if grams and product:
        factor = grams / 100
        item = {"alimento": product["alimento"], "peso": f"{grams}g", "calorias": round(product["kcal_100g"] * factor), "proteina": round(product["prot_100g"] * factor), "carboidratos": round(product["carb_100g"] * factor), "gorduras": round(product["fat_100g"] * factor), "is_precise": True}
        await state.clear()
        await process_food_entry(message, [item], "Barcode")

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
        await message.answer("❌ Erro ao salvar.")
        return
    stats = get_daily_stats(message.from_user.id)
    profile = get_user_profile(message.from_user.id)
    limit = profile['tdee'] if profile else 2000
    await message.answer(f"✅ Registrado! Hoje: {stats['kcal']} / {limit} kcal")

@dp.message(F.photo)
async def handle_photo(message: types.Message, state: FSMContext):
    status_msg = await message.answer("🔍 Analisando...")
    try:
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        photo_bytes = io.BytesIO()
        await bot.download_file(file.file_path, destination=photo_bytes)
        img = Image.open(photo_bytes)
        img.thumbnail((1600, 1600))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        items, barcode, is_packaged, error, raw = await extract_calories_list(message.from_user.id, message.caption or "Foto", buf.getvalue())
        await status_msg.delete()
        if barcode and barcode != "null":
            product = await search_openfoodfacts(barcode)
            if product:
                await state.update_data(barcode_product=product)
                await message.answer(f"📦 {product['alimento']}. Quanto consumiu?")
                await state.set_state(BarcodeState.waiting_for_portion)
                return
        await process_food_entry(message, items, raw)
    except Exception as e:
        logger.error(f"Erro foto: {e}")
        await message.answer("❌ Erro.")

@dp.message(F.text)
async def handle_text(message: types.Message):
    if is_jailbreak(message.text):
        await message.answer("Jailbreak detectado.")
        return
    status_msg = await message.answer("🧐 Calculando...")
    items, barcode, is_packaged, error, raw = await extract_calories_list(message.from_user.id, message.text)
    await status_msg.delete()
    if items: await process_food_entry(message, items, raw)
    else: await message.answer("Não entendi.")
