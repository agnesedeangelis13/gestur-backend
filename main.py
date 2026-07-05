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
from imposta_soggiorno_service import (
    CATEGORIE_DESTINAZIONE_SOGGIORNO,
    ottieni_o_crea_piano_sviluppo_locale_attivo,
    get_gettito_soggiorno,
    crea_gettito_soggiorno,
    get_categorie_destinazione,
    aggiorna_categoria_destinazione,
    get_allocazioni_soggiorno,
    crea_allocazione_soggiorno,
    elimina_allocazione_soggiorno,
)
from compensazione_territoriale_service import (
    get_quote_capitoli,
    aggiorna_quota_capitolo,
    get_suggerimento_distribuzione,
)
from qualita_esperienza_service import (
    get_quota_reinvestimento,
    aggiorna_quota_reinvestimento,
    get_capitoli_qoe,
    aggiorna_capitolo_qoe,
    get_budget_qoe_mese,
)
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

@app.post("/meteo/aggiorna")
async def aggiorna_meteo():
    risultati = await aggiorna_meteo_tutti_siti()
    return {"risultati": risultati}

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

@app.post("/festivita/popola/{anno}")
def popola_festivita_anno(anno: int):
    n = popola_festivita(anno)
    return {"records_inseriti": n}

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
        righe_giorno = dati_storici
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

COSTO_MARGINALE_PCT = 0.15

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

    cluster_dati = {}

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

def calcola_composizione_settimana(storico, date_settimana):
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
                pref_cartaceo = coeff_canale.get(chiave, 50)
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
            riduzione_pct = max(0, pct_digitale_settimana - 50) / 50
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

def calcola_impatto_da_saturazione(saturazione):
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

@app.get("/verifica-disponibilita-evento")
def verifica_disponibilita_evento(spazio_id: int, data_inizio: str, data_fine: str, sito_id: int, richiesta_id: int = None):
    try:
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

def ottieni_o_crea_piano_attivo(comune_id_str):
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
GIORNI_MINIMI_FINESTRA = 15
GIORNI_MASSIMI_FINESTRA = 365


def calcola_finestra_adattiva(prima_data, oggi):
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

SOGLIA_PCT_DEBOLEZZA = 15.0
SOGLIA_MIN_SEGNALAZIONI_DEBOLEZZA = 3

SOGLIA_PCT_FORZA = 85.0
SOGLIA_MIN_RICHIESTE_FORZA = 5

SOGLIA_PCT_PROVENIENZA_RILEVANTE = 15.0
SOGLIA_PCT_CRESCITA_FASCIA = 10.0
SOGLIA_PCT_CALO_FASCIA = -10.0
SOGLIA_PCT_DOMINANZA_PRECEDENTE = 20.0

def nome_categoria(valore):
    return valore or "Non specificata"


def genera_debolezze_da_pit(richieste_pit, sito_per_id=None):
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
    try:
        return esegui_snapshot_swot(comune_id)
    except Exception as e:
        print(f"Errore salvataggio snapshot SWOT comune {comune_id}: {e}")
        return {"errore": str(e)}


@app.post("/swot-dinamica/salva-snapshot-tutti")
def salva_snapshot_swot_tutti():
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

@app.get("/trend-domanda/{comune_id}")
def get_trend_domanda(comune_id: str):
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

SOGLIA_MESI_MIN_STAGIONALITA = 6


def livello_stagionalita(coefficiente_variazione):
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

@app.get("/analisi-competitor/{comune_id}")
def get_analisi_competitor(comune_id: str):
    try:
        piano = ottieni_o_crea_piano_attivo(comune_id)

        osservazioni_resp = supabase.table("analisi_competitor").select("*") \
            .eq("piano_id", piano["id"]).order("inserito_il", desc=True).execute()
        osservazioni = osservazioni_resp.data or []

        return {
            "piano_id": piano["id"],
            "comune_id": comune_id,
            "osservazioni": osservazioni,
        }
    except Exception as e:
        print(f"Errore analisi competitor comune {comune_id}: {e}")
        return {"errore": str(e)}


@app.post("/analisi-competitor")
def crea_osservazione_competitor(payload: dict):
    try:
        comune_id_str = payload.get("comune_id")
        testo = payload.get("testo")

        if not comune_id_str or not testo or not testo.strip():
            return {"errore": "comune_id e testo sono obbligatori"}

        piano = ottieni_o_crea_piano_attivo(comune_id_str)

        record = {
            "piano_id": piano["id"],
            "comune_id": comune_id_str,
            "testo": testo.strip(),
        }
        creato_resp = supabase.table("analisi_competitor").insert(record).execute()

        return {"status": "salvato", "osservazione": creato_resp.data[0] if creato_resp.data else None}
    except Exception as e:
        print(f"Errore creazione osservazione competitor: {e}")
        return {"errore": str(e)}


@app.delete("/analisi-competitor/{osservazione_id}")
def elimina_osservazione_competitor(osservazione_id: int):
    try:
        supabase.table("analisi_competitor").delete().eq("id", osservazione_id).execute()
        return {"status": "eliminato"}
    except Exception as e:
        print(f"Errore eliminazione osservazione competitor {osservazione_id}: {e}")
        return {"errore": str(e)}

SOGLIA_GIORNI_MIN_FASE_LATO = 15
SOGLIA_CRESCITA_ALTA_PCT = 15.0
SOGLIA_CRESCITA_NEGATIVA_PCT = -5.0

FASI_BUTLER = [
    {
        "chiave": "esplorazione_coinvolgimento",
        "nome": "Esplorazione / Coinvolgimento",
        "descrizione": "La destinazione è in una fase iniziale di crescita rapida e in accelerazione: il pubblico cresce più velocemente nel periodo più recente rispetto al precedente.",
    },
    {
        "chiave": "sviluppo",
        "nome": "Sviluppo",
        "descrizione": "La destinazione cresce in modo sostenuto, ma il ritmo di crescita non sta più accelerando: è una fase di consolidamento della domanda raggiunta.",
    },
    {
        "chiave": "consolidamento",
        "nome": "Consolidamento",
        "descrizione": "Le presenze si sono stabilizzate, con una crescita moderata o prossima allo zero: la destinazione ha raggiunto un equilibrio nella domanda.",
    },
    {
        "chiave": "declino",
        "nome": "Declino",
        "descrizione": "Le presenze sono in calo nel periodo più recente: può essere il segnale di una fase di stanchezza della destinazione, da affrontare con nuove iniziative di rilancio.",
    },
]


def calcola_finestra_ciclo_vita(prima_data, oggi):
    giorni_totali = (oggi - prima_data).days
    giorni_finestra = giorni_totali // 3
    if giorni_finestra < SOGLIA_GIORNI_MIN_FASE_LATO:
        return None
    return min(giorni_finestra, 365)


def classifica_fase_butler(crescita_recente_pct, in_accelerazione):
    if crescita_recente_pct < SOGLIA_CRESCITA_NEGATIVA_PCT:
        return "declino"
    if crescita_recente_pct >= SOGLIA_CRESCITA_ALTA_PCT:
        return "esplorazione_coinvolgimento" if in_accelerazione else "sviluppo"
    return "sviluppo" if in_accelerazione else "consolidamento"


@app.get("/ciclo-vita-destinazione/{comune_id}")
def get_ciclo_vita_destinazione(comune_id: str):
    try:
        siti = ottieni_siti_comune(comune_id)
        if not siti:
            return {"errore": "Nessun sito culturale trovato per questo comune"}
        sito_ids = [s["id"] for s in siti]

        presenze_resp = supabase.table("presenza").select("data, gruppo").in_("sito_id", sito_ids).execute()
        presenze = presenze_resp.data or []

        if not presenze:
            return {"errore": "Nessun dato di presenze disponibile per questo comune"}

        oggi = datetime.now()
        prima_data = min(pd.to_datetime(p["data"]) for p in presenze)
        giorni_finestra = calcola_finestra_ciclo_vita(prima_data, oggi)

        if giorni_finestra is None:
            giorni_totali = (oggi - prima_data).days
            return {
                "comune_id": comune_id,
                "dati_sufficienti": False,
                "giorni_storico_disponibili": giorni_totali,
                "giorni_minimi_richiesti": SOGLIA_GIORNI_MIN_FASE_LATO * 3,
                "fasi_framework": FASI_BUTLER,
                "messaggio": (
                    f"Servono almeno {SOGLIA_GIORNI_MIN_FASE_LATO * 3} giorni di storico presenze per posizionare "
                    f"la destinazione sul ciclo di vita in modo statisticamente affidabile. Attualmente disponibili: "
                    f"{giorni_totali} giorni. Il posizionamento sarà calcolato automaticamente non appena lo storico "
                    f"sarà sufficiente."
                ),
            }

        confine_1 = oggi - timedelta(days=giorni_finestra)
        confine_2 = oggi - timedelta(days=giorni_finestra * 2)
        confine_3 = oggi - timedelta(days=giorni_finestra * 3)

        totale_p1 = 0
        totale_p2 = 0
        totale_p3 = 0
        for p in presenze:
            data_p = pd.to_datetime(p["data"])
            gruppo = p.get("gruppo", 0) or 0
            if data_p >= pd.Timestamp(confine_1):
                totale_p3 += gruppo
            elif data_p >= pd.Timestamp(confine_2):
                totale_p2 += gruppo
            elif data_p >= pd.Timestamp(confine_3):
                totale_p1 += gruppo

        crescita_1 = round((totale_p2 - totale_p1) / totale_p1 * 100, 1) if totale_p1 > 0 else None
        crescita_2 = round((totale_p3 - totale_p2) / totale_p2 * 100, 1) if totale_p2 > 0 else None

        if crescita_1 is None or crescita_2 is None:
            return {
                "comune_id": comune_id,
                "dati_sufficienti": False,
                "giorni_storico_disponibili": (oggi - prima_data).days,
                "giorni_minimi_richiesti": SOGLIA_GIORNI_MIN_FASE_LATO * 3,
                "fasi_framework": FASI_BUTLER,
                "messaggio": "Uno dei periodi confrontati non ha presenze sufficienti per calcolare una crescita significativa.",
            }

        in_accelerazione = crescita_2 > crescita_1
        fase_chiave = classifica_fase_butler(crescita_2, in_accelerazione)
        fase_dettaglio = next(f for f in FASI_BUTLER if f["chiave"] == fase_chiave)

        return {
            "comune_id": comune_id,
            "dati_sufficienti": True,
            "giorni_finestra": giorni_finestra,
            "fase_attuale": fase_dettaglio,
            "crescita_periodo_precedente_pct": crescita_1,
            "crescita_periodo_recente_pct": crescita_2,
            "in_accelerazione": in_accelerazione,
            "dato_sottostante": (
                f"Confronto su 3 periodi di {giorni_finestra} giorni ciascuno: {totale_p1} presenze nel periodo più "
                f"lontano, {totale_p2} nel periodo intermedio ({crescita_1}% rispetto al precedente), {totale_p3} "
                f"nel periodo più recente ({crescita_2}% rispetto al precedente). Il tasso di crescita risulta "
                f"{'in accelerazione' if in_accelerazione else 'in decelerazione o stabile'}."
            ),
            "fasi_framework": FASI_BUTLER,
        }
    except Exception as e:
        print(f"Errore ciclo di vita destinazione comune {comune_id}: {e}")
        return {"errore": str(e)}


def calcola_valore_siti_periodo(comune_id, data_inizio_str, data_fine_str):
    siti = ottieni_siti_comune(comune_id)
    if not siti:
        return None
    sito_ids = [s["id"] for s in siti]

    valore_totale_biglietti = 0
    valore_totale_commerciale = 0
    n_siti_con_dati = 0

    for sito_id in sito_ids:
        tariffe_resp = supabase.table("siti_culturali").select(
            "nome_sito, prezzo_biglietto, prezzo_ridotto, percentuale_ridotti, "
            "percentuale_bookshop, spesa_media_bookshop, "
            "percentuale_ristorazione, spesa_media_ristorazione"
        ).eq("id", sito_id).single().execute()
        tariffe = tariffe_resp.data
        if not tariffe or tariffe.get("prezzo_biglietto") is None:
            continue

        coeff_resp = supabase.table("coefficienti_spesa").select("*").eq("sito_id", sito_id).execute()
        coefficienti = {(c["fascia"], c["provenienza_macro"], c["tipo_visitatore"]): c["coefficiente"] for c in coeff_resp.data}

        storico_resp = supabase.table("presenza").select("data, gruppo, fasce, provenienza, tipo_visitatore") \
            .eq("sito_id", sito_id).gte("data", data_inizio_str).lte("data", data_fine_str).execute()
        storico = storico_resp.data
        if not storico:
            continue

        n_siti_con_dati += 1
        prezzo_medio = tariffe["prezzo_biglietto"] * (1 - tariffe["percentuale_ridotti"] / 100) + tariffe["prezzo_ridotto"] * (tariffe["percentuale_ridotti"] / 100)
        bookshop_base = (tariffe["percentuale_bookshop"] / 100) * tariffe["spesa_media_bookshop"]
        ristorazione_base = (tariffe["percentuale_ristorazione"] / 100) * tariffe["spesa_media_ristorazione"]

        for r in storico:
            fasce = [normalizza_fascia(f) for f in (r.get("fasce") or "").split(", ") if f]
            if not fasce:
                continue
            n_persone = r.get("gruppo", 0) or 0
            tipo = r.get("tipo_visitatore") or "gruppo"
            prov_macro = mappa_provenienza_macro(r.get("provenienza"))
            per_fascia = n_persone / len(fasce)

            for f in fasce:
                coeff = coefficienti.get((f, prov_macro, tipo), 1.0)
                valore_totale_biglietti += per_fascia * prezzo_medio
                valore_totale_commerciale += per_fascia * (bookshop_base + ristorazione_base) * coeff

    if n_siti_con_dati == 0:
        return None

    return {
        "n_siti_con_dati": n_siti_con_dati,
        "valore_biglietti": round(valore_totale_biglietti, 2),
        "valore_commerciale": round(valore_totale_commerciale, 2),
        "valore_totale": round(valore_totale_biglietti + valore_totale_commerciale, 2),
    }


@app.get("/dimensione-economica/{comune_id}")
def get_dimensione_economica(comune_id: str):
    try:
        oggi = datetime.now()
        dodici_mesi_fa = oggi - timedelta(days=365)

        risultato = calcola_valore_siti_periodo(comune_id, dodici_mesi_fa.strftime("%Y-%m-%d"), oggi.strftime("%Y-%m-%d"))

        if risultato is None:
            siti = ottieni_siti_comune(comune_id)
            if not siti:
                return {"errore": "Nessun sito culturale trovato per questo comune"}
            return {"errore": "Nessun sito con tariffe configurate e dati storici sufficienti per calcolare il valore economico"}

        n_siti_con_dati = risultato["n_siti_con_dati"]
        valore_totale_biglietti = risultato["valore_biglietti"]
        valore_totale_commerciale = risultato["valore_commerciale"]
        valore_totale = risultato["valore_totale"]

        return {
            "comune_id": comune_id,
            "periodo_giorni": 365,
            "siti_inclusi": n_siti_con_dati,
            "valore_biglietti": round(valore_totale_biglietti, 2),
            "valore_commerciale": round(valore_totale_commerciale, 2),
            "valore_totale_generato": valore_totale,
            "dato_sottostante": (
                f"Valore stimato sugli ultimi 12 mesi su {n_siti_con_dati} sito/i: €{round(valore_totale_biglietti, 0):.0f} "
                f"da biglietteria, €{round(valore_totale_commerciale, 0):.0f} da bookshop e ristorazione collegati alla visita, "
                f"per un totale di €{valore_totale:.0f}."
            ),
            "nota_metodologica": (
                "Il valore è stimato applicando alle presenze reali registrate le stesse tariffe e gli stessi "
                "coefficienti di spesa per fascia/provenienza/tipo visitatore già usati nelle previsioni economiche "
                "del sito: è una stima retrospettiva, non un dato di cassa effettivo."
            ),
        }
    except Exception as e:
        print(f"Errore dimensione economica comune {comune_id}: {e}")
        return {"errore": str(e)}


