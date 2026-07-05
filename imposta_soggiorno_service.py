from supabase import create_client
import os
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

CATEGORIE_DESTINAZIONE_SOGGIORNO = {
    "aree_verdi": {"nome": "Aree verdi e arredo urbano", "unita_misura": "mq riqualificati", "conversione_fissa": True},
    "assistenza_sociale": {"nome": "Assistenza sociale e servizi alla persona", "unita_misura": "beneficiari raggiunti", "conversione_fissa": True},
    "trasporto_pubblico": {"nome": "Trasporto pubblico locale agevolato", "unita_misura": "abbonamenti agevolati", "conversione_fissa": True},
    "manutenzione_urbana": {"nome": "Manutenzione strade e decoro urbano", "unita_misura": "interventi", "conversione_fissa": True},
    "sicurezza": {"nome": "Sicurezza e videosorveglianza", "unita_misura": "punti installati", "conversione_fissa": True},
    "cultura_biblioteche": {"nome": "Cultura e biblioteche per residenti", "unita_misura": "iniziative finanziate", "conversione_fissa": True},
    "servizi_digitali": {"nome": "Servizi digitali per cittadini", "unita_misura": None, "conversione_fissa": False},
    "altro": {"nome": "Altro", "unita_misura": None, "conversione_fissa": False},
}


def ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id_str):
    esistente_resp = supabase.table("piani_sviluppo_locale").select("*") \
        .eq("comune_id", comune_id_str).neq("stato", "archiviato") \
        .order("creato_il", desc=True).limit(1).execute()
    if esistente_resp.data:
        return esistente_resp.data[0]

    nuovo_piano = {
        "comune_id": comune_id_str,
        "titolo": "Sviluppo Locale e Welfare",
        "stato": "bozza",
    }
    creato_resp = supabase.table("piani_sviluppo_locale").insert(nuovo_piano).execute()
    return creato_resp.data[0]


def get_gettito_soggiorno(comune_id):
    try:
        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id)

        gettito_resp = supabase.table("gettito_soggiorno").select("*") \
            .eq("piano_id", piano["id"]).order("anno", desc=True).order("mese", desc=True).execute()
        gettito = gettito_resp.data or []

        allocazioni_resp = supabase.table("allocazioni_soggiorno").select("anno, mese, importo_allocato") \
            .eq("piano_id", piano["id"]).eq("attivo", True).execute()
        allocazioni = allocazioni_resp.data or []

        allocato_per_mese = {}
        for a in allocazioni:
            chiave = (a["anno"], a["mese"])
            allocato_per_mese[chiave] = allocato_per_mese.get(chiave, 0) + (a["importo_allocato"] or 0)

        righe = []
        totale_incassato_storico = 0
        totale_allocato_storico = 0
        for g in gettito:
            chiave = (g["anno"], g["mese"])
            allocato_mese = allocato_per_mese.get(chiave, 0)
            indice_pct = round(allocato_mese / g["importo_incassato"] * 100, 1) if g["importo_incassato"] else None
            totale_incassato_storico += g["importo_incassato"] or 0
            totale_allocato_storico += allocato_mese
            righe.append({
                **g,
                "totale_allocato_mese": round(allocato_mese, 2),
                "indice_ritorno_sociale_pct": indice_pct,
            })

        indice_medio_storico = round(totale_allocato_storico / totale_incassato_storico * 100, 1) if totale_incassato_storico else None

        return {
            "piano_id": piano["id"],
            "comune_id": comune_id,
            "gettito": righe,
            "totale_incassato_storico": round(totale_incassato_storico, 2),
            "totale_allocato_storico": round(totale_allocato_storico, 2),
            "indice_ritorno_sociale_medio_pct": indice_medio_storico,
            "nota_metodologica": (
                "L'indice di ritorno sociale confronta, per ogni mese, quanto e stato allocato alle categorie di "
                "destinazione rispetto al gettito incassato in quel mese. Un indice inferiore al 100% non e "
                "necessariamente negativo: puo riflettere accantonamenti per interventi pluriennali."
            ),
        }
    except Exception as e:
        print(f"Errore gettito soggiorno comune {comune_id}: {e}")
        return {"errore": str(e)}


def crea_gettito_soggiorno(payload):
    try:
        comune_id_str = payload.get("comune_id")
        anno = payload.get("anno")
        mese = payload.get("mese")
        importo_incassato = payload.get("importo_incassato")
        note = payload.get("note")

        if not comune_id_str or anno is None or mese is None or importo_incassato is None:
            return {"errore": "comune_id, anno, mese e importo_incassato sono obbligatori"}

        if mese < 1 or mese > 12:
            return {"errore": "mese deve essere tra 1 e 12"}

        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id_str)

        record = {
            "piano_id": piano["id"],
            "comune_id": comune_id_str,
            "anno": anno,
            "mese": mese,
            "importo_incassato": importo_incassato,
            "note": note,
        }
        supabase.table("gettito_soggiorno").upsert(record, on_conflict="piano_id,anno,mese").execute()

        return {"status": "salvato", "piano_id": piano["id"]}
    except Exception as e:
        print(f"Errore creazione gettito soggiorno: {e}")
        return {"errore": str(e)}


def get_categorie_destinazione(comune_id):
    try:
        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id)

        config_resp = supabase.table("categorie_destinazione_config").select("*").eq("piano_id", piano["id"]).execute()
        config_map = {c["categoria"]: c for c in (config_resp.data or [])}

        categorie = []
        for chiave, info in CATEGORIE_DESTINAZIONE_SOGGIORNO.items():
            config = config_map.get(chiave)
            categorie.append({
                "categoria": chiave,
                "nome": info["nome"],
                "unita_misura": info["unita_misura"],
                "conversione_fissa": info["conversione_fissa"],
                "fattore_conversione": config["fattore_conversione"] if config else None,
            })

        return {
            "piano_id": piano["id"],
            "comune_id": comune_id,
            "categorie": categorie,
        }
    except Exception as e:
        print(f"Errore categorie destinazione comune {comune_id}: {e}")
        return {"errore": str(e)}


