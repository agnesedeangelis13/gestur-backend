from datetime import datetime, timedelta
from supabase import create_client
import os
from dotenv import load_dotenv
from imposta_soggiorno_service import ottieni_o_crea_piano_sviluppo_locale_attivo

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def get_siti_comune_ids(comune_id):
    siti_resp = supabase.table("siti_culturali").select("id").eq("comune_id", comune_id).execute()
    return [s["id"] for s in (siti_resp.data or [])]


def get_accessibilita_percepita(comune_id):
    richieste_resp = supabase.table("richieste_pit").select("categorie_disservizio").eq("comune_id", comune_id).execute()
    richieste = richieste_resp.data or []

    disservizi = [r for r in richieste if r.get("categorie_disservizio") and r["categorie_disservizio"].lower() != "altro"]
    totale_disservizi = len(disservizi)

    if totale_disservizi == 0:
        return None, "Nessuna segnalazione di disservizio registrata per questo comune"

    n_accessibilita = sum(1 for d in disservizi if d["categorie_disservizio"] == "Accessibilità")
    quota_pct = round(n_accessibilita / totale_disservizi * 100, 1)

    return {
        "quota_pct": quota_pct,
        "n_accessibilita": n_accessibilita,
        "totale_disservizi": totale_disservizi,
        "dato_sottostante": f"{n_accessibilita} segnalazioni di disservizio \"Accessibilità\" su {totale_disservizi} disservizi totali registrati ({quota_pct}%).",
    }, None


def get_quota_cultura_popolare(comune_id):
    sito_ids = get_siti_comune_ids(comune_id)
    if not sito_ids:
        return None, "Nessun sito culturale trovato per questo comune"

    dodici_mesi_fa = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
    presenze_resp = supabase.table("presenza").select("gruppo, provenienza") \
        .in_("sito_id", sito_ids).gte("data", dodici_mesi_fa).execute()
    presenze = presenze_resp.data or []

    if not presenze:
        return None, "Nessuna presenza registrata negli ultimi 12 mesi"

    totale = sum(p["gruppo"] or 0 for p in presenze)
    residenti = sum(p["gruppo"] or 0 for p in presenze if p.get("provenienza") == "Residente")

    if totale == 0:
        return None, "Presenze insufficienti per calcolare la quota di cultura popolare"

    quota_residenti_pct = round(residenti / totale * 100, 1)

    return {
        "quota_residenti_pct": quota_residenti_pct,
        "presenze_residenti": round(residenti, 1),
        "presenze_totali": round(totale, 1),
        "dato_sottostante": f"{round(residenti, 1)} presenze di residenti su {round(totale, 1)} presenze totali negli ultimi 12 mesi ({quota_residenti_pct}%).",
    }, None


def get_riepilogo_tariffe_agevolate(comune_id):
    categorie_resp = supabase.table("civic_pricing_categorie").select("*") \
        .eq("comune_id", comune_id).eq("attiva", True).execute()
    categorie = categorie_resp.data or []

    n_totali = len(categorie)
    n_gratuite = sum(1 for c in categorie if c["tariffa_proposta"] == 0)

    return {
        "n_categorie_configurate": n_totali,
        "n_gratuite": n_gratuite,
        "categorie": categorie,
    }


def get_impronta_sociale(comune_id):
    try:
        accessibilita, errore_accessibilita = get_accessibilita_percepita(comune_id)
        cultura_popolare, errore_cultura_popolare = get_quota_cultura_popolare(comune_id)
        tariffe_agevolate = get_riepilogo_tariffe_agevolate(comune_id)

        return {
            "comune_id": comune_id,
            "accessibilita_percepita": accessibilita,
            "errore_accessibilita": errore_accessibilita,
            "quota_cultura_popolare": cultura_popolare,
            "errore_cultura_popolare": errore_cultura_popolare,
            "tariffe_agevolate": tariffe_agevolate,
            "nota_metodologica": (
                "L'accessibilità percepita riusa le segnalazioni di disservizio già raccolte al Punto Informativo "
                "Turistico (stesso dato del Piano Strategico). La quota di cultura popolare confronta le presenze "
                "con provenienza \"Residente\" rispetto al totale, sugli ultimi 12 mesi: più è alta, più il "
                "patrimonio culturale funziona anche come servizio per chi vive il territorio, non solo per i "
                "turisti. Le tariffe agevolate mostrano quanto è stato configurato nel modulo Dynamic Civic "
                "Pricing: il tracciamento dell'utilizzo reale di questi ingressi richiederebbe una registrazione "
                "puntuale non ancora presente in GesTur, quindi qui si mostra la configurazione, non un dato di "
                "affluenza effettiva."
            ),
        }
    except Exception as e:
        print(f"Errore impronta sociale comune {comune_id}: {e}")
        return {"errore": str(e)}