@app.get("/dimensione-sociale/{comune_id}")
def get_dimensione_sociale(comune_id: str):
    try:
        richieste_resp = supabase.table("richieste_pit").select("sentiment").eq("comune_id", comune_id).execute()
        richieste = richieste_resp.data or []

        if not richieste:
            return {"errore": "Nessuna richiesta PIT registrata per questo comune"}

        totale_richieste = len(richieste)
        conteggio = {"positivo": 0, "neutro": 0, "negativo": 0}
        non_classificate = 0
        for r in richieste:
            s = r.get("sentiment")
            if s in conteggio:
                conteggio[s] += 1
            else:
                non_classificate += 1

        totale_classificate = totale_richieste - non_classificate

        if totale_classificate == 0:
            return {
                "comune_id": comune_id,
                "dati_sufficienti": False,
                "totale_richieste": totale_richieste,
                "totale_classificate": 0,
                "messaggio": (
                    "Nessuna delle richieste PIT registrate ha ancora un sentiment classificato. La classificazione "
                    "avviene quando l'operatore la richiede esplicitamente per una richiesta con commento."
                ),
            }

        distribuzione = {
            k: {"conteggio": v, "quota_pct": round(v / totale_classificate * 100, 1)}
            for k, v in conteggio.items()
        }

        return {
            "comune_id": comune_id,
            "dati_sufficienti": True,
            "totale_richieste": totale_richieste,
            "totale_classificate": totale_classificate,
            "non_classificate": non_classificate,
            "distribuzione": distribuzione,
            "dato_sottostante": (
                f"Su {totale_richieste} richieste PIT totali, {totale_classificate} hanno un sentiment classificato "
                f"({conteggio['positivo']} positivo, {conteggio['neutro']} neutro, {conteggio['negativo']} negativo); "
                f"{non_classificate} richieste non sono ancora state classificate."
            ),
        }
    except Exception as e:
        print(f"Errore dimensione sociale comune {comune_id}: {e}")
        return {"errore": str(e)}

VOCAZIONI_TURISTICHE = [
    "Balneare / Mare",
    "Montano",
    "Lacustre",
    "Culturale / Storico-artistico",
    "Archeologico",
    "Naturalistico / Outdoor",
    "Faunistico / Birdwatching",
    "Enogastronomico",
    "Benessere / Wellness",
    "Termale",
    "Religioso / Spirituale / Pellegrinaggio",
    "Sportivo",
    "Cicloturismo",
    "Escursionistico / Trekking",
    "Urbano / Città d'arte",
    "Congressuale / Business (MICE)",
    "Di massa (alto volume, bassa permanenza)",
    "Esperienziale / Slow tourism",
    "Sanitario / Medico",
    "Rurale / Agriturismo",
    "Sostenibile / Ecoturismo",
    "Industriale / Archeologia industriale",
    "Enologico (vino)",
    "Family / Family-friendly",
    "Scolastico / Educativo",
    "Eventi e festival",
]


@app.get("/identikit-destinazione/{comune_id}")
def get_identikit_destinazione(comune_id: str):
    try:
        piano = ottieni_o_crea_piano_attivo(comune_id)

        identikit_resp = supabase.table("identikit_destinazione").select("*") \
            .eq("piano_id", piano["id"]).limit(1).execute()
        identikit = identikit_resp.data[0] if identikit_resp.data else None

        return {
            "piano_id": piano["id"],
            "comune_id": comune_id,
            "identikit": identikit,
            "vocazioni_disponibili": VOCAZIONI_TURISTICHE,
        }
    except Exception as e:
        print(f"Errore identikit destinazione comune {comune_id}: {e}")
        return {"errore": str(e)}


@app.put("/identikit-destinazione")
def aggiorna_identikit_destinazione(payload: dict):
    try:
        comune_id_str = payload.get("comune_id")
        if not comune_id_str:
            return {"errore": "comune_id è obbligatorio"}

        vocazione_attuale = payload.get("vocazione_attuale") or []
        vocazione_desiderata = payload.get("vocazione_desiderata") or []
        note = payload.get("note")

        if not isinstance(vocazione_attuale, list) or not isinstance(vocazione_desiderata, list):
            return {"errore": "vocazione_attuale e vocazione_desiderata devono essere liste"}

        non_valide_attuale = [v for v in vocazione_attuale if v not in VOCAZIONI_TURISTICHE]
        non_valide_desiderata = [v for v in vocazione_desiderata if v not in VOCAZIONI_TURISTICHE]
        if non_valide_attuale or non_valide_desiderata:
            return {"errore": f"Vocazioni non valide: {non_valide_attuale + non_valide_desiderata}"}

        piano = ottieni_o_crea_piano_attivo(comune_id_str)

        record = {
            "piano_id": piano["id"],
            "comune_id": comune_id_str,
            "vocazione_attuale": vocazione_attuale,
            "vocazione_desiderata": vocazione_desiderata,
            "note": note,
            "aggiornato_il": datetime.now().isoformat(),
        }
        supabase.table("identikit_destinazione").upsert(record, on_conflict="piano_id").execute()

        return {"status": "salvato", "piano_id": piano["id"]}
    except Exception as e:
        print(f"Errore aggiornamento identikit destinazione: {e}")
        return {"errore": str(e)}

def esegui_snapshot_indicatori(comune_id):
    indicatori = get_indicatori_standard(comune_id)
    if "errore" in indicatori:
        return {"errore": indicatori["errore"], "comune_id": comune_id}

    stagionalita = indicatori.get("stagionalita", {})
    internazionalizzazione = indicatori.get("internazionalizzazione", {})

    if not stagionalita.get("dati_sufficienti") and not internazionalizzazione.get("dati_sufficienti"):
        return {"status": "nessun indicatore con dati sufficienti da salvare", "comune_id": comune_id}

    piano = ottieni_o_crea_piano_attivo(comune_id)

    record = {
        "piano_id": piano["id"],
        "comune_id": comune_id,
        "coefficiente_variazione_pct": stagionalita.get("coefficiente_variazione_pct"),
        "livello_stagionalita": stagionalita.get("livello"),
        "quota_estera_pct": internazionalizzazione.get("quota_estera_pct"),
    }
    creato_resp = supabase.table("indicatori_storico").insert(record).execute()

    return {"status": "salvato", "comune_id": comune_id, "snapshot": creato_resp.data[0] if creato_resp.data else None}


@app.get("/indicatori-storico/{comune_id}")
def get_indicatori_storico(comune_id: str):
    try:
        piano = ottieni_o_crea_piano_attivo(comune_id)

        storico_resp = supabase.table("indicatori_storico").select("*") \
            .eq("piano_id", piano["id"]).order("generato_il", desc=False).execute()
        storico = storico_resp.data or []

        return {
            "piano_id": piano["id"],
            "comune_id": comune_id,
            "n_snapshot": len(storico),
            "storico": storico,
        }
    except Exception as e:
        print(f"Errore storico indicatori comune {comune_id}: {e}")
        return {"errore": str(e)}


@app.post("/indicatori-storico/{comune_id}/salva-snapshot")
def salva_snapshot_indicatori(comune_id: str):
    try:
        return esegui_snapshot_indicatori(comune_id)
    except Exception as e:
        print(f"Errore salvataggio snapshot indicatori comune {comune_id}: {e}")
        return {"errore": str(e)}


@app.post("/indicatori-storico/salva-snapshot-tutti")
def salva_snapshot_indicatori_tutti():
    try:
        piani_resp = supabase.table("piani_strategici").select("comune_id").neq("stato", "archiviato").execute()
        comuni = list({p["comune_id"] for p in (piani_resp.data or [])})

        risultati = []
        for comune_id in comuni:
            esito = esegui_snapshot_indicatori(comune_id)
            risultati.append(esito)

        return {
            "comuni_processati": len(comuni),
            "risultati": risultati
        }
    except Exception as e:
        print(f"Errore snapshot indicatori tutti i comuni: {e}")
        return {"errore": str(e)}

SOGLIA_CONCENTRAZIONE_BASSA = 30.0
SOGLIA_CONCENTRAZIONE_MODERATA = 80.0
SOGLIA_CONCENTRAZIONE_ALTA = 150.0


def livello_concentrazione_weekend(indice_pct):
    if indice_pct < SOGLIA_CONCENTRAZIONE_BASSA:
        return "basso"
    elif indice_pct < SOGLIA_CONCENTRAZIONE_MODERATA:
        return "moderato"
    elif indice_pct < SOGLIA_CONCENTRAZIONE_ALTA:
        return "alto"
    else:
        return "critico"


@app.get("/sostenibilita-carico/{comune_id}")
def get_sostenibilita_carico(comune_id: str):
    try:
        siti = ottieni_siti_comune(comune_id)
        if not siti:
            return {"errore": "Nessun sito culturale trovato per questo comune"}
        sito_ids = [s["id"] for s in siti]

        novanta_giorni_fa = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
        presenze_resp = supabase.table("presenza").select("data, gruppo") \
            .in_("sito_id", sito_ids).gte("data", novanta_giorni_fa).execute()
        presenze = presenze_resp.data or []

        if not presenze:
            return {"errore": "Nessun dato di presenze disponibile negli ultimi 90 giorni per questo comune"}

        df = pd.DataFrame(presenze)
        df["data"] = pd.to_datetime(df["data"])
        aggregato_giorno = df.groupby("data")["gruppo"].sum().reset_index()
        aggregato_giorno["is_weekend"] = aggregato_giorno["data"].dt.weekday >= 5

        giorni_weekend = aggregato_giorno[aggregato_giorno["is_weekend"]]
        giorni_feriali = aggregato_giorno[~aggregato_giorno["is_weekend"]]

        if len(giorni_weekend) == 0 or len(giorni_feriali) == 0:
            return {"errore": "Servono sia giorni feriali che giorni di weekend nello storico per calcolare questo indice"}

        media_weekend = float(giorni_weekend["gruppo"].mean())
        media_feriale = float(giorni_feriali["gruppo"].mean())

        if media_feriale == 0:
            return {"errore": "Media feriale pari a zero: impossibile calcolare un confronto significativo"}

        indice_concentrazione_pct = round((media_weekend / media_feriale - 1) * 100, 1)
        livello = livello_concentrazione_weekend(indice_concentrazione_pct)

        return {
            "comune_id": comune_id,
            "periodo_giorni": 90,
            "media_weekend": round(media_weekend, 1),
            "media_feriale": round(media_feriale, 1),
            "indice_concentrazione_pct": indice_concentrazione_pct,
            "livello": livello,
            "n_weekend_osservati": len(giorni_weekend),
            "n_feriali_osservati": len(giorni_feriali),
            "dato_sottostante": (
                f"Negli ultimi 90 giorni, la media di affluenza nei weekend ({round(media_weekend, 1)} presenze/giorno, "
                f"su {len(giorni_weekend)} giorni osservati) è del {indice_concentrazione_pct}% rispetto alla media "
                f"dei giorni feriali ({round(media_feriale, 1)} presenze/giorno, su {len(giorni_feriali)} giorni osservati)."
            ),
        }
    except Exception as e:
        print(f"Errore sostenibilità carico comune {comune_id}: {e}")
        return {"errore": str(e)}

@app.get("/accessibilita-pilastro/{comune_id}")
def get_accessibilita_pilastro(comune_id: str):
    try:
        richieste_resp = supabase.table("richieste_pit").select("data, categorie_disservizio").eq("comune_id", comune_id).execute()
        richieste = richieste_resp.data or []

        disservizi = [r for r in richieste if r.get("categorie_disservizio") and r["categorie_disservizio"].lower() != "altro"]
        totale_disservizi = len(disservizi)

        if totale_disservizi == 0:
            return {"errore": "Nessuna segnalazione di disservizio registrata per questo comune"}

        segnalazioni_accessibilita = [d for d in disservizi if d["categorie_disservizio"] == "Accessibilità"]
        n_accessibilita = len(segnalazioni_accessibilita)
        quota_pct = round(n_accessibilita / totale_disservizi * 100, 1)

        df = pd.DataFrame(disservizi)
        df["data"] = pd.to_datetime(df["data"])
        df["mese"] = df["data"].dt.strftime("%Y-%m")
        df["e_accessibilita"] = df["categorie_disservizio"] == "Accessibilità"

        trend_mensile_raw = df.groupby("mese").agg(
            totale=("e_accessibilita", "count"),
            accessibilita=("e_accessibilita", "sum")
        ).reset_index()
        trend_mensile = [
            {
                "mese": r["mese"],
                "n_accessibilita": int(r["accessibilita"]),
                "totale_disservizi": int(r["totale"]),
                "quota_pct": round(r["accessibilita"] / r["totale"] * 100, 1) if r["totale"] > 0 else 0,
            }
            for _, r in trend_mensile_raw.iterrows()
        ]

        return {
            "comune_id": comune_id,
            "n_segnalazioni_accessibilita": n_accessibilita,
            "totale_disservizi": totale_disservizi,
            "quota_pct": quota_pct,
            "trend_mensile": trend_mensile,
            "dato_sottostante": (
                f"{n_accessibilita} segnalazioni di disservizio \"Accessibilità\" su {totale_disservizi} disservizi "
                f"totali registrati ({quota_pct}%)."
            ),
        }
    except Exception as e:
        print(f"Errore accessibilità pilastro comune {comune_id}: {e}")
        return {"errore": str(e)}

CATEGORIE_WELFARE_INNOVAZIONE = {
    "Accessibilità potenziata": [
        "Barriere architettoniche", "Percorsi inclusivi", "Segnaletica per ipovedenti", "Accessibilità digitale",
    ],
    "Digitalizzazione servizi": [
        "App turistica", "Prenotazioni online", "Pagamenti digitali", "Audioguide digitali",
    ],
    "Sostenibilità ambientale": [
        "Mobilità verde", "Riduzione plastica monouso", "Efficientamento energetico", "Gestione rifiuti",
    ],
    "Inclusione sociale": [
        "Prezzi agevolati categorie fragili", "Programmi per disoccupati/giovani", "Integrazione comunità straniere",
    ],
    "Valorizzazione del patrimonio": [
        "Restauri", "Recupero siti minori", "Nuove aperture", "Manutenzione preventiva",
    ],
    "Formazione e competenze": [
        "Formazione operatori", "Competenze digitali del personale", "Corsi di lingua per il turismo",
    ],
    "Partecipazione e ascolto": [
        "Consultazioni pubbliche", "Sondaggi residenti", "Tavoli di confronto", "Coinvolgimento associazioni locali",
    ],
}


