from datetime import datetime
from supabase import create_client
import os
from dotenv import load_dotenv
from imposta_soggiorno_service import ottieni_o_crea_piano_sviluppo_locale_attivo, CATEGORIE_DESTINAZIONE_SOGGIORNO
from decoro_urbano_service import CAPITOLI_DECORO
from fondo_sostenibilita_service import CAPITOLI_SOSTENIBILITA

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

MASLOW_LIVELLI = {
    "fisiologici": {
        "nome": "Bisogni fisiologici",
        "descrizione": "Aiuti alimentari, contributi per utenze essenziali, buoni spesa.",
        "parole_chiave": ["cibo", "assistenza", "salute", "servizi ospedalieri", "supporto", "equità", "beneficio"],
        "ordine": 1,
    },
    "sicurezza": {
        "nome": "Bisogni di sicurezza",
        "descrizione": "Stabilità, protezione, case popolari, centri di accoglienza, misure di tutela.",
        "parole_chiave": ["sicurezza", "responsabilità", "governo", "verde", "pulizia", "natura", "animali"],
        "ordine": 2,
    },
    "sociali": {
        "nome": "Bisogni sociali",
        "descrizione": "Spazi di aggregazione, centri anziani, ludoteche, partecipazione sociale.",
        "parole_chiave": ["società", "cittadino", "servizi sociali", "anziani", "locale", "comunità", "vita"],
        "ordine": 3,
    },
    "stima": {
        "nome": "Bisogni di stima",
        "descrizione": "Percorsi di inserimento e reinserimento sociale.",
        "parole_chiave": ["disabilità", "lavoratore", "psicologia", "qualità", "equilibrio", "riforma"],
        "ordine": 4,
    },
    "autorealizzazione": {
        "nome": "Bisogni di autorealizzazione",
        "descrizione": "Investimenti in cultura, istruzione, formazione, sport, creatività giovanile.",
        "parole_chiave": ["educazione", "cultura", "felicità", "benessere", "sviluppo", "investimenti", "tempo"],
        "ordine": 5,
    },
}

CAPITOLI_ESCLUSI_COMPENSAZIONE = {"aree_verdi", "manutenzione_urbana"}
CAPITOLI_COMPENSAZIONE_ATTIVI = {
    k: v for k, v in CATEGORIE_DESTINAZIONE_SOGGIORNO.items() if k not in CAPITOLI_ESCLUSI_COMPENSAZIONE
}

MAPPATURA_DEFAULT = {}
for chiave, info in CAPITOLI_COMPENSAZIONE_ATTIVI.items():
    MAPPATURA_DEFAULT[("compensazione", chiave)] = "sociali"
MAPPATURA_DEFAULT[("compensazione", "assistenza_sociale")] = "fisiologici"
MAPPATURA_DEFAULT[("compensazione", "sicurezza")] = "sicurezza"
MAPPATURA_DEFAULT[("compensazione", "cultura_biblioteche")] = "autorealizzazione"

for chiave in CAPITOLI_DECORO:
    MAPPATURA_DEFAULT[("decoro_urbano", chiave)] = "sicurezza"
MAPPATURA_DEFAULT[("decoro_urbano", "arredo_urbano")] = "sociali"

for chiave in CAPITOLI_SOSTENIBILITA:
    MAPPATURA_DEFAULT[("fondo_sostenibilita", chiave)] = "sicurezza"
MAPPATURA_DEFAULT[("fondo_sostenibilita", "mobilita_sostenibile")] = "sociali"
MAPPATURA_DEFAULT[("fondo_sostenibilita", "efficientamento_energetico")] = "fisiologici"
MAPPATURA_DEFAULT[("fondo_sostenibilita", "energie_rinnovabili")] = "fisiologici"
MAPPATURA_DEFAULT[("fondo_sostenibilita", "tutela_biodiversita")] = "autorealizzazione"

NOMI_CAPITOLI = {}
for chiave, info in CAPITOLI_COMPENSAZIONE_ATTIVI.items():
    NOMI_CAPITOLI[("compensazione", chiave)] = info["nome"]
for chiave, info in CAPITOLI_DECORO.items():
    NOMI_CAPITOLI[("decoro_urbano", chiave)] = info["nome"]
