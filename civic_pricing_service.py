from datetime import datetime, timedelta
from supabase import create_client
import os
from dotenv import load_dotenv
import pandas as pd

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

SOGLIA_BASSA_AFFLUENZA_PCT = -30.0
ORIZZONTE_GIORNI_DEFAULT = 30


def get_giorni_bassa_affluenza(sito_id, orizzonte_giorni=ORIZZONTE_GIORNI_DEFAULT):
    try:
        novanta_giorni_fa = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
        storico_resp = supabase.table("presenza").select("data, gruppo") \
            .eq("sito_id", sito_id).gte("data", novanta_giorni_fa).execute()
        storico = storico_resp.data or []

        if not storico:
            return {"errore": "Nessun dato storico di presenze sufficiente per calcolare una media di riferimento per questo sito"}

        df_storico = pd.DataFrame(storico)
        df_storico["data"] = pd.to_datetime(df_storico["data"])
        media_giornaliera_storica = df_storico.groupby("data")["gruppo"].sum().mean()

        if not media_giornaliera_storica or media_giornaliera_storica <= 0:
            return {"errore": "Media storica non calcolabile per questo sito"}

        oggi = datetime.now()
        fine = oggi + timedelta(days=orizzonte_giorni)
        prev_resp = supabase.table("previsioni_affluenza").select("data_previsione, affluenza_stimata") \
            .eq("sito_id", sito_id).gte("data_previsione", oggi.strftime("%Y-%m-%d")) \
            .lte("data_previsione", fine.strftime("%Y-%m-%d")).order("data_previsione").execute()
        previsioni = prev_resp.data or []

        if not previsioni:
            return {
                "sito_id": sito_id,
                "dati_sufficienti": False,
                "messaggio": (
                    "Nessuna previsione di affluenza disponibile per questo sito nell'orizzonte richiesto. Le "
                    "previsioni vengono generate automaticamente ogni settimana: torna a controllare tra qualche giorno."
                ),
            }

        nomi_giorni = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]

        giorni_bassa_affluenza = []
        for p in previsioni:
            data_p = pd.to_datetime(p["data_previsione"])
            valore = p["affluenza_stimata"]
            scostamento_pct = round(((valore - media_giornaliera_storica) / media_giornaliera_storica) * 100, 1)
            if scostamento_pct <= SOGLIA_BASSA_AFFLUENZA_PCT:
                giorni_bassa_affluenza.append({
                    "data": p["data_previsione"],
                    "nome_giorno": nomi_giorni[data_p.weekday()],
                    "affluenza_prevista": round(valore, 1),
                    "media_riferimento": round(media_giornaliera_storica, 1),
                    "scostamento_pct": scostamento_pct,
                })

        return {
            "sito_id": sito_id,
            "dati_sufficienti": True,
            "media_giornaliera_storica": round(media_giornaliera_storica, 1),
            "soglia_pct": SOGLIA_BASSA_AFFLUENZA_PCT,
            "orizzonte_giorni": orizzonte_giorni,
            "n_giorni_bassa_affluenza": len(giorni_bassa_affluenza),
            "giorni_bassa_affluenza": giorni_bassa_affluenza,
            "nota_metodologica": (
                f"Un giorno è considerato di bassissima affluenza quando la previsione SARIMAX scende di almeno "
                f"{abs(SOGLIA_BASSA_AFFLUENZA_PCT)}% sotto la media giornaliera calcolata sugli ultimi 90 giorni di "
                "presenze reali. In questi giorni il costo marginale di accogliere visitatori aggiuntivi è "
                "tipicamente prossimo allo zero (nessun aumento di personale o code): è il momento più sostenibile "
                "per offrire ingressi agevolati senza sottrarre incassi ai giorni di normale affluenza."
            ),
        }
    except Exception as e:
        print(f"Errore giorni bassa affluenza sito {sito_id}: {e}")
        return {"errore": str(e)}


def get_categorie_civic_pricing(sito_id):
    try:
        categorie_resp = supabase.table("civic_pricing_categorie").select("*") \
            .eq("sito_id", sito_id).eq("attiva", True).order("creato_il").execute()
        return {"sito_id": sito_id, "categorie": categorie_resp.data or []}
    except Exception as e:
        print(f"Errore get categorie civic pricing sito {sito_id}: {e}")
        return {"errore": str(e)}


