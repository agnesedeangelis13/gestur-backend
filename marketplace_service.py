from datetime import datetime
from supabase import create_client
import os
from dotenv import load_dotenv
from imposta_soggiorno_service import ottieni_o_crea_piano_sviluppo_locale_attivo

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

CATEGORIE_FORNITORI_DEFAULT = [
    "Guida turistica",
    "Cantina ed enogastronomia",
    "Artigiano locale",
    "Ristorazione",
    "Associazione culturale",
    "Alloggio e ricettività",
    "Trasporto locale",
    "Altro",
]

COMMISSIONE_DEFAULT_PCT = 15.0
COMMISSIONE_WELFARE_DEFAULT_PCT = 8.0


def get_categorie_fornitori(comune_id):
    try:
        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id)
        custom_resp = supabase.table("categorie_fornitori_custom").select("*") \
            .eq("piano_id", piano["id"]).execute()
        custom = [c["nome_categoria"] for c in (custom_resp.data or [])]
        return {
            "piano_id": piano["id"],
            "comune_id": comune_id,
            "categorie_default": CATEGORIE_FORNITORI_DEFAULT,
            "categorie_personalizzate": custom,
            "categorie_tutte": CATEGORIE_FORNITORI_DEFAULT + custom,
        }
    except Exception as e:
        print(f"Errore get categorie fornitori comune {comune_id}: {e}")
        return {"errore": str(e)}


def crea_categoria_fornitore(payload):
    try:
        comune_id_str = payload.get("comune_id")
        nome_categoria = payload.get("nome_categoria")

        if not comune_id_str or not nome_categoria or not nome_categoria.strip():
            return {"errore": "comune_id e nome_categoria sono obbligatori"}

        nome_pulito = nome_categoria.strip()
        if nome_pulito in CATEGORIE_FORNITORI_DEFAULT:
            return {"errore": "Questa categoria esiste già tra quelle predefinite"}

        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id_str)

        esistente_resp = supabase.table("categorie_fornitori_custom").select("id") \
            .eq("piano_id", piano["id"]).eq("nome_categoria", nome_pulito).execute()
        if esistente_resp.data:
            return {"errore": "Categoria già presente"}

        record = {"piano_id": piano["id"], "comune_id": comune_id_str, "nome_categoria": nome_pulito}
        creato_resp = supabase.table("categorie_fornitori_custom").insert(record).execute()

        return {"status": "salvato", "categoria": creato_resp.data[0] if creato_resp.data else None}
    except Exception as e:
        print(f"Errore creazione categoria fornitore: {e}")
        return {"errore": str(e)}


def get_commissione(comune_id):
    try:
        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id)
        commissione_pct = piano.get("commissione_gestione_pct")
        commissione_welfare_pct = piano.get("commissione_gestione_welfare_pct")
        return {
            "piano_id": piano["id"],
            "comune_id": comune_id,
            "commissione_pct": commissione_pct if commissione_pct is not None else COMMISSIONE_DEFAULT_PCT,
            "commissione_welfare_pct": commissione_welfare_pct if commissione_welfare_pct is not None else COMMISSIONE_WELFARE_DEFAULT_PCT,
            "configurata": commissione_pct is not None,
        }
    except Exception as e:
        print(f"Errore get commissione comune {comune_id}: {e}")
        return {"errore": str(e)}


def aggiorna_commissione(payload):
    try:
        comune_id_str = payload.get("comune_id")
        commissione_pct = payload.get("commissione_pct")
        commissione_welfare_pct = payload.get("commissione_welfare_pct")

        if not comune_id_str:
            return {"errore": "comune_id è obbligatorio"}

        aggiornamento = {}
        if commissione_pct is not None:
            if commissione_pct < 0 or commissione_pct > 100:
                return {"errore": "commissione_pct deve essere tra 0 e 100"}
            aggiornamento["commissione_gestione_pct"] = commissione_pct
        if commissione_welfare_pct is not None:
            if commissione_welfare_pct < 0 or commissione_welfare_pct > 100:
                return {"errore": "commissione_welfare_pct deve essere tra 0 e 100"}
            aggiornamento["commissione_gestione_welfare_pct"] = commissione_welfare_pct

        if not aggiornamento:
            return {"errore": "Nessun valore da aggiornare"}

        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id_str)
        supabase.table("piani_sviluppo_locale").update(aggiornamento).eq("id", piano["id"]).execute()

        return {"status": "salvato"}
    except Exception as e:
        print(f"Errore aggiornamento commissione: {e}")
        return {"errore": str(e)}


