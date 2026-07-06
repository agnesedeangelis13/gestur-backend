from datetime import datetime
from supabase import create_client
import os
from dotenv import load_dotenv
from imposta_soggiorno_service import (
    CATEGORIE_DESTINAZIONE_SOGGIORNO,
    ottieni_o_crea_piano_sviluppo_locale_attivo,
)
from qualita_esperienza_service import get_budget_qoe_mese_totale, get_budget_qoe_mese
from entrate_aggiuntive_service import get_totale_altre_entrate_mese
from decoro_urbano_service import get_budget_decoro_mese_totale, get_budget_decoro_mese

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

TETTO_CORRETTIVO_PUNTI = 15.0
SOGLIA_MIN_DISSERVIZI_CRITICITA = 3

CAPITOLI_ESCLUSI_COMPENSAZIONE = {"aree_verdi", "manutenzione_urbana"}
CATEGORIE_ATTIVE_COMPENSAZIONE = {
    k: v for k, v in CATEGORIE_DESTINAZIONE_SOGGIORNO.items() if k not in CAPITOLI_ESCLUSI_COMPENSAZIONE
}


def calcola_range_mese_locale(anno, mese):
    data_inizio = f"{anno}-{mese:02d}-01"
    if mese == 12:
        ultimo_giorno = datetime(anno + 1, 1, 1)
    else:
        ultimo_giorno = datetime(anno, mese + 1, 1)
    from datetime import timedelta
    ultimo_giorno = ultimo_giorno - timedelta(days=1)
    return data_inizio, ultimo_giorno.strftime("%Y-%m-%d")


def get_quote_capitoli(comune_id):
    try:
        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id)

        config_resp = supabase.table("categorie_destinazione_config").select("*") \
            .eq("piano_id", piano["id"]).execute()
        config_esistente = {c["categoria"]: c for c in (config_resp.data or [])}

        n_categorie = len(CATEGORIE_ATTIVE_COMPENSAZIONE)
        quota_default = round(100 / n_categorie, 1)

        capitoli = []
        for chiave, info in CATEGORIE_ATTIVE_COMPENSAZIONE.items():
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
            "nota_metodologica": (
                "Le quote base sono la ripartizione politica di partenza, decisa dal comune. Se non ancora "
                "configurate, il sistema mostra una ripartizione equa provvisoria (100% diviso per il numero "
                "di capitoli), da sostituire con le priorità reali dell'amministrazione. Aree verdi e "
                "Manutenzione urbana non compaiono qui: hanno un finanziamento vincolato garantito dal modulo "
                "Decoro Urbano e Vivibilità."
            ),
        }
    except Exception as e:
        print(f"Errore get quote capitoli comune {comune_id}: {e}")
        return {"errore": str(e)}


def aggiorna_quota_capitolo(payload):
    try:
        comune_id_str = payload.get("comune_id")
        categoria = payload.get("categoria")
        quota_base_pct = payload.get("quota_base_pct")

        if not comune_id_str or not categoria or quota_base_pct is None:
            return {"errore": "comune_id, categoria e quota_base_pct sono obbligatori"}

        if categoria in CAPITOLI_ESCLUSI_COMPENSAZIONE:
            return {"errore": "Questa categoria è ora gestita dal modulo Decoro Urbano e Vivibilità"}

        if categoria not in CATEGORIE_ATTIVE_COMPENSAZIONE:
            return {"errore": "Categoria non valida"}

        if quota_base_pct < 0 or quota_base_pct > 100:
            return {"errore": "quota_base_pct deve essere tra 0 e 100"}

        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id_str)

        esistente_resp = supabase.table("categorie_destinazione_config").select("*") \
            .eq("piano_id", piano["id"]).eq("categoria", categoria).execute()

        if esistente_resp.data:
            supabase.table("categorie_destinazione_config").update({
                "quota_base_pct": quota_base_pct,
                "aggiornato_il": datetime.now().isoformat(),
            }).eq("piano_id", piano["id"]).eq("categoria", categoria).execute()
        else:
            supabase.table("categorie_destinazione_config").insert({
                "piano_id": piano["id"],
                "comune_id": comune_id_str,
                "categoria": categoria,
                "quota_base_pct": quota_base_pct,
            }).execute()

        return {"status": "salvato"}
    except Exception as e:
        print(f"Errore aggiornamento quota capitolo: {e}")
        return {"errore": str(e)}


