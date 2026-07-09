from collections import defaultdict
from datetime import datetime
import random
import string
from supabase import create_client
import os
from dotenv import load_dotenv
from imposta_soggiorno_service import ottieni_o_crea_piano_sviluppo_locale_attivo

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

CATEGORIE_BENEFICIARIO = ["Scuole", "Centri anziani", "Fasce svantaggiate", "Associazioni locali", "Altro"]
STATI_PASS = ("emesso", "utilizzato", "scaduto")


def _genera_codice_pass():
    caratteri = string.ascii_uppercase + string.digits
    return "SOSP-" + "".join(random.choices(caratteri, k=6))


def crea_donazione(payload):
    try:
        comune_id_str = payload.get("comune_id")
        nome_donatore = payload.get("nome_donatore")
        contatti_donatore = payload.get("contatti_donatore")
        pacchetto_id = payload.get("pacchetto_id")
        tipo_generico = payload.get("tipo_generico")
        categoria_beneficiario = payload.get("categoria_beneficiario")
        nome_beneficiario = payload.get("nome_beneficiario")
        valore_totale = payload.get("valore_totale")
        numero_pass = payload.get("numero_pass")
        messaggio = payload.get("messaggio")
        data_donazione = payload.get("data_donazione")

        if not comune_id_str or not nome_donatore or not nome_donatore.strip():
            return {"errore": "comune_id e nome_donatore sono obbligatori"}
        if not categoria_beneficiario:
            return {"errore": "categoria_beneficiario è obbligatoria"}
        if not pacchetto_id and not tipo_generico:
            return {"errore": "Indica un pacchetto esistente oppure una tipologia generica"}
        if valore_totale is None or valore_totale < 0:
            return {"errore": "valore_totale non valido"}
        if not numero_pass or numero_pass < 1:
            return {"errore": "numero_pass deve essere almeno 1"}

        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id_str)

        record = {
            "piano_id": piano["id"],
            "comune_id": comune_id_str,
            "nome_donatore": nome_donatore.strip(),
            "contatti_donatore": contatti_donatore,
            "pacchetto_id": pacchetto_id,
            "tipo_generico": tipo_generico,
            "categoria_beneficiario": categoria_beneficiario,
            "nome_beneficiario": nome_beneficiario,
            "valore_totale": valore_totale,
            "numero_pass": numero_pass,
            "messaggio": messaggio,
            "data_donazione": data_donazione,
        }
        creato_resp = supabase.table("donazioni_solidali").insert(record).execute()
        donazione = creato_resp.data[0] if creato_resp.data else None
        if not donazione:
            return {"errore": "Errore nel salvataggio della donazione"}

        pass_creati = []
        for _ in range(numero_pass):
            pass_record = {
                "donazione_id": donazione["id"],
                "piano_id": piano["id"],
                "comune_id": comune_id_str,
                "codice": _genera_codice_pass(),
                "categoria_beneficiario": categoria_beneficiario,
                "nome_beneficiario": nome_beneficiario,
                "stato": "emesso",
            }
            pass_resp = supabase.table("pass_solidali").insert(pass_record).execute()
            if pass_resp.data:
                pass_creati.append(pass_resp.data[0])

        return {"status": "salvato", "donazione": donazione, "pass": pass_creati}
    except Exception as e:
        print(f"Errore creazione donazione solidale: {e}")
        return {"errore": str(e)}


def get_donazioni(comune_id):
    try:
        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id)
        resp = supabase.table("donazioni_solidali").select("*").eq("piano_id", piano["id"]).order("creato_il", desc=True).execute()
        return {"piano_id": piano["id"], "comune_id": comune_id, "donazioni": resp.data or []}
    except Exception as e:
        print(f"Errore get donazioni solidali comune {comune_id}: {e}")
        return {"errore": str(e)}


def get_pass(comune_id):
    try:
        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id)
        resp = supabase.table("pass_solidali").select("*, donazioni_solidali(nome_donatore)") \
            .eq("piano_id", piano["id"]).order("data_emissione", desc=True).execute()
        return {"piano_id": piano["id"], "comune_id": comune_id, "pass": resp.data or []}
    except Exception as e:
        print(f"Errore get pass solidali comune {comune_id}: {e}")
        return {"errore": str(e)}


