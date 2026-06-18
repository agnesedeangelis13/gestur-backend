from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client
import pandas as pd
import numpy as np
from statsmodels.tsa.statespace.sarimax import SARIMAX
from datetime import datetime, timedelta
import os
from dotenv import load_dotenv
from weather_service import aggiorna_meteo_tutti_siti
from festivita_service import popola_festivita
from alert_service import invia_alert_previsioni
load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

def genera_variabili_esogene(date_range, sito_id=None, regione="Lazio"):
    exog = []
    date_str = [d.strftime("%Y-%m-%d") for d in date_range]

    try:
        fest = supabase.table("festivita_regionali").select("data") \
            .eq("regione", regione).in_("data", date_str).execute()
        date_festivita = set(f["data"] for f in fest.data)
    except:
        date_festivita = set()

    meteo_map = {}
    if sito_id:
        try:
            meteo = supabase.table("meteo_giornaliero").select(
                "data, temperatura_max, precipitazioni_mm, condizione") \
                .eq("sito_id", sito_id).in_("data", date_str).execute()
            for m in meteo.data:
                meteo_map[m["data"]] = m
        except:
            pass

    eventi_map = {}
    if sito_id:
        try:
            eventi = supabase.table("eventi_locali").select(
                "data_inizio, data_fine, impatto_atteso") \
                .eq("sito_id", sito_id).execute()
            impatto_val = {"basso": 1, "medio": 2, "alto": 3}
            for d in date_range:
                ds = d.strftime("%Y-%m-%d")
                for ev in eventi.data:
                    if ev["data_inizio"] <= ds <= ev["data_fine"]:
                        eventi_map[ds] = impatto_val.get(ev["impatto_atteso"], 2)
        except:
            pass

    for data in date_range:
        ds = data.strftime("%Y-%m-%d")
        is_festivo = 1 if ds in date_festivita else 0
        mese = data.month
        if mese in [12,1,2]: stagione = 0
        elif mese in [3,4,5]: stagione = 1
        elif mese in [6,7,8]: stagione = 2
        else: stagione = 3
        m = meteo_map.get(ds, {})
        temp = m.get("temperatura_max", 15.0) or 15.0
        pioggia = 1 if m.get("condizione") == "pioggia" else 0
        evento = eventi_map.get(ds, 0)
        is_weekend = 1 if data.weekday() >= 5 else 0
        exog.append([is_festivo, stagione, temp, pioggia, evento, is_weekend])

    return np.array(exog)

@app.get("/")
def root():
    return {"status": "GesTur Backend attivo"}

@app.get("/previsioni/{sito_id}")
def previsioni(sito_id: str, settimane: int = 4, regione: str = "Lazio"):
    try:
        sito_id_int = int(sito_id)
        response = supabase.table("presenza").select("*").eq("sito_id", sito_id_int).order("data").execute()
        dati = response.data
        print(f"Dati trovati per sito {sito_id_int}: {len(dati)}")
        if not dati or len(dati) < 10:
            print(f"Dati insufficienti per sito {sito_id_int}: {len(dati)} record")
            return {"errore": "Dati insufficienti"}
        df = pd.DataFrame(dati)
        df["data"] = pd.to_datetime(df["data"])
        df = df.groupby("data", as_index=True)["gruppo"].sum().sort_index()
        serie = df.asfreq("W").fillna(df.mean())
        exog_train = genera_variabili_esogene(serie.index, sito_id=sito_id_int, regione=regione)
        modello = SARIMAX(serie, exog=exog_train, order=(1,1,1), seasonal_order=(1,1,1,52),
                          enforce_stationarity=False, enforce_invertibility=False)
        risultato = modello.fit(disp=False)
        ultima_data = serie.index[-1]
        date_future = pd.date_range(start=ultima_data + timedelta(weeks=1), periods=settimane, freq="W")
        exog_future = genera_variabili_esogene(date_future, sito_id=sito_id_int, regione=regione)
        previsioni_raw = risultato.forecast(steps=settimane, exog=exog_future)
        output = [{"data": d.strftime("%Y-%m-%d"), "presenze_previste": max(0, round(float(v)))}
                  for d, v in zip(date_future, previsioni_raw)]
        print(f"Previsioni generate per sito {sito_id_int}: {len(output)} settimane")
        return {"sito_id": sito_id_int, "previsioni": output}
    except Exception as e:
        print(f"Errore previsioni sito {sito_id}: {e}")
        return {"errore": str(e)}

