from datetime import datetime, timedelta
from supabase import create_client
import os
from dotenv import load_dotenv
from imposta_soggiorno_service import ottieni_o_crea_piano_sviluppo_locale_attivo

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

SOGLIA_CRESCITA_RISCHIO_ALTO_PCT = 15.0
SOGLIA_CRESCITA_RISCHIO_MEDIO_PCT = 5.0
GIORNI_FINESTRA_INVESTIMENTI_RECENTI = 180


def get_siti_comune_ids(comune_id):
    siti_resp = supabase.table("siti_culturali").select("id").eq("comune_id", comune_id).execute()
    return [s["id"] for s in (siti_resp.data or [])]


def get_valore_medio_visitatore(comune_id, valore_siti_helper):
    sito_ids = get_siti_comune_ids(comune_id)
    if not sito_ids:
        return None, "Nessun sito culturale trovato per questo comune"

    oggi = datetime.now()
    dodici_mesi_fa = oggi - timedelta(days=365)

    risultato_valore = valore_siti_helper(comune_id, dodici_mesi_fa.strftime("%Y-%m-%d"), oggi.strftime("%Y-%m-%d"))
    if not risultato_valore:
        return None, "Dati insufficienti per calcolare il valore economico generato dai siti culturali"

    presenze_resp = supabase.table("presenza").select("gruppo") \
        .in_("sito_id", sito_ids).gte("data", dodici_mesi_fa.strftime("%Y-%m-%d")).execute()
    totale_presenze = sum(p["gruppo"] or 0 for p in (presenze_resp.data or []))

    if totale_presenze == 0:
        return None, "Nessuna presenza registrata negli ultimi 12 mesi"

    valore_medio = round(risultato_valore["valore_totale"] / totale_presenze, 2)
    return {
        "valore_medio_per_visitatore": valore_medio,
        "totale_presenze_12_mesi": totale_presenze,
        "valore_totale_12_mesi": risultato_valore["valore_totale"],
    }, None


def get_flusso_medio_mensile(comune_id):
    sito_ids = get_siti_comune_ids(comune_id)
    if not sito_ids:
        return None

    novanta_giorni_fa = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    presenze_resp = supabase.table("presenza").select("gruppo") \
        .in_("sito_id", sito_ids).gte("data", novanta_giorni_fa).execute()
    totale_90gg = sum(p["gruppo"] or 0 for p in (presenze_resp.data or []))

    if totale_90gg == 0:
        return None

    return round(totale_90gg / 3, 1)


def simula_scenario_investimento(payload, valore_siti_helper):
    try:
        comune_id = payload.get("comune_id")
        titolo = payload.get("titolo", "Scenario simulato")
        costo_investimento = payload.get("costo_investimento")
        incremento_pct = payload.get("incremento_visitatori_pct_stimato")
        durata_mesi = payload.get("durata_mesi")

        if not comune_id or costo_investimento is None or incremento_pct is None or not durata_mesi:
            return {"errore": "comune_id, costo_investimento, incremento_visitatori_pct_stimato e durata_mesi sono obbligatori"}

        if costo_investimento <= 0 or durata_mesi <= 0:
            return {"errore": "costo_investimento e durata_mesi devono essere maggiori di zero"}

        valore_medio_result, errore = get_valore_medio_visitatore(comune_id, valore_siti_helper)
        if errore:
            return {"errore": errore}
        valore_medio = valore_medio_result["valore_medio_per_visitatore"]

        flusso_medio_mensile = get_flusso_medio_mensile(comune_id)
        if flusso_medio_mensile is None:
            return {"errore": "Dati di presenze insufficienti negli ultimi 90 giorni per proiettare i flussi futuri"}

        visitatori_scenario_b = round(flusso_medio_mensile * durata_mesi, 1)
        visitatori_scenario_a = round(visitatori_scenario_b * (1 + incremento_pct / 100), 1)

        valore_scenario_b = round(visitatori_scenario_b * valore_medio, 2)
        valore_scenario_a = round(visitatori_scenario_a * valore_medio, 2)

        differenza_valore = round(valore_scenario_a - valore_scenario_b, 2)
        rapporto_valore_costo = round(differenza_valore / costo_investimento, 1) if costo_investimento > 0 else None

        return {
            "titolo": titolo,
            "comune_id": comune_id,
            "durata_mesi": durata_mesi,
            "costo_investimento": costo_investimento,
            "valore_medio_per_visitatore": valore_medio,
            "flusso_medio_mensile_storico": flusso_medio_mensile,
            "scenario_b_senza_investimento": {
                "visitatori_stimati": visitatori_scenario_b,
                "valore_stimato": valore_scenario_b,
            },
            "scenario_a_con_investimento": {
                "visitatori_stimati": visitatori_scenario_a,
                "valore_stimato": valore_scenario_a,
                "incremento_pct_dichiarato": incremento_pct,
            },
            "differenza_valore": differenza_valore,
            "rapporto_valore_costo": rapporto_valore_costo,
            "nota_metodologica": (
                "Lo Scenario B (senza investimento) proietta il flusso medio degli ultimi 90 giorni in modo lineare "
                "sulla durata scelta: non è una previsione SARIMAX a lungo termine, che perderebbe affidabilità "
                "oltre pochi mesi, ma una base storica reale e verificabile. Lo Scenario A applica a quella base "
                "l'incremento percentuale che hai dichiarato tu: GesTur non può prevedere l'effetto di un "
                "investimento non ancora fatto, quindi questa è la tua stima, non un calcolo autonomo. Il valore "
                "economico per visitatore è quello realmente generato negli ultimi 12 mesi (biglietteria, "
                "bookshop, ristorazione), lo stesso già usato in Dimensione Economica. Il rapporto valore/costo "
                "indica quante volte il valore aggiuntivo stimato supera il costo dell'investimento nel periodo "
                "considerato."
            ),
        }
    except Exception as e:
        print(f"Errore simulazione scenario investimento: {e}")
        return {"errore": str(e)}