MAPPATURA_DISSERVIZIO_CAPITOLO = {
    "Trasporti": ["trasporto_pubblico"],
    "Musei e siti culturali": ["cultura_biblioteche"],
    "Wi-Fi e connettività": ["servizi_digitali"],
    "Sicurezza": ["sicurezza"],
}


def calcola_criticita_per_categoria(comune_id):
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
        if totale_disservizi < SOGLIA_MIN_DISSERVIZI_CRITICITA:
            return {}, totale_disservizi

        conteggio_per_tag = {}
        for tag in tag_disservizio:
            conteggio_per_tag[tag] = conteggio_per_tag.get(tag, 0) + 1

        peso_per_categoria = {chiave: 0 for chiave in CATEGORIE_ATTIVE_COMPENSAZIONE}
        dettaglio_match = {chiave: [] for chiave in CATEGORIE_ATTIVE_COMPENSAZIONE}

        for tag, conteggio in conteggio_per_tag.items():
            capitoli_associati = MAPPATURA_DISSERVIZIO_CAPITOLO.get(tag)
            if not capitoli_associati:
                continue
            for chiave in capitoli_associati:
                peso_per_categoria[chiave] += conteggio
                dettaglio_match[chiave].append(f"{tag} ({conteggio})")

        totale_match = sum(peso_per_categoria.values())
        if totale_match == 0:
            return {}, totale_disservizi

        quota_equa = 100 / len(CATEGORIE_ATTIVE_COMPENSAZIONE)
        correttivi = {}
        for chiave, peso in peso_per_categoria.items():
            if peso == 0:
                continue
            quota_osservata_pct = round(peso / totale_disservizi * 100, 1)
            scostamento = quota_osservata_pct - quota_equa
            scostamento_limitato = max(-TETTO_CORRETTIVO_PUNTI, min(TETTO_CORRETTIVO_PUNTI, scostamento))
            correttivi[chiave] = {
                "punti": round(scostamento_limitato, 1),
                "dato_sottostante": (
                    f"Segnalazioni PIT collegate a \"{CATEGORIE_ATTIVE_COMPENSAZIONE[chiave]['nome']}\": "
                    f"{', '.join(dettaglio_match[chiave])}, pari al {quota_osservata_pct}% delle {totale_disservizi} "
                    f"segnalazioni di disservizio registrate (una richiesta puo contenere piu di una segnalazione)."
                ),
            }

        return correttivi, totale_disservizi
    except Exception as e:
        print(f"Errore calcolo criticita comune {comune_id}: {e}")
        return {}, 0


def calcola_correttivo_storico_per_categoria(comune_id, quote_base):
    try:
        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id)

        allocazioni_resp = supabase.table("allocazioni_soggiorno").select("categoria, importo_allocato") \
            .eq("piano_id", piano["id"]).eq("attivo", True).in_("categoria", list(CATEGORIE_ATTIVE_COMPENSAZIONE.keys())).execute()
        allocazioni = allocazioni_resp.data or []

        totale_allocato_storico = sum(a["importo_allocato"] or 0 for a in allocazioni)
        if totale_allocato_storico <= 0:
            return {}

        allocato_per_categoria = {}
        for a in allocazioni:
            cat = a["categoria"]
            allocato_per_categoria[cat] = allocato_per_categoria.get(cat, 0) + (a["importo_allocato"] or 0)

        correttivi = {}
        for chiave, quota_base_pct in quote_base.items():
            allocato_cat = allocato_per_categoria.get(chiave, 0)
            quota_storica_pct = round(allocato_cat / totale_allocato_storico * 100, 1)
            scostamento = quota_base_pct - quota_storica_pct
            if abs(scostamento) < 1:
                continue
            scostamento_limitato = max(-TETTO_CORRETTIVO_PUNTI, min(TETTO_CORRETTIVO_PUNTI, scostamento))
            correttivi[chiave] = {
                "punti": round(scostamento_limitato, 1),
                "dato_sottostante": (
                    f"Storicamente questo capitolo ha ricevuto il {quota_storica_pct}% delle allocazioni totali "
                    f"registrate (contro una quota base del {quota_base_pct}%): il correttivo riequilibra verso "
                    f"la quota prevista."
                ),
            }
        return correttivi
    except Exception as e:
        print(f"Errore correttivo storico comune {comune_id}: {e}")
        return {}


