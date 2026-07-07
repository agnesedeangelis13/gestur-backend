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

TETTO_CORRETTIVO_DECORO_PUNTI = 15.0
SOGLIA_MIN_DISSERVIZI_DECORO = 3
QUOTA_DECORO_DEFAULT_PCT = 10.0

CAPITOLI_DECORO = {
    "manutenzione_stradale": {"nome": "Manutenzione e asfaltatura stradale"},
    "cura_verde_pubblico": {"nome": "Cura del verde pubblico e alberature"},
    "pulizia_straordinaria": {"nome": "Pulizia straordinaria e decoro urbano"},
    "arredo_urbano": {"nome": "Arredo urbano: panchine, illuminazione, cestini"},
    "manutenzione_marciapiedi": {"nome": "Manutenzione marciapiedi e piccola edilizia pubblica"},
    "segnaletica_non_turistica": {"nome": "Segnaletica stradale e viabilità ordinaria"},
}

MAPPATURA_DISSERVIZIO_DECORO = {
    "Segnaletica": ["segnaletica_non_turistica"],
    "Parcheggio": ["manutenzione_stradale"],
    "Servizi igienici": ["pulizia_straordinaria"],
    "Pulizia": ["pulizia_straordinaria"],
    "Manutenzione struttura": ["manutenzione_marciapiedi"],
    "Accessibilità": ["manutenzione_marciapiedi"],
}


def get_quota_decoro(comune_id):
    try:
        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id)
        quota = piano.get("quota_decoro_urbano_pct")
        configurata = quota is not None
        return {
            "piano_id": piano["id"],
            "comune_id": comune_id,
            "quota_decoro_pct": quota if configurata else QUOTA_DECORO_DEFAULT_PCT,
            "configurata": configurata,
            "nota_metodologica": (
                "Questa quota indica quale percentuale dell'incasso turistico totale del mese (imposta di "
                "soggiorno più valore lordo dei siti culturali) viene vincolata alla manutenzione e al decoro "
                "urbano, prima che il resto entri nel calcolo del residuo della Compensazione Territoriale. "
                "Se non ancora configurata, viene mostrato un valore suggerito "
                f"({QUOTA_DECORO_DEFAULT_PCT}%) da sostituire con la scelta del comune."
            ),
        }
    except Exception as e:
        print(f"Errore get quota decoro comune {comune_id}: {e}")
        return {"errore": str(e)}


def aggiorna_quota_decoro(payload):
    try:
        comune_id_str = payload.get("comune_id")
        quota_decoro_pct = payload.get("quota_decoro_pct")

        if not comune_id_str or quota_decoro_pct is None:
            return {"errore": "comune_id e quota_decoro_pct sono obbligatori"}

        if quota_decoro_pct < 0 or quota_decoro_pct > 100:
            return {"errore": "quota_decoro_pct deve essere tra 0 e 100"}

        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id_str)
        supabase.table("piani_sviluppo_locale").update({
            "quota_decoro_urbano_pct": quota_decoro_pct,
        }).eq("id", piano["id"]).execute()

        return {"status": "salvato"}
    except Exception as e:
        print(f"Errore aggiornamento quota decoro: {e}")
        return {"errore": str(e)}


def get_capitoli_decoro(comune_id):
    try:
        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id)

        config_resp = supabase.table("decoro_urbano_config").select("*") \
            .eq("piano_id", piano["id"]).execute()
        config_esistente = {c["categoria"]: c for c in (config_resp.data or [])}

        n_capitoli = len(CAPITOLI_DECORO)
        quota_default = round(100 / n_capitoli, 1)

        capitoli = []
        for chiave, info in CAPITOLI_DECORO.items():
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
        print(f"Errore get capitoli decoro comune {comune_id}: {e}")
        return {"errore": str(e)}


