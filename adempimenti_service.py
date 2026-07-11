from datetime import datetime, date
from collections import defaultdict
from supabase import create_client
import os
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

TIPOLOGIE_ADEMPIMENTO = ["SIAE", "Autorizzazione", "Piano di Sicurezza", "Assicurazione", "Permesso Suolo Pubblico", "Altro"]
STATI_ADEMPIMENTO = ("Da fare", "In corso", "Completato", "Non applicabile")
TIPI_ORIGINE_VALIDI = ("evento", "mercato")


def crea_adempimento(payload):
    try:
        comune_id_str = payload.get("comune_id")
        tipo_origine = payload.get("tipo_origine")
        evento_id = payload.get("evento_id")
        mercato_id = payload.get("mercato_id")
        tipologia = payload.get("tipologia")
        descrizione = payload.get("descrizione")
        richiede_documento = payload.get("richiede_documento", True)
        data_scadenza = payload.get("data_scadenza")

        if not comune_id_str or not tipologia:
            return {"errore": "comune_id e tipologia sono obbligatori"}
        if tipo_origine not in TIPI_ORIGINE_VALIDI:
            return {"errore": "tipo_origine non valido"}
        if tipo_origine == "evento" and not evento_id:
            return {"errore": "evento_id obbligatorio per tipo_origine evento"}
        if tipo_origine == "mercato" and not mercato_id:
            return {"errore": "mercato_id obbligatorio per tipo_origine mercato"}

        record = {
            "comune_id": comune_id_str,
            "tipo_origine": tipo_origine,
            "evento_id": evento_id,
            "mercato_id": mercato_id,
            "tipologia": tipologia,
            "descrizione": descrizione,
            "richiede_documento": bool(richiede_documento),
            "data_scadenza": data_scadenza,
            "stato": "Da fare",
        }
        creato_resp = supabase.table("checklist_adempimenti").insert(record).execute()
        return {"status": "salvato", "adempimento": creato_resp.data[0] if creato_resp.data else None}
    except Exception as e:
        print(f"Errore creazione adempimento: {e}")
        return {"errore": str(e)}


def get_checklist(tipo_origine, origine_id):
    try:
        campo = "evento_id" if tipo_origine == "evento" else "mercato_id"
        resp = supabase.table("checklist_adempimenti").select("*").eq(campo, origine_id).order("creato_il").execute()
        return {"tipo_origine": tipo_origine, "origine_id": origine_id, "adempimenti": resp.data or []}
    except Exception as e:
        print(f"Errore get checklist {tipo_origine} {origine_id}: {e}")
        return {"errore": str(e)}


def aggiorna_stato_adempimento(adempimento_id, nuovo_stato):
    try:
        if nuovo_stato not in STATI_ADEMPIMENTO:
            return {"errore": "Stato non valido"}
        supabase.table("checklist_adempimenti").update({"stato": nuovo_stato}).eq("id", adempimento_id).execute()
        return {"status": "aggiornato"}
    except Exception as e:
        print(f"Errore aggiornamento stato adempimento {adempimento_id}: {e}")
        return {"errore": str(e)}


def salva_documento_adempimento(adempimento_id, file_path, file_nome):
    try:
        if not file_path or not file_nome:
            return {"errore": "file_path e file_nome sono obbligatori"}
        supabase.table("checklist_adempimenti").update({
            "file_path": file_path,
            "file_nome": file_nome,
            "data_caricamento": datetime.now().isoformat(),
            "stato": "Completato",
        }).eq("id", adempimento_id).execute()
        return {"status": "salvato"}
    except Exception as e:
        print(f"Errore salvataggio documento adempimento {adempimento_id}: {e}")
        return {"errore": str(e)}


def elimina_documento_adempimento(adempimento_id):
    try:
        supabase.table("checklist_adempimenti").update({
            "file_path": None, "file_nome": None, "data_caricamento": None, "stato": "Da fare",
        }).eq("id", adempimento_id).execute()
        return {"status": "documento rimosso"}
    except Exception as e:
        print(f"Errore rimozione documento adempimento {adempimento_id}: {e}")
        return {"errore": str(e)}


def elimina_adempimento(adempimento_id):
    try:
        supabase.table("checklist_adempimenti").delete().eq("id", adempimento_id).execute()
        return {"status": "eliminato"}
    except Exception as e:
        print(f"Errore eliminazione adempimento {adempimento_id}: {e}")
        return {"errore": str(e)}