def get_fornitori(comune_id, categoria=None):
    try:
        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id)
        query = supabase.table("fornitori_locali").select("*").eq("piano_id", piano["id"]).eq("attivo", True)
        if categoria:
            query = query.eq("categoria", categoria)
        fornitori_resp = query.order("creato_il", desc=True).execute()
        fornitori = fornitori_resp.data or []

        fornitore_ids = [f["id"] for f in fornitori]
        esperienze_resp = supabase.table("esperienze_fornitore").select("*") \
            .in_("fornitore_id", fornitore_ids).eq("attiva", True).execute() if fornitore_ids else None
        tutte_esperienze = esperienze_resp.data if esperienze_resp else []

        for f in fornitori:
            f["esperienze"] = [e for e in tutte_esperienze if e["fornitore_id"] == f["id"]]

        return {"piano_id": piano["id"], "comune_id": comune_id, "fornitori": fornitori}
    except Exception as e:
        print(f"Errore get fornitori comune {comune_id}: {e}")
        return {"errore": str(e)}


def crea_fornitore(payload):
    try:
        comune_id_str = payload.get("comune_id")
        categoria = payload.get("categoria")
        nome_fornitore = payload.get("nome_fornitore")
        descrizione = payload.get("descrizione")
        contatti = payload.get("contatti")
        partecipa_welfare_locale = payload.get("partecipa_welfare_locale", False)

        if not comune_id_str or not categoria or not nome_fornitore or not nome_fornitore.strip():
            return {"errore": "comune_id, categoria e nome_fornitore sono obbligatori"}

        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id_str)

        record = {
            "piano_id": piano["id"],
            "comune_id": comune_id_str,
            "categoria": categoria,
            "nome_fornitore": nome_fornitore.strip(),
            "descrizione": descrizione,
            "contatti": contatti,
            "partecipa_welfare_locale": bool(partecipa_welfare_locale),
        }
        creato_resp = supabase.table("fornitori_locali").insert(record).execute()

        return {"status": "salvato", "fornitore": creato_resp.data[0] if creato_resp.data else None}
    except Exception as e:
        print(f"Errore creazione fornitore: {e}")
        return {"errore": str(e)}


def aggiorna_fornitore(fornitore_id, payload):
    try:
        campi_consentiti = {"categoria", "nome_fornitore", "descrizione", "contatti", "partecipa_welfare_locale"}
        aggiornamento = {k: v for k, v in payload.items() if k in campi_consentiti}
        if not aggiornamento:
            return {"errore": "Nessun campo valido da aggiornare"}
        supabase.table("fornitori_locali").update(aggiornamento).eq("id", fornitore_id).execute()
        return {"status": "aggiornato"}
    except Exception as e:
        print(f"Errore aggiornamento fornitore {fornitore_id}: {e}")
        return {"errore": str(e)}


def elimina_fornitore(fornitore_id):
    try:
        supabase.table("fornitori_locali").update({"attivo": False}).eq("id", fornitore_id).execute()
        return {"status": "disattivato"}
    except Exception as e:
        print(f"Errore eliminazione fornitore {fornitore_id}: {e}")
        return {"errore": str(e)}


def crea_esperienza(payload):
    try:
        fornitore_id = payload.get("fornitore_id")
        comune_id_str = payload.get("comune_id")
        nome_esperienza = payload.get("nome_esperienza")
        descrizione = payload.get("descrizione")
        prezzo = payload.get("prezzo")
        giorni_disponibili = payload.get("giorni_disponibili")
        durata_minuti = payload.get("durata_minuti")

        if not fornitore_id or not comune_id_str or not nome_esperienza or prezzo is None:
            return {"errore": "fornitore_id, comune_id, nome_esperienza e prezzo sono obbligatori"}

        if prezzo < 0:
            return {"errore": "Il prezzo non può essere negativo"}

        record = {
            "fornitore_id": fornitore_id,
            "comune_id": comune_id_str,
            "nome_esperienza": nome_esperienza.strip(),
            "descrizione": descrizione,
            "prezzo": prezzo,
            "giorni_disponibili": giorni_disponibili,
            "durata_minuti": durata_minuti,
        }
        creato_resp = supabase.table("esperienze_fornitore").insert(record).execute()

        return {"status": "salvato", "esperienza": creato_resp.data[0] if creato_resp.data else None}
    except Exception as e:
        print(f"Errore creazione esperienza: {e}")
        return {"errore": str(e)}


def elimina_esperienza(esperienza_id):
    try:
        supabase.table("esperienze_fornitore").update({"attiva": False}).eq("id", esperienza_id).execute()
        return {"status": "disattivata"}
    except Exception as e:
        print(f"Errore eliminazione esperienza {esperienza_id}: {e}")
        return {"errore": str(e)}


def get_esperienza_by_id(esperienza_id):
    try:
        resp = supabase.table("esperienze_fornitore").select("*, fornitori_locali(nome_fornitore, categoria, partecipa_welfare_locale)") \
            .eq("id", esperienza_id).single().execute()
        return resp.data
    except Exception:
        return None