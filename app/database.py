from datetime import timedelta
from app.config import logger, supabase
from app.utils import get_br_now, get_br_today_start

def log_calories(user_id: str, user_name: str, items: list):
    """Saves a list of food items to the database including macros."""
    try:
        prepared_data = []
        for item in items:
            entry = {
                "food": item.get("alimento"),
                "weight": item.get("peso"),
                "kcal": item.get("calorias"),
                "protein": item.get("proteina", 0),
                "carbs": item.get("carboidratos", 0),
                "fat": item.get("gorduras", 0),
                "meal_type": item.get("refeicao", "Outro"),
                "is_precise": item.get("is_precise", False),
                "confirmations": item.get("confirmations", 0),
                "user_id": str(user_id),
                "user_name": user_name
            }
            if item.get("embedding"):
                entry["embedding"] = item.get("embedding")
            prepared_data.append(entry)
        if prepared_data:
            res = supabase.table("logs").insert(prepared_data).execute()
            if hasattr(res, 'error') and res.error:
                logger.error(f"Erro Supabase (Insert): {res.error}")
                return False
        return True
    except Exception as e:
        logger.error(f"Erro ao salvar no Supabase (Exception): {e}")
        return False

def save_to_universal_catalog(item: dict):
    """Saves a verified food item to the dedicated universal_catalog table."""
    try:
        food_name = item.get("alimento")
        # Verifica duplicata na tabela nova
        check = supabase.table("universal_catalog") \
            .select("id") \
            .eq("food", food_name) \
            .limit(1) \
            .execute()
            
        if check.data:
            logger.info(f"⏭️ Item '{food_name}' já existe no catálogo universal. Pulando.")
            return True
            
        data = {
            "food": food_name,
            "kcal": float(item.get("calorias", 0)),
            "protein": float(item.get("proteina", 0)),
            "carbs": float(item.get("carboidratos", 0)),
            "fat": float(item.get("gorduras", 0)),
            "serving_size": item.get("peso", "100g"),
            "embedding": item.get("embedding"),
            "confirmations": item.get("confirmations", 1),
            "is_precise": item.get("is_precise", True)
        }
        
        res = supabase.table("universal_catalog").insert(data).execute()
        if hasattr(res, 'error') and res.error:
            logger.error(f"Erro Supabase (Universal Insert): {res.error}")
            return False
        logger.info(f"✅ Item '{food_name}' salvo no NOVO Catálogo Universal.")
        return True
    except Exception as e:
        logger.error(f"Erro ao salvar no catálogo universal (Exception): {e}")
        return False

def search_universal_catalog(query_embedding: list, threshold: float = 0.8):
    """Performs semantic search in the dedicated universal_catalog table."""
    try:
        # RPC call for the match_food_catalog function created in SQL
        res = supabase.rpc("match_food_catalog", {
            "query_embedding": query_embedding,
            "match_threshold": threshold,
            "match_count": 1
        }).execute()
        
        if res and hasattr(res, 'data') and res.data:
            item = res.data[0]
            return [{
                "alimento": item.get("food", "Desconhecido"),
                "peso": item.get("serving_size", "100g"),
                "calorias": item.get("kcal", 0),
                "proteina": item.get("protein", 0),
                "carboidratos": item.get("carbs", 0),
                "gorduras": item.get("fat", 0),
                "is_precise": True,
                "is_universal": True,
                "similarity": item.get("similarity", 0)
            }]
        return None
    except Exception as e:
        logger.error(f"Erro na busca vetorial do catálogo: {e}")
        return None

def get_user_profile(user_id: str):
    """Fetches the user's profile and TDEE."""
    try:
        res = supabase.table("profiles").select("*").eq("user_id", str(user_id)).execute()
        return res.data[0] if res.data else None
    except Exception as e:
        logger.error(f"Erro ao buscar perfil: {e}")
        return None

def get_daily_stats(user_id: str):
    """Calculates total calories and macros for the current day."""
    try:
        today_br_start = get_br_today_start()
        response = supabase.table("logs") \
            .select("kcal, protein, carbs, fat") \
            .eq("user_id", str(user_id)) \
            .gte("created_at", today_br_start) \
            .execute()
            
        total_kcal = sum(item.get('kcal', 0) for item in response.data)
        total_prot = sum(item.get('protein', 0) for item in response.data)
        total_carb = sum(item.get('carbs', 0) for item in response.data)
        total_fat = sum(item.get('fat', 0) for item in response.data)
        
        return {
            "kcal": total_kcal,
            "protein": total_prot,
            "carbs": total_carb,
            "fat": total_fat
        }
    except Exception as e:
        logger.error(f"Erro ao calcular estatísticas diárias: {e}")
        return {"kcal": 0, "protein": 0, "carbs": 0, "fat": 0}