def get_indice_rischio_welfare(comune_id):
    try:
        sito_ids = get_siti_comune_ids(comune_id)
        if not sito_ids:
            return {"errore": "Nessun sito culturale trovato per questo comune"}

        flusso_medio_mensile = get_flusso_medio_mensile(comune_id)
        if flusso_medio_mensile is None:
            return {"errore": "Dati di presenze insufficienti negli ultimi 90 giorni per calcolare un indice di rischio"}

        oggi = datetime.now()
        fine = oggi + timedelta(days=30)
        prev_resp = supabase.table("previsioni_affluenza").select("affluenza_stimata") \
            .in_("sito_id", sito_ids).gte("data_previsione", oggi.strftime("%Y-%m-%d")) \
            .lte("data_previsione", fine.strftime("%Y-%m-%d")).execute()
        previsioni = prev_resp.data or []

        crescita_pct = None
        if previsioni:
            totale_previsto_30gg = sum(p["affluenza_stimata"] or 0 for p in previsioni)
            baseline_giornaliera = flusso_medio_mensile / 30
            baseline_30gg = baseline_giornaliera * 30
            if baseline_30gg > 0:
                crescita_pct = round((totale_previsto_30gg - baseline_30gg) / baseline_30gg * 100, 1)

        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id)
        soglia_temporale = (oggi - timedelta(days=GIORNI_FINESTRA_INVESTIMENTI_RECENTI)).isoformat()
        progetti_resp = supabase.table("progetti_investimento").select("id") \
            .eq("piano_id", piano["id"]).in_("stato", ["approvato", "completato"]) \
            .gte("data_approvazione", soglia_temporale).execute()
        n_progetti_recenti = len(progetti_resp.data or [])

        if crescita_pct is not None and crescita_pct >= SOGLIA_CRESCITA_RISCHIO_ALTO_PCT and n_progetti_recenti == 0:
            livello = "alto"
            messaggio = (
                f"I flussi previsti crescono del {crescita_pct}% nei prossimi 30 giorni rispetto alla media storica, "
                f"ma non risulta alcun progetto di investimento approvato negli ultimi {GIORNI_FINESTRA_INVESTIMENTI_RECENTI} "
                "giorni (Decoro Urbano o Fondo Sostenibilità). Una crescita non accompagnata da investimenti in "
                "manutenzione, viabilità e decoro tende a scaricarsi sui residenti."
            )
        elif (crescita_pct is not None and crescita_pct >= SOGLIA_CRESCITA_RISCHIO_MEDIO_PCT) or n_progetti_recenti == 0:
            livello = "medio"
            messaggio = (
                "Uno dei due segnali (crescita dei flussi o assenza di investimenti recenti) indica una situazione "
                "da monitorare, senza essere ancora critica."
            )
        else:
            livello = "basso"
            messaggio = (
                "I flussi previsti non mostrano una crescita marcata rispetto alla media storica, oppure sono già "
                "accompagnati da investimenti recenti registrati nei fondi vincolati."
            )

        return {
            "comune_id": comune_id,
            "livello_rischio": livello,
            "crescita_flussi_pct_30gg": crescita_pct,
            "n_progetti_investimento_approvati_recenti": n_progetti_recenti,
            "finestra_investimenti_giorni": GIORNI_FINESTRA_INVESTIMENTI_RECENTI,
            "messaggio": messaggio,
            "nota_metodologica": (
                "Questo indice non monetizza il disagio sociale: sarebbe un numero inventato, GesTur non ha dati "
                "reali su rabbia sociale o degrado. È un indice qualitativo (alto/medio/basso) costruito su due "
                "segnali reali: la crescita dei flussi prevista da SARIMAX nei prossimi 30 giorni rispetto alla "
                "media storica, e la presenza o assenza di progetti di investimento approvati di recente nei "
                "fondi vincolati di Decoro Urbano e Fondo Sostenibilità."
            ),
        }
    except Exception as e:
        print(f"Errore indice rischio welfare comune {comune_id}: {e}")
        return {"errore": str(e)}