for chiave, info in CAPITOLI_SOSTENIBILITA.items():
    NOMI_CAPITOLI[("fondo_sostenibilita", chiave)] = info["nome"]

NOMI_MODULI = {
    "compensazione": "Compensazione Territoriale",
    "decoro_urbano": "Decoro Urbano e Vivibilità",
    "fondo_sostenibilita": "Fondo di Rigenerazione Sostenibile",
}


def get_mappatura_capitoli(comune_id):
    try:
        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id)

        config_resp = supabase.table("maslow_mappatura_capitoli").select("*") \
            .eq("piano_id", piano["id"]).execute()
        config_esistente = {(c["modulo_origine"], c["capitolo"]): c["livello_maslow"] for c in (config_resp.data or [])}

        risultati = []
        for (modulo, capitolo), livello_default in MAPPATURA_DEFAULT.items():
            livello_attuale = config_esistente.get((modulo, capitolo), livello_default)
            risultati.append({
                "modulo_origine": modulo,
                "modulo_nome": NOMI_MODULI[modulo],
                "capitolo": capitolo,
                "capitolo_nome": NOMI_CAPITOLI[(modulo, capitolo)],
                "livello_maslow": livello_attuale,
                "livello_default": livello_default,
                "personalizzato": (modulo, capitolo) in config_esistente,
            })

        risultati.sort(key=lambda x: (x["modulo_origine"], x["capitolo_nome"]))

        return {
            "piano_id": piano["id"],
            "comune_id": comune_id,
            "mappatura": risultati,
            "livelli_disponibili": MASLOW_LIVELLI,
        }
    except Exception as e:
        print(f"Errore get mappatura capitoli comune {comune_id}: {e}")
        return {"errore": str(e)}


def aggiorna_mappatura_capitolo(payload):
    try:
        comune_id_str = payload.get("comune_id")
        modulo_origine = payload.get("modulo_origine")
        capitolo = payload.get("capitolo")
        livello_maslow = payload.get("livello_maslow")

        if not comune_id_str or not modulo_origine or not capitolo or not livello_maslow:
            return {"errore": "comune_id, modulo_origine, capitolo e livello_maslow sono obbligatori"}

        if livello_maslow not in MASLOW_LIVELLI:
            return {"errore": "livello_maslow non valido"}

        if (modulo_origine, capitolo) not in MAPPATURA_DEFAULT:
            return {"errore": "Combinazione modulo_origine/capitolo non valida"}

        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id_str)

        esistente_resp = supabase.table("maslow_mappatura_capitoli").select("id") \
            .eq("piano_id", piano["id"]).eq("modulo_origine", modulo_origine).eq("capitolo", capitolo).execute()

        if esistente_resp.data:
            supabase.table("maslow_mappatura_capitoli").update({
                "livello_maslow": livello_maslow,
                "aggiornato_il": datetime.now().isoformat(),
            }).eq("piano_id", piano["id"]).eq("modulo_origine", modulo_origine).eq("capitolo", capitolo).execute()
        else:
            supabase.table("maslow_mappatura_capitoli").insert({
                "piano_id": piano["id"],
                "comune_id": comune_id_str,
                "modulo_origine": modulo_origine,
                "capitolo": capitolo,
                "livello_maslow": livello_maslow,
            }).execute()

        return {"status": "salvato"}
    except Exception as e:
        print(f"Errore aggiornamento mappatura capitolo: {e}")
        return {"errore": str(e)}


