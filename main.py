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
                        "generata_il": datetime.now().isoformat()
                    }, on_conflict="sito_id,data_previsione").execute()

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

        # ---- PREVISORE BILANCIO STAGIONALE ----
MACRO_PROVENIENZA = {
    "Italia": "Italia",
    "USA": "Nord America", "Canada": "Nord America",
}

def mappa_provenienza_macro(provenienza):
    if provenienza in MACRO_PROVENIENZA:
        return MACRO_PROVENIENZA[provenienza]
    europa = ["Francia","Germania","Spagna","Regno Unito","Svizzera","Austria","Belgio","Paesi Bassi","Portogallo","Irlanda","Polonia","Svezia","Norvegia","Danimarca","Finlandia","Grecia","Russia","Ucraina","Romania","Ungheria","Repubblica Ceca","Croazia","Slovenia","Slovacchia","Bulgaria","Albania","Serbia","Montenegro","Bosnia ed Erzegovina","Macedonia del Nord","Kosovo","Moldova","Lituania","Lettonia","Estonia","Lussemburgo","Malta","Cipro","Islanda","Liechtenstein","Monaco","San Marino","Andorra"]
    if provenienza in europa:
        return "Europa"
    return "Resto del mondo"

def normalizza_fascia(fascia):
    if not fascia:
        return fascia
    return fascia.strip().replace("\u2013", "-").replace("\u2014", "-")

def calcola_composizione_giorno(dati_storici, giorno_settimana):
    righe_giorno = [r for r in dati_storici if pd.to_datetime(r["data"]).weekday() == giorno_settimana]
    if not righe_giorno:
        righe_giorno = dati_storici  # fallback: usa tutto lo storico se manca quel giorno specifico
    if not righe_giorno:
        return []
    composizione = {}
    totale_persone = 0
    for r in righe_giorno:
        fasce = (r.get("fasce") or "").split(", ")
        fasce = [normalizza_fascia(f) for f in fasce if f]
        if not fasce:
            continue
        n_persone = r.get("gruppo", 0) or 0
        tipo = r.get("tipo_visitatore") or "gruppo"
        prov_macro = mappa_provenienza_macro(r.get("provenienza"))
        per_fascia = n_persone / len(fasce)
        for f in fasce:
            chiave = (f, prov_macro, tipo)
            composizione[chiave] = composizione.get(chiave, 0) + per_fascia
            totale_persone += per_fascia
    if totale_persone == 0:
        return []
    return [
        {"fascia": k[0], "provenienza_macro": k[1], "tipo_visitatore": k[2], "quota": v / totale_persone}
        for k, v in composizione.items()
    ]

