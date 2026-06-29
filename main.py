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
    "Residente": "Locale",
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

        # ---- ANALISI SENTIMENT RICHIESTE PIT ----
@app.get("/classifica-sentiment-pit/{richiesta_id}")
async def classifica_sentiment_pit(richiesta_id: int):
    import httpx
    try:
        riga_resp = supabase.table("richieste_pit").select("id, commento").eq("id", richiesta_id).single().execute()
        riga = riga_resp.data
        if not riga:
            return {"errore": "Richiesta non trovata"}

        commento = (riga.get("commento") or "").strip()
        if not commento:
            return {"id": richiesta_id, "sentiment": None, "nota": "Nessun commento da analizzare"}

        prompt = f"""Classifica il tono del seguente commento, scritto da un operatore di un punto informativo turistico italiano, riguardo a una richiesta di un visitatore.

Commento: "{commento}"

Rispondi con UNA SOLA PAROLA tra queste tre, esattamente come scritta, senza punteggiatura né altro testo:
positivo
neutro
negativo"""

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
                    "max_tokens": 10,
                    "messages": [{"role": "user", "content": prompt}]
                },
                timeout=30
            )
            risultato = resp.json()
            testo = risultato["content"][0]["text"].strip().lower()

        sentiment_valido = {"positivo", "neutro", "negativo"}
        sentiment = testo if testo in sentiment_valido else "neutro"

        supabase.table("richieste_pit").update({"sentiment": sentiment}).eq("id", richiesta_id).execute()

        return {"id": richiesta_id, "sentiment": sentiment}

    except Exception as e:
        print(f"Errore classificazione sentiment richiesta {richiesta_id}: {e}")
        return {"errore": str(e)}

        # ---- GENERAZIONE RELAZIONE ----
@app.post("/genera-relazione")
async def genera_relazione(payload: dict):
    import httpx
    try:
        nome_sito = payload.get("nome_sito")
        mese = payload.get("mese")
        dati = payload.get("dati", {})
        pit = payload.get("pit")

        sezione_pit_dati = ""
        sezione_pit_struttura = ""
        if pit and pit.get("totaleRichieste", 0) > 0:
            sezione_pit_dati = f"""

Dati del Punto Informativo Turistico del comune di {pit.get('comune', 'N/D')} (servizio comunale, non specifico di questo sito):
- Richieste totali gestite: {pit.get('totaleRichieste', 0)}
- Tasso di soddisfazione: {pit.get('tassoSoddisfazione', 0)}%
- Richieste parziali: {pit.get('parziali', 0)}
- Richieste non soddisfatte: {pit.get('nonDisponibili', 0)}
- Materiali più richiesti ma non disponibili: {pit.get('materialiMancanti', [])}
- Sentiment dei commenti operatore (positivo/neutro/negativo): {pit.get('sentiment', {})}
- Categorie più frequenti nei commenti negativi: {pit.get('categorieNegative', [])}
- Disservizi più frequenti segnalati dai visitatori (indipendentemente dal sentiment del commento): {pit.get('disserviziFrequenti', [])}"""
            sezione_pit_struttura = "\n## PUNTO INFORMATIVO TURISTICO"

        avviso_dati_zero = ""
        if dati.get('totaleVisitatori', 0) == 0 and dati.get('totalePrec', 0) == 0:
            avviso_dati_zero = "\n\nNOTA IMPORTANTE: i visitatori risultano a zero sia in questo mese che nel precedente. NON ipotizzare cause gestionali specifiche (manutenzione, restauro, chiusura, lavori) che non sono state fornite come dato. Indica invece, in modo neutro, che il valore zero può derivare da assenza di dati storici registrati nel sistema per questo periodo (es. sito di recente attivazione), oltre alla possibilità di una chiusura effettiva, senza affermare quale sia la causa reale."

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
- Previsione visitatori prossimo mese: {dati.get('prevTotale', 0)}{sezione_pit_dati}{avviso_dati_zero}

Struttura la relazione con queste sezioni:
## SINTESI ESECUTIVA
## ANALISI AFFLUENZA
## ANALISI ECONOMICA
## PROFILO DEI VISITATORI
## FATTORI CONTESTUALI{sezione_pit_struttura}
## PREVISIONI MESE SUCCESSIVO
## RACCOMANDAZIONI STRATEGICHE