def get_piramide_maslow(comune_id):
    try:
        mappatura_result = get_mappatura_capitoli(comune_id)
        if "errore" in mappatura_result:
            return {"errore": mappatura_result["errore"]}

        piano_id = mappatura_result["piano_id"]
        livello_per_capitolo = {(m["modulo_origine"], m["capitolo"]): m["livello_maslow"] for m in mappatura_result["mappatura"]}

        totali_per_livello = {chiave: 0 for chiave in MASLOW_LIVELLI}
        dettaglio_per_livello = {chiave: [] for chiave in MASLOW_LIVELLI}

        allocazioni_resp = supabase.table("allocazioni_soggiorno").select("categoria, importo_allocato") \
            .eq("piano_id", piano_id).eq("attivo", True).execute()
        allocato_per_categoria = {}
        for a in (allocazioni_resp.data or []):
            cat = a["categoria"]
            allocato_per_categoria[cat] = allocato_per_categoria.get(cat, 0) + (a["importo_allocato"] or 0)

        for categoria, importo in allocato_per_categoria.items():
            chiave_mappa = ("compensazione", categoria)
            livello = livello_per_capitolo.get(chiave_mappa)
            if livello and importo > 0:
                totali_per_livello[livello] += importo
                dettaglio_per_livello[livello].append({
                    "modulo": NOMI_MODULI["compensazione"],
                    "capitolo": NOMI_CAPITOLI.get(chiave_mappa, categoria),
                    "importo": round(importo, 2),
                })

        progetti_resp = supabase.table("progetti_investimento").select("fondo_origine, categoria, costo_stimato") \
            .eq("piano_id", piano_id).in_("stato", ["approvato", "completato"]).execute()
        for p in (progetti_resp.data or []):
            if not p.get("categoria"):
                continue
            chiave_mappa = (p["fondo_origine"], p["categoria"])
            livello = livello_per_capitolo.get(chiave_mappa)
            if livello:
                totali_per_livello[livello] += p["costo_stimato"] or 0
                dettaglio_per_livello[livello].append({
                    "modulo": NOMI_MODULI.get(p["fondo_origine"], p["fondo_origine"]),
                    "capitolo": NOMI_CAPITOLI.get(chiave_mappa, p["categoria"]),
                    "importo": round(p["costo_stimato"] or 0, 2),
                })

        spazi_resp = supabase.table("spazi_civici").select("id").eq("comune_id", comune_id).eq("attivo", True).execute()
        spazio_ids = [s["id"] for s in (spazi_resp.data or [])]
        totale_ore_spazi = 0
        if spazio_ids:
            utilizzi_resp = supabase.table("utilizzi_spazio_civico").select("data_inizio, data_fine") \
                .in_("spazio_id", spazio_ids).execute()
            for u in (utilizzi_resp.data or []):
                inizio = datetime.fromisoformat(u["data_inizio"].replace("Z", "+00:00"))
                fine = datetime.fromisoformat(u["data_fine"].replace("Z", "+00:00"))
                totale_ore_spazi += (fine - inizio).total_seconds() / 3600

        livelli_risultato = []
        totale_generale = sum(totali_per_livello.values())
        for chiave, info in sorted(MASLOW_LIVELLI.items(), key=lambda x: x[1]["ordine"]):
            importo_livello = round(totali_per_livello[chiave], 2)
            quota_pct = round(importo_livello / totale_generale * 100, 1) if totale_generale > 0 else 0
            livelli_risultato.append({
                "livello": chiave,
                "nome": info["nome"],
                "descrizione": info["descrizione"],
                "parole_chiave": info["parole_chiave"],
                "ordine": info["ordine"],
                "importo_totale": importo_livello,
                "quota_pct": quota_pct,
                "dettaglio": dettaglio_per_livello[chiave],
                "area_deficitaria": importo_livello == 0,
            })

        return {
            "comune_id": comune_id,
            "livelli": livelli_risultato,
            "totale_generale": round(totale_generale, 2),
            "totale_ore_spazi_civici": round(totale_ore_spazi, 1),
            "nota_metodologica": (
                "La piramide non introduce nuovi dati: riorganizza in 5 livelli le allocazioni già registrate "
                "nell'Imposta di Soggiorno e i progetti già approvati in Decoro Urbano e Fondo di Rigenerazione "
                "Sostenibile, secondo la mappatura sotto (modificabile). Le ore di apertura degli Spazi Civici sono "
                "mostrate a parte perché espresse in ore, non in euro, e contribuiscono concettualmente ai "
                "Bisogni sociali. Un livello a zero non significa necessariamente un problema: può semplicemente "
                "indicare che, finora, nessuna allocazione reale è stata registrata in quell'area — è un punto di "
                "partenza per la riflessione, non un giudizio automatico."
            ),
        }
    except Exception as e:
        print(f"Errore piramide maslow comune {comune_id}: {e}")
        return {"errore": str(e)}