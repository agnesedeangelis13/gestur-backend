from collections import defaultdict
from datetime import datetime
from supabase import create_client
import os
from dotenv import load_dotenv
from imposta_soggiorno_service import ottieni_o_crea_piano_sviluppo_locale_attivo
from marketplace_service import get_esperienza_by_id, get_commissione
from civic_pricing_service import get_giorni_bassa_affluenza

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

STATI_VALIDI = ("proposto", "approvato", "completato", "scartato")


def suggerisci_giorni_pacchetto(sito_id):
    return get_giorni_bassa_affluenza(sito_id)


def crea_pacchetto(payload):
    try:
        comune_id_str = payload.get("comune_id")
        sito_id = payload.get("sito_id")
        altro_luogo_id = payload.get("altro_luogo_id")
        titolo = payload.get("titolo")
        descrizione = payload.get("descrizione")
        esperienza_ids = payload.get("esperienza_ids") or []
        prezzo_ingresso_sito = payload.get("prezzo_ingresso_sito", 0)
        sconto_pct = payload.get("sconto_pct", 0)
        data_proposta = payload.get("data_proposta")
        generato_da_bassa_affluenza = payload.get("generato_da_bassa_affluenza", False)

        if not comune_id_str or not titolo or not titolo.strip():
            return {"errore": "comune_id e titolo sono obbligatori"}

        if sconto_pct < 0 or sconto_pct > 100:
            return {"errore": "sconto_pct deve essere tra 0 e 100"}

        commissione_risultato = get_commissione(comune_id_str)
        if "errore" in commissione_risultato:
            return {"errore": commissione_risultato["errore"]}
        commissione_pct = commissione_risultato["commissione_pct"]
        commissione_welfare_pct = commissione_risultato["commissione_welfare_pct"]

        esperienze_incluse = []
        totale_esperienze = 0
        margine_da_esperienze = 0

        for esperienza_id in esperienza_ids:
            esp = get_esperienza_by_id(esperienza_id)
            if not esp:
                continue
            fornitore = esp.get("fornitori_locali") or {}
            partecipa_welfare = fornitore.get("partecipa_welfare_locale", False)
            commissione_applicata = commissione_welfare_pct if partecipa_welfare else commissione_pct
            prezzo_esp = esp["prezzo"] or 0

            esperienze_incluse.append({
                "esperienza_id": esperienza_id,
                "nome_esperienza": esp["nome_esperienza"],
                "fornitore_nome": fornitore.get("nome_fornitore"),
                "categoria_fornitore": fornitore.get("categoria"),
                "prezzo": prezzo_esp,
                "commissione_applicata_pct": commissione_applicata,
                "partecipa_welfare_locale": partecipa_welfare,
            })
            totale_esperienze += prezzo_esp
            margine_da_esperienze += prezzo_esp * commissione_applicata / 100

        prezzo_pieno = (prezzo_ingresso_sito or 0) + totale_esperienze
        prezzo_totale_suggerito = round(prezzo_pieno * (1 - sconto_pct / 100), 2)
        margine_netto_stimato = round((prezzo_ingresso_sito or 0) + margine_da_esperienze, 2)

        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id_str)

        nome_altro_luogo = None
        if altro_luogo_id:
            luogo_resp = supabase.table("altri_luoghi_pacchetti").select("nome_luogo").eq("id", altro_luogo_id).single().execute()
            if luogo_resp.data:
                nome_altro_luogo = luogo_resp.data["nome_luogo"]

        record = {
            "piano_id": piano["id"],
            "comune_id": comune_id_str,
            "sito_id": sito_id,
            "altro_luogo_id": altro_luogo_id,
            "luogo_manuale": nome_altro_luogo,
            "titolo": titolo.strip(),
            "descrizione": descrizione,
            "esperienze_incluse": esperienze_incluse,
            "prezzo_ingresso_sito": prezzo_ingresso_sito,
            "prezzo_pieno": round(prezzo_pieno, 2),
            "sconto_pct": sconto_pct,
            "prezzo_totale_suggerito": prezzo_totale_suggerito,
            "margine_netto_stimato": margine_netto_stimato,
            "data_proposta": data_proposta,
            "generato_da_bassa_affluenza": bool(generato_da_bassa_affluenza),
            "stato": "proposto",
        }
        creato_resp = supabase.table("pacchetti_esperienziali").insert(record).execute()

        return {"status": "salvato", "pacchetto": creato_resp.data[0] if creato_resp.data else None}
    except Exception as e:
        print(f"Errore creazione pacchetto: {e}")
        return {"errore": str(e)}