@app.get("/welfare-innovazione/{comune_id}")
def get_welfare_innovazione(comune_id: str):
    try:
        piano = ottieni_o_crea_piano_attivo(comune_id)

        iniziative_resp = supabase.table("iniziative_welfare_innovazione").select("*") \
            .eq("piano_id", piano["id"]).order("inserito_il", desc=True).execute()
        iniziative = iniziative_resp.data or []

        conteggio_categoria = {cat: 0 for cat in CATEGORIE_WELFARE_INNOVAZIONE}
        for i in iniziative:
            if i.get("stato") == "attiva" and i.get("categoria") in conteggio_categoria:
                conteggio_categoria[i["categoria"]] += 1

        n_categorie_coperte = sum(1 for v in conteggio_categoria.values() if v > 0)

        radar = [
            {
                "categoria": cat,
                "sotto_voci": CATEGORIE_WELFARE_INNOVAZIONE[cat],
                "n_iniziative_attive": conteggio_categoria[cat],
            }
            for cat in CATEGORIE_WELFARE_INNOVAZIONE
        ]

        return {
            "piano_id": piano["id"],
            "comune_id": comune_id,
            "n_iniziative_totali": len(iniziative),
            "n_iniziative_attive": sum(1 for i in iniziative if i.get("stato") == "attiva"),
            "n_categorie_coperte": n_categorie_coperte,
            "n_categorie_totali": len(CATEGORIE_WELFARE_INNOVAZIONE),
            "radar": radar,
            "iniziative": iniziative,
            "categorie_disponibili": CATEGORIE_WELFARE_INNOVAZIONE,
        }
    except Exception as e:
        print(f"Errore welfare innovazione comune {comune_id}: {e}")
        return {"errore": str(e)}


@app.post("/welfare-innovazione")
def crea_iniziativa_welfare(payload: dict):
    try:
        comune_id_str = payload.get("comune_id")
        categoria = payload.get("categoria")
        titolo = payload.get("titolo")
        sotto_voce = payload.get("sotto_voce")
        descrizione = payload.get("descrizione")
        stato = payload.get("stato", "attiva")

        if not comune_id_str or not categoria or not titolo:
            return {"errore": "comune_id, categoria e titolo sono obbligatori"}

        if categoria not in CATEGORIE_WELFARE_INNOVAZIONE:
            return {"errore": "Categoria non valida"}

        if stato not in ("attiva", "completata", "sospesa"):
            return {"errore": "Stato non valido"}

        piano = ottieni_o_crea_piano_attivo(comune_id_str)

        record = {
            "piano_id": piano["id"],
            "comune_id": comune_id_str,
            "categoria": categoria,
            "sotto_voce": sotto_voce,
            "titolo": titolo,
            "descrizione": descrizione,
            "stato": stato,
        }
        creato_resp = supabase.table("iniziative_welfare_innovazione").insert(record).execute()

        return {"status": "salvato", "iniziativa": creato_resp.data[0] if creato_resp.data else None}
    except Exception as e:
        print(f"Errore creazione iniziativa welfare: {e}")
        return {"errore": str(e)}


@app.put("/welfare-innovazione/{iniziativa_id}")
def aggiorna_iniziativa_welfare(iniziativa_id: int, payload: dict):
    try:
        stato = payload.get("stato")
        if stato not in ("attiva", "completata", "sospesa"):
            return {"errore": "Stato non valido"}
        supabase.table("iniziative_welfare_innovazione").update({"stato": stato}).eq("id", iniziativa_id).execute()
        return {"status": "aggiornato"}
    except Exception as e:
        print(f"Errore aggiornamento iniziativa welfare {iniziativa_id}: {e}")
        return {"errore": str(e)}


@app.delete("/welfare-innovazione/{iniziativa_id}")
def elimina_iniziativa_welfare(iniziativa_id: int):
    try:
        supabase.table("iniziative_welfare_innovazione").delete().eq("id", iniziativa_id).execute()
        return {"status": "eliminato"}
    except Exception as e:
        print(f"Errore eliminazione iniziativa welfare {iniziativa_id}: {e}")
        return {"errore": str(e)}

PILASTRI_OBIETTIVI = {
    "sostenibilita": {
        "nome": "Sostenibilità e Carico",
        "metrica": "Indice di concentrazione weekend (%)",
        "direzione_attesa": "diminuire",
    },
    "accessibilita": {
        "nome": "Accessibilità",
        "metrica": "Quota segnalazioni accessibilità sul totale disservizi (%)",
        "direzione_attesa": "diminuire",
    },
    "welfare_innovazione": {
        "nome": "Welfare e Innovazione",
        "metrica": "Categorie coperte da iniziative attive (su 7)",
        "direzione_attesa": "aumentare",
    },
}


def leggi_valore_attuale_pilastro(comune_id, pilastro):
    if pilastro == "sostenibilita":
        dati = get_sostenibilita_carico(comune_id)
        if "errore" in dati:
            return None
        return dati["indice_concentrazione_pct"]
    elif pilastro == "accessibilita":
        dati = get_accessibilita_pilastro(comune_id)
        if "errore" in dati:
            return None
        return dati["quota_pct"]
    elif pilastro == "welfare_innovazione":
        dati = get_welfare_innovazione(comune_id)
        if "errore" in dati:
            return None
        return dati["n_categorie_coperte"]
    return None


def calcola_semaforo_obiettivo(valore_iniziale, valore_attuale, valore_target, data_inizio, data_scadenza, oggi):
    giorni_totali = (data_scadenza - data_inizio).days
    giorni_trascorsi = (oggi - data_inizio).days
    progresso_temporale = min(max(giorni_trascorsi / giorni_totali, 0), 1) if giorni_totali > 0 else 1

    distanza_totale = valore_target - valore_iniziale
    distanza_percorsa = valore_attuale - valore_iniziale

    if distanza_totale == 0:
        progresso_obiettivo = 1.0
    else:
        progresso_obiettivo = distanza_percorsa / distanza_totale

    scostamento = progresso_obiettivo - progresso_temporale

    if progresso_obiettivo >= 1.0:
        colore = "verde"
    elif scostamento >= -0.15:
        colore = "verde"
    elif scostamento >= -0.35:
        colore = "giallo"
    else:
        colore = "rosso"

    return {
        "colore": colore,
        "progresso_obiettivo_pct": round(progresso_obiettivo * 100, 1),
        "progresso_temporale_pct": round(progresso_temporale * 100, 1),
    }


INDICATORI_EFFETTO = {
    "stagionalita": {
        "nome": "Stagionalità",
        "metrica": "Coefficiente di variazione delle presenze mensili (%)",
        "direzione_migliorativa": "diminuire",
    },
    "internazionalizzazione": {
        "nome": "Internazionalizzazione",
        "metrica": "Quota di presenze estere sul totale (%)",
        "direzione_migliorativa": "aumentare",
    },
    "concentrazione_weekend": {
        "nome": "Concentrazione weekend",
        "metrica": "Indice di concentrazione weekend vs feriale (%)",
        "direzione_migliorativa": "diminuire",
    },
    "accessibilita": {
        "nome": "Accessibilità",
        "metrica": "Quota segnalazioni accessibilità sul totale disservizi (%)",
        "direzione_migliorativa": "diminuire",
    },
    "welfare_innovazione": {
        "nome": "Welfare e Innovazione",
        "metrica": "Categorie coperte da iniziative attive (su 7)",
        "direzione_migliorativa": "aumentare",
    },
}


def leggi_valore_indicatore(comune_id, indicatore):
    if indicatore == "stagionalita":
        dati = get_indicatori_standard(comune_id)
        if "errore" in dati:
            return None, None
        stag = dati.get("stagionalita", {})
        if not stag.get("dati_sufficienti"):
            return None, None
        return stag.get("coefficiente_variazione_pct"), stag.get("dato_sottostante")
    elif indicatore == "internazionalizzazione":
        dati = get_indicatori_standard(comune_id)
        if "errore" in dati:
            return None, None
        intl = dati.get("internazionalizzazione", {})
        if not intl.get("dati_sufficienti"):
            return None, None
        return intl.get("quota_estera_pct"), intl.get("dato_sottostante")
    elif indicatore == "concentrazione_weekend":
        dati = get_sostenibilita_carico(comune_id)
        if "errore" in dati:
            return None, None
        return dati.get("indice_concentrazione_pct"), dati.get("dato_sottostante")
    elif indicatore == "accessibilita":
        dati = get_accessibilita_pilastro(comune_id)
        if "errore" in dati:
            return None, None
        return dati.get("quota_pct"), dati.get("dato_sottostante")
    elif indicatore == "welfare_innovazione":
        dati = get_welfare_innovazione(comune_id)
        if "errore" in dati:
            return None, None
        raw = f"{dati.get('n_categorie_coperte')} su {dati.get('n_categorie_totali')} categorie coperte da iniziative attive."
        return dati.get("n_categorie_coperte"), raw
    return None, None


@app.get("/azioni-effetti/{comune_id}")
def get_azioni_effetti(comune_id: str):
    try:
        piano = ottieni_o_crea_piano_attivo(comune_id)

        effetti_resp = supabase.table("azione_effetti_attesi").select("*") \
            .eq("piano_id", piano["id"]).eq("attivo", True).order("creato_il", desc=True).execute()
        effetti = effetti_resp.data or []

        oggi = datetime.now().date()
        valori_cache = {}
        risultati = []
        for e in effetti:
            indicatore = e["indicatore"]
            indicatore_info = INDICATORI_EFFETTO.get(indicatore, {})

            if indicatore not in valori_cache:
                valori_cache[indicatore] = leggi_valore_indicatore(comune_id, indicatore)
            valore_attuale, dato_grezzo = valori_cache[indicatore]

            if valore_attuale is None:
                risultati.append({
                    **e,
                    "indicatore_nome": indicatore_info.get("nome", indicatore),
                    "metrica": indicatore_info.get("metrica", ""),
                    "direzione_migliorativa": indicatore_info.get("direzione_migliorativa"),
                    "valore_attuale": None,
                    "semaforo": None,
                    "dato_grezzo_attuale": None,
                    "messaggio": "Valore attuale non disponibile: dati insufficienti per questo indicatore.",
                })
                continue

            data_inizio = datetime.strptime(e["data_inizio"], "%Y-%m-%d").date()
            data_scadenza = datetime.strptime(e["data_scadenza"], "%Y-%m-%d").date()

            semaforo = calcola_semaforo_obiettivo(
                e["valore_iniziale"], valore_attuale, e["valore_target"], data_inizio, data_scadenza, oggi
            )

            risultati.append({
                **e,
                "indicatore_nome": indicatore_info.get("nome", indicatore),
                "metrica": indicatore_info.get("metrica", ""),
                "direzione_migliorativa": indicatore_info.get("direzione_migliorativa"),
                "valore_attuale": valore_attuale,
                "semaforo": semaforo["colore"],
                "progresso_obiettivo_pct": semaforo["progresso_obiettivo_pct"],
                "progresso_temporale_pct": semaforo["progresso_temporale_pct"],
                "dato_grezzo_attuale": dato_grezzo,
                "dato_sottostante": (
                    f"Partito da {e['valore_iniziale']}, target {e['valore_target']} entro il "
                    f"{data_scadenza.strftime('%d/%m/%Y')}. Valore attuale: {valore_attuale}. "
                    f"Progresso verso l'obiettivo: {semaforo['progresso_obiettivo_pct']}%, "
                    f"tempo trascorso: {semaforo['progresso_temporale_pct']}%."
                ),
            })

        return {
            "piano_id": piano["id"],
            "comune_id": comune_id,
            "effetti": risultati,
            "n_totale": len(risultati),
            "indicatori_disponibili": INDICATORI_EFFETTO,
        }
    except Exception as e:
        print(f"Errore azioni effetti comune {comune_id}: {e}")
        return {"errore": str(e)}


@app.get("/azioni-effetti/azione/{azione_id}")
def get_effetti_per_azione(azione_id: int):
    try:
        azione_resp = supabase.table("azioni_piano").select("comune_id").eq("id", azione_id).single().execute()
        azione = azione_resp.data
        if not azione:
            return {"errore": "Azione non trovata"}

        comune_id = azione["comune_id"]

        effetti_resp = supabase.table("azione_effetti_attesi").select("*") \
            .eq("azione_id", azione_id).eq("attivo", True).order("creato_il", desc=True).execute()
        effetti = effetti_resp.data or []

        oggi = datetime.now().date()
        valori_cache = {}
        risultati = []
        for e in effetti:
            indicatore = e["indicatore"]
            indicatore_info = INDICATORI_EFFETTO.get(indicatore, {})

            if indicatore not in valori_cache:
                valori_cache[indicatore] = leggi_valore_indicatore(comune_id, indicatore)
            valore_attuale, dato_grezzo = valori_cache[indicatore]

            if valore_attuale is None:
                risultati.append({
                    **e,
                    "indicatore_nome": indicatore_info.get("nome", indicatore),
                    "metrica": indicatore_info.get("metrica", ""),
                    "direzione_migliorativa": indicatore_info.get("direzione_migliorativa"),
                    "valore_attuale": None,
                    "semaforo": None,
                    "dato_grezzo_attuale": None,
                    "messaggio": "Valore attuale non disponibile: dati insufficienti per questo indicatore.",
                })
                continue

            data_inizio = datetime.strptime(e["data_inizio"], "%Y-%m-%d").date()
            data_scadenza = datetime.strptime(e["data_scadenza"], "%Y-%m-%d").date()

            semaforo = calcola_semaforo_obiettivo(
                e["valore_iniziale"], valore_attuale, e["valore_target"], data_inizio, data_scadenza, oggi
            )

            risultati.append({
                **e,
                "indicatore_nome": indicatore_info.get("nome", indicatore),
                "metrica": indicatore_info.get("metrica", ""),
                "direzione_migliorativa": indicatore_info.get("direzione_migliorativa"),
                "valore_attuale": valore_attuale,
                "semaforo": semaforo["colore"],
                "progresso_obiettivo_pct": semaforo["progresso_obiettivo_pct"],
                "progresso_temporale_pct": semaforo["progresso_temporale_pct"],
                "dato_grezzo_attuale": dato_grezzo,
                "dato_sottostante": (
                    f"Partito da {e['valore_iniziale']}, target {e['valore_target']} entro il "
                    f"{data_scadenza.strftime('%d/%m/%Y')}. Valore attuale: {valore_attuale}. "
                    f"Progresso verso l'obiettivo: {semaforo['progresso_obiettivo_pct']}%, "
                    f"tempo trascorso: {semaforo['progresso_temporale_pct']}%."
                ),
            })

        return {
            "azione_id": azione_id,
            "comune_id": comune_id,
            "effetti": risultati,
            "n_totale": len(risultati),
            "indicatori_disponibili": INDICATORI_EFFETTO,
        }
    except Exception as e:
        print(f"Errore effetti per azione {azione_id}: {e}")
        return {"errore": str(e)}


@app.post("/azioni-effetti")
def crea_effetto_atteso(payload: dict):
    try:
        azione_id = payload.get("azione_id")
        comune_id_str = payload.get("comune_id")
        indicatore = payload.get("indicatore")
        valore_target = payload.get("valore_target")
        data_scadenza = payload.get("data_scadenza")
        note = payload.get("note")

        if not azione_id or not comune_id_str or not indicatore or valore_target is None or not data_scadenza:
            return {"errore": "azione_id, comune_id, indicatore, valore_target e data_scadenza sono obbligatori"}

        if indicatore not in INDICATORI_EFFETTO:
            return {"errore": "Indicatore non valido"}

        valore_iniziale, _ = leggi_valore_indicatore(comune_id_str, indicatore)
        if valore_iniziale is None:
            return {"errore": "Impossibile leggere il valore attuale per questo indicatore: dati insufficienti"}

        piano = ottieni_o_crea_piano_attivo(comune_id_str)

        record = {
            "azione_id": azione_id,
            "piano_id": piano["id"],
            "comune_id": comune_id_str,
            "indicatore": indicatore,
            "valore_iniziale": valore_iniziale,
            "valore_target": valore_target,
            "data_inizio": datetime.now().strftime("%Y-%m-%d"),
            "data_scadenza": data_scadenza,
            "note": note,
        }
        creato_resp = supabase.table("azione_effetti_attesi").insert(record).execute()

        return {"status": "salvato", "effetto": creato_resp.data[0] if creato_resp.data else None}
    except Exception as e:
        print(f"Errore creazione effetto atteso: {e}")
        return {"errore": str(e)}