{"Nella sezione PUNTO INFORMATIVO TURISTICO, chiarisci che il servizio è gestito a livello comunale e non è specifico di questo singolo sito. Tieni distinte concettualmente le richieste di informazione (esito soddisfatta/parziale/non disponibile) dai disservizi segnalati (criticità operative come pulizia, manutenzione, codeore, ecc.): sono due categorie di dati diverse e non vanno confuse nel testo. " if sezione_pit_dati else ""}Scrivi in italiano formale e istituzionale. Sii specifico con i numeri. Lunghezza: circa 600-800 parole."""

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

        # ---- APPROVAZIONE RICHIESTA EVENTO ----
def calcola_impatto_da_saturazione(saturazione):
    """Mappa il tasso di saturazione stimato della richiesta sulla scala basso/medio/alto
    usata da eventi_locali per la variabile esogena del SARIMAX."""
    if saturazione is None:
        return "medio"
    if saturazione < 50:
        return "basso"
    elif saturazione <= 85:
        return "medio"
    else:
        return "alto"

@app.put("/richieste-eventi/{richiesta_id}/approva")
def approva_richiesta_evento(richiesta_id: int):
    try:
        richiesta_resp = supabase.table("richieste_eventi").select("*").eq("id", richiesta_id).single().execute()
        richiesta = richiesta_resp.data
        if not richiesta:
            return {"errore": "Richiesta non trovata"}

        # Evita di creare un evento duplicato se la richiesta è già stata approvata in precedenza:
        # controlla se esiste già un evento in eventi_locali collegato a questa richiesta.
        evento_esistente_resp = supabase.table("eventi_locali").select("*") \
            .eq("richiesta_evento_id", richiesta_id).execute()
        if evento_esistente_resp.data:
            supabase.table("richieste_eventi").update({"stato_richiesta": "approvata"}).eq("id", richiesta_id).execute()
            return {
                "status": "già approvata",
                "evento": evento_esistente_resp.data[0],
                "nota": "Era già presente un evento collegato a questa richiesta: non ne è stato creato un secondo."
            }

        impatto_atteso = calcola_impatto_da_saturazione(richiesta.get("tasso_saturazione_stimato"))

        nuovo_evento = {
            "sito_id": richiesta["sito_id"],
            "nome_evento": richiesta["nome_evento"],
            "data_inizio": richiesta["data_inizio"],
            "data_fine": richiesta["data_fine"],
            "tipo_evento": richiesta["tipologia_evento"],
            "impatto_atteso": impatto_atteso,
            "note": f"Creato automaticamente da richiesta evento approvata (ID {richiesta_id}).",
            "richiesta_evento_id": richiesta_id,
        }

        evento_creato = supabase.table("eventi_locali").insert(nuovo_evento).execute()

        supabase.table("richieste_eventi").update({"stato_richiesta": "approvata"}).eq("id", richiesta_id).execute()

        return {
            "status": "approvata",
            "evento": evento_creato.data[0] if evento_creato.data else None,
            "impatto_atteso": impatto_atteso
        }

    except Exception as e:
        print(f"Errore approvazione richiesta evento {richiesta_id}: {e}")
        return {"errore": str(e)}

def tenta_sarimax_su_serie(valori, passi_avanti=1):
    """Tenta di addestrare un SARIMAX semplice (senza stagionalità, che richiederebbe troppi dati)
    su una serie di valori ordinata temporalmente. Restituisce (previsione, usato_sarimax).
    Se i punti sono troppo pochi per il modello o il fit fallisce numericamente, restituisce
    (None, False) così il chiamante può ricadere sulla media storica senza fingere una previsione
    che la matematica non può sostenere con questi dati.
    """
    # Minimo realistico per un SARIMAX (1,1,1) senza stagionalità: sotto 5 punti
    # il modello non ha gradi di libertà sufficienti per stimare i suoi parametri.
    if len(valori) < 5:
        return None, False
    try:
        serie = pd.Series(valori)
        modello = SARIMAX(serie, order=(1, 1, 1), enforce_stationarity=False, enforce_invertibility=False)
        risultato = modello.fit(disp=False)
        previsione = risultato.forecast(steps=passi_avanti)
        valore_previsto = float(previsione.iloc[-1])
        return max(0, round(valore_previsto, 1)), True
    except Exception as e:
        print(f"SARIMAX fallito su serie di {len(valori)} punti: {e}")
        return None, False


def conta_eventi_per_mese(date_lista):
    """Aggrega una lista di date (stringhe YYYY-MM-DD) in conteggio eventi per mese,
    riempiendo con zero i mesi senza eventi nel range osservato, per ottenere
    una serie temporale regolare adatta a SARIMAX."""
    if not date_lista:
        return []
    date_parsed = sorted(pd.to_datetime(date_lista))
    primo_mese = date_parsed[0].to_period("M")
    ultimo_mese = date_parsed[-1].to_period("M")
    conteggio = {}
    for d in date_parsed:
        periodo = d.to_period("M")
        conteggio[periodo] = conteggio.get(periodo, 0) + 1
    mese_corrente = primo_mese
    serie = []
    while mese_corrente <= ultimo_mese:
        serie.append(conteggio.get(mese_corrente, 0))
        mese_corrente += 1
    return serie


def costruisci_esogene_evento(date_lista, sito_id, regione="Lazio"):
    """Costruisce le variabili esogene (festività, weekend, meteo completo) per una lista di date
    di eventi storici, riusando le stesse fonti dati già presenti nel sistema (festivita_regionali,
    meteo_giornaliero) invece di duplicarle: stessa logica di genera_variabili_esogene, applicata
    alle date specifiche in cui si sono svolti gli eventi piuttosto che a un range continuo."""
    date_parsed = [pd.to_datetime(d) for d in date_lista]
    date_str = [d.strftime("%Y-%m-%d") for d in date_parsed]

    try:
        fest = supabase.table("festivita_regionali").select("data").eq("regione", regione).in_("data", date_str).execute()
        date_festivita = set(f["data"] for f in fest.data)
    except:
        date_festivita = set()

    meteo_map = {}
    try:
        meteo = supabase.table("meteo_giornaliero").select("data, condizione, temperatura_max").eq("sito_id", sito_id).in_("data", date_str).execute()
        for m in meteo.data:
            meteo_map[m["data"]] = m
    except:
        pass

    exog = []
    for d, ds in zip(date_parsed, date_str):
        is_festivo = 1 if ds in date_festivita else 0
        is_weekend = 1 if d.weekday() >= 5 else 0
        m = meteo_map.get(ds, {})
        condizione = m.get("condizione")
        is_pioggia = 1 if condizione == "pioggia" else 0
        is_neve = 1 if condizione == "neve" else 0
        is_sole = 1 if condizione == "sereno" or condizione == "sole" else 0
        temperatura = m.get("temperatura_max", 20.0) or 20.0
        exog.append([is_festivo, is_weekend, is_pioggia, is_neve, is_sole, temperatura])
    return np.array(exog)


def tenta_sarimax_con_esogene(valori, date_lista, sito_id, scenario_esogeno, regione="Lazio"):
    """Versione del SARIMAX per eventi che include variabili esogene complete (festività, weekend,
    pioggia, neve, sole, temperatura), analoga a simula_scenario per le presenze ma applicata a una
    serie di eventi sparsi nel tempo anziché a una serie settimanale continua. Richiede più punti
    della versione semplice perché il modello deve stimare anche i coefficienti delle 6 variabili
    esogene: sotto questa soglia il fit non avrebbe gradi di libertà sufficienti, quindi si segnala
    onestamente l'impossibilità invece di azzardare un numero che la matematica non sostiene con
    questi dati.
    """
    MINIMO_PUNTI_CON_ESOGENE = 16
    if len(valori) < MINIMO_PUNTI_CON_ESOGENE:
        return None, False, len(valori), MINIMO_PUNTI_CON_ESOGENE
    try:
        serie = pd.Series(valori)
        exog_train = costruisci_esogene_evento(date_lista, sito_id, regione)
        modello = SARIMAX(serie, exog=exog_train, order=(1, 1, 1), enforce_stationarity=False, enforce_invertibility=False)
        risultato = modello.fit(disp=False)
        exog_scenario = np.array([[
            scenario_esogeno.get("is_festivo", 0),
            scenario_esogeno.get("is_weekend", 0),
            scenario_esogeno.get("is_pioggia", 0),
            scenario_esogeno.get("is_neve", 0),
            scenario_esogeno.get("is_sole", 0),
            scenario_esogeno.get("temperatura", 20.0),
        ]])
        previsione = risultato.forecast(steps=1, exog=exog_scenario)
        valore_previsto = float(previsione.iloc[-1])
        return max(0, round(valore_previsto, 1)), True, len(valori), MINIMO_PUNTI_CON_ESOGENE
    except Exception as e:
        print(f"SARIMAX con esogene fallito su serie di {len(valori)} punti: {e}")
        return None, False, len(valori), MINIMO_PUNTI_CON_ESOGENE


        # ---- REVENUE FORECASTING EVENTI ----
@app.get("/revenue-forecasting-eventi")
def revenue_forecasting_eventi(comune_id: str = None, sito_id: int = None):
    try:
        siti_query = supabase.table("siti_culturali").select("id, nome_sito, comune_id")
        if comune_id:
            siti_query = siti_query.eq("comune_id", comune_id)
        siti_resp = siti_query.execute()
        siti = siti_resp.data or []
        if not siti:
            return {"errore": "Nessun sito trovato"}

        siti_map = {s["id"]: s["nome_sito"] for s in siti}
        sito_ids = [s["id"] for s in siti] if sito_id is None else [sito_id]

        richieste_resp = supabase.table("richieste_eventi").select("*").in_("sito_id", sito_ids).execute()
        richieste = richieste_resp.data or []

        if not richieste:
            return {"errore": "Nessuna richiesta evento trovata"}

        def margine_effettivo(r):
            # Usa il margine reale se il consuntivo è stato inserito, altrimenti ricade sulla stima:
            # questo permette di avere sempre un numero utile anche prima che l'evento si sia svolto.
            if r.get("consuntivo_inserito") and r.get("margine_netto_reale") is not None:
                return r["margine_netto_reale"]
            return r.get("margine_netto_stimato") or 0

        confermate = [r for r in richieste if r["stato_richiesta"] in ("approvata", "completata")]
        in_valutazione = [r for r in richieste if r["stato_richiesta"] == "in_valutazione"]
        rifiutate = [r for r in richieste if r["stato_richiesta"] == "rifiutata"]

        valore_confermato = round(sum(margine_effettivo(r) for r in confermate), 2)
        valore_in_valutazione = round(sum(r.get("margine_netto_stimato") or 0 for r in in_valutazione), 2)
        valore_perso_rifiutate = round(sum(r.get("margine_netto_stimato") or 0 for r in rifiutate), 2)

        n_con_consuntivo = sum(1 for r in confermate if r.get("consuntivo_inserito"))

        # Confronto stima vs reale, solo sulle richieste che hanno già un consuntivo inserito
        confronto_stima_reale = []
        for r in confermate:
            if r.get("consuntivo_inserito") and r.get("margine_netto_reale") is not None:
                stimato = r.get("margine_netto_stimato") or 0
                reale = r["margine_netto_reale"]
                confronto_stima_reale.append({
                    "id": r["id"],
                    "nome_evento": r["nome_evento"],
                    "tipologia_evento": r["tipologia_evento"],
                    "margine_stimato": stimato,
                    "margine_reale": reale,
                    "scostamento": round(reale - stimato, 2),
                    "scostamento_pct": round(((reale - stimato) / abs(stimato)) * 100, 1) if stimato else None
                })

        # Segmentazione per tipologia: su tutte le richieste, indipendentemente dallo stato,
        # per capire quali tipologie generano più valore E quali saturano di più gli spazi.
        segmenti = {}
        for r in richieste:
            tip = r["tipologia_evento"]
            if tip not in segmenti:
                segmenti[tip] = {"tipologia_evento": tip, "conteggio": 0, "margine_totale": 0, "saturazioni": []}
            segmenti[tip]["conteggio"] += 1
            segmenti[tip]["margine_totale"] += margine_effettivo(r)
            if r.get("tasso_saturazione_stimato") is not None:
                segmenti[tip]["saturazioni"].append(r["tasso_saturazione_stimato"])

        segmentazione = []
        for tip, dati in segmenti.items():
            saturazione_media = round(sum(dati["saturazioni"]) / len(dati["saturazioni"]), 1) if dati["saturazioni"] else None
            segmentazione.append({
                "tipologia_evento": tip,
                "conteggio": dati["conteggio"],
                "margine_totale": round(dati["margine_totale"], 2),
                "margine_medio": round(dati["margine_totale"] / dati["conteggio"], 2) if dati["conteggio"] else 0,
                "saturazione_media_pct": saturazione_media
            })
        segmentazione.sort(key=lambda x: x["margine_totale"], reverse=True)

        # Previsione per tipologia: basata solo su richieste confermate (approvata/completata),
        # cioè eventi realmente accaduti, per dare una stima solida di cosa aspettarsi da una
        # nuova richiesta della stessa tipologia. Per ciascuna tipologia si tenta un SARIMAX
        # reale su margine, dimensione attesa e conteggio mensile eventi; se i punti disponibili
        # sono troppo pochi per il modello (o il fit fallisce numericamente), si ricade sulla
        # media storica, segnalando esplicitamente il metodo usato per ogni valore: questo evita
        # di presentare come "previsione modellata" un numero che la matematica non sostiene
        # ancora con questi dati, mantenendo comunque sempre un risultato utile da mostrare.
        previsioni_tipologia = {}
        for r in confermate:
            tip = r["tipologia_evento"]
            if tip not in previsioni_tipologia:
                previsioni_tipologia[tip] = {"dimensioni": [], "saturazioni": [], "margini": [], "date": []}
            if r.get("dimensione_attesa") is not None:
                previsioni_tipologia[tip]["dimensioni"].append(r["dimensione_attesa"])
            if r.get("tasso_saturazione_stimato") is not None:
                previsioni_tipologia[tip]["saturazioni"].append(r["tasso_saturazione_stimato"])
            previsioni_tipologia[tip]["margini"].append(margine_effettivo(r))
            if r.get("data_inizio"):
                previsioni_tipologia[tip]["date"].append(r["data_inizio"])

        def media(lista):
            return round(sum(lista) / len(lista), 1) if lista else None

        def livello_affidabilita(n):
            if n >= 5:
                return "alta"
            elif n >= 3:
                return "media"
            else:
                return "bassa"

        previsione_per_tipologia = []
        for tip, dati in previsioni_tipologia.items():
            n_eventi = len(dati["margini"])

            margine_sarimax, usato_margine_sarimax = tenta_sarimax_su_serie(dati["margini"])
            dimensione_sarimax, usato_dimensione_sarimax = tenta_sarimax_su_serie(dati["dimensioni"])
            serie_mensile = conta_eventi_per_mese(dati["date"])
            eventi_futuri_sarimax, usato_eventi_sarimax = tenta_sarimax_su_serie(serie_mensile)

            previsione_per_tipologia.append({
                "tipologia_evento": tip,
                "n_eventi_storici": n_eventi,
                "affidabilita": livello_affidabilita(n_eventi),
                "dimensione_media_attesa": dimensione_sarimax if usato_dimensione_sarimax else media(dati["dimensioni"]),
                "dimensione_metodo": "sarimax" if usato_dimensione_sarimax else "media_storica",
                "saturazione_media_pct": media(dati["saturazioni"]),
                "margine_medio": margine_sarimax if usato_margine_sarimax else media(dati["margini"]),
                "margine_metodo": "sarimax" if usato_margine_sarimax else "media_storica",
                "eventi_previsti_prossimo_mese": eventi_futuri_sarimax if usato_eventi_sarimax else None,
                "eventi_metodo": "sarimax" if usato_eventi_sarimax else "dati_insufficienti"
            })
        previsione_per_tipologia.sort(key=lambda x: x["n_eventi_storici"], reverse=True)

        return {
            "comune_id": comune_id,
            "sito_id": sito_id,
            "vista_globale": comune_id is None,
            "siti_inclusi": [siti_map[sid] for sid in sito_ids if sid in siti_map],
            "totale_richieste": len(richieste),
            "valore_confermato": valore_confermato,
            "n_richieste_confermate": len(confermate),
            "n_con_consuntivo_inserito": n_con_consuntivo,
            "valore_in_valutazione": valore_in_valutazione,
            "n_richieste_in_valutazione": len(in_valutazione),
            "valore_perso_rifiutate": valore_perso_rifiutate,
            "n_richieste_rifiutate": len(rifiutate),
            "segmentazione_per_tipologia": segmentazione,
            "confronto_stima_reale": confronto_stima_reale,
            "previsione_per_tipologia": previsione_per_tipologia
        }

    except Exception as e:
        print(f"Errore revenue forecasting eventi comune {comune_id}: {e}")
        return {"errore": str(e)}

        # ---- CALENDARIO STRATEGICO: VERIFICA DISPONIBILITA EVENTO ----
@app.get("/verifica-disponibilita-evento")
def verifica_disponibilita_evento(spazio_id: int, data_inizio: str, data_fine: str, sito_id: int, richiesta_id: int = None):
    try:
        # Vincolo rigido: lo stesso spazio non può ospitare due eventi confermati
        # con date che si sovrappongono, anche se nello stesso giorno c'è spazio per
        # eventi diversi in spazi diversi dello stesso sito.
        query = supabase.table("richieste_eventi").select("*") \
            .eq("spazio_id", spazio_id) \
            .in_("stato_richiesta", ["approvata", "completata"]) \
            .lte("data_inizio", data_fine) \
            .gte("data_fine", data_inizio)
        if richiesta_id:
            query = query.neq("id", richiesta_id)
        conflitti_resp = query.execute()
        conflitti = conflitti_resp.data or []

        risultato_conflitto = None
        if conflitti:
            c = conflitti[0]
            risultato_conflitto = {
                "nome_evento": c["nome_evento"],
                "data_inizio": c["data_inizio"],
                "data_fine": c["data_fine"],
                "stato_richiesta": c["stato_richiesta"]
            }

        # Suggerimento non bloccante: confronta l'affluenza storica delle presenze
        # ordinarie nello stesso range di giorni (stesso mese/giorno, anni precedenti
        # se disponibili) con la media generale del sito, per segnalare se il periodo
        # scelto per l'evento coincide con un periodo già di alta affluenza turistica.
        alta_affluenza = False
        scostamento_pct = None
        try:
            tutte_presenze_resp = supabase.table("presenza").select("data, gruppo").eq("sito_id", sito_id).execute()
            tutte_presenze = tutte_presenze_resp.data or []
            if tutte_presenze:
                df = pd.DataFrame(tutte_presenze)
                df["data"] = pd.to_datetime(df["data"])
                aggregato_giorno = df.groupby("data")["gruppo"].sum()
                media_generale = aggregato_giorno.mean()

                inizio_dt = pd.to_datetime(data_inizio)
                fine_dt = pd.to_datetime(data_fine)
                # Stesso intervallo di mese/giorno, su qualsiasi anno presente nello storico
                periodo_storico = aggregato_giorno[
                    aggregato_giorno.index.map(lambda d: (d.month, d.day) >= (inizio_dt.month, inizio_dt.day) and (d.month, d.day) <= (fine_dt.month, fine_dt.day))
                ]
                if len(periodo_storico) > 0 and media_generale > 0:
                    media_periodo = periodo_storico.mean()
                    scostamento_pct = round(((media_periodo - media_generale) / media_generale) * 100, 1)
                    alta_affluenza = scostamento_pct >= 30
        except Exception as e:
            print(f"Errore calcolo affluenza storica per verifica disponibilità: {e}")

        return {
            "conflitto": risultato_conflitto is not None,
            "dettaglio_conflitto": risultato_conflitto,
            "alta_affluenza_periodo": alta_affluenza,
            "scostamento_affluenza_pct": scostamento_pct
        }

    except Exception as e:
        print(f"Errore verifica disponibilità evento: {e}")
        return {"errore": str(e)}

        # ---- DYNAMIC PRICING: SUGGERIMENTO PER TIPOLOGIA ----
SOGLIA_SATURAZIONE_DYNAMIC_PRICING = 70

@app.get("/dynamic-pricing-eventi")
def dynamic_pricing_eventi(sito_id: int, tipologia_evento: str):
    try:
        richieste_resp = supabase.table("richieste_eventi").select("tasso_saturazione_stimato, dimensione_attesa") \
            .eq("sito_id", sito_id).eq("tipologia_evento", tipologia_evento) \
            .in_("stato_richiesta", ["approvata", "completata"]).execute()
        richieste = richieste_resp.data or []

        saturazioni = [r["tasso_saturazione_stimato"] for r in richieste if r.get("tasso_saturazione_stimato") is not None]

        if not saturazioni:
            return {"suggerimento_disponibile": False, "n_eventi_storici": 0}

        saturazione_media = round(sum(saturazioni) / len(saturazioni), 1)
        sopra_soglia = saturazione_media >= SOGLIA_SATURAZIONE_DYNAMIC_PRICING

        suggerimento = None
        if sopra_soglia:
            if saturazione_media >= 100:
                suggerimento = (
                    f"Gli eventi di tipo \"{tipologia_evento}\" in questo sito hanno storicamente superato la capacità "
                    f"dello spazio (saturazione media {saturazione_media}%). Valuta uno spazio più ampio o un secondo "
                    f"slot/data per distribuire la domanda."
                )
            else:
                suggerimento = (
                    f"Gli eventi di tipo \"{tipologia_evento}\" in questo sito registrano una saturazione media alta "
                    f"({saturazione_media}%). La domanda sembra superare l'offerta disponibile: valuta un prezzo "
                    f"del biglietto più alto rispetto al listino standard, per regolare l'afflusso e aumentare il margine."
                )

        return {
            "suggerimento_disponibile": sopra_soglia,
            "n_eventi_storici": len(saturazioni),
            "saturazione_media_pct": saturazione_media,
            "soglia_pct": SOGLIA_SATURAZIONE_DYNAMIC_PRICING,
            "suggerimento": suggerimento
        }

    except Exception as e:
        print(f"Errore dynamic pricing eventi: {e}")
        return {"errore": str(e)}

        # ---- SIMULATORE SCENARIO EVENTI (SARIMAX con esogene) ----
@app.post("/simula-scenario-evento")
def simula_scenario_evento(payload: dict):
    try:
        sito_id = int(payload.get("sito_id"))
        tipologia_evento = payload.get("tipologia_evento")
        scenario = payload.get("scenario", {})

        richieste_resp = supabase.table("richieste_eventi").select(
            "data_inizio, dimensione_attesa, margine_netto_stimato, margine_netto_reale, consuntivo_inserito"
        ).eq("sito_id", sito_id).eq("tipologia_evento", tipologia_evento) \
         .in_("stato_richiesta", ["approvata", "completata"]).order("data_inizio").execute()
        richieste = richieste_resp.data or []

        if not richieste:
            return {"errore": "Nessun evento storico confermato per questa combinazione sito/tipologia"}

        date_lista = [r["data_inizio"] for r in richieste]
        dimensioni = [r["dimensione_attesa"] for r in richieste if r.get("dimensione_attesa") is not None]
        margini = [
            r["margine_netto_reale"] if r.get("consuntivo_inserito") and r.get("margine_netto_reale") is not None
            else (r.get("margine_netto_stimato") or 0)
            for r in richieste
        ]

        dimensione_prevista, usato_sarimax_dim, n_punti, minimo_richiesto = tenta_sarimax_con_esogene(
            dimensioni, date_lista[:len(dimensioni)], sito_id, scenario
        )
        margine_previsto, usato_sarimax_margine, _, _ = tenta_sarimax_con_esogene(
            margini, date_lista, sito_id, scenario
        )

        risultato = {
            "tipologia_evento": tipologia_evento,
            "n_eventi_storici": n_punti,
            "minimo_punti_richiesti": minimo_richiesto,
            "sarimax_disponibile": usato_sarimax_dim or usato_sarimax_margine,
            "dimensione_prevista": dimensione_prevista,
            "dimensione_metodo": "sarimax" if usato_sarimax_dim else "dati_insufficienti",
            "margine_previsto": margine_previsto,
            "margine_metodo": "sarimax" if usato_sarimax_margine else "dati_insufficienti",
        }

        if not usato_sarimax_dim and not usato_sarimax_margine:
            risultato["messaggio"] = (
                f"Servono almeno {minimo_richiesto} eventi storici confermati di tipo \"{tipologia_evento}\" in questo sito "
                f"per attivare la previsione SARIMAX basata su meteo/festività/weekend. Al momento ne sono disponibili "
                f"{n_punti}. La previsione tornerà disponibile automaticamente non appena i dati saranno sufficienti."
            )

        return risultato

    except Exception as e:
        print(f"Errore simulazione scenario evento: {e}")
        return {"errore": str(e)}
# ============================================================
# PIANO STRATEGICO DELLA DESTINAZIONE — Sezione 1
# ============================================================

def ottieni_o_crea_piano_attivo(comune_id_str):
    """Recupera il piano strategico attivo per il comune. Se non esiste,
    lo crea automaticamente con titolo e periodo di default (anno corrente
    -> anno corrente + 2), in stato 'bozza', per non bloccare l'accesso
    alla sezione in attesa di un setup manuale preliminare."""
    esistente_resp = supabase.table("piani_strategici").select("*") \
        .eq("comune_id", comune_id_str).neq("stato", "archiviato") \
        .order("creato_il", desc=True).limit(1).execute()
    if esistente_resp.data:
        return esistente_resp.data[0]

    anno_corrente = datetime.now().year
    nuovo_piano = {
        "comune_id": comune_id_str,
        "titolo": f"Piano Strategico {anno_corrente}-{anno_corrente + 2}",
        "anno_inizio": anno_corrente,
        "anno_fine": anno_corrente + 2,
        "stato": "bozza",
    }
    creato_resp = supabase.table("piani_strategici").insert(nuovo_piano).execute()
    return creato_resp.data[0]


def ottieni_siti_comune(comune_id_str):
    """Recupera tutti i siti culturali appartenenti a un comune."""
    siti_resp = supabase.table("siti_culturali").select("id, nome_sito") \
        .eq("comune_id", comune_id_str).execute()
    return siti_resp.data or []


@app.get("/piano-strategico/{comune_id}")
def get_piano_strategico(comune_id: str):
    try:
        piano = ottieni_o_crea_piano_attivo(comune_id)
        return piano
    except Exception as e:
        print(f"Errore recupero piano strategico comune {comune_id}: {e}")
        return {"errore": str(e)}


@app.put("/piano-strategico/{piano_id}")
def aggiorna_piano_strategico(piano_id: int, payload: dict):
    """Permette di rinominare il piano o modificarne il periodo,
    senza toccare i dati delle sezioni già collegate a piano_id."""
    try:
        campi_consentiti = {"titolo", "anno_inizio", "anno_fine", "stato"}
        aggiornamento = {k: v for k, v in payload.items() if k in campi_consentiti}
        if not aggiornamento:
            return {"errore": "Nessun campo valido da aggiornare"}
        supabase.table("piani_strategici").update(aggiornamento).eq("id", piano_id).execute()
        return {"status": "aggiornato"}
    except Exception as e:
        print(f"Errore aggiornamento piano strategico {piano_id}: {e}")
        return {"errore": str(e)}


SOGLIA_MESI_BENCHMARK_DATATO = 18
GIORNI_MINIMI_FINESTRA = 15  # sotto questa soglia per lato, il confronto è troppo rumoroso per essere mostrato
GIORNI_MASSIMI_FINESTRA = 365  # mai oltre 12 mesi per lato, anche con storico molto lungo


def calcola_finestra_adattiva(prima_data, oggi):
    """Calcola una finestra di confronto (in giorni) che si adatta a quanto
    storico è realmente disponibile, invece di richiedere sempre 24 mesi fissi.
    Più dati ci sono, più la finestra si allarga (fino a un massimo di 12 mesi
    per lato), e con essa cresce l'affidabilità del confronto. Sotto la soglia
    minima, segnala onestamente che il dato non è ancora disponibile invece
    di calcolare un numero che il rumore statistico renderebbe inaffidabile."""
    giorni_totali_storico = (oggi - prima_data).days
    giorni_finestra = giorni_totali_storico // 2

    if giorni_finestra < GIORNI_MINIMI_FINESTRA:
        return None, None

    giorni_finestra = min(giorni_finestra, GIORNI_MASSIMI_FINESTRA)

    if giorni_finestra < 60:
        affidabilita = "bassa"
    elif giorni_finestra < 180:
        affidabilita = "media"
    else:
        affidabilita = "alta"

    return giorni_finestra, affidabilita


@app.get("/benchmark-regionale/{comune_id}")
def get_benchmark_regionale(comune_id: str):
    """Calcola la crescita reale del comune con una finestra di confronto
    adattiva (si allarga progressivamente man mano che lo storico di presenze
    cresce, fino a un massimo di 12 mesi per lato) e la confronta con l'ultimo
    valore di benchmark regionale inserito manualmente. Segnala onestamente
    se il dato esterno manca, è datato, o se lo storico di presenze è ancora
    troppo scarso per qualsiasi confronto significativo."""
    try:
        piano = ottieni_o_crea_piano_attivo(comune_id)

        siti = ottieni_siti_comune(comune_id)
        if not siti:
            return {"errore": "Nessun sito culturale trovato per questo comune"}
        sito_ids = [s["id"] for s in siti]

        oggi = datetime.now()

        presenze_resp = supabase.table("presenza").select("data, gruppo") \
            .in_("sito_id", sito_ids).execute()
        presenze = presenze_resp.data or []

        if not presenze:
            return {"errore": "Nessun dato storico di presenze disponibile per questo comune"}

        prima_data = min(pd.to_datetime(p["data"]) for p in presenze)
        giorni_finestra, affidabilita_crescita = calcola_finestra_adattiva(prima_data, oggi)

        dati_sufficienti = giorni_finestra is not None
        crescita_propria_pct = None
        visitatori_periodo_corrente = 0
        visitatori_periodo_precedente = 0

        if dati_sufficienti:
            inizio_corrente = oggi - timedelta(days=giorni_finestra)
            inizio_precedente = oggi - timedelta(days=giorni_finestra * 2)

            for p in presenze:
                data_p = pd.to_datetime(p["data"])
                gruppo = p.get("gruppo", 0) or 0
                if data_p >= pd.Timestamp(inizio_corrente):
                    visitatori_periodo_corrente += gruppo
                elif data_p >= pd.Timestamp(inizio_precedente):
                    visitatori_periodo_precedente += gruppo

            if visitatori_periodo_precedente > 0:
                crescita_propria_pct = round(
                    ((visitatori_periodo_corrente - visitatori_periodo_precedente) / visitatori_periodo_precedente) * 100, 1
                )

        benchmark_resp = supabase.table("benchmark_regionali").select("*") \
            .eq("piano_id", piano["id"]).order("anno_riferimento", desc=True).limit(1).execute()
        benchmark = benchmark_resp.data[0] if benchmark_resp.data else None

        avviso_benchmark = None
        benchmark_datato = False
        if not benchmark:
            avviso_benchmark = (
                "Nessun dato di benchmark regionale inserito. Aggiungi il valore di crescita regionale "
                "più recente (es. da report ISTAT o osservatorio turistico regionale) per attivare il confronto."
            )
        else:
            anni_da_inserimento = oggi.year - benchmark["anno_riferimento"]
            mesi_stimati = anni_da_inserimento * 12
            if mesi_stimati > SOGLIA_MESI_BENCHMARK_DATATO:
                benchmark_datato = True
                avviso_benchmark = (
                    f"Il dato di benchmark regionale risale all'anno {benchmark['anno_riferimento']} "
                    f"(fonte: {benchmark['fonte']}) ed è da considerarsi datato. Si raccomanda di aggiornarlo "
                    f"con il report più recente disponibile."
                )

        confronto = None
        if crescita_propria_pct is not None and benchmark and not benchmark_datato:
            differenza = round(crescita_propria_pct - benchmark["crescita_arrivi_pct"], 1)
            performance = "superiore" if differenza > 0 else "inferiore" if differenza < 0 else "in linea con"
            confronto = (
                f"La destinazione cresce del {crescita_propria_pct}% (ultimi {giorni_finestra} giorni vs "
                f"{giorni_finestra} giorni precedenti, dati reali GesTur su {len(siti)} sito/i, affidabilità "
                f"{affidabilita_crescita}), una performance {performance} rispetto alla media "
                f"regionale del {benchmark['crescita_arrivi_pct']}% (fonte: {benchmark['fonte']}, anno {benchmark['anno_riferimento']})."
            )

        return {
            "piano_id": piano["id"],
            "comune_id": comune_id,
            "crescita_propria_pct": crescita_propria_pct,
            "dati_sufficienti": dati_sufficienti,
            "giorni_finestra": giorni_finestra,
            "affidabilita_crescita": affidabilita_crescita,
            "visitatori_periodo_corrente": visitatori_periodo_corrente if dati_sufficienti else None,
            "visitatori_periodo_precedente": visitatori_periodo_precedente if dati_sufficienti else None,
            "benchmark": benchmark,
            "benchmark_datato": benchmark_datato,
            "avviso_benchmark": avviso_benchmark,
            "confronto": confronto,
            "nota_metodologica": (
                "Il dato di crescita propria è calcolato sulle presenze reali registrate in GesTur per i siti "
                "di questo comune (ingressi ai siti culturali), con una finestra di confronto che si allarga "
                "progressivamente fino a un massimo di 12 mesi per lato man mano che lo storico cresce: più la "
                "finestra è ampia, più alta è l'affidabilità del dato. Il benchmark regionale misura un fenomeno "
                "diverso (pernottamenti turistici in strutture ricettive a livello regionale) ed è inserito "
                "manualmente dal comune sulla base di report ufficiali: il confronto è indicativo, non un dato omogeneo."
            )
        }
    except Exception as e:
        print(f"Errore benchmark regionale comune {comune_id}: {e}")
        return {"errore": str(e)}


@app.post("/benchmark-regionale")
def crea_benchmark_regionale(payload: dict):
    """Inserisce o aggiorna (upsert) il valore di benchmark regionale
    per un anno di riferimento specifico, collegato al piano attivo del comune."""
    try:
        comune_id_str = payload.get("comune_id")
        anno_riferimento = payload.get("anno_riferimento")
        crescita_arrivi_pct = payload.get("crescita_arrivi_pct")
        fonte = payload.get("fonte")
        note = payload.get("note")

        if not comune_id_str or anno_riferimento is None or crescita_arrivi_pct is None or not fonte:
            return {"errore": "comune_id, anno_riferimento, crescita_arrivi_pct e fonte sono obbligatori"}

        piano = ottieni_o_crea_piano_attivo(comune_id_str)

        record = {
            "piano_id": piano["id"],
            "comune_id": comune_id_str,
            "anno_riferimento": anno_riferimento,
            "crescita_arrivi_pct": crescita_arrivi_pct,
            "fonte": fonte,
            "note": note,
        }
        supabase.table("benchmark_regionali").upsert(
            record, on_conflict="piano_id,anno_riferimento"
        ).execute()

        return {"status": "salvato", "piano_id": piano["id"]}
    except Exception as e:
        print(f"Errore creazione benchmark regionale: {e}")
        return {"errore": str(e)}
# ============================================================
# PIANO STRATEGICO — MATRICE SWOT DINAMICA
# ============================================================

SOGLIA_PCT_DEBOLEZZA = 15.0
SOGLIA_MIN_SEGNALAZIONI_DEBOLEZZA = 3

SOGLIA_PCT_FORZA = 85.0
SOGLIA_MIN_RICHIESTE_FORZA = 5

SOGLIA_PCT_PROVENIENZA_RILEVANTE = 15.0
SOGLIA_PCT_CRESCITA_FASCIA = 10.0  # crescita minima (%) tra periodo precedente e corrente per considerare una fascia "in crescita"
SOGLIA_PCT_CALO_FASCIA = -10.0     # calo massimo (%) per considerare una fascia/provenienza "in declino" da una posizione dominante
SOGLIA_PCT_DOMINANZA_PRECEDENTE = 20.0  # quota minima nel periodo precedente per parlare di "posizione prima dominante"

def nome_categoria(valore):
    """Le categorie PIT sono salvate nel database già come etichette
    leggibili (es. "Trasporti", "Wi-Fi e connettività"), non come chiavi
    tecniche: non serve quindi nessuna mappatura, solo un fallback di
    sicurezza nel caso il valore sia assente."""
    return valore or "Non specificata"


def genera_debolezze_da_pit(richieste_pit, sito_per_id=None):
    """Categorie disservizio PIT ricorrenti sopra soglia minima diventano
    debolezze, ciascuna sempre accompagnata dal conteggio grezzo che la
    sostiene (numero di segnalazioni su totale disservizi)."""
    conteggi = {}
    totale_disservizi = 0
    for r in richieste_pit:
        cat = r.get("categorie_disservizio")
        if not cat or cat.lower() == "altro":
            continue
        conteggi[cat] = conteggi.get(cat, 0) + 1
        totale_disservizi += 1

    if totale_disservizi == 0:
        return []

    debolezze = []
    for cat, n in conteggi.items():
        pct = round(n / totale_disservizi * 100, 1)
        if pct >= SOGLIA_PCT_DEBOLEZZA and n >= SOGLIA_MIN_SEGNALAZIONI_DEBOLEZZA:
            debolezze.append({
                "quadrante": "debolezza",
                "voce": f"{nome_categoria(cat)}",
                "dato_sottostante": f"{n} segnalazioni di disservizio \"{nome_categoria(cat)}\" su {totale_disservizi} disservizi totali ({pct}%)",
                "valore_numerico": pct,
            })
    debolezze.sort(key=lambda x: x["valore_numerico"], reverse=True)
    return debolezze


def genera_forze_da_pit(richieste_pit):
    """Categorie richiesta PIT con alto tasso di esito 'soddisfatta'
    diventano forze, sempre con il conteggio grezzo sottostante."""
    per_categoria = {}
    for r in richieste_pit:
        cat = r.get("categoria")
        if not cat or cat.lower() == "altro":
            continue
        if cat not in per_categoria:
            per_categoria[cat] = {"soddisfatte": 0, "totali": 0}
        per_categoria[cat]["totali"] += 1
        if r.get("esito") == "soddisfatta":
            per_categoria[cat]["soddisfatte"] += 1

    forze = []
    for cat, d in per_categoria.items():
        if d["totali"] < SOGLIA_MIN_RICHIESTE_FORZA:
            continue
        pct = round(d["soddisfatte"] / d["totali"] * 100, 1)
        if pct >= SOGLIA_PCT_FORZA:
            forze.append({
                "quadrante": "forza",
                "voce": f"Gestione informativa: {nome_categoria(cat)}",
                "dato_sottostante": f"{d['soddisfatte']} richieste con esito soddisfatta su {d['totali']} richieste di categoria \"{nome_categoria(cat)}\" ({pct}%)",
                "valore_numerico": pct,
            })
    forze.sort(key=lambda x: x["valore_numerico"], reverse=True)
    return forze


def calcola_quota_periodo(presenze, chiave_estrazione, giorni_finestra, oggi):
    """Calcola, per due periodi consecutivi di pari durata (giorni_finestra
    ciascuno), la quota percentuale di presenze per ciascun valore estratto
    da chiave_estrazione (es. provenienza_macro o singola fascia). Restituisce
    un dizionario {valore: {"quota_precedente": ..., "quota_corrente": ..., "n_corrente": ..., "n_precedente": ...}}.
    Usato sia per individuare opportunità (fasce in crescita) che minacce
    (fasce in calo da posizione dominante)."""
    inizio_corrente = oggi - timedelta(days=giorni_finestra)
    inizio_precedente = oggi - timedelta(days=giorni_finestra * 2)

    totale_corrente = 0
    totale_precedente = 0
    per_valore_corrente = {}
    per_valore_precedente = {}

    for r in presenze:
        data_r = pd.to_datetime(r["data"])
        gruppo = r.get("gruppo", 0) or 0
        valori = chiave_estrazione(r)
        if not valori:
            continue
        n_valori = len(valori)
        quota_persona = gruppo / n_valori

        if data_r >= pd.Timestamp(inizio_corrente):
            totale_corrente += gruppo
            for v in valori:
                per_valore_corrente[v] = per_valore_corrente.get(v, 0) + quota_persona
        elif data_r >= pd.Timestamp(inizio_precedente):
            totale_precedente += gruppo
            for v in valori:
                per_valore_precedente[v] = per_valore_precedente.get(v, 0) + quota_persona

    risultato = {}
    tutti_i_valori = set(per_valore_corrente.keys()) | set(per_valore_precedente.keys())
    for v in tutti_i_valori:
        n_corrente = per_valore_corrente.get(v, 0)
        n_precedente = per_valore_precedente.get(v, 0)
        quota_corrente = round(n_corrente / totale_corrente * 100, 1) if totale_corrente > 0 else 0
        quota_precedente = round(n_precedente / totale_precedente * 100, 1) if totale_precedente > 0 else 0
        risultato[v] = {
            "quota_corrente": quota_corrente,
            "quota_precedente": quota_precedente,
            "n_corrente": round(n_corrente, 1),
            "n_precedente": round(n_precedente, 1),
        }
    return risultato, totale_corrente, totale_precedente


def genera_opportunita_provenienza(presenze, richieste_pit, giorni_finestra, oggi):
    """Opportunità di tipo A: una provenienza macro che rappresenta una quota
    rilevante delle presenze, ma le cui richieste PIT hanno un tasso di esito
    'soddisfatta' inferiore alla media generale — segnale di domanda presente
    ma servizio non ancora adeguato (es. lacuna linguistica)."""
    if giorni_finestra is None:
        return []

    def estrai_provenienza_macro(r):
        prov = r.get("provenienza")
        if not prov:
            return []
        return [mappa_provenienza_macro(prov)]

    quote, totale_corrente, _ = calcola_quota_periodo(presenze, estrai_provenienza_macro, giorni_finestra, oggi)
    if totale_corrente == 0:
        return []

    esiti_per_provenienza = {}
    esiti_totali = {"soddisfatte": 0, "totali": 0}
    for r in richieste_pit:
        prov_macro = mappa_provenienza_macro(r.get("provenienza"))
        if prov_macro not in esiti_per_provenienza:
            esiti_per_provenienza[prov_macro] = {"soddisfatte": 0, "totali": 0}
        esiti_per_provenienza[prov_macro]["totali"] += 1
        esiti_totali["totali"] += 1
        if r.get("esito") == "soddisfatta":
            esiti_per_provenienza[prov_macro]["soddisfatte"] += 1
            esiti_totali["soddisfatte"] += 1

    if esiti_totali["totali"] == 0:
        return []
    media_generale_pct = round(esiti_totali["soddisfatte"] / esiti_totali["totali"] * 100, 1)

    opportunita = []
    for prov_macro, dati_quota in quote.items():
        if dati_quota["quota_corrente"] < SOGLIA_PCT_PROVENIENZA_RILEVANTE:
            continue
        esiti = esiti_per_provenienza.get(prov_macro)
        if not esiti or esiti["totali"] < SOGLIA_MIN_RICHIESTE_FORZA:
            continue
        pct_soddisfatta = round(esiti["soddisfatte"] / esiti["totali"] * 100, 1)
        if pct_soddisfatta < media_generale_pct:
            opportunita.append({
                "quadrante": "opportunita",
                "voce": f"Servizi per turisti \"{prov_macro}\"",
                "dato_sottostante": (
                    f"\"{prov_macro}\" rappresenta il {dati_quota['quota_corrente']}% delle presenze recenti "
                    f"(ultimi {giorni_finestra} giorni), ma le richieste PIT di questa provenienza hanno un tasso "
                    f"di esito soddisfatta del {pct_soddisfatta}% ({esiti['soddisfatte']}/{esiti['totali']}), "
                    f"contro una media generale del {media_generale_pct}%."
                ),
                "valore_numerico": dati_quota["quota_corrente"],
            })
    opportunita.sort(key=lambda x: x["valore_numerico"], reverse=True)
    return opportunita


def genera_opportunita_minacce_fasce(presenze, giorni_finestra, oggi):
    """Opportunità di tipo B (fasce in crescita) e minacce di tipo C
    (fasce/provenienze in calo da posizione prima dominante), basate sul
    confronto tra periodo corrente e precedente per ciascuna fascia d'età."""
    if giorni_finestra is None:
        return [], []

    def estrai_fasce(r):
        fasce_raw = (r.get("fasce") or "").split(", ")
        return [normalizza_fascia(f) for f in fasce_raw if f]

    quote, _, _ = calcola_quota_periodo(presenze, estrai_fasce, giorni_finestra, oggi)

    opportunita = []
    minacce = []
    for fascia, dati_quota in quote.items():
        if dati_quota["quota_precedente"] == 0:
            continue
        variazione_pct = round(
            ((dati_quota["quota_corrente"] - dati_quota["quota_precedente"]) / dati_quota["quota_precedente"]) * 100, 1
        ) if dati_quota["quota_precedente"] > 0 else None

        if variazione_pct is None:
            continue

        if variazione_pct >= SOGLIA_PCT_CRESCITA_FASCIA:
            opportunita.append({
                "quadrante": "opportunita",
                "voce": f"Fascia {fascia} anni in crescita",
                "dato_sottostante": (
                    f"La fascia {fascia} anni è passata dal {dati_quota['quota_precedente']}% al "
                    f"{dati_quota['quota_corrente']}% delle presenze (variazione {variazione_pct}%) "
                    f"tra i due periodi di {giorni_finestra} giorni confrontati."
                ),
                "valore_numerico": variazione_pct,
            })
        elif variazione_pct <= SOGLIA_PCT_CALO_FASCIA and dati_quota["quota_precedente"] >= SOGLIA_PCT_DOMINANZA_PRECEDENTE:
            minacce.append({
                "quadrante": "minaccia",
                "voce": f"Calo fascia {fascia} anni, prima dominante",
                "dato_sottostante": (
                    f"La fascia {fascia} anni, che rappresentava il {dati_quota['quota_precedente']}% delle presenze "
                    f"nel periodo precedente, è calata al {dati_quota['quota_corrente']}% (variazione {variazione_pct}%) "
                    f"nei {giorni_finestra} giorni più recenti."
                ),
                "valore_numerico": abs(variazione_pct),
            })

    opportunita.sort(key=lambda x: x["valore_numerico"], reverse=True)
    minacce.sort(key=lambda x: x["valore_numerico"], reverse=True)
    return opportunita, minacce


