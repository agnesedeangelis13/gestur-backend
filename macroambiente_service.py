import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from supabase import create_client
import os
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
WORLD_BANK_URL = "https://api.worldbank.org/v2/country/{codice}/indicator/FP.CPI.TOTL.ZG?format=json&per_page=6"
CAMBIO_VALUTA = {
    "USA": "USD", "Regno Unito": "GBP", "Giappone": "JPY", "Cina": "CNY", "Svizzera": "CHF",
    "Canada": "CAD", "Australia": "AUD", "Corea del Sud": "KRW", "Brasile": "BRL", "India": "INR",
    "Russia": "RUB", "Messico": "MXN", "Israele": "ILS", "Singapore": "SGD", "Norvegia": "NOK",
    "Sudafrica": "ZAR", "Turchia": "TRY", "Nuova Zelanda": "NZD", "Emirati Arabi": "AED", "Thailandia": "THB",
}

WB_CODICE_PAESE = {
    "USA": "US", "Regno Unito": "GB", "Giappone": "JP", "Cina": "CN", "Svizzera": "CH",
    "Canada": "CA", "Australia": "AU", "Corea del Sud": "KR", "Brasile": "BR", "India": "IN",
    "Russia": "RU", "Messico": "MX", "Israele": "IL", "Singapore": "SG", "Norvegia": "NO",
    "Sudafrica": "ZA", "Turchia": "TR", "Nuova Zelanda": "NZ", "Emirati Arabi": "AE", "Thailandia": "TH",
}

SOGLIA_GIORNO_CALDO_C = 32


def _leggi_cache(tipo, chiave):
    try:
        resp = supabase.table("macroambiente_cache_esterna").select("*").eq("tipo", tipo).eq("chiave", chiave).single().execute()
        return resp.data
    except Exception:
        return None


def _scrivi_cache(tipo, chiave, valore, data_riferimento=None):
    try:
        supabase.table("macroambiente_cache_esterna").upsert({
            "tipo": tipo, "chiave": chiave, "valore": valore,
            "data_riferimento": data_riferimento, "aggiornato_il": datetime.now().isoformat(),
        }, on_conflict="tipo,chiave").execute()
    except Exception as e:
        print(f"Errore scrittura cache macroambiente {tipo}/{chiave}: {e}")


def get_tassi_cambio():
    try:
        risposta = requests.get(ECB_URL, timeout=8)
        risposta.raise_for_status()
        root = ET.fromstring(risposta.content)
        ns = {"gesmes": "http://www.gesmes.org/xml/2002-08-01", "ecb": "http://www.ecb.int/vocabulary/2002-08-01/eurofxref"}
        cubo_giorno = root.find(".//ecb:Cube[@time]", ns)
        data_riferimento = cubo_giorno.get("time") if cubo_giorno is not None else None

        variazioni = {}
        for cubo in root.findall(".//ecb:Cube[@currency]", ns):
            valuta = cubo.get("currency")
            tasso = float(cubo.get("rate"))
            if valuta not in CAMBIO_VALUTA.values():
                continue

            precedente = _leggi_cache("cambio", valuta)
            variazione_pct = None
            if precedente and precedente.get("valore"):
                variazione_pct = round(((tasso - precedente["valore"]) / precedente["valore"]) * 100, 2)

            variazioni[valuta] = {"tasso_attuale": tasso, "variazione_pct": variazione_pct, "data_riferimento_precedente": precedente.get("data_riferimento") if precedente else None}
            _scrivi_cache("cambio", valuta, tasso, data_riferimento)

        return {"data_riferimento": data_riferimento, "valute": variazioni}
    except Exception as e:
        print(f"Errore recupero tassi di cambio BCE: {e}")
        return {"errore": str(e)}


def get_inflazione_paesi():
    risultati = {}
    for paese, codice in WB_CODICE_PAESE.items():
        cache = _leggi_cache("inflazione", codice)
        if cache and cache.get("aggiornato_il"):
            aggiornato = datetime.fromisoformat(cache["aggiornato_il"].replace("Z", "+00:00")) if isinstance(cache["aggiornato_il"], str) else None
            if aggiornato and (datetime.now(aggiornato.tzinfo) - aggiornato) < timedelta(days=30):
                risultati[paese] = {"inflazione_pct": cache["valore"], "anno_riferimento": cache.get("data_riferimento")}
                continue
        try:
            risposta = requests.get(WORLD_BANK_URL.format(codice=codice), timeout=8)
            risposta.raise_for_status()
            dati = risposta.json()
            valore_trovato = None
            anno_trovato = None
            if len(dati) > 1 and dati[1]:
                for punto in dati[1]:
                    if punto.get("value") is not None:
                        valore_trovato = round(punto["value"], 2)
                        anno_trovato = punto.get("date")
                        break
            if valore_trovato is not None:
                _scrivi_cache("inflazione", codice, valore_trovato, anno_trovato)
                risultati[paese] = {"inflazione_pct": valore_trovato, "anno_riferimento": anno_trovato}
        except Exception as e:
            print(f"Errore recupero inflazione {paese}: {e}")
    return risultati


