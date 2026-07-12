import httpx
import os
from datetime import date
from supabase import create_client

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

def classifica_condizione(codice_wmo: int) -> str:
    if codice_wmo in (95, 96, 99):
        return "temporale"
    elif codice_wmo in (71, 73, 75, 77, 85, 86):
        return "neve"
    elif codice_wmo in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82):
        return "pioggia"
    elif codice_wmo in (45, 48):
        return "nebbia"
    elif codice_wmo == 0:
        return "sole"
    else:
        return "nuvoloso"

async def fetch_meteo_per_sito(sito_id: int, lat: float, lon: float):
    url = "https://api.open-meteo.com/v1/forecast"
    parametri = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,weather_code",
        "timezone": "Europe/Rome",
        "forecast_days": 1,
    }
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, params=parametri, timeout=10)
        resp.raise_for_status()
        dati = resp.json()

    giornaliero = dati.get("daily", {})
    record = {
        "sito_id": sito_id,
        "data": str(date.today()),
        "temperatura_max": giornaliero["temperature_2m_max"][0],
        "temperatura_min": giornaliero["temperature_2m_min"][0],
        "precipitazioni_mm": giornaliero.get("precipitation_sum", [0])[0] or 0,
        "condizione": classifica_condizione(giornaliero["weather_code"][0]),
        "fonte": "open-meteo",
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