import json
import asyncio
from google.genai import types as ai_types
from app.config import logger, ai_client, groq_client, AI_MODEL, AI_MODEL_FALLBACK, user_history, AI_CACHE, supabase
from app.utils import get_br_now, extract_amount, get_embedding
from app.database import search_food_history
from app.services.search_service import search_fatsecret, search_serper, search_duckduckgo, generate_surgical_query

async def call_gemini_with_retry(contents, config=None, max_retries=3):
    """Calls Gemini with exponential backoff for 429 errors."""
    for i in range(max_retries):
        try:
            response = await ai_client.aio.models.generate_content(
                model=AI_MODEL,
                contents=contents,
                config=config
            )
            return response
        except Exception as e:
            if "429" in str(e) and i < max_retries - 1:
                wait_time = (2 ** i) + 1
                logger.warning(f"Gemini 429 Detectado. Tentando novamente em {wait_time}s... (Tentativa {i+1}/{max_retries})")
                await asyncio.sleep(wait_time)
                continue
            raise e

async def call_groq_fallback(message_text: str, image_bytes=None, prompt: str = "", search_context: str = ""):
    """Calls Groq (Llama 3.3) as a fallback, optionally with search context."""
    if not groq_client: return None
    full_prompt = f"{prompt}\nCONTEXTO DE BUSCA ADICIONAL:\n{search_context}\n\nENTRADA: {message_text}"
    try:
        response = await groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": full_prompt}],
            response_format={"type": "json_object"}
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"Erro no Groq Fallback: {e}")
        return None

