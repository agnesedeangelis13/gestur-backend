from datetime import datetime
from supabase import create_client
import os
from dotenv import load_dotenv
from imposta_soggiorno_service import ottieni_o_crea_piano_sviluppo_locale_attivo, get_gettito_mese
from entrate_aggiuntive_service import get_totale_altre_entrate_mese

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

QUOTA_SOSTENIBILITA_DEFAULT_PCT = 8.0

CAPITOLI_SOSTENIBILITA = {
    "piantumazione_alberi": {"nome": "Piantumazione e cura del verde ad alto valore ambientale", "unita_misura": "alberi piantumati", "conversione_fissa": True},
    "mobilita_sostenibile": {"nome": "Mobilità sostenibile e piste ciclabili", "unita_misura": "km di piste ciclabili", "conversione_fissa": True},
    "efficientamento_energetico": {"nome": "Efficientamento energetico degli edifici pubblici", "unita_misura": "edifici efficientati", "conversione_fissa": True},
    "energie_rinnovabili": {"nome": "Impianti a energie rinnovabili su strutture comunali", "unita_misura": "kW installati", "conversione_fissa": True},
    "gestione_rifiuti": {"nome": "Riduzione e gestione sostenibile dei rifiuti", "unita_misura": "tonnellate differenziate/anno", "conversione_fissa": True},
    "tutela_biodiversita": {"nome": "Tutela della biodiversità e delle aree naturali", "unita_misura": None, "conversione_fissa": False},
}


def get_quota_sostenibilita(comune_id):
    try:
        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id)
        quota = piano.get("quota_sostenibilita_pct")
        configurata = quota is not None
        return {
            "piano_id": piano["id"],
            "comune_id": comune_id,
            "quota_sostenibilita_pct": quota if configurata else QUOTA_SOSTENIBILITA_DEFAULT_PCT,
            "configurata": configurata,
            "nota_metodologica": (
                "Questa quota indica quale percentuale dell'incasso turistico totale del mese (imposta di "
                "soggiorno, valore lordo dei siti culturali ed eventuali altre entrate turistiche) confluisce nel "
                "Fondo di Rigenerazione Sostenibile, prima che il resto entri nel calcolo del residuo della "
                "Compensazione Territoriale. Se non ancora configurata, viene mostrato un valore suggerito "
                f"({QUOTA_SOSTENIBILITA_DEFAULT_PCT}%) da sostituire con la scelta del comune."
            ),
        }
    except Exception as e:
        print(f"Errore get quota sostenibilita comune {comune_id}: {e}")
        return {"errore": str(e)}


def aggiorna_quota_sostenibilita(payload):
    try:
        comune_id_str = payload.get("comune_id")
        quota_sostenibilita_pct = payload.get("quota_sostenibilita_pct")

        if not comune_id_str or quota_sostenibilita_pct is None:
            return {"errore": "comune_id e quota_sostenibilita_pct sono obbligatori"}

        if quota_sostenibilita_pct < 0 or quota_sostenibilita_pct > 100:
            return {"errore": "quota_sostenibilita_pct deve essere tra 0 e 100"}

        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id_str)
        supabase.table("piani_sviluppo_locale").update({
            "quota_sostenibilita_pct": quota_sostenibilita_pct,
        }).eq("id", piano["id"]).execute()

        return {"status": "salvato"}
    except Exception as e:
        print(f"Errore aggiornamento quota sostenibilita: {e}")
        return {"errore": str(e)}