def genera_minaccia_benchmark(comune_id, piano_id):
    """Minaccia di tipo B: crescita propria significativamente sotto il
    benchmark regionale inserito, se disponibile e non datato. Riusa la
    stessa logica già calcolata per la sezione benchmark, senza duplicarla."""
    risultato_benchmark = get_benchmark_regionale(comune_id)
    if "errore" in risultato_benchmark:
        return None
    if not risultato_benchmark.get("dati_sufficienti") or not risultato_benchmark.get("benchmark") or risultato_benchmark.get("benchmark_datato"):
        return None

    crescita_propria = risultato_benchmark["crescita_propria_pct"]
    crescita_regionale = risultato_benchmark["benchmark"]["crescita_arrivi_pct"]
    differenza = round(crescita_propria - crescita_regionale, 1)

    if differenza >= 0:
        return None

    return {
        "quadrante": "minaccia",
        "voce": "Crescita sotto la media regionale",
        "dato_sottostante": (
            f"La destinazione cresce del {crescita_propria}% contro una media regionale del {crescita_regionale}% "
            f"(fonte: {risultato_benchmark['benchmark']['fonte']}, anno {risultato_benchmark['benchmark']['anno_riferimento']}), "
            f"una differenza di {differenza} punti percentuali."
        ),
        "valore_numerico": abs(differenza),
    }


