from datetime import datetime
from supabase import create_client
import os
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

TIPI_UTILIZZO_CIVICO = [
    "Assemblea cittadina",
    "Laboratorio scolastico",
    "Mostra artista residente",
    "Corso o attività formativa",
    "Evento associativo",
    "Altro",
]


def get_spazi_civici(comune_id):
    try:
        spazi_resp = supabase.table("spazi_civici").select("*") \
            .eq("comune_id", comune_id).eq("attivo", True).order("creato_il").execute()
        return {"comune_id": comune_id, "spazi": spazi_resp.data or []}
    except Exception as e:
        print(f"Errore get spazi civici comune {comune_id}: {e}")
        return {"errore": str(e)}


def crea_spazio_civico(payload):
    try:
        comune_id_str = payload.get("comune_id")
        nome_spazio = payload.get("nome_spazio")
        descrizione = payload.get("descrizione")
        indirizzo = payload.get("indirizzo")
        capienza = payload.get("capienza")

        if not comune_id_str or not nome_spazio or not nome_spazio.strip():
            return {"errore": "comune_id e nome_spazio sono obbligatori"}

        record = {
            "comune_id": comune_id_str,
            "nome_spazio": nome_spazio.strip(),
            "descrizione": descrizione,
            "indirizzo": indirizzo,
            "capienza": capienza,
        }
        creato_resp = supabase.table("spazi_civici").insert(record).execute()

        return {"status": "salvato", "spazio": creato_resp.data[0] if creato_resp.data else None}
    except Exception as e:
        print(f"Errore creazione spazio civico: {e}")
        return {"errore": str(e)}


def elimina_spazio_civico(spazio_id):
    try:
        supabase.table("spazi_civici").update({"attivo": False}).eq("id", spazio_id).execute()
        return {"status": "disattivato"}
    except Exception as e:
        print(f"Errore eliminazione spazio civico {spazio_id}: {e}")
        return {"errore": str(e)}


def calcola_ore_utilizzo(data_inizio_str, data_fine_str):
    data_inizio = datetime.fromisoformat(data_inizio_str.replace("Z", "+00:00"))
    data_fine = datetime.fromisoformat(data_fine_str.replace("Z", "+00:00"))
    delta_ore = (data_fine - data_inizio).total_seconds() / 3600
    return round(delta_ore, 1)


def crea_utilizzo_spazio(payload):
    try:
        spazio_id = payload.get("spazio_id")
        comune_id_str = payload.get("comune_id")
        tipo_utilizzo = payload.get("tipo_utilizzo")
        tipo_utilizzo_altro = payload.get("tipo_utilizzo_altro")
        titolo = payload.get("titolo")
        data_inizio = payload.get("data_inizio")
        data_fine = payload.get("data_fine")
        n_partecipanti_stimati = payload.get("n_partecipanti_stimati")
        note = payload.get("note")

        if not spazio_id or not comune_id_str or not tipo_utilizzo or not data_inizio or not data_fine:
            return {"errore": "spazio_id, comune_id, tipo_utilizzo, data_inizio e data_fine sono obbligatori"}

        if tipo_utilizzo not in TIPI_UTILIZZO_CIVICO:
            return {"errore": "tipo_utilizzo non valido"}

        try:
            ore_calcolate = calcola_ore_utilizzo(data_inizio, data_fine)
        except Exception:
            return {"errore": "Formato data_inizio o data_fine non valido"}

        if ore_calcolate <= 0:
            return {"errore": "data_fine deve essere successiva a data_inizio"}

        record = {
            "spazio_id": spazio_id,
            "comune_id": comune_id_str,
            "tipo_utilizzo": tipo_utilizzo,
            "tipo_utilizzo_altro": tipo_utilizzo_altro if tipo_utilizzo == "Altro" else None,
            "titolo": titolo,
            "data_inizio": data_inizio,
            "data_fine": data_fine,
            "n_partecipanti_stimati": n_partecipanti_stimati,
            "note": note,
        }
        creato_resp = supabase.table("utilizzi_spazio_civico").insert(record).execute()

        return {"status": "salvato", "utilizzo": creato_resp.data[0] if creato_resp.data else None, "ore_calcolate": ore_calcolate}
    except Exception as e:
        print(f"Errore creazione utilizzo spazio: {e}")
        return {"errore": str(e)}