@app.delete("/azioni-effetti/{effetto_id}")
def elimina_effetto_atteso(effetto_id: int):
    try:
        supabase.table("azione_effetti_attesi").update({"attivo": False}).eq("id", effetto_id).execute()
        return {"status": "disattivato"}
    except Exception as e:
        print(f"Errore eliminazione effetto atteso {effetto_id}: {e}")
        return {"errore": str(e)}

FORMULE_PROMOZIONALI = {
    "sconto": {
        "nome": "Sconto diretto",
        "descrizione": "Riduzione immediata sul prezzo pieno per stimolare la domanda in un periodo definito.",
        "parametri": [
            {"chiave": "percentuale_sconto", "label": "Percentuale di sconto", "tipo": "percentuale"},
            {"chiave": "condizioni", "label": "Condizioni di applicazione", "tipo": "testo"},
        ],
    },
    "coupon": {
        "nome": "Coupon",
        "descrizione": "Buono che dà diritto a una riduzione o a un vantaggio all'atto dell'acquisto.",
        "parametri": [
            {"chiave": "valore_coupon", "label": "Valore del coupon", "tipo": "euro"},
            {"chiave": "condizioni_utilizzo", "label": "Condizioni di utilizzo", "tipo": "testo"},
            {"chiave": "canale_distribuzione", "label": "Canale di distribuzione", "tipo": "testo"},
        ],
    },
    "valore_aggiunto": {
        "nome": "Valore aggiunto / Omaggio",
        "descrizione": "Bene o servizio aggiuntivo offerto insieme all'acquisto principale.",
        "parametri": [
            {"chiave": "descrizione_omaggio", "label": "Descrizione dell'omaggio", "tipo": "testo"},
            {"chiave": "valore_omaggio", "label": "Valore dell'omaggio", "tipo": "euro"},
            {"chiave": "soglia_acquisto", "label": "Soglia minima di acquisto", "tipo": "euro"},
        ],
    },
    "tre_per_due": {
        "nome": "3x2 e varianti quantità",
        "descrizione": "Meccanica basata sulla quantità: se ne pagano alcune e se ne ricevono di più.",
        "parametri": [
            {"chiave": "quantita_pagata", "label": "Quantità pagata", "tipo": "intero"},
            {"chiave": "quantita_ricevuta", "label": "Quantità ricevuta", "tipo": "intero"},
            {"chiave": "condizioni", "label": "Condizioni", "tipo": "testo"},
        ],
    },
    "concorso": {
        "nome": "Concorsi e lotterie",
        "descrizione": "Assegnazione di premi tramite estrazione o meccanica di gioco, per generare partecipazione.",
        "parametri": [
            {"chiave": "montepremi_totale", "label": "Montepremi totale", "tipo": "euro"},
            {"chiave": "descrizione_premi", "label": "Descrizione dei premi", "tipo": "testo"},
            {"chiave": "meccanica_partecipazione", "label": "Meccanica di partecipazione", "tipo": "testo"},
        ],
    },
    "marketing_sociale": {
        "nome": "Marketing sociale (cause related)",
        "descrizione": "Parte del ricavo devoluta a una causa, legando l'acquisto a un beneficio collettivo.",
        "parametri": [
            {"chiave": "causa_beneficiaria", "label": "Causa beneficiaria", "tipo": "testo"},
            {"chiave": "quota_devoluta", "label": "Quota devoluta", "tipo": "euro"},
            {"chiave": "descrizione", "label": "Descrizione", "tipo": "testo"},
        ],
    },
    "self_liquidating": {
        "nome": "Self-liquidating premium",
        "descrizione": "Premio autoliquidante: il cliente lo ottiene pagando un prezzo simbolico inferiore al valore reale.",
        "parametri": [
            {"chiave": "prezzo_simbolico", "label": "Prezzo simbolico richiesto", "tipo": "euro"},
            {"chiave": "valore_reale_premio", "label": "Valore reale del premio", "tipo": "euro"},
            {"chiave": "descrizione_premio", "label": "Descrizione del premio", "tipo": "testo"},
        ],
    },
    "free_premium": {
        "nome": "Free premium",
        "descrizione": "Premio gratuito consegnato al cliente, senza costo aggiuntivo per lui.",
        "parametri": [
            {"chiave": "descrizione_premio", "label": "Descrizione del premio", "tipo": "testo"},
            {"chiave": "valore_premio", "label": "Valore del premio", "tipo": "euro"},
            {"chiave": "modalita_consegna", "label": "Modalità di consegna", "tipo": "testo"},
        ],
    },
    "merchandising": {
        "nome": "Merchandising",
        "descrizione": "Articoli brandizzati della destinazione, come leva di promozione e di ricavo accessorio.",
        "parametri": [
            {"chiave": "descrizione_articoli", "label": "Descrizione degli articoli", "tipo": "testo"},
            {"chiave": "prezzo_vendita", "label": "Prezzo di vendita", "tipo": "euro"},
            {"chiave": "punto_vendita", "label": "Punto vendita", "tipo": "testo"},
        ],
    },
}

CLASSIFICAZIONI_PROMOZIONALI_AMMESSE = ("BTL", "TTL")


@app.get("/varianti-promozionali/azione/{azione_id}")
def get_varianti_promozionali(azione_id: int):
    try:
        azione_resp = supabase.table("azioni_piano").select("area, classificazione, comune_id").eq("id", azione_id).single().execute()
        azione = azione_resp.data
        if not azione:
            return {"errore": "Azione non trovata"}

        ammessa = azione.get("area") == "turismo" and azione.get("classificazione") in CLASSIFICAZIONI_PROMOZIONALI_AMMESSE

        varianti_resp = supabase.table("varianti_promozionali").select("*") \
            .eq("azione_id", azione_id).eq("attivo", True).order("creato_il", desc=True).execute()
        varianti = varianti_resp.data or []

        risultati = []
        for v in varianti:
            tipo = v["tipo_formula"]
            formula_info = FORMULE_PROMOZIONALI.get(tipo, {})
            risultati.append({
                **v,
                "formula_nome": formula_info.get("nome", tipo),
                "formula_descrizione": formula_info.get("descrizione", ""),
                "parametri_schema": formula_info.get("parametri", []),
            })

        return {
            "azione_id": azione_id,
            "comune_id": azione.get("comune_id"),
            "configurabile": ammessa,
            "area": azione.get("area"),
            "classificazione": azione.get("classificazione"),
            "varianti": risultati,
            "n_totale": len(risultati),
            "formule_disponibili": FORMULE_PROMOZIONALI,
        }
    except Exception as e:
        print(f"Errore get varianti promozionali azione {azione_id}: {e}")
        return {"errore": str(e)}


@app.post("/varianti-promozionali")
def crea_variante_promozionale(payload: dict):
    try:
        azione_id = payload.get("azione_id")
        comune_id_str = payload.get("comune_id")
        tipo_formula = payload.get("tipo_formula")
        parametri = payload.get("parametri") or {}
        budget_stimato = payload.get("budget_stimato")
        data_inizio = payload.get("data_inizio")
        data_fine = payload.get("data_fine")
        note = payload.get("note")

        if not azione_id or not comune_id_str or not tipo_formula:
            return {"errore": "azione_id, comune_id e tipo_formula sono obbligatori"}

        if tipo_formula not in FORMULE_PROMOZIONALI:
            return {"errore": "Tipo di formula non valido"}

        azione_resp = supabase.table("azioni_piano").select("area, classificazione").eq("id", azione_id).single().execute()
        azione = azione_resp.data
        if not azione:
            return {"errore": "Azione non trovata"}

        if azione.get("area") != "turismo" or azione.get("classificazione") not in CLASSIFICAZIONI_PROMOZIONALI_AMMESSE:
            return {"errore": "Le formule promozionali sono configurabili solo su azioni di Turismo con classificazione BTL o TTL"}

        piano = ottieni_o_crea_piano_attivo(comune_id_str)

        record = {
            "azione_id": azione_id,
            "piano_id": piano["id"],
            "comune_id": comune_id_str,
            "tipo_formula": tipo_formula,
            "parametri": parametri,
            "budget_stimato": budget_stimato,
            "data_inizio": data_inizio,
            "data_fine": data_fine,
            "note": note,
        }
        creato_resp = supabase.table("varianti_promozionali").insert(record).execute()

        return {"status": "salvato", "variante": creato_resp.data[0] if creato_resp.data else None}
    except Exception as e:
        print(f"Errore creazione variante promozionale: {e}")
        return {"errore": str(e)}


@app.delete("/varianti-promozionali/{variante_id}")
def elimina_variante_promozionale(variante_id: int):
    try:
        supabase.table("varianti_promozionali").update({"attivo": False}).eq("id", variante_id).execute()
        return {"status": "disattivato"}
    except Exception as e:
        print(f"Errore eliminazione variante promozionale {variante_id}: {e}")
        return {"errore": str(e)}


@app.get("/obiettivi-piano/{comune_id}")
def get_obiettivi_piano(comune_id: str):
    try:
        piano = ottieni_o_crea_piano_attivo(comune_id)

        obiettivi_resp = supabase.table("obiettivi_piano").select("*") \
            .eq("piano_id", piano["id"]).eq("attivo", True).execute()
        obiettivi = obiettivi_resp.data or []

        oggi = datetime.now().date()
        risultati = []
        for o in obiettivi:
            valore_attuale = leggi_valore_attuale_pilastro(comune_id, o["pilastro"])
            pilastro_info = PILASTRI_OBIETTIVI.get(o["pilastro"], {})

            if valore_attuale is None:
                risultati.append({
                    **o,
                    "pilastro_nome": pilastro_info.get("nome", o["pilastro"]),
                    "metrica": pilastro_info.get("metrica", ""),
                    "valore_attuale": None,
                    "semaforo": None,
                    "messaggio": "Valore attuale non disponibile: dati insufficienti per questo pilastro.",
                })
                continue

            data_inizio = datetime.strptime(o["data_inizio"], "%Y-%m-%d").date()
            data_scadenza = datetime.strptime(o["data_scadenza"], "%Y-%m-%d").date()

            semaforo = calcola_semaforo_obiettivo(
                o["valore_iniziale"], valore_attuale, o["valore_target"], data_inizio, data_scadenza, oggi
            )

            risultati.append({
                **o,
                "pilastro_nome": pilastro_info.get("nome", o["pilastro"]),
                "metrica": pilastro_info.get("metrica", ""),
                "valore_attuale": valore_attuale,
                "semaforo": semaforo["colore"],
                "progresso_obiettivo_pct": semaforo["progresso_obiettivo_pct"],
                "progresso_temporale_pct": semaforo["progresso_temporale_pct"],
                "dato_sottostante": (
                    f"Partito da {o['valore_iniziale']}, target {o['valore_target']} entro il "
                    f"{data_scadenza.strftime('%d/%m/%Y')}. Valore attuale: {valore_attuale}. "
                    f"Progresso verso l'obiettivo: {semaforo['progresso_obiettivo_pct']}%, "
                    f"tempo trascorso: {semaforo['progresso_temporale_pct']}%."
                ),
            })

        return {
            "piano_id": piano["id"],
            "comune_id": comune_id,
            "obiettivi": risultati,
            "pilastri_disponibili": PILASTRI_OBIETTIVI,
        }
    except Exception as e:
        print(f"Errore obiettivi piano comune {comune_id}: {e}")
        return {"errore": str(e)}


@app.post("/obiettivi-piano")
def crea_obiettivo_piano(payload: dict):
    try:
        comune_id_str = payload.get("comune_id")
        pilastro = payload.get("pilastro")
        valore_target = payload.get("valore_target")
        data_scadenza = payload.get("data_scadenza")
        note = payload.get("note")

        if not comune_id_str or not pilastro or valore_target is None or not data_scadenza:
            return {"errore": "comune_id, pilastro, valore_target e data_scadenza sono obbligatori"}

        if pilastro not in PILASTRI_OBIETTIVI:
            return {"errore": "Pilastro non valido"}

        valore_iniziale = leggi_valore_attuale_pilastro(comune_id_str, pilastro)
        if valore_iniziale is None:
            return {"errore": "Impossibile leggere il valore attuale per questo pilastro: dati insufficienti"}

        piano = ottieni_o_crea_piano_attivo(comune_id_str)

        record = {
            "piano_id": piano["id"],
            "comune_id": comune_id_str,
            "pilastro": pilastro,
            "valore_iniziale": valore_iniziale,
            "valore_target": valore_target,
            "direzione": PILASTRI_OBIETTIVI[pilastro]["direzione_attesa"],
            "data_inizio": datetime.now().strftime("%Y-%m-%d"),
            "data_scadenza": data_scadenza,
            "note": note,
        }
        creato_resp = supabase.table("obiettivi_piano").insert(record).execute()

        return {"status": "salvato", "obiettivo": creato_resp.data[0] if creato_resp.data else None}
    except Exception as e:
        print(f"Errore creazione obiettivo piano: {e}")
        return {"errore": str(e)}


@app.delete("/obiettivi-piano/{obiettivo_id}")
def elimina_obiettivo_piano(obiettivo_id: int):
    try:
        supabase.table("obiettivi_piano").update({"attivo": False}).eq("id", obiettivo_id).execute()
        return {"status": "disattivato"}
    except Exception as e:
        print(f"Errore eliminazione obiettivo piano {obiettivo_id}: {e}")
        return {"errore": str(e)}