@app.get("/swot-dinamica/{comune_id}")
def get_swot_dinamica(comune_id: str):
    """Genera la matrice SWOT a 4 quadranti incrociando dati PIT e presenze,
    sempre con il dato grezzo sottostante esplicitato per ogni voce. Ogni
    chiamata genera uno snapshot live; il salvataggio dello storico avviene
    separatamente tramite l'endpoint di snapshot."""
    try:
        piano = ottieni_o_crea_piano_attivo(comune_id)

        siti = ottieni_siti_comune(comune_id)
        if not siti:
            return {"errore": "Nessun sito culturale trovato per questo comune"}
        sito_ids = [s["id"] for s in siti]

        richieste_pit_resp = supabase.table("richieste_pit").select("*").eq("comune_id", comune_id).execute()
        richieste_pit = richieste_pit_resp.data or []

        presenze_resp = supabase.table("presenza").select("data, gruppo, fasce, provenienza").in_("sito_id", sito_ids).execute()
        presenze = presenze_resp.data or []

        oggi = datetime.now()
        giorni_finestra = None
        if presenze:
            prima_data = min(pd.to_datetime(p["data"]) for p in presenze)
            giorni_finestra, _ = calcola_finestra_adattiva(prima_data, oggi)

        debolezze = genera_debolezze_da_pit(richieste_pit) if richieste_pit else []
        forze_pit = genera_forze_da_pit(richieste_pit) if richieste_pit else []

        opportunita_provenienza = genera_opportunita_provenienza(presenze, richieste_pit, giorni_finestra, oggi) if presenze and richieste_pit else []
        opportunita_fasce, minacce_fasce = genera_opportunita_minacce_fasce(presenze, giorni_finestra, oggi) if presenze else ([], [])

        minaccia_benchmark = genera_minaccia_benchmark(comune_id, piano["id"])

        forze = forze_pit
        opportunita = opportunita_provenienza + opportunita_fasce
        minacce = minacce_fasce + ([minaccia_benchmark] if minaccia_benchmark else [])

        avvisi = []
        if not richieste_pit:
            avvisi.append("Nessuna richiesta PIT registrata per questo comune: Forze e Debolezze non possono essere generate da questa fonte.")
        if not presenze:
            avvisi.append("Nessun dato di presenze disponibile: Opportunità e Minacce basate sui trend non possono essere generate.")
        if not forze and not debolezze and not opportunita and not minacce:
            avvisi.append("Nessuna voce SWOT generata: i dati raccolti finora non superano le soglie minime di significatività. La matrice si popolerà automaticamente man mano che i dati cresceranno.")

        return {
            "piano_id": piano["id"],
            "comune_id": comune_id,
            "generato_il": oggi.isoformat(),
            "forze": forze,
            "debolezze": debolezze,
            "opportunita": opportunita,
            "minacce": minacce,
            "avvisi": avvisi,
            "nota_metodologica": (
                "La matrice SWOT è generata automaticamente incrociando le richieste e i disservizi segnalati al "
                "Punto Informativo Turistico con i dati reali di presenze. Ogni voce riporta sempre il dato grezzo "
                "che la sostiene. Soglie applicate: una categoria di disservizio diventa debolezza solo sopra il "
                f"{SOGLIA_PCT_DEBOLEZZA}% dei disservizi totali (minimo {SOGLIA_MIN_SEGNALAZIONI_DEBOLEZZA} segnalazioni); "
                f"una categoria di richiesta diventa forza solo sopra il {SOGLIA_PCT_FORZA}% di esito soddisfatta "
                f"(minimo {SOGLIA_MIN_RICHIESTE_FORZA} richieste)."
            )
        }
    except Exception as e:
        print(f"Errore SWOT dinamica comune {comune_id}: {e}")
        return {"errore": str(e)}


