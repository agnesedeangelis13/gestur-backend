from datetime import datetime
from supabase import create_client
import os
from dotenv import load_dotenv
from imposta_soggiorno_service import ottieni_o_crea_piano_sviluppo_locale_attivo
from decoro_urbano_service import CAPITOLI_DECORO, get_saldo_decoro
from fondo_sostenibilita_service import CAPITOLI_SOSTENIBILITA, get_saldo_fondo, get_capitoli_sostenibilita

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

FONDI_VALIDI = {
    "decoro_urbano": {"nome": "Decoro Urbano e Vivibilità", "capitoli": CAPITOLI_DECORO},
    "fondo_sostenibilita": {"nome": "Fondo di Rigenerazione Sostenibile", "capitoli": CAPITOLI_SOSTENIBILITA},
}

STATI_VALIDI = ("proposto", "approvato", "completato", "scartato")


def get_saldo_disponibile(comune_id, fondo_origine):
    if fondo_origine == "decoro_urbano":
        saldo_risultato = get_saldo_decoro(comune_id)
    else:
        saldo_risultato = get_saldo_fondo(comune_id)

    if "errore" in saldo_risultato:
        return None, None, saldo_risultato["errore"]

    saldo_versato = saldo_risultato["saldo_totale"]
    n_mesi_registrati = saldo_risultato["n_mesi_registrati"]
    versamento_medio_mensile = round(saldo_versato / n_mesi_registrati, 2) if n_mesi_registrati > 0 else None

    piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id)
    progetti_resp = supabase.table("progetti_investimento").select("costo_stimato") \
        .eq("piano_id", piano["id"]).eq("fondo_origine", fondo_origine).in_("stato", ["approvato", "completato"]).execute()
    speso = sum(p["costo_stimato"] or 0 for p in (progetti_resp.data or []))

    saldo_disponibile = round(saldo_versato - speso, 2)
    return saldo_disponibile, versamento_medio_mensile, None


def arricchisci_progetto(progetto, saldo_disponibile, versamento_medio_mensile):
    fondo_info = FONDI_VALIDI[progetto["fondo_origine"]]
    costo = progetto["costo_stimato"]

    if progetto["stato"] in ("approvato", "completato") and progetto.get("copertura_approvazione_pct") is not None:
        copertura_pct = progetto["copertura_approvazione_pct"]
    else:
        copertura_pct = round(min(max(saldo_disponibile, 0) / costo * 100, 100), 1) if costo > 0 else None

    tempo_accumulo_mesi = round(costo / versamento_medio_mensile, 1) if versamento_medio_mensile and versamento_medio_mensile > 0 else None

    unita_stimata = None
    impatto_stimato = None
    unita_misura = None
    unita_misura_impatto = None

    if progetto["fondo_origine"] == "fondo_sostenibilita" and progetto.get("categoria"):
        capitoli_risultato = get_capitoli_sostenibilita(progetto["comune_id"])
        if "errore" not in capitoli_risultato:
            capitolo_config = next((c for c in capitoli_risultato["capitoli"] if c["categoria"] == progetto["categoria"]), None)
            if capitolo_config and capitolo_config.get("fattore_conversione"):
                unita_stimata = round(costo / capitolo_config["fattore_conversione"], 1)
                unita_misura = capitolo_config["unita_misura"]
                if capitolo_config.get("coefficiente_impatto"):
                    impatto_stimato = round(unita_stimata * capitolo_config["coefficiente_impatto"], 1)
                    unita_misura_impatto = capitolo_config.get("unita_misura_impatto")

    return {
        **progetto,
        "fondo_nome": fondo_info["nome"],
        "categoria_nome": fondo_info["capitoli"].get(progetto["categoria"], {}).get("nome") if progetto.get("categoria") else None,
        "copertura_finanziaria_pct": copertura_pct,
        "tempo_accumulo_mesi": tempo_accumulo_mesi,
        "unita_stimata": unita_stimata,
        "unita_misura": unita_misura,
        "impatto_stimato": impatto_stimato,
        "unita_misura_impatto": unita_misura_impatto,
    }


def crea_progetto(payload):
    try:
        comune_id_str = payload.get("comune_id")
        fondo_origine = payload.get("fondo_origine")
        categoria = payload.get("categoria")
        titolo = payload.get("titolo")
        descrizione = payload.get("descrizione")
        costo_stimato = payload.get("costo_stimato")

        if not comune_id_str or not fondo_origine or not titolo or costo_stimato is None:
            return {"errore": "comune_id, fondo_origine, titolo e costo_stimato sono obbligatori"}

        if fondo_origine not in FONDI_VALIDI:
            return {"errore": "fondo_origine non valido"}

        if costo_stimato <= 0:
            return {"errore": "costo_stimato deve essere maggiore di zero"}

        if categoria and categoria not in FONDI_VALIDI[fondo_origine]["capitoli"]:
            return {"errore": "categoria non valida per questo fondo"}

        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id_str)

        record = {
            "piano_id": piano["id"],
            "comune_id": comune_id_str,
            "fondo_origine": fondo_origine,
            "categoria": categoria,
            "titolo": titolo,
            "descrizione": descrizione,
            "costo_stimato": costo_stimato,
            "stato": "proposto",
        }
        creato_resp = supabase.table("progetti_investimento").insert(record).execute()

        return {"status": "salvato", "progetto": creato_resp.data[0] if creato_resp.data else None}
    except Exception as e:
        print(f"Errore creazione progetto: {e}")
        return {"errore": str(e)}


