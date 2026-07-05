from datetime import datetime
from supabase import create_client
import os
from dotenv import load_dotenv
from imposta_soggiorno_service import ottieni_o_crea_piano_sviluppo_locale_attivo

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

TETTO_CORRETTIVO_QOE_PUNTI = 15.0
SOGLIA_MIN_DISSERVIZI_QOE = 3
QUOTA_REINVESTIMENTO_DEFAULT_PCT = 15.0

CAPITOLI_QOE = {
    "digital_corner_pit": {"nome": "Potenziamento digital corner e PIT"},
    "formazione_accoglienza": {"nome": "Formazione del personale di accoglienza"},
    "segnaletica_turistica": {"nome": "Segnaletica turistica e informativa"},
    "gestione_flussi": {"nome": "Gestione flussi e riduzione attese"},
    "accessibilita_turistica": {"nome": "Accessibilità e welfare culturale per il visitatore"},
    "connettivita_digitale": {"nome": "Connettività Wi-Fi nei siti"},
}

MAPPATURA_DISSERVIZIO_QOE = {
    "Wi-Fi e connettività": ["connettivita_digitale"],
    "Segnaletica": ["segnaletica_turistica"],
    "Attese/code": ["gestione_flussi"],
    "Sovraffollamento": ["gestione_flussi"],
    "Personale": ["formazione_accoglienza"],
    "Accessibilità": ["accessibilita_turistica"],
}


def get_quota_reinvestimento(comune_id):
    try:
        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id)
        quota = piano.get("quota_reinvestimento_qoe_pct")
        configurata = quota is not None
        return {
            "piano_id": piano["id"],
            "comune_id": comune_id,
            "quota_reinvestimento_pct": quota if configurata else QUOTA_REINVESTIMENTO_DEFAULT_PCT,
            "configurata": configurata,
            "nota_metodologica": (
                "Questa quota indica quale percentuale dei ricavi mensili dei siti culturali viene riservata al "
                "reinvestimento nell'esperienza di visita, prima che il resto entri nel calcolo della Compensazione "
                "Territoriale per i cittadini. Se non ancora configurata, viene mostrato un valore suggerito "
                f"({QUOTA_REINVESTIMENTO_DEFAULT_PCT}%) da sostituire con la scelta del comune."
            ),
        }
    except Exception as e:
        print(f"Errore get quota reinvestimento comune {comune_id}: {e}")
        return {"errore": str(e)}


def aggiorna_quota_reinvestimento(payload):
    try:
        comune_id_str = payload.get("comune_id")
        quota_reinvestimento_pct = payload.get("quota_reinvestimento_pct")

        if not comune_id_str or quota_reinvestimento_pct is None:
            return {"errore": "comune_id e quota_reinvestimento_pct sono obbligatori"}

        if quota_reinvestimento_pct < 0 or quota_reinvestimento_pct > 100:
            return {"errore": "quota_reinvestimento_pct deve essere tra 0 e 100"}

        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id_str)
        supabase.table("piani_sviluppo_locale").update({
            "quota_reinvestimento_qoe_pct": quota_reinvestimento_pct,
        }).eq("id", piano["id"]).execute()

        return {"status": "salvato"}
    except Exception as e:
        print(f"Errore aggiornamento quota reinvestimento: {e}")
        return {"errore": str(e)}