def esegui_snapshot_swot(comune_id):
    """Calcola la SWOT dinamica corrente per un comune e la salva come
    snapshot permanente in swot_storico. Funzione interna riutilizzata sia
    dall'endpoint per singolo comune che da quello che itera su tutti i comuni."""
    swot = get_swot_dinamica(comune_id)
    if "errore" in swot:
        return {"errore": swot["errore"], "comune_id": comune_id}

    righe_da_salvare = []
    for quadrante_lista in [swot["forze"], swot["debolezze"], swot["opportunita"], swot["minacce"]]:
        for voce in quadrante_lista:
            righe_da_salvare.append({
                "piano_id": swot["piano_id"],
                "comune_id": comune_id,
                "quadrante": voce["quadrante"],
                "voce": voce["voce"],
                "dato_sottostante": voce["dato_sottostante"],
                "valore_numerico": voce["valore_numerico"],
            })

    if not righe_da_salvare:
        return {"status": "nessuna voce da salvare", "n_voci": 0, "comune_id": comune_id}

    supabase.table("swot_storico").insert(righe_da_salvare).execute()
    return {"status": "salvato", "n_voci": len(righe_da_salvare), "comune_id": comune_id}


@app.post("/swot-dinamica/{comune_id}/salva-snapshot")
def salva_snapshot_swot(comune_id: str):
    """Salva uno snapshot permanente della SWOT corrente in swot_storico,
    permettendo di confrontare nel tempo come evolve l'analisi."""
    try:
        return esegui_snapshot_swot(comune_id)
    except Exception as e:
        print(f"Errore salvataggio snapshot SWOT comune {comune_id}: {e}")
        return {"errore": str(e)}