def get_progetti(comune_id, fondo_origine=None, stato=None):
    try:
        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id)

        query = supabase.table("progetti_investimento").select("*").eq("piano_id", piano["id"])
        if fondo_origine:
            if fondo_origine not in FONDI_VALIDI:
                return {"errore": "fondo_origine non valido"}
            query = query.eq("fondo_origine", fondo_origine)
        if stato:
            if stato not in STATI_VALIDI:
                return {"errore": "stato non valido"}
            query = query.eq("stato", stato)
        progetti_resp = query.order("creato_il", desc=True).execute()
        progetti = progetti_resp.data or []

        cache_saldo = {}
        progetti_arricchiti = []
        for p in progetti:
            fondo = p["fondo_origine"]
            if fondo not in cache_saldo:
                saldo_disp, versamento_medio, errore_saldo = get_saldo_disponibile(comune_id, fondo)
                cache_saldo[fondo] = (saldo_disp, versamento_medio, errore_saldo)
            saldo_disp, versamento_medio, errore_saldo = cache_saldo[fondo]
            if errore_saldo:
                progetti_arricchiti.append({**p, "fondo_nome": FONDI_VALIDI[fondo]["nome"], "errore_calcolo": errore_saldo})
            else:
                progetti_arricchiti.append(arricchisci_progetto(p, saldo_disp, versamento_medio))

        riepilogo_fondi = {}
        for fondo, (saldo_disp, versamento_medio, errore_saldo) in cache_saldo.items():
            riepilogo_fondi[fondo] = {
                "nome": FONDI_VALIDI[fondo]["nome"],
                "saldo_disponibile": saldo_disp,
                "versamento_medio_mensile": versamento_medio,
            }

        return {
            "piano_id": piano["id"],
            "comune_id": comune_id,
            "progetti": progetti_arricchiti,
            "n_totale": len(progetti_arricchiti),
            "riepilogo_fondi": riepilogo_fondi,
            "nota_metodologica": (
                "La copertura finanziaria confronta il costo stimato con il saldo disponibile del fondo di "
                "provenienza (versamenti registrati meno progetti già approvati). Il tempo di accumulo necessario "
                "è il costo diviso per il versamento medio mensile storico: non è un ritorno economico, è quanto "
                "tempo servirebbe per accumulare la cifra al ritmo attuale. Le unità e l'impatto ambientale stimato "
                "compaiono solo per progetti del Fondo Sostenibilità su capitoli con un coefficiente configurato "
                "dal comune."
            ),
        }
    except Exception as e:
        print(f"Errore get progetti comune {comune_id}: {e}")
        return {"errore": str(e)}


def approva_progetto(progetto_id):
    try:
        progetto_resp = supabase.table("progetti_investimento").select("*").eq("id", progetto_id).single().execute()
        progetto = progetto_resp.data
        if not progetto:
            return {"errore": "Progetto non trovato"}

        if progetto["stato"] != "proposto":
            return {"errore": f"Il progetto è già in stato \"{progetto['stato']}\", non può essere approvato di nuovo"}

        saldo_disp, versamento_medio, errore_saldo = get_saldo_disponibile(progetto["comune_id"], progetto["fondo_origine"])
        if errore_saldo:
            return {"errore": errore_saldo}

        copertura_pct = round(min(max(saldo_disp, 0) / progetto["costo_stimato"] * 100, 100), 1) if progetto["costo_stimato"] > 0 else 0

        supabase.table("progetti_investimento").update({
            "stato": "approvato",
            "data_approvazione": datetime.now().isoformat(),
            "copertura_approvazione_pct": copertura_pct,
        }).eq("id", progetto_id).execute()

        avviso = None
        if copertura_pct < 100:
            avviso = (
                f"Attenzione: il saldo disponibile copre solo il {copertura_pct}% del costo stimato. "
                "Il progetto è stato comunque approvato: la differenza andrà coperta con altre risorse del comune."
            )

        return {"status": "approvato", "copertura_finanziaria_pct": copertura_pct, "avviso": avviso}
    except Exception as e:
        print(f"Errore approvazione progetto {progetto_id}: {e}")
        return {"errore": str(e)}


def completa_progetto(progetto_id):
    try:
        progetto_resp = supabase.table("progetti_investimento").select("stato").eq("id", progetto_id).single().execute()
        progetto = progetto_resp.data
        if not progetto:
            return {"errore": "Progetto non trovato"}
        if progetto["stato"] != "approvato":
            return {"errore": "Solo un progetto approvato può essere segnato come completato"}

        supabase.table("progetti_investimento").update({"stato": "completato"}).eq("id", progetto_id).execute()
        return {"status": "completato"}
    except Exception as e:
        print(f"Errore completamento progetto {progetto_id}: {e}")
        return {"errore": str(e)}


def elimina_progetto(progetto_id):
    try:
        progetto_resp = supabase.table("progetti_investimento").select("stato").eq("id", progetto_id).single().execute()
        progetto = progetto_resp.data
        if not progetto:
            return {"errore": "Progetto non trovato"}
        if progetto["stato"] != "proposto":
            return {"errore": "Solo un progetto ancora proposto (non approvato) può essere eliminato"}

        supabase.table("progetti_investimento").update({"stato": "scartato"}).eq("id", progetto_id).execute()
        return {"status": "scartato"}
    except Exception as e:
        print(f"Errore eliminazione progetto {progetto_id}: {e}")
        return {"errore": str(e)}