import re
from datetime import datetime, timedelta, UTC
from typing import Optional
from app.config import logger, ai_client

def get_br_now():
    """Returns the current datetime in Brazil (UTC-3)."""
    return datetime.now(UTC) - timedelta(hours=3)

def get_br_today_start():
    """Returns the start of today in Brazil (00:00:00) in ISO format with offset."""
    return get_br_now().replace(hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT00:00:00-03:00")

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
        r"jailbreak"
    ]
    for p in patterns:
        if re.search(p, text_lower):
            return True
    return False

def is_apology(text: str) -> bool:
    if not text: return False
    text_lower = text.lower()
    apology_words = ["desculpa", "perdão", "perdao", "foi mal", "sinto muito", "me desculpe"]
    return any(word in text_lower for word in apology_words)

def extract_amount(text: str) -> Optional[float]:
    """Extracts weight (g) or volume (ml) from text. Converts kg/l to g/ml."""
    if not text: return None
    t = str(text).lower().replace(",", ".")
    
    household = [
        (r"(colher\s*de\s*sopa|c\.\s*sopa|colher\s*s)", 15),
        (r"(colher\s*de\s*sobremesa)", 10),
        (r"(colher\s*de\s*ch[aá]|colher\s*c)", 5),
        (r"(colher\s*de\s*caf[eé])", 2),
        (r"(x[ií]cara)", 200),
        (r"(copo|c\.)", 200),
        (r"(fatia|f\.)", 30)
    ]

    fractions = {
        "meio": 0.5, "metade": 0.5, "1/2": 0.5,
        "um quarto": 0.25, "1/4": 0.25,
        "um terço": 0.33, "1/3": 0.33,
        "três quartos": 0.75, "3/4": 0.75
    }
    for word, mult in fractions.items():
        if word in t:
            for pattern, factor in household:
                if re.search(rf"{word}\s*{pattern}", t):
                    return mult * factor
    
    for pattern, factor in household:
        match = re.search(rf"(\d+[\.,]?\d*)?\s*{pattern}", t)
        if match:
            qty = float(match.group(1)) if match.group(1) else 1.0
            return qty * factor

    kg_match = re.search(r"(\d+[\.,]?\d*)\s*(kg|kilo|l$|litro|l\s)", t)
    if kg_match:
        try: return float(kg_match.group(1)) * 1000
        except: pass

    g_match = re.search(r"(\d+[\.,]?\d*)\s*(g|gr|ml)", t)
    if g_match:
        try: return float(g_match.group(1))
        except: pass
    
    num_match = re.search(r"^(\d+[\.,]?\d*)$", t)
    if num_match:
        try: return float(num_match.group(1))
        except: pass
    return None

async def get_embedding(text: str):
    """Generates a vector embedding for a piece of text using Gemini."""
    logger.info(f"📡 SERVIÇO: Gerando embedding para: {text[:20]}...")
    try:
        # Tenta usar o modelo mais recente
        res = await ai_client.aio.models.embed_content(model='text-embedding-004', contents=text)
        return res.embeddings[0].values
    except Exception as e:
        logger.warning(f"Erro no text-embedding-004: {e}. Tentando fallback models/embedding-001...")
        try:
            # Fallback com prefixo explícito caso o SDK precise
            res = await ai_client.aio.models.embed_content(model='models/embedding-001', contents=text)
            return res.embeddings[0].values
        except Exception as e2:
            logger.error(f"Falha total de embedding: {e2}")
            return None

def parse_numeric(text: str) -> Optional[float]:
    """Robust parser for numeric inputs like '75,5', '175cm', '80kg'."""
    if not text: return None
    t = text.lower().replace(',', '.')
    match = re.search(r"(\d+[\.,]?\d*)", t)
    if match:
        try: return float(match.group(1))
        except: return None
    return None