def get_dati_climatici(comune_id):
    try:
        siti_resp = supabase.table("siti_culturali").select("id").eq("comune_id", comune_id).execute()
        siti_ids = [s["id"] for s in (siti_resp.data or [])]
        if not siti_ids:
            return {"errore": "Nessun sito registrato per questo comune"}

        trenta_giorni_fa = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

        meteo_resp = supabase.table("meteo_giornaliero").select("sito_id, data, temperatura_max") \
            .in_("sito_id", siti_ids).gte("data", trenta_giorni_fa).execute()
        meteo = meteo_resp.data or []
        if not meteo:
            return {"errore": "Dati meteo non ancora disponibili per i siti di questo comune: verifica che l'aggiornamento meteo giornaliero sia attivo."}

        date_osservate = set()
        giorni_caldi_per_sito = set()
        for m in meteo:
            date_osservate.add(m["data"])
            if m.get("temperatura_max") is not None and m["temperatura_max"] >= SOGLIA_GIORNO_CALDO_C:
                giorni_caldi_per_sito.add((m["sito_id"], m["data"]))

        date_calde = {d for (_, d) in giorni_caldi_per_sito}

        presenze_resp = supabase.table("presenza").select("data, ora, sito_id") \
            .in_("sito_id", siti_ids).gte("data", trenta_giorni_fa).execute()
        presenze = presenze_resp.data or []

        def ora_media(righe):
            ore = [int(r["ora"].split(":")[0]) for r in righe if r.get("ora")]
            return round(sum(ore) / len(ore), 1) if ore else None

        presenze_giorni_caldi = [p for p in presenze if (p["sito_id"], p["data"]) in giorni_caldi_per_sito]
        presenze_giorni_normali = [p for p in presenze if (p["sito_id"], p["data"]) not in giorni_caldi_per_sito]

        ora_media_calda = ora_media(presenze_giorni_caldi)
        ora_media_normale = ora_media(presenze_giorni_normali)

        return {
            "n_giorni_analizzati": len(date_osservate),
            "n_giorni_caldi": len(date_calde),
            "soglia_giorno_caldo_c": SOGLIA_GIORNO_CALDO_C,
            "ora_media_visita_giorni_caldi": ora_media_calda,
            "ora_media_visita_giorni_normali": ora_media_normale,
            "spostamento_confermato": ora_media_calda is not None and ora_media_normale is not None and ora_media_calda > ora_media_normale,
        }
    except Exception as e:
        print(f"Errore dati climatici comune {comune_id}: {e}")
        return {"errore": str(e)}


def get_tasso_digitalizzazione(comune_id):
    try:
        siti_resp = supabase.table("siti_culturali").select("id").eq("comune_id", comune_id).execute()
        siti_ids = [s["id"] for s in (siti_resp.data or [])]
        if not siti_ids:
            return {"errore": "Nessun sito registrato per questo comune"}

        coeff_resp = supabase.table("coefficienti_canale").select("pct_preferenza_cartaceo").in_("sito_id", siti_ids).execute()
        coefficienti = coeff_resp.data or []
        if not coefficienti:
            return {"errore": "Nessun coefficiente di canale configurato per i siti di questo comune"}

        media_cartaceo = sum(c["pct_preferenza_cartaceo"] for c in coefficienti) / len(coefficienti)
        return {
            "pct_preferenza_cartaceo_comune": round(media_cartaceo, 1),
            "pct_preferenza_digitale_comune": round(100 - media_cartaceo, 1),
            "n_siti_inclusi": len(siti_ids),
        }
    except Exception as e:
        print(f"Errore tasso digitalizzazione comune {comune_id}: {e}")
        return {"errore": str(e)}