def get_pacchetti(comune_id, stato=None):
    try:
        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id)
        query = supabase.table("pacchetti_esperienziali").select("*").eq("piano_id", piano["id"])
        if stato:
            if stato not in STATI_VALIDI:
                return {"errore": "stato non valido"}
            query = query.eq("stato", stato)
        pacchetti_resp = query.order("creato_il", desc=True).execute()
        return {"piano_id": piano["id"], "comune_id": comune_id, "pacchetti": pacchetti_resp.data or []}
    except Exception as e:
        print(f"Errore get pacchetti comune {comune_id}: {e}")
        return {"errore": str(e)}


def cambia_stato_pacchetto(pacchetto_id, nuovo_stato):
    try:
        if nuovo_stato not in STATI_VALIDI:
            return {"errore": "Stato non valido"}

        pacchetto_resp = supabase.table("pacchetti_esperienziali").select("stato").eq("id", pacchetto_id).single().execute()
        pacchetto = pacchetto_resp.data
        if not pacchetto:
            return {"errore": "Pacchetto non trovato"}

        aggiornamento = {"stato": nuovo_stato}
        if nuovo_stato == "approvato":
            aggiornamento["data_approvazione"] = datetime.now().isoformat()
        if nuovo_stato == "completato":
            aggiornamento["data_completamento"] = datetime.now().isoformat()

        supabase.table("pacchetti_esperienziali").update(aggiornamento).eq("id", pacchetto_id).execute()
        return {"status": nuovo_stato}
    except Exception as e:
        print(f"Errore cambio stato pacchetto {pacchetto_id}: {e}")
        return {"errore": str(e)}


def salva_consuntivo_pacchetto(pacchetto_id, margine_netto_reale=None, n_partecipanti=None):
    try:
        pacchetto_resp = supabase.table("pacchetti_esperienziali").select("stato").eq("id", pacchetto_id).single().execute()
        pacchetto = pacchetto_resp.data
        if not pacchetto:
            return {"errore": "Pacchetto non trovato"}
        if pacchetto["stato"] != "completato":
            return {"errore": "Il consuntivo può essere inserito solo su un pacchetto completato"}

        aggiornamento = {}
        if margine_netto_reale is not None:
            aggiornamento["margine_netto_reale"] = margine_netto_reale
            aggiornamento["consuntivo_inserito"] = True
        if n_partecipanti is not None:
            aggiornamento["n_partecipanti"] = n_partecipanti

        if not aggiornamento:
            return {"errore": "Nessun valore da salvare"}

        supabase.table("pacchetti_esperienziali").update(aggiornamento).eq("id", pacchetto_id).execute()
        return {"status": "salvato"}
    except Exception as e:
        print(f"Errore salvataggio consuntivo pacchetto {pacchetto_id}: {e}")
        return {"errore": str(e)}