def get_utilizzi_spazio(spazio_id):
    try:
        utilizzi_resp = supabase.table("utilizzi_spazio_civico").select("*") \
            .eq("spazio_id", spazio_id).order("data_inizio", desc=True).execute()
        utilizzi = utilizzi_resp.data or []

        risultati = []
        for u in utilizzi:
            ore = calcola_ore_utilizzo(u["data_inizio"], u["data_fine"])
            risultati.append({**u, "ore_utilizzo": ore})

        return {"spazio_id": spazio_id, "utilizzi": risultati}
    except Exception as e:
        print(f"Errore get utilizzi spazio {spazio_id}: {e}")
        return {"errore": str(e)}


def elimina_utilizzo_spazio(utilizzo_id):
    try:
        supabase.table("utilizzi_spazio_civico").delete().eq("id", utilizzo_id).execute()
        return {"status": "eliminato"}
    except Exception as e:
        print(f"Errore eliminazione utilizzo spazio {utilizzo_id}: {e}")
        return {"errore": str(e)}


def get_dashboard_spazi_civici(comune_id):
    try:
        spazi_resp = supabase.table("spazi_civici").select("*") \
            .eq("comune_id", comune_id).eq("attivo", True).execute()
        spazi = spazi_resp.data or []

        if not spazi:
            return {"comune_id": comune_id, "spazi": [], "totale_ore_comune": 0, "n_utilizzi_comune": 0}

        spazio_ids = [s["id"] for s in spazi]
        utilizzi_resp = supabase.table("utilizzi_spazio_civico").select("*") \
            .in_("spazio_id", spazio_ids).execute()
        tutti_utilizzi = utilizzi_resp.data or []

        risultati_spazi = []
        totale_ore_comune = 0
        breakdown_tipo_comune = {}

        for spazio in spazi:
            utilizzi_spazio = [u for u in tutti_utilizzi if u["spazio_id"] == spazio["id"]]
            ore_spazio = 0
            breakdown_tipo_spazio = {}
            for u in utilizzi_spazio:
                ore = calcola_ore_utilizzo(u["data_inizio"], u["data_fine"])
                ore_spazio += ore
                tipo = u["tipo_utilizzo"]
                breakdown_tipo_spazio[tipo] = breakdown_tipo_spazio.get(tipo, 0) + ore
                breakdown_tipo_comune[tipo] = breakdown_tipo_comune.get(tipo, 0) + ore

            totale_ore_comune += ore_spazio

            risultati_spazi.append({
                "id": spazio["id"],
                "nome_spazio": spazio["nome_spazio"],
                "indirizzo": spazio.get("indirizzo"),
                "capienza": spazio.get("capienza"),
                "n_utilizzi": len(utilizzi_spazio),
                "totale_ore_apertura": round(ore_spazio, 1),
                "breakdown_per_tipo": {k: round(v, 1) for k, v in breakdown_tipo_spazio.items()},
            })

        risultati_spazi.sort(key=lambda x: x["totale_ore_apertura"], reverse=True)

        return {
            "comune_id": comune_id,
            "spazi": risultati_spazi,
            "totale_ore_comune": round(totale_ore_comune, 1),
            "n_utilizzi_comune": len(tutti_utilizzi),
            "breakdown_tipo_comune": {k: round(v, 1) for k, v in breakdown_tipo_comune.items()},
            "nota_metodologica": (
                "Le ore di apertura alla comunità sono calcolate sulla durata reale di ogni utilizzo registrato "
                "(assemblee cittadine, laboratori scolastici, mostre di artisti residenti e altre attività non "
                "turistiche). È una misura di quanto gli spazi comunali vengono restituiti alla cittadinanza, "
                "indipendente dal modulo Gestione Spazi Eventi, che riguarda invece il booking commerciale/turistico."
            ),
        }
    except Exception as e:
        print(f"Errore dashboard spazi civici comune {comune_id}: {e}")
        return {"errore": str(e)}