CATEGORIE_RISORSE_TERRITORIALI = {
    "Collocazione geografica": [
        "Collegamenti con l'esterno", "Numero e rotte dei vettori", "Inserimento nei circuiti turistici nazionali",
        "Inserimento nei circuiti turistici internazionali", "Zona di interscambio di merci e servizi",
        "Zona in espansione nella prospettiva di integrazione",
    ],
    "Infrastrutture": [
        "Dotazione infrastrutturale", "Punte di eccellenza e casi di mediocrità", "Modernizzazione del sistema",
        "Competitività complessiva del territorio", "Piano generale di assetto e coordinamento delle infrastrutture di servizio",
        "Pianificazione degli interventi e attribuzione specializzata di compiti con economie di scala",
        "Distribuzione idrica", "Smaltimento e riciclaggio dei rifiuti",
    ],
    "Accesso al territorio": [
        "Agenzie di viaggio nel luogo di partenza/arrivo dei turisti", "Sistemi e punti informazione",
        "Sistemi segnaletici", "Sistemi di flusso di traffico facilitato", "Parcheggi per auto e pullman",
    ],
    "Ristorazione": [
        "Ristoranti", "Trattorie", "Pizzerie", "Caffè", "Bar", "Self service", "Birrerie",
        "Gastronomia basata sulla cucina e sui prodotti del territorio",
    ],
    "Alloggi": [
        "Natura", "Dimensione", "Densità", "Categoria", "Ruolo dei consumatori di spazio",
        "Categoria individuale", "Categoria familiare", "Categoria collettiva", "Categoria commerciale",
        "Categoria non commerciale",
    ],
    "Turismo": [
        "Vocazione turistica più datata e conosciuta anche all'estero", "Elementi che esprimono i livelli di vocazione turistica",
        "Composizione del sistema ricettivo", "Zone di interesse del sistema ricettivo",
        "Forme ed esito di proiezione del turismo all'esterno", "Articolazione della domanda turistica",
        "Richieste di servizi alternativi alla pura vacanza", "Azioni da parte dei turisti", "Shopping",
    ],
    "Soggetti e attori": [
        "Soggetti decisionali che gestiscono l'offerta", "Soggetti operativi", "Residenti", "Turisti", "Tipologia",
        "Motivazioni della presenza nella zona", "Caratteri socio-economici del target", "Valutazioni su carenze o disfunzioni",
        "Conoscenza della zona", "Apprezzamento della zona", "Stimoli degli input comunicativi",
    ],
    "Attrattive dell'ambiente naturale": [
        "Caratteristiche proprie dell'ambiente naturale", "Spiagge", "Rocce", "Grotte", "Fiumi e laghi", "Foreste",
        "Flora e fauna",
    ],
    "Edifici di forte richiamo non specificamente turistici": [
        "Chiese e cattedrali", "Dimore signorili e palazzi storici", "Siti archeologici",
        "Siti di archeologia industriale", "Ferrovie a vapore", "Miniere",
    ],
    "Strutture create per attirare visitatori": [
        "Parchi divertimento", "Musei all'aria aperta", "Casinò", "Terme", "Aree pic-nic", "Fiere",
        "Stabilimenti balneari",
    ],
    "Avvenimenti particolari": [
        "Eventi sportivi", "Festival artistici", "Fiere e mercati", "Eventi folcloristici", "Anniversari storici",
        "Eventi religiosi",
    ],
    "Rilevanza delle risorse ambientali, storiche e culturali": [
        "Morfologia del territorio", "Presenza di località e centri storici d'interesse",
        "Caratteristiche socio-culturali dei comuni e dei centri storici",
        "Dinamiche e caratterizzazioni socio-economiche e motivazionali", "Presenza di parchi ed aree protette",
        "Tutela ambientale", "Sistema di aree e parchi naturali attrezzati e strutturati",
        "Utilizzo del patrimonio ambientale in termini di reddito e occupazione",
    ],
    "Elementi naturali": [
        "Elementi topografici, idrici ed aerei", "Flora e fauna", "Topografia dei luoghi (percezione dei paesaggi)",
        "Natura delle rocce", "Altezza e pendenza degli spazi", "Profondità topografiche per la speleologia (grotte attrezzate)",
        "Coste come confine tra terra e mare (isole, baie, capi, strade panoramiche)", "Mare balneabile",
        "Grandi laghi", "Laghi vulcanici o laghetti alpini", "Corsi d'acqua per turismo fluviale e pesca sportiva",
        "Fonti termali", "Piogge", "Neve", "Quantità e ritmo delle precipitazioni", "Persistenza del manto nevoso",
        "Ghiaccio (sci estivo sui ghiacciai, pattinaggio)", "Temperatura dell'aria", "Variazioni termiche e igrometriche",
        "Nicchie di microclima", "Vento", "Vegetazione naturale", "Vegetazione coltivata", "Fauna",
        "Spazi di osservazione della fauna", "Riserve ornitologiche", "Parchi nazionali e naturali",
    ],
    "Patrimonio artificiale": [
        "Elementi del patrimonio storico-culturale incorporati nel prodotto territorio", "Parte artistica della città",
        "Quartieri", "Villaggi protetti e classificati", "Monumenti civili e religiosi (intatti o in rovina)",
        "Musei di belle arti", "Musei di scienza e tecnica", "Musei di arti e tradizioni popolari", "Eco-musei",
    ],
    "Patrimonio storico con funzione originaria conservata": [
        "Cittadelle militari visitabili in giornate determinate", "Chiese e cattedrali non visitabili durante le funzioni",
        "Villaggi o quartieri urbani abitati dai residenti senza rapporti con il turismo",
    ],
    "Patrimonio storico-culturale non costruito (immateriale)": [
        "Cultura locale", "Lingua", "Costume", "Gastronomia", "Folclore", "Feste", "Cultura materiale",
    ],
    "Elementi creati per piacere e svago": [
        "Casinò", "Teatri e sale da concerti delle stazioni termali o balneari",
    ],
    "Impianti sportivi": [
        "Golf", "Ippodromi", "Impianti di risalita", "Sentieri attrezzati", "Complessi sportivi",
        "Grandi stadi multifunzionali",
    ],
    "Impianti per turismo d'affari e congressuale": [
        "Saloni", "Fiere", "Parchi d'esposizione", "Palazzi per congressi", "Parchi di attrazione", "Spettacoli",
        "Eventi", "Avvenimenti", "Operazioni di rinnovamento urbano finalizzate allo sviluppo turistico",
        "Quartieri conservati e restaurati", "Zone pedonali",
    ],
    "Servizi alle imprese e infrastrutture di ricerca": [
        "Trasferimento di innovazione alle imprese", "Sviluppo e dinamica dei servizi privati e alle imprese",
        "Proiezione turistica e vocazione nel terziario",
    ],
    "Imprese": [
        "Diversificazione produttiva", "Integrazione intersettoriale per la crescita simultanea delle componenti",
        "Potenzialità professionali delle risorse umane presenti nel territorio", "Opportunità offerte dai mercati",
        "Dimensioni delle imprese", "Segmenti di mercato", "Possibilità di sviluppo e fasi di espansione",
        "Ruolo della grande impresa industriale", "Formazione sul campo di una cultura imprenditoriale",
        "Formazione sul campo di una cultura tecnica diffusa", "Settori proiettati sull'estero",
        "Potenzialità dei settori", "Presenza dei distretti industriali riconosciuti",
        "Grado di iniziativa dell'artigianato",
    ],
    "Dinamica imprenditoriale": [
        "Espansione numerica delle imprese attive", "Natalità/mortalità delle imprese",
        "Condizioni per l'affermazione delle imprese", "Sbocchi sui mercati esterni",
    ],
    "Propensione all'export e sistema di marketing": [
        "Propensione all'export di prodotti locali", "Sistema di marketing", "Apertura sull'estero per l'export di prodotti",
        "Rilevanza e notorietà della zona e collocamento dei suoi prodotti tipici",
        "Rispondenza delle produzioni in quantità e qualità alle esigenze dei canali distributivi",
        "Attivazione di iniziative di marketing mirate",
    ],
    "Rischiosità del credito": [
        "Livello di rischiosità del credito", "Grado di rischiosità degli impieghi bancari",
        "Crescita degli impieghi bancari", "Conoscenze dei settori delle banche",
        "Valutazioni creditizie e finanziarie",
    ],
    "Enti locali": [
        "Coordinamento tra gli enti locali", "Utilizzo del metodo di programmazione negoziata",
        "Partecipazione comune tra enti locali, imprenditori e sindacati su progetti di sviluppo condivisi",
    ],
}


def leggi_risorse_automatiche(comune_id):
    risorse = []

    dimensione = get_dimensione_economica(comune_id)
    if "errore" not in dimensione:
        risorse.append({
            "id": None,
            "categoria": "Turismo",
            "sotto_voce": "Composizione del sistema ricettivo",
            "nome": "Siti culturali gestiti (valore generato)",
            "valore_economico_euro": dimensione["valore_totale_generato"],
            "punteggio_valore_sociale": None,
            "punteggio_valore_naturale": None,
            "tipo_valore": "dati_gestur",
            "fonte": "Calcolato da presenze reali e tariffe dei siti (ultimi 12 mesi)",
            "note": dimensione["dato_sottostante"],
        })

    revenue_eventi = revenue_forecasting_eventi(comune_id=comune_id)
    if "errore" not in revenue_eventi and revenue_eventi.get("valore_confermato", 0) > 0:
        risorse.append({
            "id": None,
            "categoria": "Avvenimenti particolari",
            "sotto_voce": None,
            "nome": "Eventi locali confermati (valore generato)",
            "valore_economico_euro": revenue_eventi["valore_confermato"],
            "punteggio_valore_sociale": None,
            "punteggio_valore_naturale": None,
            "tipo_valore": "dati_gestur",
            "fonte": f"Margine netto di {revenue_eventi['n_richieste_confermate']} eventi confermati",
            "note": f"{revenue_eventi['n_richieste_confermate']} eventi confermati, margine netto totale di euro {revenue_eventi['valore_confermato']:.0f}",
        })

    return risorse


@app.get("/risorse-territoriali/{comune_id}")
def get_risorse_territoriali(comune_id: str):
    try:
        piano = ottieni_o_crea_piano_attivo(comune_id)

        risorse_manuali_resp = supabase.table("risorse_territoriali").select("*") \
            .eq("piano_id", piano["id"]).order("inserito_il", desc=True).execute()
        risorse_manuali = risorse_manuali_resp.data or []
        for r in risorse_manuali:
            r["tipo_valore"] = "stima_manuale"

        risorse_auto = leggi_risorse_automatiche(comune_id)

        tutte_le_risorse = risorse_auto + risorse_manuali

        valori_economici = [r["valore_economico_euro"] for r in tutte_le_risorse if r.get("valore_economico_euro") is not None]
        valore_economico_totale = round(sum(valori_economici), 2) if valori_economici else None

        punteggi_sociali = [r["punteggio_valore_sociale"] for r in tutte_le_risorse if r.get("punteggio_valore_sociale") is not None]
        punteggio_sociale_medio = round(sum(punteggi_sociali) / len(punteggi_sociali), 1) if punteggi_sociali else None

        punteggi_naturali = [r["punteggio_valore_naturale"] for r in tutte_le_risorse if r.get("punteggio_valore_naturale") is not None]
        punteggio_naturale_medio = round(sum(punteggi_naturali) / len(punteggi_naturali), 1) if punteggi_naturali else None

        distribuzione_categoria = {}
        for r in tutte_le_risorse:
            cat = r["categoria"]
            if cat not in distribuzione_categoria:
                distribuzione_categoria[cat] = {
                    "n_risorse": 0,
                    "valore_economico": 0,
                    "punteggi_sociali": [],
                    "punteggi_naturali": [],
                }
            distribuzione_categoria[cat]["n_risorse"] += 1
            if r.get("valore_economico_euro") is not None:
                distribuzione_categoria[cat]["valore_economico"] += r["valore_economico_euro"]
            if r.get("punteggio_valore_sociale") is not None:
                distribuzione_categoria[cat]["punteggi_sociali"].append(r["punteggio_valore_sociale"])
            if r.get("punteggio_valore_naturale") is not None:
                distribuzione_categoria[cat]["punteggi_naturali"].append(r["punteggio_valore_naturale"])

        distribuzione_economica = [
            {"categoria": cat, "valore_economico": round(d["valore_economico"], 2)}
            for cat, d in distribuzione_categoria.items() if d["valore_economico"] > 0
        ]
        distribuzione_economica.sort(key=lambda x: x["valore_economico"], reverse=True)

        distribuzione_sociale = [
            {"categoria": cat, "punteggio_medio": round(sum(d["punteggi_sociali"]) / len(d["punteggi_sociali"]), 1), "n_valutazioni": len(d["punteggi_sociali"])}
            for cat, d in distribuzione_categoria.items() if d["punteggi_sociali"]
        ]
        distribuzione_sociale.sort(key=lambda x: x["punteggio_medio"], reverse=True)

        distribuzione_naturale = [
            {"categoria": cat, "punteggio_medio": round(sum(d["punteggi_naturali"]) / len(d["punteggi_naturali"]), 1), "n_valutazioni": len(d["punteggi_naturali"])}
            for cat, d in distribuzione_categoria.items() if d["punteggi_naturali"]
        ]
        distribuzione_naturale.sort(key=lambda x: x["punteggio_medio"], reverse=True)

        return {
            "piano_id": piano["id"],
            "comune_id": comune_id,
            "risorse": tutte_le_risorse,
            "n_risorse_totali": len(tutte_le_risorse),
            "n_risorse_automatiche": len(risorse_auto),
            "n_risorse_manuali": len(risorse_manuali),
            "valore_economico_totale": valore_economico_totale,
            "punteggio_sociale_medio": punteggio_sociale_medio,
            "punteggio_naturale_medio": punteggio_naturale_medio,
            "distribuzione_economica_categoria": distribuzione_economica,
            "distribuzione_sociale_categoria": distribuzione_sociale,
            "distribuzione_naturale_categoria": distribuzione_naturale,
            "categorie_disponibili": CATEGORIE_RISORSE_TERRITORIALI,
            "nota_metodologica": (
                "Le risorse etichettate \"dati GesTur\" derivano da calcoli su presenze e ricavi reali gia "
                "presenti nel sistema (dimensione economica e modulo Eventi). Le risorse etichettate \"stima "
                "comune\" sono inserite manualmente dall'amministrazione, con riferimento alla griglia standard "
                "di analisi del territorio: il valore economico e facoltativo, mentre il punteggio di valore "
                "sociale e quello di valore naturale sono valutazioni qualitative da 1 a 5 (rispettivamente sul "
                "potenziale identitario/attrattivo e sul pregio ambientale/naturalistico della risorsa), non dati "
                "misurati. Le tre dimensioni non vengono mai sommate tra loro perche esprimono unita di misura "
                "diverse ed eterogenee."
            )
        }
    except Exception as e:
        print(f"Errore risorse territoriali comune {comune_id}: {e}")
        return {"errore": str(e)}


@app.post("/risorse-territoriali")
def crea_risorsa_territoriale(payload: dict):
    try:
        comune_id_str = payload.get("comune_id")
        categoria = payload.get("categoria")
        sotto_voce = payload.get("sotto_voce")
        nome = payload.get("nome")
        valore_economico_euro = payload.get("valore_economico_euro")
        punteggio_valore_sociale = payload.get("punteggio_valore_sociale")
        punteggio_valore_naturale = payload.get("punteggio_valore_naturale")
        fonte = payload.get("fonte")
        note = payload.get("note")

        if not comune_id_str or not categoria or not nome:
            return {"errore": "comune_id, categoria e nome sono obbligatori"}

        if categoria not in CATEGORIE_RISORSE_TERRITORIALI:
            return {"errore": "Categoria non valida"}

        if sotto_voce and sotto_voce not in CATEGORIE_RISORSE_TERRITORIALI[categoria]:
            return {"errore": "Sotto-voce non valida per questa categoria"}

        if punteggio_valore_sociale is not None and punteggio_valore_sociale not in (1, 2, 3, 4, 5):
            return {"errore": "Il punteggio di valore sociale deve essere un numero intero tra 1 e 5"}

        if punteggio_valore_naturale is not None and punteggio_valore_naturale not in (1, 2, 3, 4, 5):
            return {"errore": "Il punteggio di valore naturale deve essere un numero intero tra 1 e 5"}

        piano = ottieni_o_crea_piano_attivo(comune_id_str)

        record = {
            "piano_id": piano["id"],
            "comune_id": comune_id_str,
            "categoria": categoria,
            "sotto_voce": sotto_voce,
            "nome": nome,
            "valore_economico_euro": valore_economico_euro,
            "punteggio_valore_sociale": punteggio_valore_sociale,
            "punteggio_valore_naturale": punteggio_valore_naturale,
            "fonte": fonte,
            "note": note,
        }
        creato_resp = supabase.table("risorse_territoriali").insert(record).execute()

        return {"status": "salvato", "risorsa": creato_resp.data[0] if creato_resp.data else None}
    except Exception as e:
        print(f"Errore creazione risorsa territoriale: {e}")
        return {"errore": str(e)}


@app.delete("/risorse-territoriali/{risorsa_id}")
def elimina_risorsa_territoriale(risorsa_id: int):
    try:
        supabase.table("risorse_territoriali").delete().eq("id", risorsa_id).execute()
        return {"status": "eliminato"}
    except Exception as e:
        print(f"Errore eliminazione risorsa territoriale {risorsa_id}: {e}")
        return {"errore": str(e)}