def utilizza_pass(pass_id, nome_beneficiario=None, note=None):
    try:
        pass_resp = supabase.table("pass_solidali").select("stato").eq("id", pass_id).single().execute()
        pass_corrente = pass_resp.data
        if not pass_corrente:
            return {"errore": "Pass non trovato"}
        if pass_corrente["stato"] != "emesso":
            return {"errore": f"Il pass è già in stato \"{pass_corrente['stato']}\""}

        aggiornamento = {"stato": "utilizzato", "data_utilizzo": datetime.now().isoformat()}
        if nome_beneficiario is not None and nome_beneficiario != "":
            aggiornamento["nome_beneficiario"] = nome_beneficiario
        if note is not None:
            aggiornamento["note"] = note

        supabase.table("pass_solidali").update(aggiornamento).eq("id", pass_id).execute()
        return {"status": "utilizzato"}
    except Exception as e:
        print(f"Errore utilizzo pass {pass_id}: {e}")
        return {"errore": str(e)}


def annulla_pass(pass_id):
    try:
        pass_resp = supabase.table("pass_solidali").select("stato").eq("id", pass_id).single().execute()
        pass_corrente = pass_resp.data
        if not pass_corrente:
            return {"errore": "Pass non trovato"}
        if pass_corrente["stato"] != "emesso":
            return {"errore": "Solo un pass ancora emesso può essere annullato"}

        supabase.table("pass_solidali").update({"stato": "scaduto"}).eq("id", pass_id).execute()
        return {"status": "scaduto"}
    except Exception as e:
        print(f"Errore annullamento pass {pass_id}: {e}")
        return {"errore": str(e)}


def get_impatto_sociale(comune_id):
    try:
        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id)
        donazioni_resp = supabase.table("donazioni_solidali").select("*").eq("piano_id", piano["id"]).execute()
        donazioni = donazioni_resp.data or []
        pass_resp = supabase.table("pass_solidali").select("*").eq("piano_id", piano["id"]).execute()
        pass_tutti = pass_resp.data or []

        valore_totale_donato = sum(d.get("valore_totale") or 0 for d in donazioni)
        n_donatori = len({d["nome_donatore"] for d in donazioni})
        n_pass_emessi = len(pass_tutti)
        n_pass_utilizzati = len([p for p in pass_tutti if p["stato"] == "utilizzato"])
        n_pass_disponibili = len([p for p in pass_tutti if p["stato"] == "emesso"])

        per_categoria_raw = defaultdict(lambda: {"emessi": 0, "utilizzati": 0})
        for p in pass_tutti:
            cat = p.get("categoria_beneficiario") or "Altro"
            per_categoria_raw[cat]["emessi"] += 1
            if p["stato"] == "utilizzato":
                per_categoria_raw[cat]["utilizzati"] += 1

        per_categoria = [
            {"categoria": cat, "emessi": dati["emessi"], "utilizzati": dati["utilizzati"]}
            for cat, dati in sorted(per_categoria_raw.items(), key=lambda x: x[1]["emessi"], reverse=True)
        ]

        top_donatori_raw = defaultdict(float)
        for d in donazioni:
            top_donatori_raw[d["nome_donatore"]] += d.get("valore_totale") or 0
        top_donatori = [
            {"nome_donatore": nome, "valore": round(valore, 2)}
            for nome, valore in sorted(top_donatori_raw.items(), key=lambda x: x[1], reverse=True)[:5]
        ]

        return {
            "comune_id": comune_id,
            "valore_totale_donato": round(valore_totale_donato, 2),
            "n_donatori": n_donatori,
            "n_pass_emessi": n_pass_emessi,
            "n_pass_utilizzati": n_pass_utilizzati,
            "n_pass_disponibili": n_pass_disponibili,
            "per_categoria": per_categoria,
            "top_donatori": top_donatori,
            "nota_metodologica": (
                "Ogni donazione genera automaticamente i pass richiesti, distribuiti gratuitamente alle fasce "
                "beneficiarie indicate. Questo flusso ha contabilità propria, indipendente dai fondi di Sviluppo "
                "Locale e Welfare. L'impatto sociale è misurato in valore donato e in pass effettivamente "
                "utilizzati, non solo emessi."
            ),
        }
    except Exception as e:
        print(f"Errore impatto sociale comune {comune_id}: {e}")
        return {"errore": str(e)}