def get_ricchezza_estratta_mese(comune_id, anno, mese, valore_siti_helper):
    piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id)

    gettito_resp = supabase.table("gettito_soggiorno").select("importo_incassato") \
        .eq("piano_id", piano["id"]).eq("anno", anno).eq("mese", mese).execute()
    gettito_mese = gettito_resp.data[0]["importo_incassato"] if gettito_resp.data else None

    data_inizio, data_fine = calcola_range_mese_locale(anno, mese)
    risultato_siti = valore_siti_helper(comune_id, data_inizio, data_fine)
    valore_siti_lordo_mese = risultato_siti["valore_totale"] if risultato_siti else None

    altre_entrate_mese = get_totale_altre_entrate_mese(comune_id, anno, mese)

    budget_qoe_mese = get_budget_qoe_mese_totale(comune_id, valore_siti_lordo_mese) if valore_siti_lordo_mese else 0
    valore_siti_netto_mese = (valore_siti_lordo_mese - budget_qoe_mese) if valore_siti_lordo_mese is not None else None

    dati_disponibili = gettito_mese is not None or valore_siti_lordo_mese is not None or altre_entrate_mese is not None
    incasso_totale_lordo = (gettito_mese or 0) + (valore_siti_lordo_mese or 0) + (altre_entrate_mese or 0) if dati_disponibili else None
    budget_decoro_mese = get_budget_decoro_mese_totale(incasso_totale_lordo, comune_id) if incasso_totale_lordo else 0

    allocazioni_resp = supabase.table("allocazioni_soggiorno").select("importo_allocato") \
        .eq("piano_id", piano["id"]).eq("anno", anno).eq("mese", mese).eq("attivo", True).execute()
    gia_allocato_mese = sum(a["importo_allocato"] or 0 for a in (allocazioni_resp.data or []))

    ricchezza_totale = (gettito_mese or 0) + (valore_siti_netto_mese or 0) + (altre_entrate_mese or 0) - budget_decoro_mese

    return {
        "piano_id": piano["id"],
        "gettito_soggiorno_mese": gettito_mese,
        "valore_siti_culturali_lordo_mese": valore_siti_lordo_mese,
        "altre_entrate_turistiche_mese": altre_entrate_mese,
        "budget_qoe_riservato_mese": budget_qoe_mese,
        "budget_decoro_riservato_mese": budget_decoro_mese,
        "valore_siti_culturali_mese": valore_siti_netto_mese,
        "ricchezza_totale_mese": round(ricchezza_totale, 2) if dati_disponibili else None,
        "gia_allocato_mese": round(gia_allocato_mese, 2),
        "residuo_da_distribuire": round(max(ricchezza_totale - gia_allocato_mese, 0), 2),
        "allocato_supera_ricchezza": gia_allocato_mese > ricchezza_totale,
    }