def aggiorna_capitolo_decoro(payload):
    try:
        comune_id_str = payload.get("comune_id")
        categoria = payload.get("categoria")
        quota_base_pct = payload.get("quota_base_pct")

        if not comune_id_str or not categoria or quota_base_pct is None:
            return {"errore": "comune_id, categoria e quota_base_pct sono obbligatori"}

        if categoria not in CAPITOLI_DECORO:
            return {"errore": "Categoria non valida"}

        if quota_base_pct < 0 or quota_base_pct > 100:
            return {"errore": "quota_base_pct deve essere tra 0 e 100"}

        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id_str)

        esistente_resp = supabase.table("decoro_urbano_config").select("*") \
            .eq("piano_id", piano["id"]).eq("categoria", categoria).execute()

        if esistente_resp.data:
            supabase.table("decoro_urbano_config").update({
                "quota_base_pct": quota_base_pct,
                "aggiornato_il": datetime.now().isoformat(),
            }).eq("piano_id", piano["id"]).eq("categoria", categoria).execute()
        else:
            supabase.table("decoro_urbano_config").insert({
                "piano_id": piano["id"],
                "comune_id": comune_id_str,
                "categoria": categoria,
                "quota_base_pct": quota_base_pct,
            }).execute()

        return {"status": "salvato"}
    except Exception as e:
        print(f"Errore aggiornamento capitolo decoro: {e}")
        return {"errore": str(e)}


def calcola_criticita_decoro(comune_id):
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
        if totale_disservizi < SOGLIA_MIN_DISSERVIZI_DECORO:
            return {}, totale_disservizi

        conteggio_per_tag = {}
        for tag in tag_disservizio:
            conteggio_per_tag[tag] = conteggio_per_tag.get(tag, 0) + 1

        peso_per_categoria = {chiave: 0 for chiave in CAPITOLI_DECORO}
        dettaglio_match = {chiave: [] for chiave in CAPITOLI_DECORO}

        for tag, conteggio in conteggio_per_tag.items():
            capitoli_associati = MAPPATURA_DISSERVIZIO_DECORO.get(tag)
            if not capitoli_associati:
                continue
            for chiave in capitoli_associati:
                peso_per_categoria[chiave] += conteggio
                dettaglio_match[chiave].append(f"{tag} ({conteggio})")

        totale_match = sum(peso_per_categoria.values())
        if totale_match == 0:
            return {}, totale_disservizi

        quota_equa = 100 / len(CAPITOLI_DECORO)
        correttivi = {}
        for chiave, peso in peso_per_categoria.items():
            if peso == 0:
                continue
            quota_osservata_pct = round(peso / totale_disservizi * 100, 1)
            scostamento = quota_osservata_pct - quota_equa
            scostamento_limitato = max(-TETTO_CORRETTIVO_DECORO_PUNTI, min(TETTO_CORRETTIVO_DECORO_PUNTI, scostamento))
            correttivi[chiave] = {
                "punti": round(scostamento_limitato, 1),
                "dato_sottostante": (
                    f"Segnalazioni PIT collegate a \"{CAPITOLI_DECORO[chiave]['nome']}\": "
                    f"{', '.join(dettaglio_match[chiave])}, pari al {quota_osservata_pct}% delle {totale_disservizi} "
                    f"segnalazioni di disservizio registrate."
                ),
            }

        return correttivi, totale_disservizi
    except Exception as e:
        print(f"Errore calcolo criticita decoro comune {comune_id}: {e}")
        return {}, 0


def get_incasso_totale_mese(comune_id, anno, mese, valore_siti_helper, calcola_range_mese_fn):
    gettito_mese = get_gettito_mese(comune_id, anno, mese)
    data_inizio, data_fine = calcola_range_mese_fn(anno, mese)
    risultato_siti = valore_siti_helper(comune_id, data_inizio, data_fine)
    valore_siti_lordo_mese = risultato_siti["valore_totale"] if risultato_siti else None
    altre_entrate_mese = get_totale_altre_entrate_mese(comune_id, anno, mese)
    return gettito_mese, valore_siti_lordo_mese, altre_entrate_mese


def get_budget_decoro_mese_totale(incasso_totale_mese, comune_id):
    if not incasso_totale_mese:
        return 0
    quota_risultato = get_quota_decoro(comune_id)
    if "errore" in quota_risultato:
        return 0
    quota_pct = quota_risultato["quota_decoro_pct"]
    return round(incasso_totale_mese * quota_pct / 100, 2)