def ottieni_o_crea_impostazione_marketing(comune_id_str, piano_id):
    esistente_resp = supabase.table("piano_marketing_impostazione").select("*") \
        .eq("piano_id", piano_id).limit(1).execute()
    if esistente_resp.data:
        return esistente_resp.data[0]
    nuovo = {
        "piano_id": piano_id,
        "comune_id": comune_id_str,
        "tipo_piano": None,
        "data_inizio": None,
        "data_fine": None,
        "punto_partenza": None,
        "obiettivi_mercato": None,
        "strategie_tattiche": None,
        "monitoraggio": None,
    }
    creato_resp = supabase.table("piano_marketing_impostazione").insert(nuovo).execute()
    return creato_resp.data[0]


@app.get("/piano-marketing/{comune_id}")
def get_piano_marketing(comune_id: str):
    try:
        piano = ottieni_o_crea_piano_attivo(comune_id)
        impostazione = ottieni_o_crea_impostazione_marketing(comune_id, piano["id"])
        return {
            "piano_id": piano["id"],
            "comune_id": comune_id,
            "impostazione": impostazione,
        }
    except Exception as e:
        print(f"Errore piano marketing comune {comune_id}: {e}")
        return {"errore": str(e)}


@app.put("/piano-marketing")
def aggiorna_piano_marketing(payload: dict):
    try:
        comune_id_str = payload.get("comune_id")
        if not comune_id_str:
            return {"errore": "comune_id è obbligatorio"}

        tipo_piano = payload.get("tipo_piano")
        if tipo_piano is not None and tipo_piano not in ("strategico", "tattico"):
            return {"errore": "tipo_piano deve essere 'strategico' o 'tattico'"}

        piano = ottieni_o_crea_piano_attivo(comune_id_str)

        campi_consentiti = {"tipo_piano", "data_inizio", "data_fine", "punto_partenza", "obiettivi_mercato", "strategie_tattiche", "monitoraggio"}
        aggiornamento = {k: v for k, v in payload.items() if k in campi_consentiti}
        aggiornamento["piano_id"] = piano["id"]
        aggiornamento["comune_id"] = comune_id_str
        aggiornamento["aggiornato_il"] = datetime.now().isoformat()

        supabase.table("piano_marketing_impostazione").upsert(aggiornamento, on_conflict="piano_id").execute()

        return {"status": "salvato", "piano_id": piano["id"]}
    except Exception as e:
        print(f"Errore aggiornamento piano marketing: {e}")
        return {"errore": str(e)}


@app.get("/marketing-mix-prodotto/{comune_id}")
def get_marketing_mix_prodotto(comune_id: str):
    try:
        risorse = get_risorse_territoriali(comune_id)
        identikit = get_identikit_destinazione(comune_id)
        ciclo_vita = get_ciclo_vita_destinazione(comune_id)

        return {
            "comune_id": comune_id,
            "risorse_riepilogo": {
                "n_risorse_totali": risorse.get("n_risorse_totali"),
                "valore_economico_totale": risorse.get("valore_economico_totale"),
                "punteggio_sociale_medio": risorse.get("punteggio_sociale_medio"),
                "punteggio_naturale_medio": risorse.get("punteggio_naturale_medio"),
            } if "errore" not in risorse else None,
            "identikit_riepilogo": {
                "vocazione_attuale": identikit.get("identikit", {}).get("vocazione_attuale") if identikit.get("identikit") else None,
                "vocazione_desiderata": identikit.get("identikit", {}).get("vocazione_desiderata") if identikit.get("identikit") else None,
            } if "errore" not in identikit else None,
            "ciclo_vita_riepilogo": {
                "fase_attuale": ciclo_vita.get("fase_attuale", {}).get("nome") if ciclo_vita.get("dati_sufficienti") else None,
                "dati_sufficienti": ciclo_vita.get("dati_sufficienti", False),
            } if "errore" not in ciclo_vita else None,
            "nota_metodologica": (
                "Questo riepilogo non ricalcola nulla: richiama i dati già presenti in \"Patrimonio e Risorse "
                "del Territorio\" (Sezione 4) e in \"Ciclo di Vita\" e \"Identikit della Destinazione\" (Sezione 2). "
                "Per il dettaglio completo, consulta quelle sezioni."
            )
        }
    except Exception as e:
        print(f"Errore marketing mix prodotto comune {comune_id}: {e}")
        return {"errore": str(e)}


@app.get("/marketing-mix-prezzo/{comune_id}")
def get_marketing_mix_prezzo(comune_id: str):
    try:
        siti = ottieni_siti_comune(comune_id)
        if not siti:
            return {"errore": "Nessun sito culturale trovato per questo comune"}

        prezzi_siti = []
        for s in siti:
            tariffe_resp = supabase.table("siti_culturali").select(
                "nome_sito, prezzo_biglietto, prezzo_ridotto, percentuale_ridotti"
            ).eq("id", s["id"]).single().execute()
            t = tariffe_resp.data
            if t and t.get("prezzo_biglietto") is not None:
                prezzi_siti.append({
                    "nome_sito": t["nome_sito"],
                    "prezzo_intero": t["prezzo_biglietto"],
                    "prezzo_ridotto": t.get("prezzo_ridotto"),
                    "percentuale_ridotti": t.get("percentuale_ridotti"),
                })

        piano = ottieni_o_crea_piano_attivo(comune_id)
        note_resp = supabase.table("note_marketing_mix").select("*") \
            .eq("piano_id", piano["id"]).eq("sezione", "prezzo").order("inserito_il", desc=True).execute()
        note = note_resp.data or []

        prezzo_medio_interi = round(sum(p["prezzo_intero"] for p in prezzi_siti) / len(prezzi_siti), 2) if prezzi_siti else None

        return {
            "piano_id": piano["id"],
            "comune_id": comune_id,
            "prezzi_siti": prezzi_siti,
            "prezzo_medio_intero": prezzo_medio_interi,
            "note": note,
            "nota_metodologica": (
                "Il territorio in se non ha un prezzo di vendita diretto: qui sono riepilogati i prezzi reali "
                "gia impostati per i singoli siti culturali (biglietteria), come riferimento oggettivo. Le note "
                "qualitative sottostanti servono per registrare considerazioni di posizionamento (es. prezzo "
                "percepito, confronto con territori concorrenti), che restano una valutazione del comune."
            )
        }
    except Exception as e:
        print(f"Errore marketing mix prezzo comune {comune_id}: {e}")
        return {"errore": str(e)}


@app.get("/note-marketing/{comune_id}")
def get_note_marketing(comune_id: str, sezione: str):
    try:
        piano = ottieni_o_crea_piano_attivo(comune_id)
        note_resp = supabase.table("note_marketing_mix").select("*") \
            .eq("piano_id", piano["id"]).eq("sezione", sezione).order("inserito_il", desc=True).execute()
        return {"piano_id": piano["id"], "comune_id": comune_id, "sezione": sezione, "note": note_resp.data or []}
    except Exception as e:
        print(f"Errore note marketing comune {comune_id}: {e}")
        return {"errore": str(e)}


@app.post("/note-marketing")
def crea_nota_marketing(payload: dict):
    try:
        comune_id_str = payload.get("comune_id")
        sezione = payload.get("sezione")
        testo = payload.get("testo")

        if not comune_id_str or not sezione or not testo or not testo.strip():
            return {"errore": "comune_id, sezione e testo sono obbligatori"}

        if sezione not in ("prezzo", "distribuzione_trend", "distribuzione_scenari"):
            return {"errore": "Sezione non valida"}

        piano = ottieni_o_crea_piano_attivo(comune_id_str)
        record = {"piano_id": piano["id"], "comune_id": comune_id_str, "sezione": sezione, "testo": testo.strip()}
        creato_resp = supabase.table("note_marketing_mix").insert(record).execute()

        return {"status": "salvato", "nota": creato_resp.data[0] if creato_resp.data else None}
    except Exception as e:
        print(f"Errore creazione nota marketing: {e}")
        return {"errore": str(e)}


@app.delete("/note-marketing/{nota_id}")
def elimina_nota_marketing(nota_id: int):
    try:
        supabase.table("note_marketing_mix").delete().eq("id", nota_id).execute()
        return {"status": "eliminato"}
    except Exception as e:
        print(f"Errore eliminazione nota marketing {nota_id}: {e}")
        return {"errore": str(e)}

@app.get("/targetizzazione-marketing/{comune_id}")
def get_targetizzazione_marketing(comune_id: str):
    try:
        siti = ottieni_siti_comune(comune_id)
        if not siti:
            return {"errore": "Nessun sito culturale trovato per questo comune"}
        sito_ids = [s["id"] for s in siti]

        dodici_mesi_fa = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
        presenze_resp = supabase.table("presenza").select("gruppo, provenienza, fasce") \
            .in_("sito_id", sito_ids).gte("data", dodici_mesi_fa).execute()
        presenze = presenze_resp.data or []

        if not presenze:
            return {"errore": "Nessun dato di presenze disponibile negli ultimi 12 mesi per questo comune"}

        totale = 0
        conteggio_prov = {}
        conteggio_fascia = {}
        for r in presenze:
            gruppo = r.get("gruppo", 0) or 0
            totale += gruppo
            prov_macro = mappa_provenienza_macro(r.get("provenienza"))
            conteggio_prov[prov_macro] = conteggio_prov.get(prov_macro, 0) + gruppo

            fasce_riga = [normalizza_fascia(f) for f in (r.get("fasce") or "").split(", ") if f]
            if fasce_riga:
                quota = gruppo / len(fasce_riga)
                for f in fasce_riga:
                    conteggio_fascia[f] = conteggio_fascia.get(f, 0) + quota

        if totale == 0:
            return {"errore": "Presenze insufficienti per calcolare la targetizzazione"}

        top_provenienze = sorted(
            [{"valore": k, "quota_pct": round(v / totale * 100, 1), "n_persone": round(v, 1)} for k, v in conteggio_prov.items()],
            key=lambda x: x["quota_pct"], reverse=True
        )[:5]
        top_fasce = sorted(
            [{"valore": k, "quota_pct": round(v / totale * 100, 1), "n_persone": round(v, 1)} for k, v in conteggio_fascia.items()],
            key=lambda x: x["quota_pct"], reverse=True
        )[:5]

        return {
            "comune_id": comune_id,
            "periodo_giorni": 365,
            "totale_presenze_periodo": totale,
            "top_provenienze": top_provenienze,
            "top_fasce": top_fasce,
            "dato_sottostante": (
                f"Calcolato su {totale} presenze reali degli ultimi 12 mesi su {len(siti)} sito/i. "
                f"Provenienza dominante: {top_provenienze[0]['valore']} ({top_provenienze[0]['quota_pct']}%). "
                f"Fascia dominante: {top_fasce[0]['valore']} anni ({top_fasce[0]['quota_pct']}%)."
            ) if top_provenienze and top_fasce else None,
        }
    except Exception as e:
        print(f"Errore targetizzazione marketing comune {comune_id}: {e}")
        return {"errore": str(e)}

SOGLIA_CALO_ALERT_MARKETING_PCT = -25.0

@app.get("/alert-previsionale-marketing/{comune_id}")
def get_alert_previsionale_marketing(comune_id: str):
    try:
        siti = ottieni_siti_comune(comune_id)
        if not siti:
            return {"errore": "Nessun sito culturale trovato per questo comune"}
        sito_ids = [s["id"] for s in siti]

        novanta_giorni_fa = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
        storico_resp = supabase.table("presenza").select("data, gruppo") \
            .in_("sito_id", sito_ids).gte("data", novanta_giorni_fa).execute()
        storico = storico_resp.data or []
        if not storico:
            return {"errore": "Nessun dato storico sufficiente per calcolare una media di riferimento"}

        df_storico = pd.DataFrame(storico)
        df_storico["data"] = pd.to_datetime(df_storico["data"])
        media_settimanale_storica = df_storico.groupby(pd.Grouper(key="data", freq="W"))["gruppo"].sum().mean()

        if not media_settimanale_storica or media_settimanale_storica <= 0:
            return {"errore": "Media storica non calcolabile per questo comune"}

        oggi = datetime.now()
        fine = oggi + timedelta(days=182)
        prev_resp = supabase.table("previsioni_affluenza").select("sito_id, data_previsione, affluenza_stimata") \
            .in_("sito_id", sito_ids).gte("data_previsione", oggi.strftime("%Y-%m-%d")) \
            .lte("data_previsione", fine.strftime("%Y-%m-%d")).order("data_previsione").execute()
        previsioni = prev_resp.data or []

        if not previsioni:
            return {
                "comune_id": comune_id,
                "alert_disponibili": False,
                "messaggio": (
                    "Nessuna previsione di affluenza a 6 mesi disponibile per questo comune. Le previsioni "
                    "vengono generate automaticamente ogni settimana per ciascun sito: torna a controllare "
                    "tra qualche giorno."
                )
            }

        df_prev = pd.DataFrame(previsioni)
        df_prev["data_previsione"] = pd.to_datetime(df_prev["data_previsione"])
        df_prev["settimana"] = df_prev["data_previsione"].dt.to_period("W").dt.start_time
        aggregato_settimanale = df_prev.groupby("settimana")["affluenza_stimata"].sum()

        nomi_mesi = ["", "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
                     "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"]

        alert = []
        for settimana, valore in aggregato_settimanale.items():
            scostamento_pct = round(((valore - media_settimanale_storica) / media_settimanale_storica) * 100, 1)
            if scostamento_pct <= SOGLIA_CALO_ALERT_MARKETING_PCT:
                alert.append({
                    "settimana_inizio": settimana.strftime("%Y-%m-%d"),
                    "mese": nomi_mesi[settimana.month],
                    "affluenza_prevista": round(valore, 1),
                    "media_riferimento": round(media_settimanale_storica, 1),
                    "scostamento_pct": scostamento_pct,
                    "suggerimento": (
                        f"Calo previsto del {abs(scostamento_pct)}% rispetto alla media delle ultime 12 settimane "
                        f"per la settimana del {settimana.strftime('%d/%m')} ({nomi_mesi[settimana.month]}). "
                        f"Valuta di anticipare una campagna di promozione su questo periodo, eventualmente "
                        f"allocando budget dai canali attivi in Comunicazione."
                    )
                })

        return {
            "comune_id": comune_id,
            "alert_disponibili": True,
            "media_settimanale_storica": round(media_settimanale_storica, 1),
            "n_alert": len(alert),
            "alert": alert,
            "nota_metodologica": (
                f"Un calo viene segnalato quando l'affluenza settimanale prevista scende di almeno "
                f"{abs(SOGLIA_CALO_ALERT_MARKETING_PCT)}% sotto la media settimanale calcolata sugli ultimi 90 "
                f"giorni di presenze reali. Le previsioni provengono dal modello SARIMAX gia usato nel modulo "
                f"Previsioni, aggiornato automaticamente ogni settimana: non sono una garanzia, ma un supporto "
                f"decisionale per anticipare le azioni di comunicazione."
            )
        }
    except Exception as e:
        print(f"Errore alert previsionale marketing comune {comune_id}: {e}")
        return {"errore": str(e)}

CANALI_COMUNICAZIONE_STANDARD = [
    "Social media (Facebook/Instagram)", "Sito web istituzionale", "Newsletter ed email marketing",
    "Radio locale", "Stampa locale", "Cartellonistica e affissioni", "Fiere ed eventi di settore",
    "Influencer e content creator", "App turistica", "Passaparola e referral",
    "Partnership con tour operator", "Ufficio stampa e public relations",
]

CANALI_DISTRIBUZIONE_STANDARD = [
    "Vendita diretta in loco", "Booking online diretto", "OTA (Booking.com, Expedia, ecc.)",
    "Tour operator e agenzie di viaggio", "DMO regionale o provinciale",
    "Partnership con territori limitrofi", "Pacchetti bundle con eventi locali",
]