def get_suggerimento_distribuzione(comune_id, anno, mese, valore_siti_helper):
    try:
        ricchezza = get_ricchezza_estratta_mese(comune_id, anno, mese, valore_siti_helper)

        if ricchezza["gettito_soggiorno_mese"] is None and ricchezza["valore_siti_culturali_mese"] is None:
            return {
                "comune_id": comune_id,
                "anno": anno,
                "mese": mese,
                "dati_sufficienti": False,
                "messaggio": (
                    "Nessun gettito di imposta di soggiorno registrato per questo mese e nessun dato di presenze "
                    "sufficiente per stimare il valore dei siti culturali: impossibile calcolare la ricchezza "
                    "estratta dal turismo per questo periodo."
                ),
            }

        quote_risultato = get_quote_capitoli(comune_id)
        if "errore" in quote_risultato:
            return {"errore": quote_risultato["errore"]}

        quote_base = {c["categoria"]: c["quota_base_pct"] for c in quote_risultato["capitoli"]}

        correttivi_criticita, totale_disservizi = calcola_criticita_per_categoria(comune_id)
        correttivi_storico = calcola_correttivo_storico_per_categoria(comune_id, quote_base)

        pct_grezze = {}
        dettaglio_capitoli = []
        for chiave, quota_base_pct in quote_base.items():
            corr_crit = correttivi_criticita.get(chiave, {"punti": 0, "dato_sottostante": None})
            corr_stor = correttivi_storico.get(chiave, {"punti": 0, "dato_sottostante": None})

            pct_finale_grezza = quota_base_pct + corr_crit["punti"] + corr_stor["punti"]
            pct_grezze[chiave] = max(pct_finale_grezza, 0)

            dettaglio_capitoli.append({
                "categoria": chiave,
                "nome": CATEGORIE_ATTIVE_COMPENSAZIONE[chiave]["nome"],
                "quota_base_pct": quota_base_pct,
                "correttivo_criticita_punti": corr_crit["punti"],
                "correttivo_criticita_motivo": corr_crit["dato_sottostante"],
                "correttivo_storico_punti": corr_stor["punti"],
                "correttivo_storico_motivo": corr_stor["dato_sottostante"],
            })

        totale_grezzo = sum(pct_grezze.values())
        residuo = ricchezza["residuo_da_distribuire"]

        for d in dettaglio_capitoli:
            pct_normalizzata = round(pct_grezze[d["categoria"]] / totale_grezzo * 100, 1) if totale_grezzo > 0 else 0
            d["quota_finale_pct"] = pct_normalizzata
            d["importo_suggerito"] = round(residuo * pct_normalizzata / 100, 2) if residuo else 0

        dettaglio_capitoli.sort(key=lambda x: x["quota_finale_pct"], reverse=True)

        return {
            "comune_id": comune_id,
            "anno": anno,
            "mese": mese,
            "dati_sufficienti": True,
            "ricchezza_estratta": ricchezza,
            "n_disservizi_pit_considerati": totale_disservizi,
            "distribuzione_suggerita": dettaglio_capitoli,
            "tetto_correttivo_punti": TETTO_CORRETTIVO_PUNTI,
            "nota_metodologica": (
                "La ricchezza estratta somma il gettito dell'imposta di soggiorno del mese, il valore economico "
                "netto dei siti culturali nello stesso mese (biglietteria, bookshop, ristorazione collegati alla "
                "visita, al netto di quanto già riservato al reinvestimento in Qualità dell'Esperienza e al "
                "Decoro Urbano e Vivibilità) e le eventuali altre entrate turistiche aggiuntive definite dal "
                "comune (es. parcheggi, ticket bus); gli eventi locali non sono inclusi. Il residuo da distribuire "
                "è la ricchezza del mese non ancora allocata tramite il modulo Imposta di Soggiorno. La "
                "distribuzione suggerita riguarda i 6 capitoli non vincolati altrove, e parte dalle "
                "quote base decise dal comune, corrette da due segnali motivati e limitati a un massimo di "
                f"{TETTO_CORRETTIVO_PUNTI} punti percentuali ciascuno: le criticità segnalate al Punto Informativo "
                "Turistico, e lo scostamento tra quanto storicamente allocato e la quota base prevista per ciascun "
                "capitolo. Il suggerimento non è vincolante: resta una proposta motivata, non una decisione automatica."
            ),
        }
    except Exception as e:
        print(f"Errore suggerimento distribuzione comune {comune_id}: {e}")
        return {"errore": str(e)}