def get_budget_decoro_mese(comune_id, anno, mese, valore_siti_helper, calcola_range_mese_fn):
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
                    "impossibile calcolare il budget vincolato al decoro urbano."
                ),
            }

        incasso_totale_mese = (gettito_mese or 0) + (valore_siti_lordo_mese or 0) + (altre_entrate_mese or 0)

        quota_risultato = get_quota_decoro(comune_id)
        if "errore" in quota_risultato:
            return {"errore": quota_risultato["errore"]}
        quota_decoro_pct = quota_risultato["quota_decoro_pct"]

        budget_totale_decoro = round(incasso_totale_mese * quota_decoro_pct / 100, 2)

        capitoli_risultato = get_capitoli_decoro(comune_id)
        if "errore" in capitoli_risultato:
            return {"errore": capitoli_risultato["errore"]}
        quote_base = {c["categoria"]: c["quota_base_pct"] for c in capitoli_risultato["capitoli"]}

        correttivi_criticita, totale_disservizi = calcola_criticita_decoro(comune_id)

        pct_grezze = {}
        dettaglio_capitoli = []
        for chiave, quota_base_pct in quote_base.items():
            corr = correttivi_criticita.get(chiave, {"punti": 0, "dato_sottostante": None})
            pct_finale_grezza = max(quota_base_pct + corr["punti"], 0)
            pct_grezze[chiave] = pct_finale_grezza

            dettaglio_capitoli.append({
                "categoria": chiave,
                "nome": CAPITOLI_DECORO[chiave]["nome"],
                "quota_base_pct": quota_base_pct,
                "correttivo_criticita_punti": corr["punti"],
                "correttivo_criticita_motivo": corr["dato_sottostante"],
            })

        totale_grezzo = sum(pct_grezze.values())
        for d in dettaglio_capitoli:
            pct_normalizzata = round(pct_grezze[d["categoria"]] / totale_grezzo * 100, 1) if totale_grezzo > 0 else 0
            d["quota_finale_pct"] = pct_normalizzata
            d["importo_suggerito"] = round(budget_totale_decoro * pct_normalizzata / 100, 2)

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
            "quota_decoro_pct": quota_decoro_pct,
            "budget_totale_decoro": budget_totale_decoro,
            "n_disservizi_pit_considerati": totale_disservizi,
            "distribuzione_capitoli": dettaglio_capitoli,
            "tetto_correttivo_punti": TETTO_CORRETTIVO_DECORO_PUNTI,
            "nota_metodologica": (
                f"Il budget vincolato al decoro urbano è calcolato applicando la quota configurata ({quota_decoro_pct}%) "
                "all'incasso turistico totale del mese (imposta di soggiorno, valore lordo dei siti culturali ed "
                "eventuali altre entrate turistiche aggiuntive definite dal comune, eventi esclusi). Questa cifra "
                "viene riservata prima che il resto confluisca nel calcolo del residuo della Compensazione "
                "Territoriale: non è una cifra aggiuntiva a quella suggerita altrove per manutenzione urbana e "
                "aree verdi, la sostituisce con un vincolo matematico garantito. La distribuzione tra i 6 capitoli "
                "parte dalle quote base decise dal comune, corrette da un segnale motivato basato sulle "
                f"segnalazioni PIT, con un tetto di {TETTO_CORRETTIVO_DECORO_PUNTI} punti percentuali."
            ),
        }
    except Exception as e:
        print(f"Errore budget decoro comune {comune_id}: {e}")
        return {"errore": str(e)}


def registra_versamento_mese_decoro(comune_id, anno, mese, valore_siti_helper, calcola_range_mese_fn):
    try:
        anteprima = get_budget_decoro_mese(comune_id, anno, mese, valore_siti_helper, calcola_range_mese_fn)
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
            "importo_versato": anteprima["budget_totale_decoro"],
            "da_soggiorno": anteprima["gettito_soggiorno_mese"],
            "da_siti": anteprima["valore_siti_culturali_lordo_mese"],
            "da_altre_entrate": anteprima.get("altre_entrate_turistiche_mese"),
        }
        supabase.table("decoro_urbano_versamenti").upsert(
            record, on_conflict="piano_id,anno,mese"
        ).execute()

        return {"status": "registrato", "importo_versato": anteprima["budget_totale_decoro"]}
    except Exception as e:
        print(f"Errore registrazione versamento decoro comune {comune_id}: {e}")
        return {"errore": str(e)}


def get_saldo_decoro(comune_id):
    try:
        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id)

        versamenti_resp = supabase.table("decoro_urbano_versamenti").select("*") \
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
                "mese è un'azione volontaria: finché non viene confermato, il mese non entra nel saldo "
                "accumulato, anche se la cifra è già visibile in anteprima."
            ),
        }
    except Exception as e:
        print(f"Errore saldo decoro comune {comune_id}: {e}")
        return {"errore": str(e)}