def get_statistiche_pacchetti(comune_id):
    try:
        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id)
        pacchetti_resp = supabase.table("pacchetti_esperienziali").select("*").eq("piano_id", piano["id"]).execute()
        pacchetti = pacchetti_resp.data or []

        def margine_effettivo(p):
            if p.get("consuntivo_inserito") and p.get("margine_netto_reale") is not None:
                return p["margine_netto_reale"]
            return p.get("margine_netto_stimato") or 0

        completati = [p for p in pacchetti if p["stato"] == "completato"]
        proposti = [p for p in pacchetti if p["stato"] == "proposto"]
        approvati = [p for p in pacchetti if p["stato"] == "approvato"]
        scartati = [p for p in pacchetti if p["stato"] == "scartato"]
        generati_da_bassa_affluenza = [p for p in pacchetti if p.get("generato_da_bassa_affluenza")]

        valore_completati = round(sum(margine_effettivo(p) for p in completati), 2)
        valore_generati_bassa_affluenza = round(sum(margine_effettivo(p) for p in generati_da_bassa_affluenza if p["stato"] == "completato"), 2)

        breakdown_categoria = {}
        for p in pacchetti:
            for e in (p.get("esperienze_incluse") or []):
                cat = e.get("categoria_fornitore") or "N/D"
                breakdown_categoria[cat] = breakdown_categoria.get(cat, 0) + 1

        andamento_raw = defaultdict(lambda: {"margine": 0.0, "presenze": 0})
        for p in completati:
            riferimento = p.get("data_completamento") or p.get("data_proposta")
            if not riferimento:
                continue
            mese = str(riferimento)[:7]
            andamento_raw[mese]["margine"] += margine_effettivo(p)
            andamento_raw[mese]["presenze"] += p.get("n_partecipanti") or 0

        andamento_mensile = [
            {"mese": mese, "margine": round(dati["margine"], 2), "presenze": dati["presenze"]}
            for mese, dati in sorted(andamento_raw.items())
        ]

        return {
            "comune_id": comune_id,
            "n_totale": len(pacchetti),
            "n_proposti": len(proposti),
            "n_approvati": len(approvati),
            "n_completati": len(completati),
            "n_scartati": len(scartati),
            "valore_completati": valore_completati,
            "n_generati_da_bassa_affluenza": len(generati_da_bassa_affluenza),
            "valore_generati_da_bassa_affluenza": valore_generati_bassa_affluenza,
            "breakdown_categoria_fornitori": breakdown_categoria,
            "andamento_mensile": andamento_mensile,
            "nota_metodologica": (
                "Il margine per il comune è l'ingresso al sito (intero) più la commissione di gestione applicata "
                "su ciascuna esperienza inclusa (ridotta per i fornitori che partecipano al welfare locale). Il "
                "resto del prezzo dell'esperienza va al fornitore. Il valore mostra il margine reale se inserito "
                "il consuntivo, altrimenti la stima al momento della proposta."
            ),
        }
    except Exception as e:
        print(f"Errore statistiche pacchetti comune {comune_id}: {e}")
        return {"errore": str(e)}


def get_storico_pacchetti(comune_id):
    try:
        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id)
        pacchetti_resp = supabase.table("pacchetti_esperienziali").select("*") \
            .eq("piano_id", piano["id"]).in_("stato", ["completato", "scartato"]).execute()
        pacchetti = pacchetti_resp.data or []

        def data_ordinamento(p):
            return p.get("data_completamento") or p.get("data_approvazione") or p.get("creato_il") or ""

        pacchetti.sort(key=data_ordinamento, reverse=True)

        return {"comune_id": comune_id, "pacchetti": pacchetti}
    except Exception as e:
        print(f"Errore storico pacchetti comune {comune_id}: {e}")
        return {"errore": str(e)}


def get_altri_luoghi(comune_id):
    try:
        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id)
        luoghi_resp = supabase.table("altri_luoghi_pacchetti").select("*") \
            .eq("piano_id", piano["id"]).eq("attivo", True).order("nome_luogo").execute()
        return {"piano_id": piano["id"], "comune_id": comune_id, "luoghi": luoghi_resp.data or []}
    except Exception as e:
        print(f"Errore get altri luoghi comune {comune_id}: {e}")
        return {"errore": str(e)}


def crea_altro_luogo(payload):
    try:
        comune_id_str = payload.get("comune_id")
        nome_luogo = payload.get("nome_luogo")

        if not comune_id_str or not nome_luogo or not nome_luogo.strip():
            return {"errore": "comune_id e nome_luogo sono obbligatori"}

        nome_pulito = nome_luogo.strip()
        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id_str)

        esistenti_resp = supabase.table("altri_luoghi_pacchetti").select("nome_luogo") \
            .eq("piano_id", piano["id"]).eq("attivo", True).execute()
        if any(l["nome_luogo"].strip().lower() == nome_pulito.lower() for l in (esistenti_resp.data or [])):
            return {"errore": "Questo luogo è già presente in elenco"}

        record = {"piano_id": piano["id"], "comune_id": comune_id_str, "nome_luogo": nome_pulito}
        creato_resp = supabase.table("altri_luoghi_pacchetti").insert(record).execute()

        return {"status": "salvato", "luogo": creato_resp.data[0] if creato_resp.data else None}
    except Exception as e:
        print(f"Errore creazione altro luogo: {e}")
        return {"errore": str(e)}