def get_matrice_redistribuzione(comune_id, anno, mese, valore_siti_helper, calcola_range_mese_fn):
    try:
        suggerimento = get_suggerimento_distribuzione(comune_id, anno, mese, valore_siti_helper)
        if "errore" in suggerimento:
            return {"errore": suggerimento["errore"]}
        if not suggerimento.get("dati_sufficienti"):
            return {
                "comune_id": comune_id,
                "anno": anno,
                "mese": mese,
                "dati_sufficienti": False,
                "messaggio": suggerimento.get("messaggio"),
            }

        budget_qoe = get_budget_qoe_mese(comune_id, anno, mese, valore_siti_helper, calcola_range_mese_fn)
        budget_decoro = get_budget_decoro_mese(comune_id, anno, mese, valore_siti_helper, calcola_range_mese_fn)

        ricchezza = suggerimento["ricchezza_estratta"]
        incasso_totale = (
            (ricchezza["gettito_soggiorno_mese"] or 0)
            + (ricchezza["valore_siti_culturali_lordo_mese"] or 0)
            + (ricchezza.get("altre_entrate_turistiche_mese") or 0)
        )

        segmenti = []
        for c in suggerimento["distribuzione_suggerita"]:
            if c["importo_suggerito"] > 0:
                segmenti.append({
                    "nome": c["nome"],
                    "importo": c["importo_suggerito"],
                    "dominio": "welfare_cittadino",
                })

        if budget_qoe.get("dati_sufficienti"):
            for c in budget_qoe["distribuzione_capitoli"]:
                if c["importo_suggerito"] > 0:
                    segmenti.append({
                        "nome": c["nome"],
                        "importo": c["importo_suggerito"],
                        "dominio": "welfare_turista",
                    })

        if budget_decoro.get("dati_sufficienti"):
            for c in budget_decoro["distribuzione_capitoli"]:
                if c["importo_suggerito"] > 0:
                    segmenti.append({
                        "nome": c["nome"],
                        "importo": c["importo_suggerito"],
                        "dominio": "decoro_urbano",
                    })

        if ricchezza["gia_allocato_mese"] > 0:
            segmenti.append({
                "nome": "Già allocato (Imposta di Soggiorno)",
                "importo": ricchezza["gia_allocato_mese"],
                "dominio": "gia_allocato",
            })

        segmenti.sort(key=lambda x: x["importo"], reverse=True)

        return {
            "comune_id": comune_id,
            "anno": anno,
            "mese": mese,
            "dati_sufficienti": True,
            "incasso_totale": round(incasso_totale, 2),
            "gettito_soggiorno_mese": ricchezza["gettito_soggiorno_mese"],
            "valore_siti_culturali_lordo_mese": ricchezza["valore_siti_culturali_lordo_mese"],
            "altre_entrate_turistiche_mese": ricchezza.get("altre_entrate_turistiche_mese"),
            "budget_qoe_totale": ricchezza["budget_qoe_riservato_mese"],
            "budget_decoro_totale": ricchezza["budget_decoro_riservato_mese"],
            "residuo_cittadino_totale": ricchezza["residuo_da_distribuire"],
            "gia_allocato_mese": ricchezza["gia_allocato_mese"],
            "segmenti": segmenti,
            "nota_metodologica": (
                "Questa matrice non introduce nuovi calcoli: combina in un'unica vista i risultati già calcolati "
                "dai moduli Compensazione Territoriale (welfare cittadino), Qualità dell'Esperienza (welfare del "
                "turista) e Decoro Urbano e Vivibilità (manutenzione e decoro), a partire dallo stesso incasso "
                "totale (imposta di soggiorno, valore lordo dei siti culturali ed eventuali altre entrate "
                "turistiche aggiuntive definite dal comune, eventi esclusi). Ogni segmento del grafico rimanda "
                "esattamente alla stessa cifra mostrata nel modulo di origine."
            ),
        }
    except Exception as e:
        print(f"Errore matrice redistribuzione comune {comune_id}: {e}")
        return {"errore": str(e)}