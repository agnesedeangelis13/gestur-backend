from datetime import datetime
from supabase import create_client
import os
from dotenv import load_dotenv
from imposta_soggiorno_service import ottieni_o_crea_piano_sviluppo_locale_attivo

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def get_fonti_entrata(comune_id):
    try:
        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id)
        fonti_resp = supabase.table("fonti_entrata_turistica").select("*") \
            .eq("piano_id", piano["id"]).eq("attiva", True).order("creato_il").execute()
        return {
            "piano_id": piano["id"],
            "comune_id": comune_id,
            "fonti": fonti_resp.data or [],
        }
    except Exception as e:
        print(f"Errore get fonti entrata comune {comune_id}: {e}")
        return {"errore": str(e)}


def crea_fonte_entrata(payload):
    try:
        comune_id_str = payload.get("comune_id")
        nome_fonte = payload.get("nome_fonte")

        if not comune_id_str or not nome_fonte or not nome_fonte.strip():
            return {"errore": "comune_id e nome_fonte sono obbligatori"}

        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id_str)

        esistente_resp = supabase.table("fonti_entrata_turistica").select("id") \
            .eq("piano_id", piano["id"]).eq("nome_fonte", nome_fonte.strip()).eq("attiva", True).execute()
        if esistente_resp.data:
            return {"errore": "Esiste già una fonte di entrata con questo nome"}

        record = {
            "piano_id": piano["id"],
            "comune_id": comune_id_str,
            "nome_fonte": nome_fonte.strip(),
        }
        creato_resp = supabase.table("fonti_entrata_turistica").insert(record).execute()

        return {"status": "salvato", "fonte": creato_resp.data[0] if creato_resp.data else None}
    except Exception as e:
        print(f"Errore creazione fonte entrata: {e}")
        return {"errore": str(e)}


def elimina_fonte_entrata(fonte_id):
    try:
        supabase.table("fonti_entrata_turistica").update({"attiva": False}).eq("id", fonte_id).execute()
        return {"status": "disattivata"}
    except Exception as e:
        print(f"Errore eliminazione fonte entrata {fonte_id}: {e}")
        return {"errore": str(e)}


def get_entrate_aggiuntive_mese(comune_id, anno, mese):
    try:
        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id)

        fonti_resp = supabase.table("fonti_entrata_turistica").select("*") \
            .eq("piano_id", piano["id"]).eq("attiva", True).execute()
        fonti = fonti_resp.data or []
        nomi_fonte = {f["id"]: f["nome_fonte"] for f in fonti}

        entrate_resp = supabase.table("entrate_turistiche_aggiuntive").select("*") \
            .eq("piano_id", piano["id"]).eq("anno", anno).eq("mese", mese).execute()
        entrate = entrate_resp.data or []

        righe = []
        totale_mese = 0
        for f in fonti:
            entrata = next((e for e in entrate if e["fonte_id"] == f["id"]), None)
            importo = entrata["importo_incassato"] if entrata else None
            if importo is not None:
                totale_mese += importo
            righe.append({
                "fonte_id": f["id"],
                "nome_fonte": f["nome_fonte"],
                "importo_incassato": importo,
                "note": entrata["note"] if entrata else None,
            })

        return {
            "piano_id": piano["id"],
            "comune_id": comune_id,
            "anno": anno,
            "mese": mese,
            "righe": righe,
            "totale_mese": round(totale_mese, 2),
            "n_fonti_configurate": len(fonti),
        }
    except Exception as e:
        print(f"Errore entrate aggiuntive comune {comune_id}: {e}")
        return {"errore": str(e)}


def crea_entrata_aggiuntiva(payload):
    try:
        comune_id_str = payload.get("comune_id")
        fonte_id = payload.get("fonte_id")
        anno = payload.get("anno")
        mese = payload.get("mese")
        importo_incassato = payload.get("importo_incassato")
        note = payload.get("note")

        if not comune_id_str or not fonte_id or anno is None or mese is None or importo_incassato is None:
            return {"errore": "comune_id, fonte_id, anno, mese e importo_incassato sono obbligatori"}

        if importo_incassato < 0:
            return {"errore": "L'importo incassato non può essere negativo"}

        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id_str)

        record = {
            "piano_id": piano["id"],
            "comune_id": comune_id_str,
            "fonte_id": fonte_id,
            "anno": anno,
            "mese": mese,
            "importo_incassato": importo_incassato,
            "note": note,
        }
        supabase.table("entrate_turistiche_aggiuntive").upsert(
            record, on_conflict="fonte_id,anno,mese"
        ).execute()

        return {"status": "salvato"}
    except Exception as e:
        print(f"Errore creazione entrata aggiuntiva: {e}")
        return {"errore": str(e)}


def get_totale_altre_entrate_mese(comune_id, anno, mese):
    try:
        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id)
        entrate_resp = supabase.table("entrate_turistiche_aggiuntive").select("importo_incassato") \
            .eq("piano_id", piano["id"]).eq("anno", anno).eq("mese", mese).execute()
        entrate = entrate_resp.data or []
        if not entrate:
            return None
        totale = sum(e["importo_incassato"] or 0 for e in entrate)
        return round(totale, 2)
    except Exception as e:
        print(f"Errore totale altre entrate comune {comune_id}: {e}")
        return None