@app.get("/canali-marketing/{comune_id}")
def get_canali_marketing(comune_id: str, tipo: str):
    try:
        if tipo not in ("comunicazione", "distribuzione"):
            return {"errore": "tipo deve essere 'comunicazione' o 'distribuzione'"}

        piano = ottieni_o_crea_piano_attivo(comune_id)
        canali_resp = supabase.table("canali_marketing").select("*") \
            .eq("piano_id", piano["id"]).eq("tipo", tipo).execute()
        canali_salvati = {c["nome"]: c for c in (canali_resp.data or [])}

        elenco_standard = CANALI_COMUNICAZIONE_STANDARD if tipo == "comunicazione" else CANALI_DISTRIBUZIONE_STANDARD

        canali = []
        for nome in elenco_standard:
            salvato = canali_salvati.get(nome)
            canali.append({
                "id": salvato["id"] if salvato else None,
                "nome": nome,
                "attivo": salvato["attivo"] if salvato else False,
                "budget_stanziato": salvato.get("budget_stanziato") if salvato else None,
                "note": salvato.get("note") if salvato else None,
            })

        budget_totale = sum(c["budget_stanziato"] for c in canali if c.get("budget_stanziato") is not None)
        n_attivi = sum(1 for c in canali if c["attivo"])

        return {
            "piano_id": piano["id"],
            "comune_id": comune_id,
            "tipo": tipo,
            "canali": canali,
            "n_canali_attivi": n_attivi,
            "n_canali_totali": len(canali),
            "budget_totale": round(budget_totale, 2) if budget_totale else None,
        }
    except Exception as e:
        print(f"Errore canali marketing comune {comune_id}: {e}")
        return {"errore": str(e)}


@app.put("/canali-marketing")
def aggiorna_canale_marketing(payload: dict):
    try:
        comune_id_str = payload.get("comune_id")
        tipo = payload.get("tipo")
        nome = payload.get("nome")
        attivo = payload.get("attivo")
        budget_stanziato = payload.get("budget_stanziato")
        note = payload.get("note")

        if not comune_id_str or tipo not in ("comunicazione", "distribuzione") or not nome:
            return {"errore": "comune_id, tipo e nome sono obbligatori"}

        elenco_standard = CANALI_COMUNICAZIONE_STANDARD if tipo == "comunicazione" else CANALI_DISTRIBUZIONE_STANDARD
        if nome not in elenco_standard:
            return {"errore": "Canale non valido per questo tipo"}

        piano = ottieni_o_crea_piano_attivo(comune_id_str)

        record = {
            "piano_id": piano["id"],
            "comune_id": comune_id_str,
            "tipo": tipo,
            "nome": nome,
            "attivo": bool(attivo),
            "budget_stanziato": budget_stanziato,
            "note": note,
        }
        supabase.table("canali_marketing").upsert(record, on_conflict="piano_id,tipo,nome").execute()

        return {"status": "salvato"}
    except Exception as e:
        print(f"Errore aggiornamento canale marketing: {e}")
        return {"errore": str(e)}


@app.get("/campagne-marketing/{comune_id}")
def get_campagne_marketing(comune_id: str):
    try:
        piano = ottieni_o_crea_piano_attivo(comune_id)
        campagne_resp = supabase.table("campagne_marketing").select("*") \
            .eq("piano_id", piano["id"]).order("data_inizio", desc=True).execute()
        campagne = campagne_resp.data or []

        budget_totale = sum(c["budget_stanziato"] for c in campagne if c.get("budget_stanziato") is not None)

        return {
            "piano_id": piano["id"],
            "comune_id": comune_id,
            "campagne": campagne,
            "n_campagne": len(campagne),
            "budget_totale": round(budget_totale, 2) if budget_totale else None,
        }
    except Exception as e:
        print(f"Errore campagne marketing comune {comune_id}: {e}")
        return {"errore": str(e)}


@app.post("/campagne-marketing")
def crea_campagna_marketing(payload: dict):
    try:
        comune_id_str = payload.get("comune_id")
        nome_campagna = payload.get("nome_campagna")
        canale = payload.get("canale")
        budget_stanziato = payload.get("budget_stanziato")
        data_inizio = payload.get("data_inizio")
        data_fine = payload.get("data_fine")
        stato = payload.get("stato", "pianificata")
        note = payload.get("note")

        if not comune_id_str or not nome_campagna:
            return {"errore": "comune_id e nome_campagna sono obbligatori"}

        if stato not in ("pianificata", "attiva", "completata"):
            return {"errore": "Stato non valido"}

        piano = ottieni_o_crea_piano_attivo(comune_id_str)

        record = {
            "piano_id": piano["id"],
            "comune_id": comune_id_str,
            "nome_campagna": nome_campagna,
            "canale": canale,
            "budget_stanziato": budget_stanziato,
            "data_inizio": data_inizio,
            "data_fine": data_fine,
            "stato": stato,
            "note": note,
        }
        creato_resp = supabase.table("campagne_marketing").insert(record).execute()

        return {"status": "salvato", "campagna": creato_resp.data[0] if creato_resp.data else None}
    except Exception as e:
        print(f"Errore creazione campagna marketing: {e}")
        return {"errore": str(e)}


@app.delete("/campagne-marketing/{campagna_id}")
def elimina_campagna_marketing(campagna_id: int):
    try:
        supabase.table("campagne_marketing").delete().eq("id", campagna_id).execute()
        return {"status": "eliminato"}
    except Exception as e:
        print(f"Errore eliminazione campagna marketing {campagna_id}: {e}")
        return {"errore": str(e)}

@app.get("/marketing-mix-distribuzione/{comune_id}")
def get_marketing_mix_distribuzione(comune_id: str):
    try:
        canali = get_canali_marketing(comune_id, tipo="distribuzione")

        piano = ottieni_o_crea_piano_attivo(comune_id)
        note_trend_resp = supabase.table("note_marketing_mix").select("*") \
            .eq("piano_id", piano["id"]).eq("sezione", "distribuzione_trend").order("inserito_il", desc=True).execute()
        note_scenari_resp = supabase.table("note_marketing_mix").select("*") \
            .eq("piano_id", piano["id"]).eq("sezione", "distribuzione_scenari").order("inserito_il", desc=True).execute()

        identikit = get_identikit_destinazione(comune_id)
        portafoglio = None
        if "errore" not in identikit and identikit.get("identikit"):
            portafoglio = {
                "vocazione_attuale": identikit["identikit"].get("vocazione_attuale"),
                "vocazione_desiderata": identikit["identikit"].get("vocazione_desiderata"),
            }

        return {
            "piano_id": piano["id"],
            "comune_id": comune_id,
            "canali_distributivi": canali if "errore" not in canali else None,
            "note_trend_consumo": note_trend_resp.data or [],
            "note_scenari_futuri": note_scenari_resp.data or [],
            "portafoglio_prodotti": portafoglio,
            "nota_metodologica": (
                "Il portafoglio prodotti richiama l'identikit della destinazione gia definito in Sezione 2: "
                "le vocazioni attuali e desiderate rappresentano le linee di prodotto del territorio su cui "
                "valutare possibili sviluppi (nuove vocazioni da aggiungere, o rafforzamento di quelle esistenti)."
            )
        }
    except Exception as e:
        print(f"Errore marketing mix distribuzione comune {comune_id}: {e}")
        return {"errore": str(e)}

@app.get("/profilo-visitatore/{comune_id}")
def get_profilo_visitatore(comune_id: str):
    try:
        targetizzazione = get_targetizzazione_marketing(comune_id)
        indicatori = get_indicatori_standard(comune_id)

        richieste_resp = supabase.table("richieste_pit").select("categoria, categorie_disservizio, materiale_mancante").eq("comune_id", comune_id).execute()
        richieste = richieste_resp.data or []

        conteggio_categoria = {}
        conteggio_disservizio = {}
        conteggio_materiale_mancante = {}
        for r in richieste:
            cat = r.get("categoria")
            if cat and cat.lower() != "altro":
                conteggio_categoria[cat] = conteggio_categoria.get(cat, 0) + 1
            dis = r.get("categorie_disservizio")
            if dis and dis.lower() != "altro":
                conteggio_disservizio[dis] = conteggio_disservizio.get(dis, 0) + 1
            mat = (r.get("materiale_mancante") or "").strip()
            if mat:
                conteggio_materiale_mancante[mat] = conteggio_materiale_mancante.get(mat, 0) + 1

        totale_richieste = len(richieste)
        top_richieste = sorted(
            [{"voce": k, "conteggio": v, "quota_pct": round(v / totale_richieste * 100, 1)} for k, v in conteggio_categoria.items()],
            key=lambda x: x["conteggio"], reverse=True
        )[:5]
        top_disservizi = sorted(
            [{"voce": k, "conteggio": v} for k, v in conteggio_disservizio.items()],
            key=lambda x: x["conteggio"], reverse=True
        )[:5]
        top_materiale_mancante = sorted(
            [{"voce": k, "conteggio": v} for k, v in conteggio_materiale_mancante.items()],
            key=lambda x: x["conteggio"], reverse=True
        )[:5]

        return {
            "comune_id": comune_id,
            "chi_da_dove": {
                "top_provenienze": targetizzazione.get("top_provenienze") if "errore" not in targetizzazione else None,
                "top_fasce": targetizzazione.get("top_fasce") if "errore" not in targetizzazione else None,
                "errore": targetizzazione.get("errore") if "errore" in targetizzazione else None,
            },
            "quando": {
                "livello_stagionalita": indicatori.get("stagionalita", {}).get("livello") if "errore" not in indicatori else None,
                "mese_massimo": indicatori.get("stagionalita", {}).get("mese_massimo") if "errore" not in indicatori else None,
                "mese_minimo": indicatori.get("stagionalita", {}).get("mese_minimo") if "errore" not in indicatori else None,
                "dati_sufficienti": indicatori.get("stagionalita", {}).get("dati_sufficienti", False) if "errore" not in indicatori else False,
            },
            "cosa_cercano": {
                "totale_richieste_pit": totale_richieste,
                "top_categorie_richieste": top_richieste,
            } if totale_richieste > 0 else None,
            "cosa_manca": {
                "top_disservizi_segnalati": top_disservizi,
                "top_materiale_mancante": top_materiale_mancante,
            } if (top_disservizi or top_materiale_mancante) else None,
            "nota_metodologica": (
                "Questo profilo non raccoglie nuovi dati: aggrega presenze reali (chi/da dove/quando, stessa "
                "fonte di Targetizzazione e Indicatori Standard) e richieste al Punto Informativo Turistico "
                "(cosa cercano i visitatori e quali servizi/materiali mancano), gia registrate nei rispettivi moduli."
            )
        }
    except Exception as e:
        print(f"Errore profilo visitatore comune {comune_id}: {e}")
        return {"errore": str(e)}

AZIONI_TIPO = [
    {"codice": "T-01", "area": "territorio", "titolo": "Segnaletica turistica tematica con QR storytelling", "descrizione": "Cartelli con leggende/curiosita oltre agli indicatori di direzione, con QR per approfondimenti digitali."},
    {"codice": "T-02", "area": "territorio", "titolo": "Censimento e valorizzazione risorse minori", "descrizione": "Recupero di risorse secondarie o di supporto non ancora valorizzate nella griglia Patrimonio e Risorse."},
    {"codice": "T-03", "area": "territorio", "titolo": "Miglioramento accessibilita architettonica dei siti", "descrizione": "Rimozione barriere fisiche nei punti a maggiore criticita segnalata."},
    {"codice": "T-04", "area": "territorio", "titolo": "Mobilita sostenibile e parcheggi scambiatori", "descrizione": "Riduzione del traffico privato nei pressi dei siti a alta concentrazione weekend."},
    {"codice": "T-05", "area": "territorio", "titolo": "Riqualificazione aree naturali a fruizione turistica", "descrizione": "Sentieristica, aree pic-nic, punti panoramici attrezzati."},
    {"codice": "T-06", "area": "territorio", "titolo": "Programma ambassador residenti", "descrizione": "Coinvolgimento della popolazione locale nell'accoglienza dei visitatori."},
    {"codice": "T-07", "area": "territorio", "titolo": "Rete Wi-Fi pubblica nei punti di interesse", "descrizione": "Infrastruttura digitale di base per la fruizione di app e contenuti QR."},
    {"codice": "T-08", "area": "territorio", "titolo": "Piano di decongestionamento weekend", "descrizione": "Redistribuzione dei flussi su piu giorni tramite orari estesi o eventi infrasettimanali."},
    {"codice": "T-09", "area": "territorio", "titolo": "Recupero patrimonio industriale o archeologico minore", "descrizione": "Valorizzazione di risorse storiche non ancora aperte al pubblico."},
    {"codice": "T-10", "area": "territorio", "titolo": "Itinerari tematici intercomunali", "descrizione": "Percorsi che collegano risorse di comuni limitrofi attorno a un tema comune."},
    {"codice": "T-11", "area": "territorio", "titolo": "Formazione operatori locali", "descrizione": "Lingue straniere, storia locale, tecniche di accoglienza."},
    {"codice": "T-12", "area": "territorio", "titolo": "Efficientamento infrastrutture di servizio", "descrizione": "Smaltimento rifiuti, distribuzione idrica, in ottica di carico turistico."},

    {"codice": "TU-01", "area": "turismo", "classificazione": "ATL", "titolo": "Affissioni dinamiche", "descrizione": "Bus wrap, pensiline, cartellonistica nei nodi di traffico."},
    {"codice": "TU-02", "area": "turismo", "classificazione": "ATL", "titolo": "Spot radio/TV in co-marketing regionale", "descrizione": "Costi condivisi con enti sovracomunali o regionali."},
    {"codice": "TU-03", "area": "turismo", "classificazione": "ATL", "titolo": "Native advertising su portali viaggio generalisti", "descrizione": "Contenuti sponsorizzati integrati editorialmente."},
    {"codice": "TU-04", "area": "turismo", "classificazione": "ATL", "titolo": "Pubblicazioni illustrate di qualita", "descrizione": "Libri fotografici e guide cartacee curate."},
    {"codice": "TU-05", "area": "turismo", "classificazione": "BTL", "titolo": "Educational tour per giornalisti e blogger", "descrizione": "Viaggi stampa dedicati a copertura editoriale."},
    {"codice": "TU-06", "area": "turismo", "classificazione": "BTL", "titolo": "Workshop B2B con tour operator e buyer", "descrizione": "Fiere e buy rivolte al trade di settore."},
    {"codice": "TU-07", "area": "turismo", "classificazione": "BTL", "titolo": "Fame trip per opinion leader", "descrizione": "Viaggi di celebrita o soggetti noti per generare copertura stampa."},
    {"codice": "TU-08", "area": "turismo", "classificazione": "BTL", "titolo": "Co-marketing con imprese locali", "descrizione": "Cantine, ristoranti, artigiani in pacchetti cross-promozionali."},
    {"codice": "TU-09", "area": "turismo", "classificazione": "BTL", "titolo": "Sponsorizzazioni culturali o sportive", "descrizione": "Sostegno a iniziative locali per attrarre attenzione mediatica."},
    {"codice": "TU-10", "area": "turismo", "classificazione": "BTL", "titolo": "Direct mail e newsletter CRM ex-visitatori", "descrizione": "Fidelizzazione di chi ha gia visitato la destinazione."},
    {"codice": "TU-11", "area": "turismo", "classificazione": "TTL", "titolo": "Influencer e content creator locali", "descrizione": "Ospitalita in cambio di contenuti social."},
    {"codice": "TU-12", "area": "turismo", "classificazione": "TTL", "titolo": "Campagne social geolocalizzate", "descrizione": "Retargeting su chi ha gia cercato la destinazione online."},
    {"codice": "TU-13", "area": "turismo", "classificazione": "TTL", "titolo": "Product placement", "descrizione": "Negoziazione per apparire in film, serie tv o spot pubblicitari."},

    {"codice": "EV-01", "area": "eventi", "titolo": "Calendario eventi integrato multi-sito", "descrizione": "Vista unica degli eventi su tutti i siti del comune."},
    {"codice": "EV-02", "area": "eventi", "titolo": "Pacchetti bundle evento piu sito culturale", "descrizione": "Biglietto combinato a prezzo agevolato."},
    {"codice": "EV-03", "area": "eventi", "titolo": "Rassegna stagionale ricorrente", "descrizione": "Format fisso che diventa identitario per la destinazione."},
    {"codice": "EV-04", "area": "eventi", "titolo": "City card con accesso eventi e trasporti", "descrizione": "Estensione della city card anche agli eventi locali."},
    {"codice": "EV-05", "area": "eventi", "titolo": "Format itinerante tra le frazioni", "descrizione": "Evento che si sposta tra le localita minori del comune."},
    {"codice": "EV-06", "area": "eventi", "titolo": "Rievocazioni storiche a tema identitario", "descrizione": "Collegate alla vocazione storico-culturale della destinazione."},
    {"codice": "EV-07", "area": "eventi", "titolo": "Festival enogastronomico di prodotti locali", "descrizione": "Valorizza filiera corta e artigianato alimentare."},
    {"codice": "EV-08", "area": "eventi", "titolo": "Programma family-friendly per eventi", "descrizione": "Attivita dedicate ai bambini durante gli eventi principali."},
    {"codice": "EV-09", "area": "eventi", "titolo": "Evento di destagionalizzazione", "descrizione": "In bassa stagione, con dynamic pricing agevolato."},
    {"codice": "EV-10", "area": "eventi", "titolo": "Partnership eventi con comuni limitrofi", "descrizione": "Circuito regionale per aumentare l'attrattivita complessiva."},

    {"codice": "SE-01", "area": "servizi", "titolo": "Potenziamento centro di accoglienza attivo", "descrizione": "Personale che si attiva proattivamente verso il visitatore, non solo su richiesta."},
    {"codice": "SE-02", "area": "servizi", "titolo": "App turistica con audioguide e realta aumentata", "descrizione": "Contenuti digitali fruibili in loco."},
    {"codice": "SE-03", "area": "servizi", "titolo": "City card unica", "descrizione": "Trasporti, sconti e accesso ai siti in un solo voucher."},
    {"codice": "SE-04", "area": "servizi", "titolo": "Voucher e carnet sconto multi-esercizio", "descrizione": "Buoni per musei, negozi, bar, ristoranti, trasporti."},
    {"codice": "SE-05", "area": "servizi", "titolo": "Servizio di visite guidate tematiche", "descrizione": "Aumenta la qualita percepita e i tempi di permanenza."},
    {"codice": "SE-06", "area": "servizi", "titolo": "Formazione linguistica operatori PIT e siti", "descrizione": "Copertura lingue in base alla provenienza dominante rilevata."},
    {"codice": "SE-07", "area": "servizi", "titolo": "Punto assistenza digitale self-service", "descrizione": "QR o app nei siti a bassa preferenza cartacea rilevata."},
    {"codice": "SE-08", "area": "servizi", "titolo": "Servizio di prenotazione online centralizzato", "descrizione": "Unico portale prenotazioni per tutti i siti ed eventi del comune."},
    {"codice": "SE-09", "area": "servizi", "titolo": "Percorso a 5 tappe del visitatore", "descrizione": "Touchpoint dedicati a pre-partenza, arrivo, permanenza, partenza, ricordo."},
    {"codice": "SE-10", "area": "servizi", "titolo": "Sistema di raccolta feedback post-visita", "descrizione": "Survey automatizzata via QR o app dopo la visita."},
    {"codice": "SE-11", "area": "servizi", "titolo": "Accessibilita linguistica per disabilita sensoriali", "descrizione": "Materiali e assistenza per turisti con disabilita visive o uditive."},
    {"codice": "SE-12", "area": "servizi", "titolo": "Sportello digitale per reclami e segnalazioni", "descrizione": "Canale rapido di segnalazione in tempo reale durante la visita."},
]

