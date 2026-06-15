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

def genera_variabili_esogene(date_range):
    festivi = ["01-01","06-01","25-04","01-05","02-06","15-08","01-11","08-12","25-12","26-12"]
    exog = []
    for data in date_range:
        mese_giorno = data.strftime("%d-%m")
        is_festivo = 1 if mese_giorno in festivi else 0
        mese = data.month
        if mese in [12,1,2]: stagione = 0
        elif mese in [3,4,5]: stagione = 1
        elif mese in [6,7,8]: stagione = 2
        else: stagione = 3
        exog.append([is_festivo, stagione])
    return np.array(exog)

@app.get("/")
def root():
    return {"status": "GesTur Backend attivo"}

@app.get("/previsioni/{sito_id}")
def previsioni(sito_id: str, settimane: int = 4):
    try:
        response = supabase.table("presenze").select("*").eq("sito_id", sito_id).order("data").execute()
        dati = response.data
        if not dati or len(dati) < 10:
            return {"errore": "Dati insufficienti"}
        df = pd.DataFrame(dati)
        df["data"] = pd.to_datetime(df["data"])
        df = df.sort_values("data").set_index("data")
        serie = df["presenze"].asfreq("W").fillna(df["presenze"].mean())
        exog_train = genera_variabili_esogene(serie.index)
        modello = SARIMAX(serie, exog=exog_train, order=(1,1,1), seasonal_order=(1,1,1,52), enforce_stationarity=False, enforce_invertibility=False)
        risultato = modello.fit(disp=False)
        ultima_data = serie.index[-1]
        date_future = pd.date_range(start=ultima_data + timedelta(weeks=1), periods=settimane, freq="W")
        exog_future = genera_variabili_esogene(date_future)
        previsioni_raw = risultato.forecast(steps=settimane, exog=exog_future)
        output = [{"data": d.strftime("%Y-%m-%d"), "presenze_previste": max(0, round(float(v)))} for d, v in zip(date_future, previsioni_raw)]
        return {"sito_id": sito_id, "previsioni": output}
    except Exception as e:
        return {"errore": str(e)}

@app.get("/aggiorna-previsioni")
def aggiorna_tutte():
    try:
        siti = supabase.table("siti").select("id").execute()
        risultati = []
        for sito in siti.data:
            sito_id = sito["id"]
            prev = previsioni(sito_id)
            if "previsioni" in prev:
                for p in prev["previsioni"]:
                    supabase.table("previsioni").upsert({"sito_id": sito_id, "data": p["data"], "presenze_previste": p["presenze_previste"], "aggiornato_il": datetime.now().isoformat()}).execute()
            risultati.append({"sito_id": sito_id, "stato": "ok"})
        return {"risultati": risultati}
    except Exception as e:
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