@app.get("/previsioni-economiche/{sito_id}")
def previsioni_economiche(sito_id: str, giorni: int = 14):
    try:
        sito_id_int = int(sito_id)

        tariffe_resp = supabase.table("siti_culturali").select(
            "nome_sito, prezzo_biglietto, prezzo_ridotto, percentuale_ridotti, "
            "percentuale_bookshop, spesa_media_bookshop, "
            "percentuale_ristorazione, spesa_media_ristorazione"
        ).eq("id", sito_id_int).single().execute()
        tariffe = tariffe_resp.data
        if not tariffe:
            return {"errore": "Sito non trovato"}

        coeff_resp = supabase.table("coefficienti_spesa").select("*").eq("sito_id", sito_id_int).execute()
        coefficienti = {(c["fascia"], c["provenienza_macro"], c["tipo_visitatore"]): c["coefficiente"] for c in coeff_resp.data}

        novanta_giorni_fa = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
        storico_resp = supabase.table("presenza").select("data, gruppo, fasce, provenienza, tipo_visitatore") \
            .eq("sito_id", sito_id_int).gte("data", novanta_giorni_fa).execute()
        storico = storico_resp.data

        oggi = datetime.now().strftime("%Y-%m-%d")
        fine = (datetime.now() + timedelta(days=giorni)).strftime("%Y-%m-%d")
        prev_resp = supabase.table("previsioni_affluenza").select("*").eq("sito_id", sito_id_int) \
            .gte("data_previsione", oggi).lte("data_previsione", fine).order("data_previsione").execute()
        previsioni_aff = prev_resp.data

        if not previsioni_aff:
            return {"errore": "Nessuna previsione di affluenza disponibile per questo sito"}

        risultati = []
        for p in previsioni_aff:
            data_str = p["data_previsione"]
            visitatori_previsti = p["affluenza_stimata"]
            giorno_settimana = pd.to_datetime(data_str).weekday()

            composizione = calcola_composizione_giorno(storico, giorno_settimana)

            ricavo_biglietti = 0
            ricavo_commerciale = 0
            dettaglio_profili = []

            for comp in composizione:
                n_persone_profilo = visitatori_previsti * comp["quota"]
                chiave = (comp["fascia"], comp["provenienza_macro"], comp["tipo_visitatore"])
                coeff = coefficienti.get(chiave, 1.0)

                prezzo_medio = tariffe["prezzo_biglietto"] * (1 - tariffe["percentuale_ridotti"]/100) + tariffe["prezzo_ridotto"] * (tariffe["percentuale_ridotti"]/100)
                bookshop_base = (tariffe["percentuale_bookshop"]/100) * tariffe["spesa_media_bookshop"]
                ristorazione_base = (tariffe["percentuale_ristorazione"]/100) * tariffe["spesa_media_ristorazione"]

                ricavo_biglietti += n_persone_profilo * prezzo_medio
                ricavo_commerciale += n_persone_profilo * (bookshop_base + ristorazione_base) * coeff

                dettaglio_profili.append({
                    "profilo": f"{comp['fascia']} · {comp['provenienza_macro']} · {comp['tipo_visitatore']}",
                    "quota_pct": round(comp["quota"] * 100, 1),
                    "coefficiente": coeff
                })

            margine_netto = ricavo_biglietti + ricavo_commerciale
            dettaglio_profili.sort(key=lambda x: x["quota_pct"], reverse=True)
            composizione_dominante = dettaglio_profili[0]["profilo"] if dettaglio_profili else "N/D"

            risultati.append({
                "data": data_str,
                "visitatori_previsti": round(visitatori_previsti, 1),
                "ricavo_biglietti": round(ricavo_biglietti, 2),
                "ricavo_commerciale": round(ricavo_commerciale, 2),
                "margine_netto": round(margine_netto, 2),
                "composizione_dominante": composizione_dominante,
                "top_profili": dettaglio_profili[:3]
            })

            supabase.table("previsioni_economiche").upsert({
                "sito_id": sito_id_int,
                "data": data_str,
                "visitatori_previsti": round(visitatori_previsti, 1),
                "ricavo_biglietti": round(ricavo_biglietti, 2),
                "ricavo_commerciale": round(ricavo_commerciale, 2),
                "margine_netto": round(margine_netto, 2),
                "composizione_dominante": composizione_dominante,
            }, on_conflict="sito_id,data").execute()

        return {"sito_id": sito_id_int, "nome_sito": tariffe["nome_sito"], "previsioni": risultati}

    except Exception as e:
        print(f"Errore previsioni economiche sito {sito_id}: {e}")
        return {"errore": str(e)}

        # ---- INDICE DI AUTONOMIA FINANZIARIA PREDITTIVA ----
@app.get("/indice-autonomia/{sito_id}")
def indice_autonomia(sito_id: str):
    try:
        sito_id_int = int(sito_id)

        sito_resp = supabase.table("siti_culturali").select("nome_sito, costo_fisso_settimanale").eq("id", sito_id_int).single().execute()
        sito = sito_resp.data
        if not sito:
            return {"errore": "Sito non trovato"}

        costo_fisso = sito.get("costo_fisso_settimanale") or 0
        if costo_fisso <= 0:
            return {"errore": "Costo fisso settimanale non impostato per questo sito"}

        risultato_economico = previsioni_economiche(sito_id, giorni=7)
        if "errore" in risultato_economico:
            return {"errore": risultato_economico["errore"]}

        previsioni_7gg = risultato_economico["previsioni"]
        ricavi_totali = sum(p["margine_netto"] for p in previsioni_7gg)
        indice_pct = round((ricavi_totali / costo_fisso) * 100, 1)
        surplus_deficit = round(ricavi_totali - costo_fisso, 2)

        profili_settimana = {}
        for g in previsioni_7gg:
            for p in g.get("top_profili", []):
                profili_settimana[p["profilo"]] = profili_settimana.get(p["profilo"], 0) + p["quota_pct"]
        profilo_trainante = max(profili_settimana.items(), key=lambda x: x[1])[0] if profili_settimana else "N/D"

        if indice_pct >= 100:
            stato = "autofinanziamento"
            verdetto = f"Il sito è in autofinanziamento, generando un surplus reinvestibile di €{abs(surplus_deficit):,.0f}.".replace(",", ".")
        elif indice_pct >= 90:
            stato = "equilibrio"
            verdetto = f"Il sito è in equilibrio finanziario, con una variazione di €{surplus_deficit:,.0f} rispetto al pareggio.".replace(",", ".")
        else:
            stato = "sotto_soglia"
            verdetto = f"Il sito è sotto la soglia di sostenibilità, con un deficit previsto di €{abs(surplus_deficit):,.0f}.".replace(",", ".")

        return {
            "sito_id": sito_id_int,
            "nome_sito": sito["nome_sito"],
            "indice_autonomia_pct": indice_pct,
            "ricavi_previsti_7gg": round(ricavi_totali, 2),
            "costo_fisso_settimanale": costo_fisso,
            "surplus_deficit": surplus_deficit,
            "stato": stato,
            "profilo_trainante": profilo_trainante,
            "verdetto": verdetto
        }

    except Exception as e:
        print(f"Errore indice autonomia sito {sito_id}: {e}")
        return {"errore": str(e)}

        # ---- PREDICTIVE BREAK-EVEN MATRIX ----