def get_capitoli_qoe(comune_id):
    try:
        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id)

        config_resp = supabase.table("qoe_capitoli_config").select("*") \
            .eq("piano_id", piano["id"]).execute()
        config_esistente = {c["categoria"]: c for c in (config_resp.data or [])}

        n_capitoli = len(CAPITOLI_QOE)
        quota_default = round(100 / n_capitoli, 1)

        capitoli = []
        for chiave, info in CAPITOLI_QOE.items():
            config = config_esistente.get(chiave)
            capitoli.append({
                "categoria": chiave,
                "nome": info["nome"],
                "quota_base_pct": config.get("quota_base_pct") if config and config.get("quota_base_pct") is not None else quota_default,
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
        print(f"Errore get capitoli QoE comune {comune_id}: {e}")
        return {"errore": str(e)}


def aggiorna_capitolo_qoe(payload):
    try:
        comune_id_str = payload.get("comune_id")
        categoria = payload.get("categoria")
        quota_base_pct = payload.get("quota_base_pct")

        if not comune_id_str or not categoria or quota_base_pct is None:
            return {"errore": "comune_id, categoria e quota_base_pct sono obbligatori"}

        if categoria not in CAPITOLI_QOE:
            return {"errore": "Categoria non valida"}

        if quota_base_pct < 0 or quota_base_pct > 100:
            return {"errore": "quota_base_pct deve essere tra 0 e 100"}

        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id_str)

        esistente_resp = supabase.table("qoe_capitoli_config").select("*") \
            .eq("piano_id", piano["id"]).eq("categoria", categoria).execute()

        if esistente_resp.data:
            supabase.table("qoe_capitoli_config").update({
                "quota_base_pct": quota_base_pct,
                "aggiornato_il": datetime.now().isoformat(),
            }).eq("piano_id", piano["id"]).eq("categoria", categoria).execute()
        else:
            supabase.table("qoe_capitoli_config").insert({
                "piano_id": piano["id"],
                "comune_id": comune_id_str,
                "categoria": categoria,
                "quota_base_pct": quota_base_pct,
            }).execute()

        return {"status": "salvato"}
    except Exception as e:
        print(f"Errore aggiornamento capitolo QoE: {e}")
        return {"errore": str(e)}


def calcola_criticita_qoe(comune_id):
    try:
        richieste_resp = supabase.table("richieste_pit").select("categorie_disservizio") \
            .eq("comune_id", comune_id).execute()
        richieste = richieste_resp.data or []

        tag_disservizio = []
        for r in richieste:
            valore = r.get("categorie_disservizio")
            if not valore:
                continue
            for tag in valore.split(", "):
                tag = tag.strip()
                if tag and tag.lower() != "altro":
                    tag_disservizio.append(tag)

        totale_disservizi = len(tag_disservizio)
        if totale_disservizi < SOGLIA_MIN_DISSERVIZI_QOE:
            return {}, totale_disservizi

        conteggio_per_tag = {}
        for tag in tag_disservizio:
            conteggio_per_tag[tag] = conteggio_per_tag.get(tag, 0) + 1

        peso_per_categoria = {chiave: 0 for chiave in CAPITOLI_QOE}
        dettaglio_match = {chiave: [] for chiave in CAPITOLI_QOE}

        for tag, conteggio in conteggio_per_tag.items():
            capitoli_associati = MAPPATURA_DISSERVIZIO_QOE.get(tag)
            if not capitoli_associati:
                continue
            for chiave in capitoli_associati:
                peso_per_categoria[chiave] += conteggio
                dettaglio_match[chiave].append(f"{tag} ({conteggio})")

        totale_match = sum(peso_per_categoria.values())
        if totale_match == 0:
            return {}, totale_disservizi

        quota_equa = 100 / len(CAPITOLI_QOE)
        correttivi = {}
        for chiave, peso in peso_per_categoria.items():
            if peso == 0:
                continue
            quota_osservata_pct = round(peso / totale_disservizi * 100, 1)
            scostamento = quota_osservata_pct - quota_equa
            scostamento_limitato = max(-TETTO_CORRETTIVO_QOE_PUNTI, min(TETTO_CORRETTIVO_QOE_PUNTI, scostamento))
            correttivi[chiave] = {
                "punti": round(scostamento_limitato, 1),
                "dato_sottostante": (
                    f"Segnalazioni PIT legate all'esperienza di visita collegate a \"{CAPITOLI_QOE[chiave]['nome']}\": "
                    f"{', '.join(dettaglio_match[chiave])}, pari al {quota_osservata_pct}% delle {totale_disservizi} "
                    f"segnalazioni di disservizio registrate."
                ),
            }

        return correttivi, totale_disservizi
    except Exception as e:
        print(f"Errore calcolo criticita QoE comune {comune_id}: {e}")
        return {}, 0


def get_budget_qoe_mese_totale(comune_id, valore_siti_lordo_mese):
    if not valore_siti_lordo_mese:
        return 0
    quota_risultato = get_quota_reinvestimento(comune_id)
    if "errore" in quota_risultato:
        return 0
    quota_pct = quota_risultato["quota_reinvestimento_pct"]
    return round(valore_siti_lordo_mese * quota_pct / 100, 2)


def get_budget_qoe_mese(comune_id, anno, mese, valore_siti_helper, calcola_range_mese_fn):
    try:
        data_inizio, data_fine = calcola_range_mese_fn(anno, mese)
        risultato_siti = valore_siti_helper(comune_id, data_inizio, data_fine)
        valore_siti_lordo_mese = risultato_siti["valore_totale"] if risultato_siti else None

        if valore_siti_lordo_mese is None:
            return {
                "comune_id": comune_id,
                "anno": anno,
                "mese": mese,
                "dati_sufficienti": False,
                "messaggio": (
                    "Nessun dato di presenze o tariffe sufficiente per stimare il valore dei siti culturali in "
                    "questo mese: impossibile calcolare il budget di reinvestimento."
                ),
            }

        quota_risultato = get_quota_reinvestimento(comune_id)
        if "errore" in quota_risultato:
            return {"errore": quota_risultato["errore"]}
        quota_reinvestimento_pct = quota_risultato["quota_reinvestimento_pct"]

        budget_totale_qoe = round(valore_siti_lordo_mese * quota_reinvestimento_pct / 100, 2)

        capitoli_risultato = get_capitoli_qoe(comune_id)
        if "errore" in capitoli_risultato:
            return {"errore": capitoli_risultato["errore"]}
        quote_base = {c["categoria"]: c["quota_base_pct"] for c in capitoli_risultato["capitoli"]}

        correttivi_criticita, totale_disservizi = calcola_criticita_qoe(comune_id)

        pct_grezze = {}
        dettaglio_capitoli = []
        for chiave, quota_base_pct in quote_base.items():
            corr = correttivi_criticita.get(chiave, {"punti": 0, "dato_sottostante": None})
            pct_finale_grezza = max(quota_base_pct + corr["punti"], 0)
            pct_grezze[chiave] = pct_finale_grezza

            dettaglio_capitoli.append({
                "categoria": chiave,
                "nome": CAPITOLI_QOE[chiave]["nome"],
                "quota_base_pct": quota_base_pct,
                "correttivo_criticita_punti": corr["punti"],
                "correttivo_criticita_motivo": corr["dato_sottostante"],
            })

        totale_grezzo = sum(pct_grezze.values())
        for d in dettaglio_capitoli:
            pct_normalizzata = round(pct_grezze[d["categoria"]] / totale_grezzo * 100, 1) if totale_grezzo > 0 else 0
            d["quota_finale_pct"] = pct_normalizzata
            d["importo_suggerito"] = round(budget_totale_qoe * pct_normalizzata / 100, 2)

        dettaglio_capitoli.sort(key=lambda x: x["quota_finale_pct"], reverse=True)

        return {
            "comune_id": comune_id,
            "anno": anno,
            "mese": mese,
            "dati_sufficienti": True,
            "valore_siti_culturali_mese": round(valore_siti_lordo_mese, 2),
            "quota_reinvestimento_pct": quota_reinvestimento_pct,
            "budget_totale_qoe": budget_totale_qoe,
            "n_disservizi_pit_considerati": totale_disservizi,
            "distribuzione_capitoli": dettaglio_capitoli,
            "tetto_correttivo_punti": TETTO_CORRETTIVO_QOE_PUNTI,
            "nota_metodologica": (
                f"Il budget di reinvestimento è calcolato applicando la quota di reinvestimento ({quota_reinvestimento_pct}%) "
                "al valore economico stimato dei soli siti culturali in questo mese (biglietteria, bookshop, ristorazione "
                "collegati alla visita). Questa cifra viene riservata prima che il resto del valore dei siti confluisca "
                "nel calcolo del residuo della Compensazione Territoriale per i cittadini: non è una cifra aggiuntiva, "
                "è una prima destinazione. La distribuzione tra i capitoli parte dalle quote base decise dal comune, "
                f"corrette da un segnale motivato basato sulle segnalazioni PIT legate all'esperienza di visita, "
                f"con un tetto di {TETTO_CORRETTIVO_QOE_PUNTI} punti percentuali."
            ),
        }
    except Exception as e:
        print(f"Errore budget QoE comune {comune_id}: {e}")
        return {"errore": str(e)}