@app.get("/aggiorna-previsioni")
async def aggiorna_tutte():
    try:
        siti = supabase.table("siti_culturali").select("id, nome_sito, comune_id").execute()
        print(f"Siti trovati: {len(siti.data)}")
        risultati = []
        for sito in siti.data:
            sito_id = sito["id"]
            nome_sito = sito.get("nome_sito", f"Sito {sito_id}")
            print(f"Elaboro sito {sito_id} - {nome_sito}")
            prev = previsioni(str(sito_id), regione="Lazio")
            print(f"Risultato previsioni sito {sito_id}: {prev}")
            if "previsioni" in prev:
                for p in prev["previsioni"]:
                    supabase.table("previsioni_affluenza").upsert({
                        "sito_id": sito_id,
                        "data_previsione": p["data"],
                        "affluenza_stimata": p["presenze_previste"],
                        "aggiornato_il": datetime.now().isoformat()
                    }).execute()

                utenti = supabase.table("utenti").select("email, ruolo") \
                    .in_("ruolo", ["admin", "comune"]).execute()
                destinatari = [u["email"] for u in utenti.data if u.get("email")]
                print(f"Destinatari email: {destinatari}")

                if destinatari:
                    await invia_alert_previsioni(nome_sito, prev["previsioni"], destinatari)

            risultati.append({"sito_id": sito_id, "stato": "ok"})
        return {"risultati": risultati}
    except Exception as e:
        print(f"Errore aggiorna_tutte: {e}")
        return {"errore": str(e)}

# ---- METEO ----
@app.post("/meteo/aggiorna")
async def aggiorna_meteo():
    risultati = await aggiorna_meteo_tutti_siti()
    return {"risultati": risultati}

# ---- EVENTI LOCALI ----
@app.get("/eventi/{sito_id}")
def get_eventi(sito_id: int):
    data = supabase.table("eventi_locali").select("*").eq("sito_id", sito_id).order("data_inizio").execute()
    return data.data

@app.post("/eventi")
def crea_evento(payload: dict):
    supabase.table("eventi_locali").insert(payload).execute()
    return {"status": "creato"}

@app.delete("/eventi/{evento_id}")
def elimina_evento(evento_id: int):
    supabase.table("eventi_locali").delete().eq("id", evento_id).execute()
    return {"status": "eliminato"}

# ---- FESTIVITA ----
@app.post("/festivita/popola/{anno}")
def popola_festivita_anno(anno: int):
    n = popola_festivita(anno)
    return {"records_inseriti": n}

    # ---- SIMULATORE SCENARI ----