def crea_categoria_civic_pricing(payload):
    try:
        sito_id = payload.get("sito_id")
        comune_id_str = payload.get("comune_id")
        nome_categoria = payload.get("nome_categoria")
        tariffa_proposta = payload.get("tariffa_proposta")
        note = payload.get("note")

        if not sito_id or not comune_id_str or not nome_categoria or not nome_categoria.strip() or tariffa_proposta is None:
            return {"errore": "sito_id, comune_id, nome_categoria e tariffa_proposta sono obbligatori"}

        if tariffa_proposta < 0:
            return {"errore": "La tariffa proposta non può essere negativa"}

        record = {
            "sito_id": sito_id,
            "comune_id": comune_id_str,
            "nome_categoria": nome_categoria.strip(),
            "tariffa_proposta": tariffa_proposta,
            "note": note,
        }
        creato_resp = supabase.table("civic_pricing_categorie").insert(record).execute()

        return {"status": "salvato", "categoria": creato_resp.data[0] if creato_resp.data else None}
    except Exception as e:
        print(f"Errore creazione categoria civic pricing: {e}")
        return {"errore": str(e)}


def elimina_categoria_civic_pricing(categoria_id):
    try:
        supabase.table("civic_pricing_categorie").update({"attiva": False}).eq("id", categoria_id).execute()
        return {"status": "disattivata"}
    except Exception as e:
        print(f"Errore eliminazione categoria civic pricing {categoria_id}: {e}")
        return {"errore": str(e)}


def simula_impatto_civic_pricing(payload):
    try:
        sito_id = payload.get("sito_id")
        n_visitatori_stimati = payload.get("n_visitatori_stimati")
        tariffa_applicata = payload.get("tariffa_applicata", 0)

        if not sito_id or n_visitatori_stimati is None:
            return {"errore": "sito_id e n_visitatori_stimati sono obbligatori"}

        if n_visitatori_stimati < 0:
            return {"errore": "n_visitatori_stimati non può essere negativo"}

        sito_resp = supabase.table("siti_culturali").select(
            "nome_sito, prezzo_biglietto, percentuale_bookshop, spesa_media_bookshop, "
            "percentuale_ristorazione, spesa_media_ristorazione"
        ).eq("id", sito_id).single().execute()
        sito = sito_resp.data
        if not sito:
            return {"errore": "Sito non trovato"}

        ricavo_biglietteria_diretto = round(n_visitatori_stimati * tariffa_applicata, 2)

        bookshop_per_persona = (sito.get("percentuale_bookshop") or 0) / 100 * (sito.get("spesa_media_bookshop") or 0)
        ristorazione_per_persona = (sito.get("percentuale_ristorazione") or 0) / 100 * (sito.get("spesa_media_ristorazione") or 0)
        ricavo_indiretto_stimato = round(n_visitatori_stimati * (bookshop_per_persona + ristorazione_per_persona), 2)

        ricavo_pieno_teorico = round(n_visitatori_stimati * (sito.get("prezzo_biglietto") or 0), 2)

        return {
            "sito_id": sito_id,
            "nome_sito": sito["nome_sito"],
            "n_visitatori_stimati": n_visitatori_stimati,
            "tariffa_applicata": tariffa_applicata,
            "ricavo_biglietteria_diretto": ricavo_biglietteria_diretto,
            "ricavo_indiretto_stimato": ricavo_indiretto_stimato,
            "ricavo_totale_stimato": round(ricavo_biglietteria_diretto + ricavo_indiretto_stimato, 2),
            "ricavo_pieno_teorico_non_incassato": max(round(ricavo_pieno_teorico - ricavo_biglietteria_diretto, 2), 0),
            "nota_metodologica": (
                "Il numero di visitatori aggiuntivi è una stima che inserisci tu: GesTur non può prevedere quante "
                "persone risponderanno a un ingresso agevolato. Il ricavo indiretto stimato riusa gli stessi "
                "coefficienti di spesa media (bookshop, ristorazione) già configurati per questo sito nelle "
                "previsioni economiche standard. Il \"ricavo pieno teorico non incassato\" non è una perdita reale: "
                "in un giorno di bassissima affluenza, questi visitatori con ogni probabilità non sarebbero venuti "
                "affatto pagando il prezzo pieno, quindi rappresenta valore sociale creato a costo marginale quasi nullo, "
                "non un mancato guadagno sottratto ad altri visitatori."
            ),
        }
    except Exception as e:
        print(f"Errore simulazione impatto civic pricing: {e}")
        return {"errore": str(e)}