@app.get("/break-even/{sito_id}")
def break_even(sito_id: str):
    try:
        sito_id_int = int(sito_id)

        sito_resp = supabase.table("siti_culturali").select("nome_sito, costo_fisso_settimanale").eq("id", sito_id_int).single().execute()
        sito = sito_resp.data
        if not sito:
            return {"errore": "Sito non trovato"}

        costo_fisso = sito.get("costo_fisso_settimanale") or 0
        if costo_fisso <= 0:
            return {"errore": "Costo fisso settimanale non impostato per questo sito"}

        risultato_economico = previsioni_economiche(sito_id, giorni=7)
        if "errore" in risultato_economico:
            return {"errore": risultato_economico["errore"]}

        previsioni_7gg = risultato_economico["previsioni"]
        nomi_giorni = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]

        cumulativo = 0
        giorno_pareggio = None
        dettaglio_giorni = []

        for p in previsioni_7gg:
            cumulativo += p["margine_netto"]
            giorno_settimana = pd.to_datetime(p["data"]).weekday()
            raggiunto = cumulativo >= costo_fisso and giorno_pareggio is None
            if raggiunto:
                giorno_pareggio = {
                    "data": p["data"],
                    "nome_giorno": nomi_giorni[giorno_settimana],
                    "indice_giorno": len(dettaglio_giorni)
                }
            dettaglio_giorni.append({
                "data": p["data"],
                "nome_giorno": nomi_giorni[giorno_settimana],
                "margine_giorno": round(p["margine_netto"], 2),
                "cumulativo": round(cumulativo, 2),
                "pct_costo_coperto": round(min(cumulativo / costo_fisso, 1.5) * 100, 1),
                "e_giorno_pareggio": raggiunto
            })

        profili_settimana = {}
        for g in previsioni_7gg:
            for p in g.get("top_profili", []):
                profili_settimana[p["profilo"]] = profili_settimana.get(p["profilo"], 0) + p["quota_pct"]
        profilo_trainante = max(profili_settimana.items(), key=lambda x: x[1])[0] if profili_settimana else "N/D"

        if giorno_pareggio:
            giorni_residui = 7 - giorno_pareggio["indice_giorno"] - 1
            verdetto = (
                f"Grazie all'alta concentrazione prevista di \"{profilo_trainante}\", "
                f"il punto di pareggio aziendale verrà raggiunto {giorno_pareggio['nome_giorno'].lower()}. "
                + (f"I successivi {giorni_residui} giorni di flussi saranno orientati al 100% di utile netto."
                   if giorni_residui > 0 else "Il pareggio coincide con la fine della settimana.")
            )
        else:
            mancante = round(costo_fisso - cumulativo, 2)
            verdetto = (
                f"Il punto di pareggio non viene raggiunto entro la settimana prevista. "
                f"Mancano €{mancante:,.0f} per coprire i costi fissi, nonostante la presenza di \"{profilo_trainante}\".".replace(",", ".")
            )

        return {
            "sito_id": sito_id_int,
            "nome_sito": sito["nome_sito"],
            "costo_fisso_settimanale": costo_fisso,
            "giorno_pareggio": giorno_pareggio,
            "profilo_trainante": profilo_trainante,
            "verdetto": verdetto,
            "dettaglio_giorni": dettaglio_giorni
        }

    except Exception as e:
        print(f"Errore break-even sito {sito_id}: {e}")
        return {"errore": str(e)}

        # ---- MATRICE DI ALLOCAZIONE BUDGET DI PROMOZIONE ----