@app.post("/simula-scenario")
def simula_scenario(payload: dict):
    try:
        sito_id = payload.get("sito_id")
        sito_id_int = int(sito_id)
        settimane = payload.get("settimane", 8)
        scenario = payload.get("scenario", {})

        response = supabase.table("presenza").select("*").eq("sito_id", sito_id_int).order("data").execute()
        dati = response.data
        if not dati or len(dati) < 10:
            return {"errore": "Dati insufficienti"}

        df = pd.DataFrame(dati)
        df["data"] = pd.to_datetime(df["data"])
        df = df.groupby("data", as_index=True)["gruppo"].sum().sort_index()
        serie = df.asfreq("W").fillna(df.mean())

        exog_train = genera_variabili_esogene(serie.index, sito_id=sito_id_int, regione="Lazio")
        modello = SARIMAX(serie, exog=exog_train, order=(1,1,1), seasonal_order=(1,1,1,52),
                          enforce_stationarity=False, enforce_invertibility=False)
        risultato = modello.fit(disp=False)

        ultima_data = serie.index[-1]
        date_future = pd.date_range(start=ultima_data + timedelta(weeks=1), periods=settimane, freq="W")

        # Costruisci esogene con valori dello scenario
        exog_scenario = []
        for data in date_future:
            exog_scenario.append([
                scenario.get("is_festivo", 0),
                1 if data.month in [3,4,5] else 2 if data.month in [6,7,8] else 0 if data.month in [12,1,2] else 3,
                scenario.get("temperatura", 20),
                scenario.get("is_pioggia", 0),
                scenario.get("impatto_evento", 0),
                scenario.get("is_weekend", 0),
            ])

        exog_scenario = np.array(exog_scenario)
        previsioni_raw = risultato.forecast(steps=settimane, exog=exog_scenario)
        output = [{"data": d.strftime("%Y-%m-%d"), "presenze_previste": max(0, round(float(v)))}
                  for d, v in zip(date_future, previsioni_raw)]

        return {"sito_id": sito_id_int, "previsioni": output}
    except Exception as e:
        print(f"Errore simulazione: {e}")
        return {"errore": str(e)}

        # ---- REVENUE FORECASTING ----
@app.get("/tariffe/{sito_id}")
def get_tariffe(sito_id: int):
    try:
        data = supabase.table("siti_culturali").select(
            "nome_sito, prezzo_biglietto, prezzo_ridotto, percentuale_ridotti, "
            "percentuale_bookshop, spesa_media_bookshop, "
            "percentuale_ristorazione, spesa_media_ristorazione"
        ).eq("id", sito_id).single().execute()
        return data.data
    except Exception as e:
        return {"errore": str(e)}

@app.put("/tariffe/{sito_id}")
def aggiorna_tariffe(sito_id: int, payload: dict):
    try:
        supabase.table("siti_culturali").update(payload).eq("id", sito_id).execute()
        return {"status": "aggiornato"}
    except Exception as e:
        return {"errore": str(e)}

        # ---- GENERAZIONE RELAZIONE ----
@app.post("/genera-relazione")
async def genera_relazione(payload: dict):
    import httpx
    try:
        nome_sito = payload.get("nome_sito")
        mese = payload.get("mese")
        dati = payload.get("dati", {})

        prompt = f"""Sei un esperto di gestione dei beni culturali italiani. Genera una relazione mensile professionale e istituzionale per il sito "{nome_sito}" relativa al mese di {mese}.

Dati del mese:
- Visitatori totali: {dati.get('totaleVisitatori', 0)}
- Visitatori mese precedente: {dati.get('totalePrec', 0)}
- Variazione percentuale: {dati.get('varPercent', 0)}%
- Ricavi stimati: €{dati.get('ricaviTotali', 0)}
- Condizione meteo prevalente: {dati.get('meteoPrev', 'non disponibile')}
- Temperatura media: {dati.get('tempMedia', 'non disponibile')}°C
- Top provenienza visitatori: {dati.get('topProv', [])}
- Eventi del mese: {dati.get('eventi', [])}
- Previsione visitatori prossimo mese: {dati.get('prevTotale', 0)}

Struttura la relazione con queste sezioni:
## SINTESI ESECUTIVA
## ANALISI AFFLUENZA
## ANALISI ECONOMICA
## PROFILO DEI VISITATORI
## FATTORI CONTESTUALI
## PREVISIONI MESE SUCCESSIVO
## RACCOMANDAZIONI STRATEGICHE

Scrivi in italiano formale e istituzionale. Sii specifico con i numeri. Lunghezza: circa 600-800 parole."""

        ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 2000,
                    "messages": [{"role": "user", "content": prompt}]
                },
                timeout=60
            )
            risultato = resp.json()
            testo = risultato["content"][0]["text"]

        return {"testo": testo}
    except Exception as e:
        print(f"Errore generazione relazione: {e}")
        return {"errore": str(e)}