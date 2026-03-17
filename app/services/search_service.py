import json
import httpx
import asyncio
from ddgs import DDGS
from app.config import logger, groq_client, SERPER_API_KEY, FATSECRET_CLIENT_ID, FATSECRET_CLIENT_SECRET, FATSECRET_PROXIES, fs_token, http_client

fs_lock = asyncio.Lock()

async def get_fatsecret_token():
    """Retrieves and caches an OAuth 2.0 token for the FatSecret API."""
    global fs_token
    import time
    from app.config import get_fs_client
    now = time.time()
    
    if fs_token.get("access_token") and now < fs_token.get("expires_at", 0):
        return fs_token["access_token"]
    
    async with fs_lock:
        if fs_token.get("access_token") and now < fs_token.get("expires_at", 0):
            return fs_token["access_token"]

        if not FATSECRET_CLIENT_ID or not FATSECRET_CLIENT_SECRET:
            logger.error("❌ Faltando credenciais FatSecret (ID ou SECRET)")
            return None
            
        url = "https://oauth.fatsecret.com/connect/token"
        data = {
            "grant_type": "client_credentials", 
            "scope": "basic",
            "client_id": FATSECRET_CLIENT_ID,
            "client_secret": FATSECRET_CLIENT_SECRET
        }
        # Keep auth for backward compatibility
        auth = (FATSECRET_CLIENT_ID, FATSECRET_CLIENT_SECRET)
        
        # Try with up to 3 different proxies if there's a connection error
        max_retries = 3 if FATSECRET_PROXIES else 1
        for attempt in range(max_retries):
            client = get_fs_client()
            try:
                logger.info(f"📡 FatSecret Token: Tentativa {attempt+1} (Proxy: {getattr(client, 'proxies', 'None')})")
                response = await client.post(url, data=data, auth=auth)
                if response.status_code == 200:
                    res_data = response.json()
                    fs_token["access_token"] = res_data["access_token"]
                    fs_token["expires_at"] = now + res_data["expires_in"] - 60
                    logger.info("✅ FatSecret: Token obtido com sucesso.")
                    return fs_token["access_token"]
                else:
                    logger.error(f"❌ FatSecret Token Error ({response.status_code}): {response.text}")
            except Exception as e:
                logger.warning(f"⚠️ Erro ao obter token FatSecret (Tentativa {attempt+1}): {e}")
            finally:
                if client is not http_client: await client.aclose()
    return None

async def search_fatsecret(food_name: str) -> str:
    """Searches the FatSecret database for nutritional information."""
    from app.config import get_fs_client
    token = await get_fatsecret_token()
    if not token: return ""
        
    url = "https://platform.fatsecret.com/rest/server.api"
    params = {
        "method": "foods.search",
        "search_expression": food_name,
        "format": "json",
        "region": "BR",
        "max_results": 3
    }
    headers = {"Authorization": f"Bearer {token}"}
    
    # Try with proxy rotation
    max_retries = 3 if FATSECRET_PROXIES else 1
    for attempt in range(max_retries):
        client = get_fs_client()
        try:
            logger.info(f"📡 FatSecret Search: {food_name} (Attempt {attempt+1})")
            response = await client.get(url, params=params, headers=headers)
            
            if response.status_code != 200:
                logger.error(f"❌ FatSecret Search API Error ({response.status_code}): {response.text}")
                continue # Try next proxy
                
            data = response.json()
            if "error" in data:
                logger.error(f"❌ FatSecret Search Logic Error: {data['error'].get('message')}")
                return ""
                
            foods = data.get("foods", {}).get("food", [])
            if not foods: return ""
            
            food_id = foods[0]["food_id"]
            detail_params = {"method": "food.get.v2", "food_id": food_id, "format": "json"}
            detail_res = await client.get(url, params=detail_params, headers=headers)
            
            if detail_res.status_code != 200:
                logger.error(f"❌ FatSecret Detail API Error ({detail_res.status_code}): {detail_res.text}")
                continue
                
            detail_data = detail_res.json()
            if "error" in detail_data:
                logger.error(f"❌ FatSecret Detail Logic Error: {detail_data['error'].get('message')}")
                return ""
                
            d = detail_data.get("food", {})
            name = d.get("food_name", "")
            servings = d.get("servings", {}).get("serving", [])
            if isinstance(servings, dict): servings = [servings]
            
            s_100 = next((s for s in servings if s.get("metric_serving_amount") == "100.000"), servings[0])
            
            # Persistência
            from app.database import save_to_universal_catalog
            from app.utils import get_embedding
            
            embedding = await get_embedding(name)
            save_to_universal_catalog({
                "alimento": name,
                "peso": "100g",
                "calorias": float(s_100.get('calories', 0)),
                "proteina": float(s_100.get('protein', 0)),
                "carboidratos": float(s_100.get('carbohydrate', 0)),
                "gorduras": float(s_100.get('fat', 0)),
                "is_precise": True,
                "confirmations": 10,
                "embedding": embedding
            })
            
            context = f"DADO VERIFICADO (Catálogo): {name}\n"
            context += f"- Calorias: {s_100.get('calories')} kcal\n"
            context += f"- Carboidratos: {s_100.get('carbohydrate')}g\n"
            context += f"- Proteínas: {s_100.get('protein')}g\n"
            context += f"- Gorduras: {s_100.get('fat')}g\n"
            context += f"(Valores por {s_100.get('metric_serving_amount')}{s_100.get('metric_serving_unit')})"
            return context
        except Exception as e:
            logger.warning(f"Erro no FatSecret Search (Tentativa {attempt+1}): {e}")
        finally:
            if client is not http_client: await client.aclose()
    return ""