COSTO_MARGINALE_PCT = 0.15  # quota stimata di costi variabili sul ricavo per visitatore

def calcola_clv_clusters(sito_id_int, giorni_storico=90, data_inizio=None, data_fine=None):
    tariffe_resp = supabase.table("siti_culturali").select(
        "nome_sito, costo_fisso_settimanale, prezzo_biglietto, prezzo_ridotto, percentuale_ridotti, "
        "percentuale_bookshop, spesa_media_bookshop, "
        "percentuale_ristorazione, spesa_media_ristorazione"
    ).eq("id", sito_id_int).single().execute()
    tariffe = tariffe_resp.data
    if not tariffe:
        return None, "Sito non trovato"

    coeff_resp = supabase.table("coefficienti_spesa").select("*").eq("sito_id", sito_id_int).execute()
    coefficienti = {(c["fascia"], c["provenienza_macro"], c["tipo_visitatore"]): c["coefficiente"] for c in coeff_resp.data}

    if data_inizio is None:
        data_inizio = (datetime.now() - timedelta(days=giorni_storico)).strftime("%Y-%m-%d")

    query = supabase.table("presenza").select("data, gruppo, fasce, provenienza, tipo_visitatore") \
        .eq("sito_id", sito_id_int).gte("data", data_inizio)
    if data_fine:
        query = query.lte("data", data_fine)
    storico_resp = query.execute()
    storico = storico_resp.data

    if not storico:
        return None, "Nessun dato storico disponibile per questo sito nel periodo richiesto"

    prezzo_medio = tariffe["prezzo_biglietto"] * (1 - tariffe["percentuale_ridotti"]/100) + tariffe["prezzo_ridotto"] * (tariffe["percentuale_ridotti"]/100)
    bookshop_base = (tariffe["percentuale_bookshop"]/100) * tariffe["spesa_media_bookshop"]
    ristorazione_base = (tariffe["percentuale_ristorazione"]/100) * tariffe["spesa_media_ristorazione"]

    cluster_dati = {}  # chiave: (provenienza, fascia, tipo_visitatore)

    for r in storico:
        fasce = (r.get("fasce") or "").split(", ")
        fasce = [normalizza_fascia(f) for f in fasce if f]
        if not fasce:
            continue
        n_persone = r.get("gruppo", 0) or 0
        tipo = r.get("tipo_visitatore") or "gruppo"
        provenienza = r.get("provenienza") or "N/D"
        prov_macro = mappa_provenienza_macro(provenienza)
        per_fascia = n_persone / len(fasce)

        for f in fasce:
            chiave = (provenienza, f, tipo)
            coeff = coefficienti.get((f, prov_macro, tipo), 1.0)

            ricavo_biglietti = per_fascia * prezzo_medio
            ricavo_commerciale = per_fascia * (bookshop_base + ristorazione_base) * coeff
            ricavo_cluster = ricavo_biglietti + ricavo_commerciale

            if chiave not in cluster_dati:
                cluster_dati[chiave] = {"n_presenze": 0, "ricavo_storico": 0}
            cluster_dati[chiave]["n_presenze"] += per_fascia
            cluster_dati[chiave]["ricavo_storico"] += ricavo_cluster

    costo_fisso = tariffe.get("costo_fisso_settimanale") or 0
    if data_fine:
        giorni_periodo = (datetime.strptime(data_fine, "%Y-%m-%d") - datetime.strptime(data_inizio, "%Y-%m-%d")).days + 1
    else:
        giorni_periodo = giorni_storico

    risultati = []
    for (provenienza, fascia, tipo), d in cluster_dati.items():
        ricavo_storico = d["ricavo_storico"]
        costi_marginali = ricavo_storico * COSTO_MARGINALE_PCT
        clv = ricavo_storico - costi_marginali

        margine_medio_giornaliero = ricavo_storico / giorni_periodo if giorni_periodo > 0 else 0
        if costo_fisso > 0 and margine_medio_giornaliero > 0:
            giorni_breakeven_raw = (costo_fisso / 7) / margine_medio_giornaliero
            giorni_breakeven = round(giorni_breakeven_raw, 1)
            breakeven_oltre_anno = giorni_breakeven_raw > 365
        else:
            giorni_breakeven = None
            breakeven_oltre_anno = False

        risultati.append({
            "provenienza": provenienza,
            "fascia": fascia,
            "tipo_visitatore": tipo,
            "n_presenze_storiche": round(d["n_presenze"], 1),
            "ricavo_storico": round(ricavo_storico, 2),
            "clv": round(clv, 2),
            "giorni_breakeven_cluster": giorni_breakeven if not breakeven_oltre_anno else None,
            "breakeven_oltre_anno": breakeven_oltre_anno
        })

    return {"cluster": risultati, "nome_sito": tariffe["nome_sito"]}, None


