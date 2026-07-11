from collections import defaultdict
from datetime import datetime
from supabase import create_client
import os
from dotenv import load_dotenv
from adempimenti_service import verifica_checklist_completa

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

STATI_VALIDI = ("richiesto", "approvato", "completato", "rifiutato")

CAMPI_CONFORMITA = [
    "permessi_stato",
    "igienico_sanitario_stato",
    "piano_sicurezza_stato",
    "durc_stato",
    "licenza_stato",
    "autorizzazioni_espositori_stato",
]


def crea_mercato(payload):
    try:
        comune_id_str = payload.get("comune_id")
        titolo = payload.get("titolo")
        data_mercato = payload.get("data_mercato")
        ora_inizio = payload.get("ora_inizio")
        ora_fine = payload.get("ora_fine")
        tipologia_mercato = payload.get("tipologia_mercato")
        n_stalli_totali = payload.get("n_stalli_totali")
        n_stalli_occupati = payload.get("n_stalli_occupati")
        n_espositori_stimati = payload.get("n_espositori_stimati")
        n_visitatori_stimati = payload.get("n_visitatori_stimati")
        incassi_stimati = payload.get("incassi_stimati")
        canone_valore = payload.get("canone_valore")
        tipo_canone = payload.get("tipo_canone")
        permessi_stato = payload.get("permessi_stato")
        igienico_sanitario_stato = payload.get("igienico_sanitario_stato")
        piano_sicurezza_stato = payload.get("piano_sicurezza_stato")
        stato_delibera = payload.get("stato_delibera")
        stato_concessione = payload.get("stato_concessione")
        durc_stato = payload.get("durc_stato")
        licenza_stato = payload.get("licenza_stato")
        autorizzazioni_espositori_stato = payload.get("autorizzazioni_espositori_stato")
        origine_filiera = payload.get("origine_filiera") or []
        note = payload.get("note")

        if not comune_id_str or not titolo or not titolo.strip():
            return {"errore": "comune_id e titolo sono obbligatori"}
        if not data_mercato:
            return {"errore": "data_mercato è obbligatoria"}
        if n_stalli_totali is not None and n_stalli_occupati is not None and n_stalli_occupati > n_stalli_totali:
            return {"errore": "Gli stalli occupati non possono superare il totale"}

        record = {
            "comune_id": comune_id_str,
            "titolo": titolo.strip(),
            "data_mercato": data_mercato,
            "ora_inizio": ora_inizio,
            "ora_fine": ora_fine,
            "tipologia_mercato": tipologia_mercato,
            "n_stalli_totali": n_stalli_totali,
            "n_stalli_occupati": n_stalli_occupati,
            "n_espositori_stimati": n_espositori_stimati,
            "n_visitatori_stimati": n_visitatori_stimati,
            "incassi_stimati": incassi_stimati,
            "canone_valore": canone_valore,
            "tipo_canone": tipo_canone,
            "permessi_stato": permessi_stato,
            "igienico_sanitario_stato": igienico_sanitario_stato,
            "piano_sicurezza_stato": piano_sicurezza_stato,
            "stato_delibera": stato_delibera,
            "stato_concessione": stato_concessione,
            "durc_stato": durc_stato,
            "licenza_stato": licenza_stato,
            "autorizzazioni_espositori_stato": autorizzazioni_espositori_stato,
            "origine_filiera": origine_filiera,
            "note": note,
            "stato": "richiesto",
        }
        creato_resp = supabase.table("mercati_richiesti").insert(record).execute()
        return {"status": "salvato", "mercato": creato_resp.data[0] if creato_resp.data else None}
    except Exception as e:
        print(f"Errore creazione mercato: {e}")
        return {"errore": str(e)}


def get_mercati(comune_id):
    try:
        resp = supabase.table("mercati_richiesti").select("*").eq("comune_id", comune_id).order("data_mercato", desc=True).execute()
        return {"comune_id": comune_id, "mercati": resp.data or []}
    except Exception as e:
        print(f"Errore get mercati comune {comune_id}: {e}")
        return {"errore": str(e)}


def cambia_stato_mercato(mercato_id, nuovo_stato):
    try:
        if nuovo_stato not in STATI_VALIDI:
            return {"errore": "Stato non valido"}

        mercato_resp = supabase.table("mercati_richiesti").select("stato").eq("id", mercato_id).single().execute()
        if not mercato_resp.data:
            return {"errore": "Mercato non trovato"}

        aggiornamento = {"stato": nuovo_stato}
        if nuovo_stato == "approvato":
            verifica = verifica_checklist_completa("mercato", mercato_id)
            if "errore" in verifica:
                return verifica
            if not verifica["completa"]:
                return {"errore": f"Adempimenti mancanti prima dell'approvazione: {', '.join(verifica['mancanti'])}"}
            aggiornamento["data_approvazione"] = datetime.now().isoformat()
        if nuovo_stato == "completato":
            aggiornamento["data_completamento"] = datetime.now().isoformat()

        supabase.table("mercati_richiesti").update(aggiornamento).eq("id", mercato_id).execute()
        return {"status": nuovo_stato}
    except Exception as e:
        print(f"Errore cambio stato mercato {mercato_id}: {e}")
        return {"errore": str(e)}