async def extract_calories_list(user_id: int, message_text: str = "", image_bytes=None):
    """Core extraction logic with multi-provider routing and hierarchical search."""
    history = user_history.get(user_id, [])
    history_ctx = "\n".join(history[-5:]) if history else "Sem histórico."
    now_br = get_br_now()
    hora_local = now_br.strftime("%H:%M")
    cache_key = f"{message_text}_{image_bytes is not None}"

    if not image_bytes:
        # Cache Semântico
        embedding = await get_embedding(message_text)
        if embedding:
            try:
                match_res = supabase.rpc("match_logs", {
                    "query_embedding": embedding,
                    "match_threshold": 0.92,
                    "match_count": 1,
                    "p_user_id": str(user_id)
                }).execute()
                if match_res.data:
                    item = match_res.data[0]
                    logger.info(f"🎯 Cache Semântico: {message_text} -> {item['food']}")
                    db_match = [{
                        "alimento": item["food"], "peso": item["weight"], "calorias": item["kcal"],
                        "proteina": item["protein"], "carboidratos": item["carbs"], "gorduras": item["fat"],
                        "refeicao": item["meal_type"], "is_precise": item["is_precise"]
                    }]
                    return db_match, None, None, None, "SEMANTIC_CACHE"
            except Exception as e: logger.warning(f"Erro na busca semântica: {e}")
                
        if cache_key in AI_CACHE:
            logger.info(f"Usando Cache em memória para: {message_text}")
            return AI_CACHE[cache_key], None, None, None, "MEMORY_CACHE"
        
        if len(message_text.split()) <= 3:
            db_match = search_food_history(user_id, message_text.strip())
            if db_match and db_match[0].get("is_precise"):
                logger.info(f"Usando Cache DB VERIFICADO para: {message_text}")
                AI_CACHE[cache_key] = db_match
                return db_match, None, None, None, "DB_VERIFIED_CACHE"

    prompt = f"""
    Você é um nutricionista especialista de ELITE com visão computacional avançada.
    OBJETIVO: Identificar alimentos da "ENTRADA ATUAL", extraindo calorias, proteínas, carboidratos e gorduras.
    
    ### REGRAS DE OURO (Siga rigorosamente):
    1.  **ESTRUTURA JSON**: Retorne APENAS um objeto JSON válido seguindo este esquema:
        {{
          "items": [
            {{
              "alimento": "Nome do alimento (inclua marca se for industrializado)",
              "peso": "Peso estimado (ex: '150g', '200ml')",
              "calorias": Int (BASE 100g/ml),
              "proteina": Int (BASE 100g/ml),
              "carboidratos": Int (BASE 100g/ml),
              "gorduras": Int (BASE 100g/ml),
              "refeicao": "Café, Almoço, Jantar, Lanche ou Outro",
              "is_precise": Boolean (True se leu de tabela/marca exata)
            }}
          ],
          "barcode": "String ou null",
          "is_packaged": Boolean (True se for embalagem industrializada)
        }}
    
    2.  **CONVERSÃO BASE 100**: Se encontrar dados específicos (ex: 30g), calcule o valor proporcional para 100g/ml antes de preencher o JSON.
    3.  **VISÃO (Se houver imagem)**: Foque primeiro em ler a **Tabela Nutricional** se houver.
    4.  **REFEIÇÃO**: Classifique com base na hora ({hora_local}). 
    5.  **CONTEXTO**: Use o histórico para entender se este item é uma correção ou adição.
    
    HISTÓRICO: {history_ctx}
    ENTRADA ATUAL: "{message_text}"
    """
    # Note: Full prompt content preserved from main.py

    raw_text = ""
    use_vision = image_bytes is not None
    ai_success = False

    if use_vision:
        logger.info("📡 ROTA IMAGEM: Usando Gemini Vision para análise...")
        config = ai_types.GenerateContentConfig(tools=[ai_types.Tool(google_search=ai_types.GoogleSearch())], temperature=0.1)
        contents = [prompt, ai_types.Part.from_bytes(data=image_bytes, mime_type='image/jpeg')]
        try:
            response = await call_gemini_with_retry(contents, config=config)
            raw_text = response.text
            ai_success = True
        except Exception as e: logger.warning(f"Gemini Vision falhou: {e}. Indo para Fallback...")
    else:
        search_keywords = ["marca", "paderri", "nestle", "bauducco", "coca", "sadia", "perdigao", "danone", "heineken", "ambev", "swift", "sear"]
        is_branded = any(k in message_text.lower() for k in search_keywords) or len(message_text.split()) > 5
        if groq_client:
            search_context = ""
            if is_branded:
                search_context = await search_fatsecret(message_text)
                if not search_context:
                    surgical_query = await generate_surgical_query(message_text)
                    search_context = await search_serper(surgical_query)
                    if not search_context:
                        search_context = await search_duckduckgo(surgical_query)
                        if not search_context or "Nenhuma informação" in search_context:
                            search_context = await search_duckduckgo(f"tabela nutricional {message_text}")
            try:
                raw_text = await call_groq_fallback(message_text, None, prompt, search_context=search_context)
                if raw_text: ai_success = True
            except Exception as e: logger.warning(f"Groq falhou: {e}")

    if not ai_success:
        logger.warning("📡 ROTA FALLBACK: Usando Gemini Flash (Cota Alta/Fallback)...")
        contents = [prompt]
        if use_vision: contents.append(ai_types.Part.from_bytes(data=image_bytes, mime_type='image/jpeg'))
        try:
            response = await ai_client.aio.models.generate_content(model=AI_MODEL_FALLBACK, contents=contents)
            raw_text = response.text
        except Exception as e2:
            logger.error(f"Apocalipse de IA: Ambas falharam: {e2}")
            return None, None, False, "ai_error", str(e2)

    try:
        cleaned_text = raw_text.strip()
        if "```json" in cleaned_text: cleaned_text = cleaned_text.split("```json")[1].split("```")[0]
        elif "```" in cleaned_text: cleaned_text = cleaned_text.split("```")[1].split("```")[0]
        result_json = json.loads(cleaned_text.strip())
        items = result_json.get("items", [])
        barcode = result_json.get("barcode")
        is_packaged = result_json.get("is_packaged", False)
        
        sanitized_items = []
        for item in items:
            if isinstance(item, dict) and item.get("alimento"):
                item["calorias"] = int(float(item.get("calorias", 0)))
                item["proteina"] = int(float(item.get("proteina", 0)))
                item["carboidratos"] = int(float(item.get("carboidratos", 0)))
                item["gorduras"] = int(float(item.get("gorduras", 0)))
                sanitized_items.append(item)

        if sanitized_items:
            if user_id not in user_history: user_history[user_id] = []
            extracted_summary = ", ".join([f"{i['alimento']} ({i.get('peso', '')})" for i in sanitized_items])
            user_history[user_id].append(f"LOGADO ANTERIORMENTE: {extracted_summary}")
            if len(user_history[user_id]) > 10: user_history[user_id] = user_history[user_id][-10:]
            if not image_bytes: AI_CACHE[cache_key] = sanitized_items

        return sanitized_items, barcode, is_packaged, None, raw_text
    except Exception as e:
        logger.error(f"Erro ao processar resposta: {e}")
        return None, None, False, "json_error", raw_text

def calculate_tdee(w, h, a, g, act, goal="manter"):
    if g == 'M': bmr = (10 * w) + (6.25 * h) - (5 * a) + 5
    else: bmr = (10 * w) + (6.25 * h) - (5 * a) - 161
    multipliers = {"sedentario": 1.2, "leve": 1.375, "moderado": 1.55, "ativo": 1.725, "atleta": 1.9}
    tdee = bmr * multipliers.get(act, 1.2)
    if goal == "perder": return round(tdee - 500)
    elif goal == "ganhar": return round(tdee + 300)
    return round(tdee)

async def generate_sarcastic_response(user_id: int, message_text: str):
    prompt = f"""
    Você é o CaloriesBot, um assistente de dieta passivo-agressivo, sarcástico e levemente julgador. 
    O usuário disse algo que não é um alimento ou uma tentativa de burlar seu sistema.
    Responda com sarcasmo fino, usando emojis, de forma curta e engraçada.
    
    USUÁRIO DISSE: "{message_text}"
    """
    try:
        response = await ai_client.aio.models.generate_content(model=AI_MODEL, contents=[prompt])
        return response.text.strip()
    except: return "Ah, que original. Outra tentativa brilhante. 🙄"