def calcola_range_mese(anno, mese):
    data_inizio = f"{anno}-{mese:02d}-01"
    if mese == 12:
        ultimo_giorno = datetime(anno + 1, 1, 1) - timedelta(days=1)
    else:
        ultimo_giorno = datetime(anno, mese + 1, 1) - timedelta(days=1)
    data_fine = ultimo_giorno.strftime("%Y-%m-%d")
    return data_inizio, data_fine


def esegui_snapshot_clv_mensile(sito_id_int, anno_target, mese_target):
    data_inizio, data_fine = calcola_range_mese(anno_target, mese_target)

    risultato, errore = calcola_clv_clusters(sito_id_int, data_inizio=data_inizio, data_fine=data_fine)
    if errore:
        return {"errore": errore, "anno": anno_target, "mese": mese_target}

    cluster_list = risultato["cluster"]
    if not cluster_list:
        return {"errore": "Nessun cluster con dati per questo mese", "anno": anno_target, "mese": mese_target}

    salvati = 0
    for c in cluster_list:
        supabase.table("storico_clv_mensile").upsert({
            "sito_id": sito_id_int,
            "anno": anno_target,
            "mese": mese_target,
            "provenienza": c["provenienza"],
            "fascia": c["fascia"],
            "tipo_visitatore": c["tipo_visitatore"],
            "n_presenze": c["n_presenze_storiche"],
            "clv": c["clv"],
            "generato_il": datetime.now().isoformat()
        }, on_conflict="sito_id,anno,mese,provenienza,fascia,tipo_visitatore").execute()
        salvati += 1

    return {
        "sito_id": sito_id_int,
        "anno": anno_target,
        "mese": mese_target,
        "cluster_salvati": salvati
    }


@app.get("/snapshot-clv-mensile/{sito_id}")
def salva_snapshot_clv_mensile(sito_id: str, anno: int = None, mese: int = None):
    try:
        sito_id_int = int(sito_id)
        oggi = datetime.now()
        anno_target = anno or oggi.year
        mese_target = mese or oggi.month

        return esegui_snapshot_clv_mensile(sito_id_int, anno_target, mese_target)

    except Exception as e:
        print(f"Errore snapshot CLV mensile sito {sito_id}: {e}")
        return {"errore": str(e)}


@app.get("/snapshot-clv-mensile-tutti")
def salva_snapshot_clv_mensile_tutti(anno: int = None, mese: int = None):
    try:
        oggi = datetime.now()
        anno_target = anno or oggi.year
        mese_target = mese or oggi.month

        siti_resp = supabase.table("siti_culturali").select("id, nome_sito").execute()
        siti = siti_resp.data or []

        risultati = []
        for sito in siti:
            sito_id_int = sito["id"]
            esito = esegui_snapshot_clv_mensile(sito_id_int, anno_target, mese_target)
            esito["nome_sito"] = sito.get("nome_sito", f"Sito {sito_id_int}")
            risultati.append(esito)

        return {
            "anno": anno_target,
            "mese": mese_target,
            "siti_processati": len(risultati),
            "risultati": risultati
        }

    except Exception as e:
        print(f"Errore snapshot CLV mensile tutti i siti: {e}")
        return {"errore": str(e)}