@app.post("/swot-dinamica/salva-snapshot-tutti")
def salva_snapshot_swot_tutti():
    """Salva lo snapshot SWOT per tutti i comuni che hanno un piano
    strategico attivo. Pensato per essere richiamato da un cron job mensile,
    analogamente a come /aggiorna-previsioni viene richiamato settimanalmente
    per le previsioni di affluenza."""
    try:
        piani_resp = supabase.table("piani_strategici").select("comune_id").neq("stato", "archiviato").execute()
        comuni = list({p["comune_id"] for p in (piani_resp.data or [])})

        risultati = []
        for comune_id in comuni:
            esito = esegui_snapshot_swot(comune_id)
            risultati.append(esito)

        return {
            "comuni_processati": len(comuni),
            "risultati": risultati
        }
    except Exception as e:
        print(f"Errore snapshot SWOT tutti i comuni: {e}")
        return {"errore": str(e)}
# ============================================================
# PIANO STRATEGICO — TREND DOMANDA TURISTICA
# ============================================================

@app.get("/trend-domanda/{comune_id}")
def get_trend_domanda(comune_id: str):
    """Aggrega le presenze reali degli ultimi 12 mesi per mese e per sito,
    per mostrare l'andamento della domanda turistica nel comune e il
    contributo di ciascun sito culturale al totale. Per ciascun mese calcola
    anche la provenienza macro e la fascia d'età dominanti, con la relativa
    quota percentuale: un dato utile per orientare iniziative di marketing
    mirate, oltre al solo volume di presenze."""
    try:
        siti = ottieni_siti_comune(comune_id)
        if not siti:
            return {"errore": "Nessun sito culturale trovato per questo comune"}
        sito_ids = [s["id"] for s in siti]
        nomi_siti = {s["id"]: s["nome_sito"] for s in siti}

        oggi = datetime.now()
        dodici_mesi_fa = oggi - timedelta(days=365)

        presenze_resp = supabase.table("presenza").select("sito_id, data, gruppo, provenienza, fasce") \
            .in_("sito_id", sito_ids).gte("data", dodici_mesi_fa.strftime("%Y-%m-%d")).execute()
        presenze = presenze_resp.data or []

        if not presenze:
            return {"errore": "Nessun dato di presenze disponibile negli ultimi 12 mesi per questo comune"}

        df = pd.DataFrame(presenze)
        df["data"] = pd.to_datetime(df["data"])
        df["mese"] = df["data"].dt.strftime("%Y-%m")

        aggregato_sito = df.groupby(["mese", "sito_id"])["gruppo"].sum().reset_index()

        mesi_ordinati = sorted(df["mese"].unique())
        serie_per_mese = []
        for mese in mesi_ordinati:
            righe_sito_mese = aggregato_sito[aggregato_sito["mese"] == mese]
            per_sito = {nomi_siti.get(int(r["sito_id"]), f"Sito {r['sito_id']}"): int(r["gruppo"]) for _, r in righe_sito_mese.iterrows()}
            totale_mese = sum(per_sito.values())

            righe_mese = df[df["mese"] == mese]
            conteggio_prov = {}
            conteggio_fascia = {}
            for _, r in righe_mese.iterrows():
                gruppo = r.get("gruppo", 0) or 0
                prov_macro = mappa_provenienza_macro(r.get("provenienza"))
                conteggio_prov[prov_macro] = conteggio_prov.get(prov_macro, 0) + gruppo

                fasce_riga = [normalizza_fascia(f) for f in (r.get("fasce") or "").split(", ") if f]
                if fasce_riga:
                    quota_fascia = gruppo / len(fasce_riga)
                    for f in fasce_riga:
                        conteggio_fascia[f] = conteggio_fascia.get(f, 0) + quota_fascia

            provenienza_dominante = None
            if conteggio_prov and totale_mese > 0:
                prov_top = max(conteggio_prov.items(), key=lambda x: x[1])
                provenienza_dominante = {"valore": prov_top[0], "quota_pct": round(prov_top[1] / totale_mese * 100, 1)}

            fascia_dominante = None
            if conteggio_fascia and totale_mese > 0:
                fascia_top = max(conteggio_fascia.items(), key=lambda x: x[1])
                fascia_dominante = {"valore": fascia_top[0], "quota_pct": round(fascia_top[1] / totale_mese * 100, 1)}

            serie_per_mese.append({
                "mese": mese,
                "totale": totale_mese,
                "per_sito": per_sito,
                "provenienza_dominante": provenienza_dominante,
                "fascia_dominante": fascia_dominante,
            })

        totale_periodo = sum(m["totale"] for m in serie_per_mese)

        return {
            "comune_id": comune_id,
            "mesi_inclusi": len(serie_per_mese),
            "totale_periodo": totale_periodo,
            "siti_inclusi": list(nomi_siti.values()),
            "serie_mensile": serie_per_mese,
        }
    except Exception as e:
        print(f"Errore trend domanda comune {comune_id}: {e}")
        return {"errore": str(e)}