async def search_serper(query: str) -> str:
    """Performs a real Google Search via Serper.dev."""
    if not SERPER_API_KEY: return ""
    
    url = "https://google.serper.dev/search"
    payload = json.dumps({"q": query, "gl": "br", "hl": "pt-br"})
    headers = {'X-API-KEY': SERPER_API_KEY, 'Content-Type': 'application/json'}
    
    try:
        logger.info(f"🌐 Iniciando Busca SERPER (Google): {query}")
        response = await http_client.post(url, headers=headers, data=payload)
        if response.status_code == 200:
            data = response.json()
            results = data.get('organic', [])[:3]
            if not results: return ""
            context = "\n".join([f"- {r.get('title')}: {r.get('snippet')}" for r in results])
            return context
    except Exception as e:
        logger.warning(f"Erro no Serper Search: {e}")
    return ""

async def search_duckduckgo(query: str) -> str:
    """Performs a text search via DuckDuckGo."""
    try:
        logger.info(f"🌐 Iniciando Busca DuckDuckGo: {query}")
        with DDGS() as ddgs:
            results = ddgs.text(query, max_results=3, region="br-pt")
            context = "\n".join([f"- {r['title']}: {r['body']}" for r in results])
            return context if context else "Nenhuma informação adicional encontrada via search."
    except Exception as e:
        logger.warning(f"Erro no DuckDuckGo Search: {e}")
        return "Falha ao realizar busca externa."

async def generate_surgical_query(food_name: str) -> str:
    """Uses Groq to transform a common name into a high-precision surgical query."""
    if not groq_client: return food_name
    prompt = f"""
    Transforme o nome do alimento/produto abaixo em uma QUERY DE PESQUISA CIRÚRGICA para encontrar a tabela nutricional oficial.
    REGRAS:
    - Agrupe sites com OR entre parênteses: (site:marca.com.br OR site:carrefour.com.br)
    - Termine com: "tabela nutricional" [nome simples do alimento]
    - Exemplo: (site:visconti.com.br OR site:carrefour.com.br OR site:paodeacucar.com.br) "tabela nutricional" pão de forma
    - Retorne APENAS a query, sem comentários.
    
    ALIMENTO: "{food_name}"
    """
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "llama-3.1-8b-instant",
        "messages": [{"role": "user", "content": prompt}]
    }
    try:
        response = await http_client.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload)
        if response.status_code == 200:
            data = response.json()
            return data["choices"][0]["message"]["content"].strip().replace('"', '')
        else:
            logger.error(f"Erro Direto Groq ({response.status_code}): {response.text}")
            return food_name
    except:
        return food_name

async def search_openfoodfacts(barcode: str):
    """Fetches product data from OpenFoodFacts by barcode."""
    url = f"https://world.openfoodfacts.org/api/v0/product/{barcode}.json"
    try:
        res = await http_client.get(url)
        if res.status_code == 200:
            data = res.json()
            if data.get("status") == 1:
                product = data.get("product", {})
                nutriments = product.get("nutriments", {})
                return {
                    "alimento": f"{product.get('product_name', 'Desconhecido')} ({product.get('brands', '')})",
                    "kcal_100g": nutriments.get("energy-kcal_100g", 0),
                    "prot_100g": nutriments.get("proteins_100g", 0),
                    "carb_100g": nutriments.get("carbohydrates_100g", 0),
                    "fat_100g": nutriments.get("fat_100g", 0),
                }
    except Exception as e:
        logger.error(f"Erro ao buscar OpenFoodFacts: {e}")
    return None
