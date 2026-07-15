from datetime import datetime, timedelta
from supabase import create_client
import os
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

PUNTEGGIO_VALUTAZIONE = {"Pessima": 0, "Sufficiente": 40, "Buona": 70, "Ottima": 100}

MESSAGGI_QUADRANTE = {
    "vantaggio_competitivo": "Valore percepito alto e calca contenuta: il sito mantiene un solido vantaggio competitivo.",
    "successo_a_rischio": "Valore percepito alto ma calca elevata: il sito rischia di scivolare verso l'overtourism, con possibile erosione della qualita percepita nel tempo.",
    "nicchia_da_valorizzare": "Valore percepito basso e calca contenuta: il sito e una nicchia non ancora valorizzata, con margine di crescita se accompagnato da investimenti mirati.",
    "rischio_declassamento": "Valore percepito basso e calca elevata: se il comune non investe nel miglioramento dei servizi, il sito rischia di scivolare verso il quadrante a basso valore e alta calca, perdendo il proprio vantaggio competitivo.",
}


def _calca_sito(sito_id):
    try:
        trenta_giorni_fa = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        dodici_mesi_fa = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")

        recenti_resp = supabase.table("presenza").select("data, gruppo").eq("sito_id", sito_id).gte("data", trenta_giorni_fa).execute()
        recenti = recenti_resp.data or []

        storico_resp = supabase.table("presenza").select("data, gruppo").eq("sito_id", sito_id).gte("data", dodici_mesi_fa).execute()
        storico = storico_resp.data or []

        if not recenti or not storico:
            return None

        per_giorno_recente = {}
        for r in recenti:
            per_giorno_recente[r["data"]] = per_giorno_recente.get(r["data"], 0) + (r.get("gruppo") or 0)
        media_recente = sum(per_giorno_recente.values()) / len(per_giorno_recente) if per_giorno_recente else 0

        per_giorno_storico = {}
        for r in storico:
            per_giorno_storico[r["data"]] = per_giorno_storico.get(r["data"], 0) + (r.get("gruppo") or 0)
        picco_storico = max(per_giorno_storico.values()) if per_giorno_storico else 0

        if picco_storico == 0:
            return None

        return round(min(100, (media_recente / picco_storico) * 100), 1)
    except Exception as e:
        print(f"Errore calca sito {sito_id}: {e}")
        return None


def _prezzo_medio_sito(sito_id):
    try:
        resp = supabase.table("categorie_biglietto").select("prezzo").eq("sito_id", sito_id).execute()
        prezzi = [c["prezzo"] for c in (resp.data or []) if c.get("prezzo") is not None]
        return round(sum(prezzi) / len(prezzi), 2) if prezzi else None
    except Exception as e:
        print(f"Errore prezzo medio sito {sito_id}: {e}")
        return None


def _valore_percepito_sito(sito_id, comune_id):
    try:
        questionari_sito_resp = supabase.table("questionari_accoglienza").select("indice_soddisfazione, valutazione_accoglienza").eq("sito_id", sito_id).execute()
        questionari_sito = questionari_sito_resp.data or []

        if questionari_sito:
            fonte_soddisfazione = questionari_sito
            specifico_del_sito = True
        else:
            questionari_comune_resp = supabase.table("questionari_accoglienza").select("indice_soddisfazione, valutazione_accoglienza").eq("comune_id", comune_id).execute()
            fonte_soddisfazione = questionari_comune_resp.data or []
            specifico_del_sito = False

        if not fonte_soddisfazione:
            punteggio_soddisfazione = None
        else:
            indici = [q["indice_soddisfazione"] * 10 for q in fonte_soddisfazione if q.get("indice_soddisfazione") is not None]
            valutazioni = [PUNTEGGIO_VALUTAZIONE.get(q["valutazione_accoglienza"], 50) for q in fonte_soddisfazione if q.get("valutazione_accoglienza")]
            componenti = indici + valutazioni
            punteggio_soddisfazione = round(sum(componenti) / len(componenti), 1) if componenti else None

        disservizi_resp = supabase.table("richieste_pit").select("categorie_disservizio").eq("comune_id", comune_id).execute()
        disservizi = [d for d in (disservizi_resp.data or []) if d.get("categorie_disservizio") and d["categorie_disservizio"].lower() != "altro"]
        if disservizi:
            n_accessibilita = len([d for d in disservizi if d["categorie_disservizio"] == "Accessibilità"])
            punteggio_accessibilita = round(100 - (n_accessibilita / len(disservizi) * 100), 1)
        else:
            punteggio_accessibilita = None

        componenti_valide = [c for c in [punteggio_soddisfazione, punteggio_accessibilita] if c is not None]
        if not componenti_valide:
            return None, specifico_del_sito

        valore_percepito = round(sum(componenti_valide) / len(componenti_valide), 1)
        return valore_percepito, specifico_del_sito
    except Exception as e:
        print(f"Errore valore percepito sito {sito_id}: {e}")
        return None, False


def _quadrante(valore_percepito, calca):
    if valore_percepito >= 50 and calca < 50:
        return "vantaggio_competitivo"
    elif valore_percepito >= 50 and calca >= 50:
        return "successo_a_rischio"
    elif valore_percepito < 50 and calca < 50:
        return "nicchia_da_valorizzare"
    else:
        return "rischio_declassamento"


def get_mappa_posizionamento(comune_id):
    try:
        siti_resp = supabase.table("siti_culturali").select("id, nome_sito").eq("comune_id", comune_id).execute()
        siti = siti_resp.data or []
        if not siti:
            return {"errore": "Nessun sito culturale trovato per questo comune"}

        punti = []
        for s in siti:
            calca = _calca_sito(s["id"])
            prezzo = _prezzo_medio_sito(s["id"])
            valore_percepito, specifico_del_sito = _valore_percepito_sito(s["id"], comune_id)

            if calca is None or valore_percepito is None:
                punti.append({
                    "sito_id": s["id"], "nome_sito": s["nome_sito"],
                    "calca": calca, "valore_percepito": valore_percepito, "prezzo_medio": prezzo,
                    "quadrante": None, "messaggio": None, "dati_insufficienti": True,
                    "soddisfazione_specifica_del_sito": specifico_del_sito,
                })
                continue

            quadrante = _quadrante(valore_percepito, calca)
            punti.append({
                "sito_id": s["id"], "nome_sito": s["nome_sito"],
                "calca": calca, "valore_percepito": valore_percepito, "prezzo_medio": prezzo,
                "quadrante": quadrante, "messaggio": MESSAGGI_QUADRANTE[quadrante], "dati_insufficienti": False,
                "soddisfazione_specifica_del_sito": specifico_del_sito,
            })

        return {
            "comune_id": comune_id,
            "punti": punti,
            "nota_metodologica": (
                "Calca: occupazione media degli ultimi 30 giorni rispetto al picco storico del sito negli ultimi "
                "12 mesi. Valore percepito: media tra soddisfazione (questionari di accoglienza, specifici del "
                "sito se disponibili, altrimenti a livello comunale) e accessibilita (quota di segnalazioni non "
                "legate all'accessibilita sul totale dei disservizi PIT del comune). Prezzo: prezzo medio dei "
                "biglietti del sito, mostrato come dimensione della bolla, non come asse."
            ),
        }
    except Exception as e:
        print(f"Errore mappa posizionamento comune {comune_id}: {e}")
        return {"errore": str(e)}