def calcola_preavviso_marketing(sito_id_int):
    oggi = datetime.now()
    mese_prossimo = oggi.month + 1 if oggi.month < 12 else 1
    anno_riferimento = oggi.year - 1 if oggi.month < 12 else oggi.year - 1
    # Se il mese prossimo cade nell'anno successivo (es. oggi dicembre, prossimo mese gennaio),
    # l'anno di riferimento dello scorso ciclo resta comunque "un anno fa rispetto a oggi".

    snapshot_resp = supabase.table("storico_clv_mensile").select("*") \
        .eq("sito_id", sito_id_int).eq("anno", anno_riferimento).eq("mese", mese_prossimo) \
        .order("clv", desc=True).execute()
    snapshot = snapshot_resp.data

    if not snapshot:
        return None

    cluster_top_anno_scorso = snapshot[0]

    nomi_mesi = ["", "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
                 "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"]
    nome_mese_prossimo = nomi_mesi[mese_prossimo]

    messaggio = (
        f"Avviso anticipato: lo scorso anno, a {nome_mese_prossimo}, il cluster \"{cluster_top_anno_scorso['tipo_visitatore']} "
        f"provenienti da {cluster_top_anno_scorso['provenienza']}\" di fascia {cluster_top_anno_scorso['fascia']} anni "
        f"è stato il segmento con il valore più alto. Se il pattern si confermasse, anticipare la campagna di promozione "
        f"su questo target nelle prossime settimane potrebbe massimizzare l'affluenza prevista. "
        f"Questo è un confronto storico, non una previsione garantita: il turismo è un fenomeno variabile."
    )

    return {
        "mese_riferimento": nome_mese_prossimo,
        "anno_riferimento": anno_riferimento,
        "cluster_atteso": {
            "provenienza": cluster_top_anno_scorso["provenienza"],
            "fascia": cluster_top_anno_scorso["fascia"],
            "tipo_visitatore": cluster_top_anno_scorso["tipo_visitatore"],
            "clv_anno_scorso": cluster_top_anno_scorso["clv"]
        },
        "messaggio": messaggio
    }


@app.get("/budget-promozione/{sito_id}")
def budget_promozione(sito_id: str):
    try:
        sito_id_int = int(sito_id)

        risultato, errore = calcola_clv_clusters(sito_id_int)
        if errore:
            return {"errore": errore}

        cluster_list = risultato["cluster"]
        nome_sito = risultato["nome_sito"]

        if not cluster_list:
            return {"errore": "Nessun cluster con dati sufficienti per questo sito"}

        clv_totale = sum(c["clv"] for c in cluster_list)
        if clv_totale <= 0:
            return {"errore": "CLV complessivo non positivo: dati insufficienti o costi superiori ai ricavi storici"}

        giorni_validi = [c["giorni_breakeven_cluster"] for c in cluster_list if c["giorni_breakeven_cluster"] is not None]
        media_giorni_breakeven = sum(giorni_validi) / len(giorni_validi) if giorni_validi else None

        for c in cluster_list:
            c["pct_budget_allocato"] = round((c["clv"] / clv_totale) * 100, 1) if c["clv"] > 0 else 0.0
            if c["giorni_breakeven_cluster"] is not None and media_giorni_breakeven and media_giorni_breakeven > 0:
                c["velocita_relativa"] = round(media_giorni_breakeven / c["giorni_breakeven_cluster"], 2) if c["giorni_breakeven_cluster"] > 0 else None
            else:
                c["velocita_relativa"] = None

        cluster_list.sort(key=lambda x: x["clv"], reverse=True)

        for c in cluster_list:
            supabase.table("previsioni_budget_promozione").upsert({
                "sito_id": sito_id_int,
                "provenienza": c["provenienza"],
                "fascia": c["fascia"],
                "tipo_visitatore": c["tipo_visitatore"],
                "n_presenze_storiche": c["n_presenze_storiche"],
                "ricavo_storico": c["ricavo_storico"],
                "clv": c["clv"],
                "pct_budget_allocato": c["pct_budget_allocato"],
                "giorni_breakeven_cluster": c["giorni_breakeven_cluster"],
                "velocita_relativa": c["velocita_relativa"],
                "breakeven_oltre_anno": c.get("breakeven_oltre_anno", False),
                "generata_il": datetime.now().isoformat()
            }, on_conflict="sito_id,provenienza,fascia,tipo_visitatore").execute()

        cluster_top = cluster_list[0]
        if cluster_top.get("breakeven_oltre_anno"):
            velocita_txt = "pur con un orizzonte di pareggio autonomo superiore all'anno se isolato dagli altri segmenti"
        elif cluster_top.get("velocita_relativa") and cluster_top["velocita_relativa"] > 1:
            velocita_txt = f"con una velocità di pareggio {cluster_top['velocita_relativa']}x rispetto alla media"
        else:
            velocita_txt = "con un ritmo di break-even nella norma"
        verdetto = (
            f"Il cluster \"{cluster_top['tipo_visitatore']} provenienti da {cluster_top['provenienza']}\" "
            f"di fascia {cluster_top['fascia']} anni si è confermato il segmento più rilevante per l'economia locale, "
            f"{velocita_txt}. Il consiglio di gestione suggerisce di allocare il "
            f"{cluster_top['pct_budget_allocato']}% del budget di promozione su questo target "
            f"per massimizzare il ritorno economico delle casse comunali. "
            f"Questa analisi si basa sui dati storici degli ultimi 90 giorni: è una lettura di ciò che ha "
            f"funzionato finora, non una previsione garantita per il futuro."
        )

        preavviso_marketing = calcola_preavviso_marketing(sito_id_int)

        return {
            "sito_id": sito_id_int,
            "nome_sito": nome_sito,
            "cluster_top": cluster_top,
            "verdetto": verdetto,
            "matrice": cluster_list,
            "preavviso_marketing": preavviso_marketing
        }

    except Exception as e:
        print(f"Errore budget promozione sito {sito_id}: {e}")
        return {"errore": str(e)}

        # ---- WELCOME DESK COST OPTIMIZER ----