def crea_risorsa_valore_perso(payload):
    try:
        comune_id_str = payload.get("comune_id")
        nome_risorsa = payload.get("nome_risorsa")
        descrizione_vincolo = payload.get("descrizione_vincolo")
        domanda_persa_stimata_settimana = payload.get("domanda_persa_stimata_settimana")
        costo_intervento = payload.get("costo_intervento")

        if not comune_id_str or not nome_risorsa or domanda_persa_stimata_settimana is None:
            return {"errore": "comune_id, nome_risorsa e domanda_persa_stimata_settimana sono obbligatori"}

        if domanda_persa_stimata_settimana < 0:
            return {"errore": "domanda_persa_stimata_settimana non può essere negativa"}

        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id_str)

        record = {
            "piano_id": piano["id"],
            "comune_id": comune_id_str,
            "nome_risorsa": nome_risorsa,
            "descrizione_vincolo": descrizione_vincolo,
            "domanda_persa_stimata_settimana": domanda_persa_stimata_settimana,
            "costo_intervento": costo_intervento,
        }
        creato_resp = supabase.table("matrice_valore_perso").insert(record).execute()

        return {"status": "salvato", "risorsa": creato_resp.data[0] if creato_resp.data else None}
    except Exception as e:
        print(f"Errore creazione risorsa valore perso: {e}")
        return {"errore": str(e)}


def get_risorse_valore_perso(comune_id, valore_siti_helper):
    try:
        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id)

        risorse_resp = supabase.table("matrice_valore_perso").select("*") \
            .eq("piano_id", piano["id"]).order("creato_il", desc=True).execute()
        risorse = risorse_resp.data or []

        valore_medio_result, errore = get_valore_medio_visitatore(comune_id, valore_siti_helper)

        risultati = []
        for r in risorse:
            if errore:
                risultati.append({**r, "valore_perso_settimana": None, "tempo_rientro_settimane": None})
                continue

            valore_medio = valore_medio_result["valore_medio_per_visitatore"]
            valore_perso_settimana = round(r["domanda_persa_stimata_settimana"] * valore_medio, 2)
            tempo_rientro_settimane = None
            if r.get("costo_intervento") and valore_perso_settimana > 0:
                tempo_rientro_settimane = round(r["costo_intervento"] / valore_perso_settimana, 1)

            risultati.append({
                **r,
                "valore_perso_settimana": valore_perso_settimana,
                "tempo_rientro_settimane": tempo_rientro_settimane,
            })

        return {
            "piano_id": piano["id"],
            "comune_id": comune_id,
            "valore_medio_per_visitatore": valore_medio_result["valore_medio_per_visitatore"] if not errore else None,
            "errore_valore_medio": errore,
            "risorse": risultati,
            "nota_metodologica": (
                "La domanda persa stimata a settimana è una tua valutazione: quante persone in più visiterebbero "
                "questa risorsa se il vincolo (orari, accessibilità, mancanza di fondi) fosse rimosso. GesTur "
                "applica a quella stima il valore medio reale per visitatore già calcolato sugli ultimi 12 mesi "
                "in questo comune, e confronta il valore settimanale così ottenuto con il costo dichiarato per "
                "rimuovere il vincolo, per stimare in quante settimane l'investimento si ripagherebbe."
            ),
        }
    except Exception as e:
        print(f"Errore get risorse valore perso comune {comune_id}: {e}")
        return {"errore": str(e)}


def elimina_risorsa_valore_perso(risorsa_id):
    try:
        supabase.table("matrice_valore_perso").delete().eq("id", risorsa_id).execute()
        return {"status": "eliminato"}
    except Exception as e:
        print(f"Errore eliminazione risorsa valore perso {risorsa_id}: {e}")
        return {"errore": str(e)}