def get_coefficiente_moltiplicatore(comune_id):
    try:
        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id)
        coefficiente = piano.get("coefficiente_moltiplicatore_locale")
        return {
            "piano_id": piano["id"],
            "comune_id": comune_id,
            "coefficiente_moltiplicatore": coefficiente,
            "configurato": coefficiente is not None,
        }
    except Exception as e:
        print(f"Errore get coefficiente moltiplicatore comune {comune_id}: {e}")
        return {"errore": str(e)}


def aggiorna_coefficiente_moltiplicatore(payload):
    try:
        comune_id_str = payload.get("comune_id")
        coefficiente_moltiplicatore = payload.get("coefficiente_moltiplicatore")

        if not comune_id_str or coefficiente_moltiplicatore is None:
            return {"errore": "comune_id e coefficiente_moltiplicatore sono obbligatori"}

        if coefficiente_moltiplicatore <= 0:
            return {"errore": "Il coefficiente moltiplicatore deve essere maggiore di zero"}

        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id_str)
        supabase.table("piani_sviluppo_locale").update({
            "coefficiente_moltiplicatore_locale": coefficiente_moltiplicatore,
        }).eq("id", piano["id"]).execute()

        return {"status": "salvato"}
    except Exception as e:
        print(f"Errore aggiornamento coefficiente moltiplicatore: {e}")
        return {"errore": str(e)}


def get_valore_territoriale(comune_id, valore_siti_helper):
    try:
        coeff_result = get_coefficiente_moltiplicatore(comune_id)
        if "errore" in coeff_result:
            return {"errore": coeff_result["errore"]}

        if not coeff_result["configurato"]:
            return {
                "comune_id": comune_id,
                "dati_sufficienti": False,
                "messaggio": (
                    "Configura il coefficiente di moltiplicazione locale (una tua stima, tratta da uno studio "
                    "economico di riferimento) per vedere questa dashboard."
                ),
            }

        oggi = datetime.now()
        dodici_mesi_fa = oggi - timedelta(days=365)
        risultato_valore = valore_siti_helper(comune_id, dodici_mesi_fa.strftime("%Y-%m-%d"), oggi.strftime("%Y-%m-%d"))

        if not risultato_valore:
            return {
                "comune_id": comune_id,
                "dati_sufficienti": False,
                "messaggio": "Dati insufficienti per calcolare il valore economico diretto generato dai siti culturali.",
            }

        valore_diretto = risultato_valore["valore_totale"]
        coefficiente = coeff_result["coefficiente_moltiplicatore"]
        valore_indotto_stimato = round(valore_diretto * coefficiente, 2)
        valore_aggiuntivo_territorio = round(valore_indotto_stimato - valore_diretto, 2)

        return {
            "comune_id": comune_id,
            "dati_sufficienti": True,
            "valore_diretto_12_mesi": valore_diretto,
            "coefficiente_moltiplicatore": coefficiente,
            "valore_indotto_stimato": valore_indotto_stimato,
            "valore_aggiuntivo_territorio": valore_aggiuntivo_territorio,
            "nota_metodologica": (
                f"Il valore diretto è quello realmente generato dai siti culturali negli ultimi 12 mesi (stesso "
                "dato di Dimensione Economica). Il coefficiente di moltiplicazione non è calcolato da GesTur: è "
                "un numero che hai inserito tu, tratto da un modello econometrico di riferimento (es. studi "
                "regionali sul moltiplicatore turistico). GesTur applica solo l'aritmetica: il valore indotto "
                "stimato è quanto quella spesa diretta genera complessivamente nell'indotto del territorio "
                "circostante (commercio, servizi, filiera locale), secondo il coefficiente che hai scelto."
            ),
        }
    except Exception as e:
        print(f"Errore valore territoriale comune {comune_id}: {e}")
        return {"errore": str(e)}