def get_capitoli_sostenibilita(comune_id):
    try:
        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id)

        config_resp = supabase.table("fondo_sostenibilita_config").select("*") \
            .eq("piano_id", piano["id"]).execute()
        config_esistente = {c["categoria"]: c for c in (config_resp.data or [])}

        n_capitoli = len(CAPITOLI_SOSTENIBILITA)
        quota_default = round(100 / n_capitoli, 1)

        capitoli = []
        for chiave, info in CAPITOLI_SOSTENIBILITA.items():
            config = config_esistente.get(chiave)
            capitoli.append({
                "categoria": chiave,
                "nome": info["nome"],
                "unita_misura": info["unita_misura"],
                "conversione_fissa": info["conversione_fissa"],
                "quota_base_pct": config.get("quota_base_pct") if config and config.get("quota_base_pct") is not None else quota_default,
                "fattore_conversione": config.get("fattore_conversione") if config else None,
                "coefficiente_impatto": config.get("coefficiente_impatto") if config else None,
                "unita_misura_impatto": config.get("unita_misura_impatto") if config else None,
                "configurata": config is not None and config.get("quota_base_pct") is not None,
            })

        totale_quote = round(sum(c["quota_base_pct"] for c in capitoli), 1)

        return {
            "piano_id": piano["id"],
            "comune_id": comune_id,
            "capitoli": capitoli,
            "totale_quote_pct": totale_quote,
            "quote_bilanciate": abs(totale_quote - 100) < 0.5,
        }
    except Exception as e:
        print(f"Errore get capitoli sostenibilita comune {comune_id}: {e}")
        return {"errore": str(e)}


def aggiorna_capitolo_sostenibilita(payload):
    try:
        comune_id_str = payload.get("comune_id")
        categoria = payload.get("categoria")

        if not comune_id_str or not categoria:
            return {"errore": "comune_id e categoria sono obbligatori"}

        if categoria not in CAPITOLI_SOSTENIBILITA:
            return {"errore": "Categoria non valida"}

        quota_base_pct = payload.get("quota_base_pct")
        fattore_conversione = payload.get("fattore_conversione")
        coefficiente_impatto = payload.get("coefficiente_impatto")
        unita_misura_impatto = payload.get("unita_misura_impatto")

        if quota_base_pct is not None and (quota_base_pct < 0 or quota_base_pct > 100):
            return {"errore": "quota_base_pct deve essere tra 0 e 100"}

        if not CAPITOLI_SOSTENIBILITA[categoria]["conversione_fissa"] and (fattore_conversione is not None or coefficiente_impatto is not None):
            return {"errore": "Questa categoria è descrittiva e non supporta coefficienti di conversione"}

        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id_str)

        esistente_resp = supabase.table("fondo_sostenibilita_config").select("*") \
            .eq("piano_id", piano["id"]).eq("categoria", categoria).execute()

        aggiornamento = {"aggiornato_il": datetime.now().isoformat()}
        if quota_base_pct is not None:
            aggiornamento["quota_base_pct"] = quota_base_pct
        if fattore_conversione is not None:
            aggiornamento["fattore_conversione"] = fattore_conversione
        if unita_misura_impatto is not None:
            aggiornamento["unita_misura_impatto"] = unita_misura_impatto
        if coefficiente_impatto is not None:
            aggiornamento["coefficiente_impatto"] = coefficiente_impatto

        if esistente_resp.data:
            supabase.table("fondo_sostenibilita_config").update(aggiornamento).eq("piano_id", piano["id"]).eq("categoria", categoria).execute()
        else:
            supabase.table("fondo_sostenibilita_config").insert({
                "piano_id": piano["id"],
                "comune_id": comune_id_str,
                "categoria": categoria,
                **aggiornamento,
            }).execute()

        return {"status": "salvato"}
    except Exception as e:
        print(f"Errore aggiornamento capitolo sostenibilita: {e}")
        return {"errore": str(e)}


def get_incasso_totale_mese(comune_id, anno, mese, valore_siti_helper, calcola_range_mese_fn):
    gettito_mese = get_gettito_mese(comune_id, anno, mese)
    data_inizio, data_fine = calcola_range_mese_fn(anno, mese)
    risultato_siti = valore_siti_helper(comune_id, data_inizio, data_fine)
    valore_siti_lordo_mese = risultato_siti["valore_totale"] if risultato_siti else None
    altre_entrate_mese = get_totale_altre_entrate_mese(comune_id, anno, mese)
    return gettito_mese, valore_siti_lordo_mese, altre_entrate_mese


