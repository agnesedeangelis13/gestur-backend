
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
        response = supabase.table("presenza").select("*").eq("sito_id", sito_id).order("data").execute()
        dati = response.data
        print(f"Dati trovati per sito {sito_id}: {len(dati)}")
        if not dati or len(dati) < 10:
            print(f"Dati insufficienti per sito {sito_id}: {len(dati)} record")
            return {"errore": "Dati insufficienti"}
        df = pd.DataFrame(dati)
        df["data"] = pd.to_datetime(df["data"])
        df = df.sort_values("data").set_index("data")
        serie = df["gruppo"].asfreq("W").fillna(df["gruppo"].mean())
        exog_train = genera_variabili_esogene(serie.index, sito_id=sito_id, regione=regione)
        modello = SARIMAX(serie, exog=exog_train, order=(1,1,1), seasonal_order=(1,1,1,52),
                          enforce_stationarity=False, enforce_invertibility=False)
        risultato = modello.fit(disp=False)
        ultima_data = serie.index[-1]
        date_future = pd.date_range(start=ultima_data + timedelta(weeks=1), periods=settimane, freq="W")
        exog_future = genera_variabili_esogene(date_future, sito_id=sito_id, regione=regione)
        previsioni_raw = risultato.forecast(steps=settimane, exog=exog_future)
        output = [{"data": d.strftime("%Y-%m-%d"), "presenze_previste": max(0, round(float(v)))}
                  for d, v in zip(date_future, previsioni_raw)]
        print(f"Previsioni generate per sito {sito_id}: {len(output)} settimane")
        return {"sito_id": sito_id, "previsioni": output}
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