import httpx
import os
from datetime import date
from supabase import create_client

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
OWM_API_KEY = os.getenv("OPENWEATHERMAP_API_KEY")

def classifica_condizione(weather_id: int) -> str:
    if weather_id < 300: return "temporale"
    elif weather_id < 600: return "pioggia"
    elif weather_id < 700: return "neve"
    elif weather_id < 800: return "nebbia"
    elif weather_id == 800: return "sole"
    else: return "nuvoloso"

async def fetch_meteo_per_sito(sito_id: int, lat: float, lon: float):
    url = f"https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&appid={OWM_API_KEY}&units=metric"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, timeout=10)
        resp.raise_for_status()
        dati = resp.json()
    
    record = {
        "sito_id": sito_id,
        "data": str(date.today()),
        "temperatura_max": dati["main"]["temp_max"],
        "temperatura_min": dati["main"]["temp_min"],
        "precipitazioni_mm": dati.get("rain", {}).get("1h", 0),
        "condizione": classifica_condizione(dati["weather"][0]["id"]),
        "fonte": "openweathermap"
    }
    supabase.table("meteo_giornaliero").upsert(record, on_conflict="sito_id,data").execute()
    return record

async def aggiorna_meteo_tutti_siti():
    siti = supabase.table("siti_culturali").select("id, nome_sito, lat, lon").execute()
    risultati = []
    for sito in siti.data:
        if sito.get("lat") and sito.get("lon"):
            try:
                r = await fetch_meteo_per_sito(sito["id"], sito["lat"], sito["lon"])
                risultati.append({"sito": sito["nome_sito"], "status": "ok", "condizione": r["condizione"]})
            except Exception as e:
                risultati.append({"sito": sito["nome_sito"], "status": f"errore: {e}"})
    return risultati