def get_budget_sostenibilita_mese_totale(incasso_totale_mese, comune_id):
    if not incasso_totale_mese:
        return 0
    quota_risultato = get_quota_sostenibilita(comune_id)
    if "errore" in quota_risultato:
        return 0
    quota_pct = quota_risultato["quota_sostenibilita_pct"]
    return round(incasso_totale_mese * quota_pct / 100, 2)


def get_anteprima_mese(comune_id, anno, mese, valore_siti_helper, calcola_range_mese_fn):
    try:
        gettito_mese, valore_siti_lordo_mese, altre_entrate_mese = get_incasso_totale_mese(comune_id, anno, mese, valore_siti_helper, calcola_range_mese_fn)

        if gettito_mese is None and valore_siti_lordo_mese is None and altre_entrate_mese is None:
            return {
                "comune_id": comune_id,
                "anno": anno,
                "mese": mese,
                "dati_sufficienti": False,
                "messaggio": (
                    "Nessun gettito di imposta di soggiorno registrato, nessuna entrata turistica aggiuntiva e "
                    "nessun dato di presenze sufficiente per stimare il valore dei siti culturali in questo mese: "
                    "impossibile calcolare il versamento al Fondo di Rigenerazione Sostenibile."
                ),
            }

        incasso_totale_mese = (gettito_mese or 0) + (valore_siti_lordo_mese or 0) + (altre_entrate_mese or 0)

        quota_risultato = get_quota_sostenibilita(comune_id)
        if "errore" in quota_risultato:
            return {"errore": quota_risultato["errore"]}
        quota_sostenibilita_pct = quota_risultato["quota_sostenibilita_pct"]

        versamento_totale = round(incasso_totale_mese * quota_sostenibilita_pct / 100, 2)

        capitoli_risultato = get_capitoli_sostenibilita(comune_id)
        if "errore" in capitoli_risultato:
            return {"errore": capitoli_risultato["errore"]}

        totale_quote_configurate = capitoli_risultato["totale_quote_pct"]
        dettaglio_capitoli = []
        for c in capitoli_risultato["capitoli"]:
            quota_finale_pct = c["quota_base_pct"]
            importo_potenziale = round(versamento_totale * quota_finale_pct / 100, 2)

            unita_potenziale = None
            impatto_potenziale = None
            if c["conversione_fissa"] and c["fattore_conversione"]:
                unita_potenziale = round(importo_potenziale / c["fattore_conversione"], 1)
                if c["coefficiente_impatto"]:
                    impatto_potenziale = round(unita_potenziale * c["coefficiente_impatto"], 1)

            dettaglio_capitoli.append({
                "categoria": c["categoria"],
                "nome": c["nome"],
                "unita_misura": c["unita_misura"],
                "quota_finale_pct": quota_finale_pct,
                "importo_potenziale": importo_potenziale,
                "unita_potenziale": unita_potenziale,
                "impatto_potenziale": impatto_potenziale,
                "unita_misura_impatto": c["unita_misura_impatto"],
            })

        dettaglio_capitoli.sort(key=lambda x: x["quota_finale_pct"], reverse=True)

        return {
            "comune_id": comune_id,
            "anno": anno,
            "mese": mese,
            "dati_sufficienti": True,
            "gettito_soggiorno_mese": gettito_mese,
            "valore_siti_culturali_lordo_mese": valore_siti_lordo_mese,
            "altre_entrate_turistiche_mese": altre_entrate_mese,
            "incasso_totale_mese": round(incasso_totale_mese, 2),
            "quota_sostenibilita_pct": quota_sostenibilita_pct,
            "versamento_totale_mese": versamento_totale,
            "quote_capitoli_bilanciate": capitoli_risultato["quote_bilanciate"],
            "distribuzione_capitoli": dettaglio_capitoli,
            "nota_metodologica": (
                f"Il versamento mensile applica la quota configurata ({quota_sostenibilita_pct}%) all'incasso "
                "turistico totale del mese (imposta di soggiorno, valore lordo dei siti culturali ed eventuali "
                "altre entrate turistiche, eventi esclusi). Questa cifra è riservata prima che il resto confluisca "
                "nel residuo della Compensazione Territoriale. Gli importi potenziali per capitolo sono una "
                "ripartizione secondo le quote base decise dal comune; le unità e l'impatto ambientale stimato "
                "compaiono solo dove il comune ha configurato un fattore di conversione e un coefficiente di "
                "impatto di propria fiducia: GesTur applica l'aritmetica su numeri forniti dal comune, non stima "
                "dati ambientali in autonomia."
            ),
        }
    except Exception as e:
        print(f"Errore anteprima mese sostenibilita comune {comune_id}: {e}")
        return {"errore": str(e)}