def get_daily_total(user_id: str):
    """Legacy helper: returns only kcal."""
    return get_daily_stats(user_id)["kcal"]

def get_report_data(user_id: str, days: int):
    """Aggregates data for periodic reports."""
    try:
        now_br = get_br_now()
        start_date = (now_br - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00-03:00")
        res = supabase.table("logs") \
            .select("created_at, kcal") \
            .eq("user_id", str(user_id)) \
            .gte("created_at", start_date) \
            .execute()
        if hasattr(res, 'error') and res.error:
            logger.error(f"Erro Supabase (Report): {res.error}")
            return []
        return res.data or []
    except Exception as e:
        logger.error(f"Erro ao buscar dados do relatório: {e}")
        return []

def delete_last_log(user_id: str):
    """Deletes the most recent meal log."""
    try:
        res = supabase.table("logs") \
            .select("id, created_at") \
            .eq("user_id", str(user_id)) \
            .order("created_at", desc=True) \
            .limit(1) \
            .execute()
        if res.data:
            created_at = res.data[0]['created_at']
            supabase.table("logs").delete() \
                .eq("user_id", str(user_id)) \
                .eq("created_at", created_at) \
                .execute()
            return True
        return False
    except Exception as e:
        logger.error(f"Erro ao deletar último log: {e}")
        return False

def delete_today_logs(user_id: str):
    """Deletes all logs from the current day."""
    try:
        today_br_start = get_br_today_start()
        supabase.table("logs").delete() \
            .eq("user_id", str(user_id)) \
            .gte("created_at", today_br_start) \
            .execute()
        return True
    except Exception as e:
        logger.error(f"Erro ao deletar logs de hoje: {e}")
        return False

def delete_entire_profile(user_id: str):
    """Deletes profile and all logs for a user."""
    try:
        supabase.table("logs").delete().eq("user_id", str(user_id)).execute()
        supabase.table("profiles").delete().eq("user_id", str(user_id)).execute()
        return True
    except Exception as e:
        logger.error(f"Erro ao deletar perfil completo: {e}")
        return False

def delete_log_by_id(user_id: str, log_id: int):
    """Deletes a specific log by its ID."""
    try:
        supabase.table("logs").delete() \
            .eq("user_id", str(user_id)) \
            .eq("id", log_id) \
            .execute()
        return True
    except Exception as e:
        logger.error(f"Erro ao deletar log por ID: {e}")
        return False

def search_food_history(user_id: str, food_query: str):
    """Searches for a historical log entry."""
    try:
        # Pessoal Exato
        res = supabase.table("logs") \
            .select("food, weight, kcal, protein, carbs, fat, meal_type, is_precise") \
            .eq("user_id", str(user_id)) \
            .ilike("food", f"{food_query}") \
            .order("created_at", desc=True) \
            .limit(1) \
            .execute()
        
        # Pessoal Aproximado
        if not res.data:
            res = supabase.table("logs") \
                .select("food, weight, kcal, protein, carbs, fat, meal_type, is_precise") \
                .eq("user_id", str(user_id)) \
                .ilike("food", f"%{food_query}%") \
                .order("created_at", desc=True) \
                .limit(1) \
                .execute()
            if res.data: res.data[0]["is_approximate"] = True

        # Catálogo Universal (Fallback se não achar no pessoal)
        is_universal = False
        if not res or not res.data:
            res = supabase.table("logs") \
                .select("food, weight, kcal, protein, carbs, fat, meal_type, is_precise, confirmations") \
                .ilike("food", f"{food_query}") \
                .or_("is_precise.eq.true,confirmations.gte.5") \
                .order("created_at", desc=True) \
                .limit(1) \
                .execute()
            if res and res.data:
                is_universal = True
                logger.info(f"Item encontrado no Catálogo Universal: {food_query}")

        if res and hasattr(res, 'data') and res.data:
            item = res.data[0]
            return [{
                "alimento": item.get("food", food_query),
                "peso": item.get("weight", "100g"),
                "calorias": item.get("kcal", 0),
                "proteina": item.get("protein", 0),
                "carboidratos": item.get("carbs", 0),
                "gorduras": item.get("fat", 0),
                "refeicao": item.get("meal_type", "Outro"),
                "is_precise": item.get("is_precise", False),
                "is_approximate": item.get("is_approximate", False),
                "is_universal": is_universal
            }]
        return None
    except Exception as e:
        logger.error(f"Erro ao buscar no histórico: {e}")
        return None
