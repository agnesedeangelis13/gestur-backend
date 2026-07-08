from datetime import datetime
from supabase import create_client
import os
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

TIPI_BIGLIETTO_VALIDI = ("intero", "ridotto", "gratuito")


def get_categorie_biglietto(sito_id):
    try:
        categorie_resp = supabase.table("categorie_biglietto").select("*") \
            .eq("sito_id", sito_id).eq("attiva", True).order("tipo_biglietto").execute()
        return {"sito_id": sito_id, "categorie": categorie_resp.data or []}
    except Exception as e:
        print(f"Errore get categorie biglietto sito {sito_id}: {e}")
        return {"errore": str(e)}


def crea_categoria_biglietto(payload):
    try:
        sito_id = payload.get("sito_id")
        tipo_biglietto = payload.get("tipo_biglietto")
        nome_categoria = payload.get("nome_categoria")
        prezzo = payload.get("prezzo")

        if not sito_id or not tipo_biglietto or not nome_categoria or not nome_categoria.strip() or prezzo is None:
            return {"errore": "sito_id, tipo_biglietto, nome_categoria e prezzo sono obbligatori"}

        if tipo_biglietto not in TIPI_BIGLIETTO_VALIDI:
            return {"errore": "tipo_biglietto non valido"}

        if tipo_biglietto == "gratuito" and prezzo != 0:
            return {"errore": "Una categoria gratuita deve avere prezzo 0"}

        if prezzo < 0:
            return {"errore": "Il prezzo non può essere negativo"}

        record = {
            "sito_id": sito_id,
            "tipo_biglietto": tipo_biglietto,
            "nome_categoria": nome_categoria.strip(),
            "prezzo": prezzo,
        }
        creato_resp = supabase.table("categorie_biglietto").insert(record).execute()

        return {"status": "salvato", "categoria": creato_resp.data[0] if creato_resp.data else None}
    except Exception as e:
        print(f"Errore creazione categoria biglietto: {e}")
        return {"errore": str(e)}


def elimina_categoria_biglietto(categoria_id):
    try:
        supabase.table("categorie_biglietto").update({"attiva": False}).eq("id", categoria_id).execute()
        return {"status": "disattivata"}
    except Exception as e:
        print(f"Errore eliminazione categoria biglietto {categoria_id}: {e}")
        return {"errore": str(e)}