def registra_versamento_mese(comune_id, anno, mese, valore_siti_helper, calcola_range_mese_fn):
    try:
        anteprima = get_anteprima_mese(comune_id, anno, mese, valore_siti_helper, calcola_range_mese_fn)
        if "errore" in anteprima:
            return {"errore": anteprima["errore"]}
        if not anteprima["dati_sufficienti"]:
            return {"errore": anteprima["messaggio"]}

        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id)

        record = {
            "piano_id": piano["id"],
            "comune_id": comune_id,
            "anno": anno,
            "mese": mese,
            "importo_versato": anteprima["versamento_totale_mese"],
            "da_soggiorno": anteprima["gettito_soggiorno_mese"],
            "da_siti": anteprima["valore_siti_culturali_lordo_mese"],
            "da_altre_entrate": anteprima["altre_entrate_turistiche_mese"],
        }
        supabase.table("fondo_sostenibilita_versamenti").upsert(
            record, on_conflict="piano_id,anno,mese"
        ).execute()

        return {"status": "registrato", "importo_versato": anteprima["versamento_totale_mese"]}
    except Exception as e:
        print(f"Errore registrazione versamento comune {comune_id}: {e}")
        return {"errore": str(e)}


def get_saldo_fondo(comune_id):
    try:
        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id)

        versamenti_resp = supabase.table("fondo_sostenibilita_versamenti").select("*") \
            .eq("piano_id", piano["id"]).order("anno").order("mese").execute()
        versamenti = versamenti_resp.data or []

        saldo_totale = sum(v["importo_versato"] or 0 for v in versamenti)
        totale_da_soggiorno = sum(v["da_soggiorno"] or 0 for v in versamenti if v.get("da_soggiorno"))
        totale_da_siti = sum(v["da_siti"] or 0 for v in versamenti if v.get("da_siti"))
        totale_da_altre_entrate = sum(v["da_altre_entrate"] or 0 for v in versamenti if v.get("da_altre_entrate"))

        return {
            "piano_id": piano["id"],
            "comune_id": comune_id,
            "saldo_totale": round(saldo_totale, 2),
            "totale_da_soggiorno": round(totale_da_soggiorno, 2),
            "totale_da_siti": round(totale_da_siti, 2),
            "totale_da_altre_entrate": round(totale_da_altre_entrate, 2),
            "n_mesi_registrati": len(versamenti),
            "versamenti": versamenti,
            "nota_metodologica": (
                "Il saldo somma tutti i versamenti mensili registrati esplicitamente nel fondo. Registrare un "
                "mese è un'azione volontaria (pulsante \"Registra versamento del mese\"): finché non viene "
                "confermato, il mese non entra nel saldo accumulato, anche se la cifra è già visibile in anteprima."
            ),
        }
    except Exception as e:
        print(f"Errore saldo fondo comune {comune_id}: {e}")
        return {"errore": str(e)}