def calcola_composizione_settimana(storico, date_settimana):
    """Aggrega la composizione per cluster su un insieme di giorni futuri, usando il pattern
    storico del relativo giorno della settimana (stesso meccanismo di calcola_composizione_giorno)."""
    composizione_per_giorno = []
    for d in date_settimana:
        giorno_settimana = pd.to_datetime(d).weekday()
        composizione_per_giorno.append(calcola_composizione_giorno(storico, giorno_settimana))
    return composizione_per_giorno


@app.get("/welcome-desk-planner/{sito_id}")
def welcome_desk_planner(sito_id: str):
    try:
        sito_id_int = int(sito_id)

        sito_resp = supabase.table("siti_culturali").select(
            "nome_sito, costo_stampa_materiale_settimanale, costo_assistenza_digitale_settimanale"
        ).eq("id", sito_id_int).single().execute()
        sito = sito_resp.data
        if not sito:
            return {"errore": "Sito non trovato"}

        coeff_canale_resp = supabase.table("coefficienti_canale").select("*").eq("sito_id", sito_id_int).execute()
        coeff_canale = {
            (c["fascia"], c["provenienza_macro"], c["tipo_visitatore"]): c["pct_preferenza_cartaceo"]
            for c in coeff_canale_resp.data
        }
        if not coeff_canale:
            return {"errore": "Nessun coefficiente di canale configurato per questo sito"}

        novanta_giorni_fa = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
        storico_resp = supabase.table("presenza").select("data, gruppo, fasce, provenienza, tipo_visitatore") \
            .eq("sito_id", sito_id_int).gte("data", novanta_giorni_fa).execute()
        storico = storico_resp.data
        if not storico:
            return {"errore": "Nessun dato storico disponibile per questo sito"}

        oggi = datetime.now()
        fine = oggi + timedelta(days=7)
        prev_resp = supabase.table("previsioni_affluenza").select("*").eq("sito_id", sito_id_int) \
            .gte("data_previsione", oggi.strftime("%Y-%m-%d")).lte("data_previsione", fine.strftime("%Y-%m-%d")) \
            .order("data_previsione").execute()
        previsioni_aff = prev_resp.data
        if not previsioni_aff:
            return {"errore": "Nessuna previsione di affluenza disponibile per questo sito"}

        nomi_giorni = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]
        dettaglio_giorni = []
        somma_pct_cartaceo_pesata = 0
        somma_visitatori = 0

        for p in previsioni_aff:
            data_str = p["data_previsione"]
            visitatori_previsti = p["affluenza_stimata"]
            giorno_settimana = pd.to_datetime(data_str).weekday()

            composizione = calcola_composizione_giorno(storico, giorno_settimana)

            pct_cartaceo_giorno = 0
            for comp in composizione:
                chiave = (comp["fascia"], comp["provenienza_macro"], comp["tipo_visitatore"])
                pref_cartaceo = coeff_canale.get(chiave, 50)  # default neutro se cluster non configurato
                pct_cartaceo_giorno += comp["quota"] * pref_cartaceo

            composizione_ordinata = sorted(composizione, key=lambda x: x["quota"], reverse=True)
            cluster_dominante_giorno = composizione_ordinata[0] if composizione_ordinata else None

            dettaglio_giorni.append({
                "data": data_str,
                "nome_giorno": nomi_giorni[giorno_settimana],
                "visitatori_previsti": round(visitatori_previsti, 1),
                "pct_preferenza_cartaceo": round(pct_cartaceo_giorno, 1),
                "pct_preferenza_digitale": round(100 - pct_cartaceo_giorno, 1),
                "cluster_dominante": (
                    f"{cluster_dominante_giorno['fascia']} · {cluster_dominante_giorno['provenienza_macro']} · {cluster_dominante_giorno['tipo_visitatore']}"
                    if cluster_dominante_giorno else "N/D"
                ),
                "e_weekend": giorno_settimana >= 5
            })

            somma_pct_cartaceo_pesata += pct_cartaceo_giorno * visitatori_previsti
            somma_visitatori += visitatori_previsti

        pct_cartaceo_settimana = round(somma_pct_cartaceo_pesata / somma_visitatori, 1) if somma_visitatori > 0 else 50
        pct_digitale_settimana = round(100 - pct_cartaceo_settimana, 1)

        costo_stampa = sito.get("costo_stampa_materiale_settimanale")
        costo_digitale = sito.get("costo_assistenza_digitale_settimanale")

        risparmio_stimato = None
        if costo_stampa is not None:
            # Riduzione di stampa proporzionale alla quota digitale della settimana,
            # rispetto a un'allocazione di base 50/50 presa come riferimento neutro.
            riduzione_pct = max(0, pct_digitale_settimana - 50) / 50  # 0 se <=50% digitale, fino a 1 se 100% digitale
            risparmio_stimato = round(costo_stampa * riduzione_pct, 2)

        giorni_weekend = [g for g in dettaglio_giorni if g["e_weekend"]]
        nota_weekend = None
        if giorni_weekend:
            media_cartaceo_weekend = sum(g["pct_preferenza_cartaceo"] for g in giorni_weekend) / len(giorni_weekend)
            media_cartaceo_settimana_feriale = pct_cartaceo_settimana
            if abs(media_cartaceo_weekend - media_cartaceo_settimana_feriale) > 15:
                if media_cartaceo_weekend > media_cartaceo_settimana_feriale:
                    nota_weekend = (
                        f"Il weekend si distingue dal resto della settimana con una preferenza più marcata "
                        f"per il materiale cartaceo ({round(media_cartaceo_weekend, 1)}%): si consiglia di "
                        f"rifornire i desk di mappe fisiche prima del weekend."
                    )
                else:
                    nota_weekend = (
                        f"Il weekend si distingue dal resto della settimana con una preferenza più marcata "
                        f"per l'assistenza digitale ({round(100 - media_cartaceo_weekend, 1)}% digitale): "
                        f"si consiglia di concentrare il personale sull'assistenza rapida via QR/app."
                    )

        if pct_digitale_settimana >= 65:
            indicazione = (
                f"la settimana sarà dominata da un pubblico a forte preferenza digitale "
                f"({pct_digitale_settimana}%). Il consiglio di gestione è ridurre la stampa di materiale "
                f"cartaceo e deviare il budget sull'assistenza digitale rapida (QR code, app, postazioni self-service)."
            )
        elif pct_cartaceo_settimana >= 65:
            indicazione = (
                f"la settimana sarà dominata da un pubblico a forte preferenza cartacea "
                f"({pct_cartaceo_settimana}%). Il consiglio di gestione è rifornire adeguatamente i desk "
                f"di mappe e materiale informativo fisico, e mantenere personale dedicato all'assistenza diretta."
            )
        else:
            indicazione = (
                f"la settimana presenta una composizione mista tra preferenza cartacea ({pct_cartaceo_settimana}%) "
                f"e digitale ({pct_digitale_settimana}%). Il consiglio di gestione è mantenere un'allocazione "
                f"bilanciata delle risorse tra i due canali."
            )

        verdetto = f"In base alla composizione prevista dei visitatori, {indicazione}"
        if risparmio_stimato is not None and risparmio_stimato > 0:
            verdetto += f" Risparmio stimato sulla stampa rispetto a un'allocazione standard: €{risparmio_stimato:,.0f}.".replace(",", ".")
        if nota_weekend:
            verdetto += f" {nota_weekend}"
        verdetto += (
            " Questa analisi si basa su pattern storici e previsioni di affluenza: è un supporto decisionale, "
            "non una previsione garantita."
        )

        avviso_costi = None
        if costo_stampa is None or costo_digitale is None:
            avviso_costi = (
                "Per visualizzare il risparmio stimato in euro, imposta i costi settimanali di stampa "
                "materiale e assistenza digitale nella configurazione del sito."
            )

        return {
            "sito_id": sito_id_int,
            "nome_sito": sito["nome_sito"],
            "pct_preferenza_cartaceo_settimana": pct_cartaceo_settimana,
            "pct_preferenza_digitale_settimana": pct_digitale_settimana,
            "risparmio_stimato_euro": risparmio_stimato,
            "avviso_costi": avviso_costi,
            "verdetto": verdetto,
            "dettaglio_giorni": dettaglio_giorni
        }

    except Exception as e:
        print(f"Errore welcome desk planner sito {sito_id}: {e}")
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