def aggiorna_categoria_destinazione(payload):
    try:
        comune_id_str = payload.get("comune_id")
        categoria = payload.get("categoria")
        fattore_conversione = payload.get("fattore_conversione")

        if not comune_id_str or not categoria:
            return {"errore": "comune_id e categoria sono obbligatori"}

        if categoria not in CATEGORIE_DESTINAZIONE_SOGGIORNO:
            return {"errore": "Categoria non valida"}

        if not CATEGORIE_DESTINAZIONE_SOGGIORNO[categoria]["conversione_fissa"]:
            return {"errore": "Questa categoria non prevede un fattore di conversione fisso"}

        if fattore_conversione is not None and fattore_conversione <= 0:
            return {"errore": "Il fattore di conversione deve essere positivo"}

        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id_str)

        record = {
            "piano_id": piano["id"],
            "comune_id": comune_id_str,
            "categoria": categoria,
            "fattore_conversione": fattore_conversione,
        }
        supabase.table("categorie_destinazione_config").upsert(record, on_conflict="piano_id,categoria").execute()

        return {"status": "salvato"}
    except Exception as e:
        print(f"Errore aggiornamento categoria destinazione: {e}")
        return {"errore": str(e)}


def genera_descrizione_risultato(nome_categoria, unita_misura, unita_calcolata, importo_allocato):
    if unita_calcolata is not None:
        return f"Grazie a questa allocazione di €{importo_allocato:,.0f} sono stati finanziati {unita_calcolata} {unita_misura}.".replace(",", ".")
    return f"Allocazione di €{importo_allocato:,.0f} destinata a \"{nome_categoria}\".".replace(",", ".")


def get_allocazioni_soggiorno(comune_id, anno=None, mese=None):
    try:
        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id)

        query = supabase.table("allocazioni_soggiorno").select("*").eq("piano_id", piano["id"]).eq("attivo", True)
        if anno is not None:
            query = query.eq("anno", anno)
        if mese is not None:
            query = query.eq("mese", mese)
        allocazioni_resp = query.order("anno", desc=True).order("mese", desc=True).execute()
        allocazioni = allocazioni_resp.data or []

        risultati = []
        for a in allocazioni:
            info_categoria = CATEGORIE_DESTINAZIONE_SOGGIORNO.get(a["categoria"], {})
            risultati.append({
                **a,
                "categoria_nome": info_categoria.get("nome", a["categoria"]),
                "unita_misura": info_categoria.get("unita_misura"),
            })

        return {
            "piano_id": piano["id"],
            "comune_id": comune_id,
            "allocazioni": risultati,
            "n_totale": len(risultati),
            "categorie_disponibili": CATEGORIE_DESTINAZIONE_SOGGIORNO,
        }
    except Exception as e:
        print(f"Errore allocazioni soggiorno comune {comune_id}: {e}")
        return {"errore": str(e)}


def crea_allocazione_soggiorno(payload):
    try:
        comune_id_str = payload.get("comune_id")
        anno = payload.get("anno")
        mese = payload.get("mese")
        categoria = payload.get("categoria")
        importo_allocato = payload.get("importo_allocato")
        descrizione_manuale = payload.get("descrizione_risultato")
        note = payload.get("note")

        if not comune_id_str or anno is None or mese is None or not categoria or importo_allocato is None:
            return {"errore": "comune_id, anno, mese, categoria e importo_allocato sono obbligatori"}

        if categoria not in CATEGORIE_DESTINAZIONE_SOGGIORNO:
            return {"errore": "Categoria non valida"}

        info_categoria = CATEGORIE_DESTINAZIONE_SOGGIORNO[categoria]
        piano = ottieni_o_crea_piano_sviluppo_locale_attivo(comune_id_str)

        unita_calcolata = None
        if info_categoria["conversione_fissa"]:
            config_resp = supabase.table("categorie_destinazione_config").select("fattore_conversione") \
                .eq("piano_id", piano["id"]).eq("categoria", categoria).limit(1).execute()
            fattore = config_resp.data[0]["fattore_conversione"] if config_resp.data else None
            if fattore:
                unita_calcolata = round(importo_allocato / fattore, 1)

        if descrizione_manuale and descrizione_manuale.strip():
            descrizione_risultato = descrizione_manuale.strip()
        else:
            descrizione_risultato = genera_descrizione_risultato(
                info_categoria["nome"], info_categoria["unita_misura"], unita_calcolata, importo_allocato
            )

        record = {
            "piano_id": piano["id"],
            "comune_id": comune_id_str,
            "anno": anno,
            "mese": mese,
            "categoria": categoria,
            "importo_allocato": importo_allocato,
            "unita_calcolata": unita_calcolata,
            "descrizione_risultato": descrizione_risultato,
            "note": note,
        }
        creato_resp = supabase.table("allocazioni_soggiorno").insert(record).execute()

        return {"status": "salvato", "allocazione": creato_resp.data[0] if creato_resp.data else None}
    except Exception as e:
        print(f"Errore creazione allocazione soggiorno: {e}")
        return {"errore": str(e)}


def elimina_allocazione_soggiorno(allocazione_id):
    try:
        supabase.table("allocazioni_soggiorno").update({"attivo": False}).eq("id", allocazione_id).execute()
        return {"status": "disattivato"}
    except Exception as e:
        print(f"Errore eliminazione allocazione soggiorno {allocazione_id}: {e}")
        return {"errore": str(e)}