def salva_consuntivo_mercato(mercato_id, n_espositori_reali=None, n_visitatori_reali=None, incassi_reali=None, n_stalli_occupati=None):
    try:
        mercato_resp = supabase.table("mercati_richiesti").select("stato").eq("id", mercato_id).single().execute()
        mercato = mercato_resp.data
        if not mercato:
            return {"errore": "Mercato non trovato"}
        if mercato["stato"] != "completato":
            return {"errore": "Il consuntivo può essere inserito solo su un mercato completato"}

        aggiornamento = {}
        if n_espositori_reali is not None:
            aggiornamento["n_espositori_reali"] = n_espositori_reali
        if n_visitatori_reali is not None:
            aggiornamento["n_visitatori_reali"] = n_visitatori_reali
        if incassi_reali is not None:
            aggiornamento["incassi_reali"] = incassi_reali
            aggiornamento["consuntivo_inserito"] = True
        if n_stalli_occupati is not None:
            aggiornamento["n_stalli_occupati"] = n_stalli_occupati

        if not aggiornamento:
            return {"errore": "Nessun valore da salvare"}

        supabase.table("mercati_richiesti").update(aggiornamento).eq("id", mercato_id).execute()
        return {"status": "salvato"}
    except Exception as e:
        print(f"Errore salvataggio consuntivo mercato {mercato_id}: {e}")
        return {"errore": str(e)}


def get_storico_mercati(comune_id):
    try:
        resp = supabase.table("mercati_richiesti").select("*") \
            .eq("comune_id", comune_id).in_("stato", ["completato", "rifiutato"]).execute()
        mercati = resp.data or []

        def data_ordinamento(m):
            return m.get("data_completamento") or m.get("data_approvazione") or m.get("creato_il") or ""

        mercati.sort(key=data_ordinamento, reverse=True)
        return {"comune_id": comune_id, "mercati": mercati}
    except Exception as e:
        print(f"Errore storico mercati comune {comune_id}: {e}")
        return {"errore": str(e)}


def get_statistiche_mercati(comune_id):
    try:
        resp = supabase.table("mercati_richiesti").select("*").eq("comune_id", comune_id).execute()
        mercati = resp.data or []

        completati = [m for m in mercati if m["stato"] == "completato"]
        richiesti = [m for m in mercati if m["stato"] == "richiesto"]
        approvati = [m for m in mercati if m["stato"] == "approvato"]
        rifiutati = [m for m in mercati if m["stato"] == "rifiutato"]

        def incasso_effettivo(m):
            if m.get("consuntivo_inserito") and m.get("incassi_reali") is not None:
                return m["incassi_reali"]
            return m.get("incassi_stimati") or 0

        incassi_totali = round(sum(incasso_effettivo(m) for m in completati), 2)
        visitatori_totali = sum(m.get("n_visitatori_reali") or 0 for m in completati)
        espositori_totali = sum(m.get("n_espositori_reali") or 0 for m in completati)

        occupazioni = [
            (m.get("n_stalli_occupati") or 0) / m["n_stalli_totali"]
            for m in mercati if m.get("n_stalli_totali")
        ]
        occupazione_media_pct = round((sum(occupazioni) / len(occupazioni)) * 100, 1) if occupazioni else 0

        conteggio_tipologia = defaultdict(int)
        for m in mercati:
            if m["stato"] == "rifiutato":
                continue
            tipologia = m.get("tipologia_mercato") or "Non specificato"
            conteggio_tipologia[tipologia] += 1
        per_tipologia = [
            {"tipo": tipo, "valore": conteggio}
            for tipo, conteggio in sorted(conteggio_tipologia.items(), key=lambda x: x[1], reverse=True)
        ]

        conformita_raw = defaultdict(int)
        for m in mercati:
            for campo in CAMPI_CONFORMITA:
                valore = m.get(campo)
                if valore:
                    conformita_raw[valore] += 1
        per_conformita = [
            {"tipo": stato, "valore": conteggio}
            for stato, conteggio in sorted(conformita_raw.items(), key=lambda x: x[1], reverse=True)
        ]

        andamento_raw = defaultdict(lambda: {"incassi": 0.0, "visitatori": 0})
        for m in completati:
            riferimento = m.get("data_completamento") or m.get("data_mercato")
            if not riferimento:
                continue
            mese = str(riferimento)[:7]
            andamento_raw[mese]["incassi"] += incasso_effettivo(m)
            andamento_raw[mese]["visitatori"] += m.get("n_visitatori_reali") or 0
        andamento_mensile = [
            {"mese": mese, "incassi": round(dati["incassi"], 2), "visitatori": dati["visitatori"]}
            for mese, dati in sorted(andamento_raw.items())
        ]

        return {
            "comune_id": comune_id,
            "n_totale": len(mercati),
            "n_richiesti": len(richiesti),
            "n_approvati": len(approvati),
            "n_completati": len(completati),
            "n_rifiutati": len(rifiutati),
            "incassi_totali": incassi_totali,
            "visitatori_totali": visitatori_totali,
            "espositori_totali": espositori_totali,
            "occupazione_media_pct": occupazione_media_pct,
            "per_tipologia": per_tipologia,
            "per_conformita": per_conformita,
            "andamento_mensile": andamento_mensile,
            "nota_metodologica": (
                "Gli incassi mostrano il valore reale se inserito il consuntivo, altrimenti la stima alla "
                "richiesta. L'occupazione media è calcolata sul rapporto stalli occupati/totali dichiarato per "
                "ciascun mercato. La conformità (permessi, igienico-sanitario, sicurezza, DURC, licenza, "
                "autorizzazioni) è aggregata a livello di mercato, non per singolo espositore."
            ),
        }
    except Exception as e:
        print(f"Errore statistiche mercati comune {comune_id}: {e}")
        return {"errore": str(e)}