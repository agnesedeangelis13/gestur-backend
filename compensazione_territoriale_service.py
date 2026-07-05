import re
from datetime import datetime
from supabase import create_client
import os
from dotenv import load_dotenv
from imposta_soggiorno_service import (
    CATEGORIE_DESTINAZIONE_SOGGIORNO,
    ottieni_o_crea_piano_sviluppo_locale_attivo,
)

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

TETTO_CORRETTIVO_PUNTI = 15.0
SOGLIA_MIN_DISSERVIZI_CRITICITA = 3

STOPWORD_KEYWORDS = {"per", "dei", "delle", "della", "del", "e", "di", "da", "al", "alla", "locale", "locali"}


def normalizza_parola(testo):
    testo = testo.lower()
    testo = testo.replace("à", "a").replace("è", "e").replace("é", "e").replace("ì", "i").replace("ò", "o").replace("ù", "u")
    testo = re.sub(r"[^a-z0-9\s]", " ", testo)
    return [p for p in testo.split() if len(p) > 3 and p not in STOPWORD_KEYWORDS]


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

        n_categorie = len(CATEGORIE_DESTINAZIONE_SOGGIORNO)
        quota_default = round(100 / n_categorie, 1)

        capitoli = []
        for chiave, info in CATEGORIE_DESTINAZIONE_SOGGIORNO.items():
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
                "di capitoli), da sostituire con le priorità reali dell'amministrazione."
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

        if categoria not in CATEGORIE_DESTINAZIONE_SOGGIORNO:
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


def calcola_criticita_per_categoria(comune_id):
    try:
        richieste_resp = supabase.table("richieste_pit").select("categorie_disservizio") \
            .eq("comune_id", comune_id).execute()
        richieste = richieste_resp.data or []

        disservizi = [r["categorie_disservizio"] for r in richieste if r.get("categorie_disservizio") and r["categorie_disservizio"].strip().lower() != "altro"]
        totale_disservizi = len(disservizi)

        if totale_disservizi < SOGLIA_MIN_DISSERVIZI_CRITICITA:
            return {}, totale_disservizi

        conteggio_per_valore = {}
        for d in disservizi:
            conteggio_per_valore[d] = conteggio_per_valore.get(d, 0) + 1

        parole_categoria = {}
        for chiave, info in CATEGORIE_DESTINAZIONE_SOGGIORNO.items():
            parole_categoria[chiave] = set(normalizza_parola(info["nome"]))

        peso_per_categoria = {chiave: 0 for chiave in CATEGORIE_DESTINAZIONE_SOGGIORNO}
        dettaglio_match = {chiave: [] for chiave in CATEGORIE_DESTINAZIONE_SOGGIORNO}

        for valore_disservizio, conteggio in conteggio_per_valore.items():
            parole_disservizio = set(normalizza_parola(valore_disservizio))
            for chiave, parole_cat in parole_categoria.items():
                if parole_disservizio & parole_cat:
                    peso_per_categoria[chiave] += conteggio
                    dettaglio_match[chiave].append(f"{valore_disservizio} ({conteggio})")

        totale_match = sum(peso_per_categoria.values())
        if totale_match == 0:
            return {}, totale_disservizi

        quota_equa = 100 / len(CATEGORIE_DESTINAZIONE_SOGGIORNO)
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
                    f"Segnalazioni PIT collegate a \"{CATEGORIE_DESTINAZIONE_SOGGIORNO[chiave]['nome']}\": "
                    f"{', '.join(dettaglio_match[chiave])}, pari al {quota_osservata_pct}% dei {totale_disservizi} "
                    f"disservizi totali registrati."
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
            .eq("piano_id", piano["id"]).eq("attivo", True).execute()
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
    valore_siti_mese = risultato_siti["valore_totale"] if risultato_siti else None

    allocazioni_resp = supabase.table("allocazioni_soggiorno").select("importo_allocato") \
        .eq("piano_id", piano["id"]).eq("anno", anno).eq("mese", mese).eq("attivo", True).execute()
    gia_allocato_mese = sum(a["importo_allocato"] or 0 for a in (allocazioni_resp.data or []))

    ricchezza_totale = (gettito_mese or 0) + (valore_siti_mese or 0)

    return {
        "piano_id": piano["id"],
        "gettito_soggiorno_mese": gettito_mese,
        "valore_siti_culturali_mese": valore_siti_mese,
        "ricchezza_totale_mese": round(ricchezza_totale, 2) if (gettito_mese is not None or valore_siti_mese is not None) else None,
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
                "nome": CATEGORIE_DESTINAZIONE_SOGGIORNO[chiave]["nome"],
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
                "La ricchezza estratta somma il gettito dell'imposta di soggiorno del mese e il valore economico "
                "stimato dei siti culturali nello stesso mese (biglietteria, bookshop, ristorazione collegati alla "
                "visita); gli eventi locali non sono inclusi. Il residuo da distribuire è la ricchezza del mese "
                "non ancora allocata tramite il modulo Imposta di Soggiorno. La distribuzione suggerita parte dalle "
                "quote base decise dal comune, corrette da due segnali motivati e limitati a un massimo di "
                f"{TETTO_CORRETTIVO_PUNTI} punti percentuali ciascuno: le criticità segnalate al Punto Informativo "
                "Turistico, e lo scostamento tra quanto storicamente allocato e la quota base prevista per ciascun "
                "capitolo. Il suggerimento non è vincolante: resta una proposta motivata, non una decisione automatica."
            ),
        }
    except Exception as e:
        print(f"Errore suggerimento distribuzione comune {comune_id}: {e}")
        return {"errore": str(e)}