def verifica_checklist_completa(tipo_origine, origine_id):
    try:
        campo = "evento_id" if tipo_origine == "evento" else "mercato_id"
        resp = supabase.table("checklist_adempimenti").select("*").eq(campo, origine_id).execute()
        adempimenti = resp.data or []

        mancanti = []
        for a in adempimenti:
            if a["stato"] == "Non applicabile":
                continue
            if a["stato"] != "Completato":
                mancanti.append(f"{a['tipologia']} (stato: {a['stato']})")
            elif a.get("richiede_documento") and not a.get("file_path"):
                mancanti.append(f"{a['tipologia']} (documento non caricato)")

        return {"completa": len(mancanti) == 0, "mancanti": mancanti, "n_totale": len(adempimenti)}
    except Exception as e:
        print(f"Errore verifica checklist {tipo_origine} {origine_id}: {e}")
        return {"errore": str(e)}


def get_scadenziario(comune_id):
    try:
        resp = supabase.table("checklist_adempimenti").select("*") \
            .eq("comune_id", comune_id).not_.is_("data_scadenza", "null").neq("stato", "Non applicabile").execute()
        adempimenti = resp.data or []

        eventi_ids = [a["evento_id"] for a in adempimenti if a.get("evento_id")]
        mercati_ids = [a["mercato_id"] for a in adempimenti if a.get("mercato_id")]

        eventi_map = {}
        if eventi_ids:
            eventi_resp = supabase.table("richieste_eventi").select("id, nome_evento").in_("id", eventi_ids).execute()
            eventi_map = {e["id"]: e["nome_evento"] for e in (eventi_resp.data or [])}

        mercati_map = {}
        if mercati_ids:
            mercati_resp = supabase.table("mercati_richiesti").select("id, titolo").in_("id", mercati_ids).execute()
            mercati_map = {m["id"]: m["titolo"] for m in (mercati_resp.data or [])}

        oggi = date.today().isoformat()
        for a in adempimenti:
            if a.get("evento_id"):
                a["titolo_origine"] = eventi_map.get(a["evento_id"], "Evento")
            elif a.get("mercato_id"):
                a["titolo_origine"] = mercati_map.get(a["mercato_id"], "Mercato")
            else:
                a["titolo_origine"] = "—"

            if a["stato"] == "Completato":
                a["urgenza"] = "ok"
            elif a["data_scadenza"] < oggi:
                a["urgenza"] = "scaduto"
            else:
                giorni_mancanti = (date.fromisoformat(a["data_scadenza"]) - date.today()).days
                a["urgenza"] = "in_scadenza" if giorni_mancanti <= 7 else "ok"

        adempimenti.sort(key=lambda a: a["data_scadenza"])
        return {"comune_id": comune_id, "adempimenti": adempimenti}
    except Exception as e:
        print(f"Errore scadenziario comune {comune_id}: {e}")
        return {"errore": str(e)}


def get_statistiche_adempimenti(comune_id):
    try:
        resp = supabase.table("checklist_adempimenti").select("*").eq("comune_id", comune_id).execute()
        adempimenti = resp.data or []

        conteggio_stato = defaultdict(int)
        for a in adempimenti:
            conteggio_stato[a["stato"]] += 1
        per_stato = [{"tipo": stato, "valore": conteggio} for stato, conteggio in conteggio_stato.items()]

        conteggio_tipologia = defaultdict(int)
        for a in adempimenti:
            conteggio_tipologia[a["tipologia"]] += 1
        per_tipologia = [
            {"tipo": tipo, "valore": conteggio}
            for tipo, conteggio in sorted(conteggio_tipologia.items(), key=lambda x: x[1], reverse=True)
        ]

        oggi = date.today().isoformat()
        n_scaduti = len([a for a in adempimenti if a.get("data_scadenza") and a["data_scadenza"] < oggi and a["stato"] != "Completato"])
        n_documenti_mancanti = len([a for a in adempimenti if a["stato"] != "Completato" and a["stato"] != "Non applicabile" and a.get("richiede_documento")])

        return {
            "comune_id": comune_id,
            "n_totale": len(adempimenti),
            "n_completati": conteggio_stato.get("Completato", 0),
            "n_scaduti": n_scaduti,
            "n_documenti_mancanti": n_documenti_mancanti,
            "per_stato": per_stato,
            "per_tipologia": per_tipologia,
            "nota_metodologica": (
                "Un adempimento è considerato scaduto se ha una data di scadenza superata e non è ancora "
                "completato. L'approvazione finale di eventi e mercati collegati viene bloccata finché non tutti "
                "gli adempimenti richiesti risultano completati, con documento caricato dove previsto."
            ),
        }
    except Exception as e:
        print(f"Errore statistiche adempimenti comune {comune_id}: {e}")
        return {"errore": str(e)}