# ============================================================
# PIANO STRATEGICO — INDICATORI STANDARD
# ============================================================

SOGLIA_MESI_MIN_STAGIONALITA = 6


def livello_stagionalita(coefficiente_variazione):
    """Classifica il coefficiente di variazione mensile in una fascia
    leggibile: sotto il 20% il flusso è considerato regolare, sopra il 60%
    fortemente concentrato in pochi mesi dell'anno."""
    if coefficiente_variazione < 20:
        return "bassa"
    elif coefficiente_variazione < 40:
        return "media"
    elif coefficiente_variazione < 60:
        return "alta"
    else:
        return "molto alta"


@app.get("/indicatori-standard/{comune_id}")
def get_indicatori_standard(comune_id: str):
    """Calcola due indicatori standard sulla domanda turistica del comune:
    il tasso di stagionalità (quanto le presenze sono concentrate in pochi
    mesi dell'anno, misurato come coefficiente di variazione mensile) e
    l'indice di internazionalizzazione (quota di presenze con provenienza
    estera sul totale). Entrambi calcolati sui dati reali disponibili, con
    soglie minime dichiarate per evitare numeri statisticamente fragili."""
    try:
        siti = ottieni_siti_comune(comune_id)
        if not siti:
            return {"errore": "Nessun sito culturale trovato per questo comune"}
        sito_ids = [s["id"] for s in siti]

        presenze_resp = supabase.table("presenza").select("data, gruppo, provenienza").in_("sito_id", sito_ids).execute()
        presenze = presenze_resp.data or []

        if not presenze:
            return {"errore": "Nessun dato di presenze disponibile per questo comune"}

        df = pd.DataFrame(presenze)
        df["data"] = pd.to_datetime(df["data"])
        df["mese"] = df["data"].dt.strftime("%Y-%m")

        # ---- Tasso di stagionalità ----
        totali_mensili = df.groupby("mese")["gruppo"].sum()
        n_mesi = len(totali_mensili)

        stagionalita = {
            "n_mesi_disponibili": n_mesi,
            "dati_sufficienti": n_mesi >= SOGLIA_MESI_MIN_STAGIONALITA,
        }
        if n_mesi >= SOGLIA_MESI_MIN_STAGIONALITA:
            media_mensile = float(totali_mensili.mean())
            std_mensile = float(totali_mensili.std(ddof=0))
            coefficiente_variazione = round(std_mensile / media_mensile * 100, 1) if media_mensile > 0 else 0
            mese_massimo = totali_mensili.idxmax()
            mese_minimo = totali_mensili.idxmin()
            stagionalita.update({
                "coefficiente_variazione_pct": coefficiente_variazione,
                "livello": livello_stagionalita(coefficiente_variazione),
                "media_mensile": round(media_mensile, 1),
                "mese_massimo": {"mese": mese_massimo, "valore": int(totali_mensili[mese_massimo])},
                "mese_minimo": {"mese": mese_minimo, "valore": int(totali_mensili[mese_minimo])},
                "dato_sottostante": (
                    f"Su {n_mesi} mesi osservati, le presenze mensili variano da un minimo di "
                    f"{int(totali_mensili[mese_minimo])} ({mese_minimo}) a un massimo di {int(totali_mensili[mese_massimo])} "
                    f"({mese_massimo}), con una media di {round(media_mensile, 1)} al mese."
                ),
            })

        # ---- Indice di internazionalizzazione ----
        totale_presenze = int(df["gruppo"].sum())
        df["provenienza_macro"] = df["provenienza"].apply(mappa_provenienza_macro)
        presenze_estere = int(df[~df["provenienza_macro"].isin(["Italia", "Locale"])]["gruppo"].sum())
        presenze_locali_italiane = totale_presenze - presenze_estere

        internazionalizzazione = {
            "dati_sufficienti": totale_presenze > 0,
        }
        if totale_presenze > 0:
            quota_estera_pct = round(presenze_estere / totale_presenze * 100, 1)
            internazionalizzazione.update({
                "quota_estera_pct": quota_estera_pct,
                "presenze_estere": presenze_estere,
                "presenze_italiane_locali": presenze_locali_italiane,
                "totale_presenze": totale_presenze,
                "dato_sottostante": (
                    f"{presenze_estere} presenze con provenienza estera su {totale_presenze} totali "
                    f"({quota_estera_pct}%); le restanti {presenze_locali_italiane} sono italiane o residenti locali."
                ),
            })

        return {
            "comune_id": comune_id,
            "stagionalita": stagionalita,
            "internazionalizzazione": internazionalizzazione,
        }
    except Exception as e:
        print(f"Errore indicatori standard comune {comune_id}: {e}")
        return {"errore": str(e)}