AZIONI_TIPO_MAP = {a["codice"]: a for a in AZIONI_TIPO}


@app.get("/azioni-tipo")
def get_azioni_tipo():
    per_area = {}
    for a in AZIONI_TIPO:
        per_area.setdefault(a["area"], []).append(a)
    return {
        "azioni": AZIONI_TIPO,
        "per_area": per_area,
        "n_totale": len(AZIONI_TIPO),
    }


def genera_raccomandazioni_azioni(comune_id):
    raccomandazioni = []

    sostenibilita = get_sostenibilita_carico(comune_id)
    if "errore" not in sostenibilita and sostenibilita["livello"] in ("alto", "critico"):
        for codice in ["T-04", "T-08"]:
            raccomandazioni.append({
                "codice_azione_tipo": codice,
                "azione": AZIONI_TIPO_MAP[codice],
                "segnale": "sostenibilita_carico",
                "dato_sottostante": sostenibilita["dato_sottostante"],
            })

    accessibilita = get_accessibilita_pilastro(comune_id)
    if "errore" not in accessibilita and accessibilita["quota_pct"] >= SOGLIA_PCT_DEBOLEZZA:
        raccomandazioni.append({
            "codice_azione_tipo": "T-03",
            "azione": AZIONI_TIPO_MAP["T-03"],
            "segnale": "accessibilita_pilastro",
            "dato_sottostante": accessibilita["dato_sottostante"],
        })

    welfare = get_welfare_innovazione(comune_id)
    if "errore" not in welfare:
        mappa_categoria_azione = {
            "Digitalizzazione servizi": "SE-02",
            "Sostenibilita ambientale": "T-04",
            "Accessibilita potenziata": "T-03",
            "Inclusione sociale": "SE-11",
            "Valorizzazione del patrimonio": "T-02",
        }
        for r in welfare.get("radar", []):
            if r["n_iniziative_attive"] == 0 and r["categoria"] in mappa_categoria_azione:
                codice = mappa_categoria_azione[r["categoria"]]
                raccomandazioni.append({
                    "codice_azione_tipo": codice,
                    "azione": AZIONI_TIPO_MAP[codice],
                    "segnale": "welfare_innovazione_categoria_scoperta",
                    "dato_sottostante": f"Nessuna iniziativa attiva registrata nella categoria \"{r['categoria']}\" ({welfare['n_categorie_coperte']} su {welfare['n_categorie_totali']} categorie coperte in totale).",
                })

    alert = get_alert_previsionale_marketing(comune_id)
    if "errore" not in alert and alert.get("alert_disponibili") and alert.get("n_alert", 0) > 0:
        primo_alert = alert["alert"][0]
        for codice in ["EV-09", "TU-10"]:
            raccomandazioni.append({
                "codice_azione_tipo": codice,
                "azione": AZIONI_TIPO_MAP[codice],
                "segnale": "alert_previsionale_sarimax",
                "dato_sottostante": primo_alert["suggerimento"],
            })

    obiettivi = get_obiettivi_piano(comune_id)
    if "errore" not in obiettivi:
        mappa_pilastro_azione = {
            "sostenibilita": "T-08",
            "accessibilita": "T-03",
            "welfare_innovazione": "T-02",
        }
        for o in obiettivi.get("obiettivi", []):
            if o.get("semaforo") == "rosso" and o.get("pilastro") in mappa_pilastro_azione:
                codice = mappa_pilastro_azione[o["pilastro"]]
                raccomandazioni.append({
                    "codice_azione_tipo": codice,
                    "azione": AZIONI_TIPO_MAP[codice],
                    "segnale": "obiettivo_mandato_rosso",
                    "dato_sottostante": o.get("dato_sottostante", ""),
                })

    return raccomandazioni


@app.get("/raccomandazioni-azioni/{comune_id}")
def get_raccomandazioni_azioni(comune_id: str):
    try:
        raccomandazioni = genera_raccomandazioni_azioni(comune_id)
        return {
            "comune_id": comune_id,
            "n_raccomandazioni": len(raccomandazioni),
            "raccomandazioni": raccomandazioni,
            "nota_metodologica": (
                "Le raccomandazioni derivano da 5 segnali gia calcolati altrove in GesTur: indice di "
                "concentrazione weekend critico, quota di segnalazioni di accessibilita sopra soglia, "
                "categorie del radar Welfare e Innovazione senza iniziative attive, cali di affluenza "
                "previsti dal modello SARIMAX a 6 mesi, e obiettivi di mandato in semaforo rosso. Ogni "
                "raccomandazione riporta il dato che l'ha generata: non sono suggerimenti generici."
            ),
        }
    except Exception as e:
        print(f"Errore raccomandazioni azioni comune {comune_id}: {e}")
        return {"errore": str(e)}


@app.get("/azioni-piano/{comune_id}")
def get_azioni_piano(comune_id: str):
    try:
        piano = ottieni_o_crea_piano_attivo(comune_id)
        azioni_resp = supabase.table("azioni_piano").select("*") \
            .eq("piano_id", piano["id"]).order("inserito_il", desc=True).execute()
        azioni = azioni_resp.data or []

        per_stato = {}
        for a in azioni:
            per_stato[a["stato"]] = per_stato.get(a["stato"], 0) + 1

        return {
            "piano_id": piano["id"],
            "comune_id": comune_id,
            "azioni": azioni,
            "n_totale": len(azioni),
            "per_stato": per_stato,
        }
    except Exception as e:
        print(f"Errore azioni piano comune {comune_id}: {e}")
        return {"errore": str(e)}


@app.post("/azioni-piano")
def crea_azione_piano(payload: dict):
    try:
        comune_id_str = payload.get("comune_id")
        titolo = payload.get("titolo")
        area = payload.get("area")

        if not comune_id_str or not titolo or not area:
            return {"errore": "comune_id, titolo e area sono obbligatori"}

        if area not in ("territorio", "turismo", "eventi", "servizi"):
            return {"errore": "Area non valida"}

        codice_azione_tipo = payload.get("codice_azione_tipo")
        if codice_azione_tipo and codice_azione_tipo not in AZIONI_TIPO_MAP:
            return {"errore": "codice_azione_tipo non valido"}

        piano = ottieni_o_crea_piano_attivo(comune_id_str)

        record = {
            "piano_id": piano["id"],
            "comune_id": comune_id_str,
            "codice_azione_tipo": codice_azione_tipo,
            "area": area,
            "classificazione": payload.get("classificazione"),
            "titolo": titolo,
            "descrizione": payload.get("descrizione"),
            "vocazione_collegata": payload.get("vocazione_collegata"),
            "budget_stimato": payload.get("budget_stimato"),
            "data_inizio": payload.get("data_inizio"),
            "data_scadenza": payload.get("data_scadenza"),
            "stato": payload.get("stato", "proposta"),
            "origine": payload.get("origine", "manuale"),
            "segnale_origine": payload.get("segnale_origine"),
            "dato_sottostante_origine": payload.get("dato_sottostante_origine"),
            "note": payload.get("note"),
        }
        creato_resp = supabase.table("azioni_piano").insert(record).execute()

        return {"status": "salvato", "azione": creato_resp.data[0] if creato_resp.data else None}
    except Exception as e:
        print(f"Errore creazione azione piano: {e}")
        return {"errore": str(e)}


@app.put("/azioni-piano/{azione_id}")
def aggiorna_azione_piano(azione_id: int, payload: dict):
    try:
        campi_consentiti = {
            "titolo", "descrizione", "vocazione_collegata", "budget_stimato",
            "data_inizio", "data_scadenza", "stato", "note",
        }
        aggiornamento = {k: v for k, v in payload.items() if k in campi_consentiti}
        if not aggiornamento:
            return {"errore": "Nessun campo valido da aggiornare"}

        if "stato" in aggiornamento and aggiornamento["stato"] not in ("proposta", "pianificata", "attiva", "completata", "scartata"):
            return {"errore": "Stato non valido"}

        aggiornamento["aggiornato_il"] = datetime.now().isoformat()
        supabase.table("azioni_piano").update(aggiornamento).eq("id", azione_id).execute()
        return {"status": "aggiornato"}
    except Exception as e:
        print(f"Errore aggiornamento azione piano {azione_id}: {e}")
        return {"errore": str(e)}


@app.delete("/azioni-piano/{azione_id}")
def elimina_azione_piano(azione_id: int):
    try:
        supabase.table("azioni_piano").delete().eq("id", azione_id).execute()
        return {"status": "eliminato"}
    except Exception as e:
        print(f"Errore eliminazione azione piano {azione_id}: {e}")
        return {"errore": str(e)}


@app.get("/gettito-soggiorno/{comune_id}")
def endpoint_get_gettito_soggiorno(comune_id: str):
    return get_gettito_soggiorno(comune_id)


@app.post("/gettito-soggiorno")
def endpoint_crea_gettito_soggiorno(payload: dict):
    return crea_gettito_soggiorno(payload)


@app.get("/categorie-destinazione-soggiorno/{comune_id}")
def endpoint_get_categorie_destinazione(comune_id: str):
    return get_categorie_destinazione(comune_id)


@app.put("/categorie-destinazione-soggiorno")
def endpoint_aggiorna_categoria_destinazione(payload: dict):
    return aggiorna_categoria_destinazione(payload)


@app.get("/allocazioni-soggiorno/{comune_id}")
def endpoint_get_allocazioni_soggiorno(comune_id: str, anno: int = None, mese: int = None):
    return get_allocazioni_soggiorno(comune_id, anno, mese)


@app.post("/allocazioni-soggiorno")
def endpoint_crea_allocazione_soggiorno(payload: dict):
    return crea_allocazione_soggiorno(payload)


@app.delete("/allocazioni-soggiorno/{allocazione_id}")
def endpoint_elimina_allocazione_soggiorno(allocazione_id: int):
    return elimina_allocazione_soggiorno(allocazione_id)


@app.get("/compensazione-territoriale/quote/{comune_id}")
def endpoint_get_quote_capitoli(comune_id: str):
    return get_quote_capitoli(comune_id)


@app.put("/compensazione-territoriale/quote")
def endpoint_aggiorna_quota_capitolo(payload: dict):
    return aggiorna_quota_capitolo(payload)


@app.get("/compensazione-territoriale/suggerimento/{comune_id}")
def endpoint_get_suggerimento_distribuzione(comune_id: str, anno: int, mese: int):
    return get_suggerimento_distribuzione(comune_id, anno, mese, calcola_valore_siti_periodo)


@app.get("/qualita-esperienza/quota-reinvestimento/{comune_id}")
def endpoint_get_quota_reinvestimento(comune_id: str):
    return get_quota_reinvestimento(comune_id)


@app.put("/qualita-esperienza/quota-reinvestimento")
def endpoint_aggiorna_quota_reinvestimento(payload: dict):
    return aggiorna_quota_reinvestimento(payload)


@app.get("/qualita-esperienza/capitoli/{comune_id}")
def endpoint_get_capitoli_qoe(comune_id: str):
    return get_capitoli_qoe(comune_id)


@app.put("/qualita-esperienza/capitoli")
def endpoint_aggiorna_capitolo_qoe(payload: dict):
    return aggiorna_capitolo_qoe(payload)


@app.get("/qualita-esperienza/budget/{comune_id}")
def endpoint_get_budget_qoe_mese(comune_id: str, anno: int, mese: int):
    return get_budget_qoe_mese(comune_id, anno, mese, calcola_valore_siti_periodo, calcola_range_mese)