def get_potere_acquisto_flussi(comune_id):
    try:
        novanta_giorni_fa = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
        presenze_resp = supabase.table("presenza").select("provenienza, gruppo, siti_culturali!inner(comune_id)") \
            .eq("siti_culturali.comune_id", comune_id).gte("data", novanta_giorni_fa).execute()
        presenze = presenze_resp.data or []

        conteggio_paese = {}
        for p in presenze:
            for prov in (p.get("provenienza") or "").split(", "):
                prov = prov.strip()
                if prov in CAMBIO_VALUTA:
                    conteggio_paese[prov] = conteggio_paese.get(prov, 0) + (p.get("gruppo") or 0)

        if not conteggio_paese:
            return {"n_visitatori_extra_ue": 0, "dettaglio_paesi": [], "componente_vulnerabilita": 0}

        cambi = get_tassi_cambio()
        inflazioni = get_inflazione_paesi()
        totale_visitatori = sum(conteggio_paese.values())

        dettaglio = []
        punteggio_pesato = 0
        for paese, n_visitatori in conteggio_paese.items():
            valuta = CAMBIO_VALUTA[paese]
            dato_cambio = cambi.get("valute", {}).get(valuta, {}) if "errore" not in cambi else {}
            dato_inflazione = inflazioni.get(paese, {})

            variazione_cambio = dato_cambio.get("variazione_pct")
            inflazione_pct = dato_inflazione.get("inflazione_pct")

            rischio_cambio = min(100, max(0, -variazione_cambio * 10)) if variazione_cambio is not None else 0
            rischio_inflazione = min(100, (inflazione_pct or 0) * 8)
            rischio_paese = round((rischio_cambio + rischio_inflazione) / 2, 1)

            peso = n_visitatori / totale_visitatori
            punteggio_pesato += rischio_paese * peso

            dettaglio.append({
                "paese": paese, "n_visitatori": n_visitatori, "valuta": valuta,
                "variazione_cambio_pct": variazione_cambio, "inflazione_pct": inflazione_pct,
                "rischio_paese": rischio_paese,
            })

        dettaglio.sort(key=lambda d: d["n_visitatori"], reverse=True)

        return {
            "n_visitatori_extra_ue": totale_visitatori,
            "dettaglio_paesi": dettaglio,
            "componente_vulnerabilita": round(punteggio_pesato, 1),
        }
    except Exception as e:
        print(f"Errore potere acquisto flussi comune {comune_id}: {e}")
        return {"errore": str(e)}


def get_indice_vulnerabilita_macroambiente(comune_id):
    try:
        economico = get_potere_acquisto_flussi(comune_id)
        climatico = get_dati_climatici(comune_id)
        digitale = get_tasso_digitalizzazione(comune_id)

        componente_economico = economico.get("componente_vulnerabilita") if "errore" not in economico else None
        componente_climatico = (
            round((climatico["n_giorni_caldi"] / climatico["n_giorni_analizzati"]) * 100, 1)
            if "errore" not in climatico and climatico.get("n_giorni_analizzati") else None
        )
        componente_digitale = digitale.get("pct_preferenza_cartaceo_comune") if "errore" not in digitale else None

        componenti_disponibili = [c for c in [componente_economico, componente_climatico, componente_digitale] if c is not None]
        if not componenti_disponibili:
            return {"errore": "Dati insufficienti per calcolare l'indice di vulnerabilita macroambientale"}

        indice_vulnerabilita = round(sum(componenti_disponibili) / len(componenti_disponibili), 1)

        if indice_vulnerabilita >= 70:
            livello = "critico"
            messaggio = (
                "Il macroambiente sta cambiando piu velocemente della capacita di risposta del sito. Se non si "
                "investe nell'adeguamento (tecnologico, orari, comunicazione ai mercati extra-UE), il "
                "posizionamento della destinazione rischia un peggioramento significativo nei prossimi mesi."
            )
        elif indice_vulnerabilita >= 40:
            livello = "moderato"
            messaggio = (
                "Alcuni segnali del macroambiente mostrano pressione crescente. Non e ancora una criticita "
                "acuta, ma vale la pena monitorare l'evoluzione e pianificare interventi preventivi."
            )
        else:
            livello = "basso"
            messaggio = "Il macroambiente non mostra al momento segnali di pressione significativi sulla destinazione."

        return {
            "comune_id": comune_id,
            "indice_vulnerabilita": indice_vulnerabilita,
            "livello": livello,
            "messaggio": messaggio,
            "componente_economico": componente_economico,
            "componente_climatico": componente_climatico,
            "componente_digitale": componente_digitale,
            "dettaglio_economico": economico,
            "dettaglio_climatico": climatico,
            "dettaglio_digitale": digitale,
            "nota_metodologica": (
                "Indice 0-100 calcolato come media delle componenti disponibili: economica (potere d'acquisto dei "
                "flussi extra-UE, incrociando variazione del cambio rispetto all'ultimo controllo e inflazione nel "
                "paese di provenienza), climatica (quota di giornate sopra 32°C negli ultimi 30 giorni), digitale "
                "(quota di preferenza per materiale cartaceo sui siti del comune, come proxy di ritardo digitale). "
                "Se una componente non e calcolabile per mancanza di dati, l'indice si basa sulle altre disponibili."
            ),
        }
    except Exception as e:
        print(f"Errore indice vulnerabilita macroambientale comune {comune_id}: {e}")
        return {"errore": str(e)}