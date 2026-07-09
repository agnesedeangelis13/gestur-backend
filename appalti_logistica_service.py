from collections import defaultdict
from supabase import create_client
import os
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

STATI_SCHEDA = ("bozza", "confermata")


def crea_fornitore(payload):
    try:
        comune_id_str = payload.get("comune_id")
        nome_interno = payload.get("nome_interno")
        categoria_servizio = payload.get("categoria_servizio")
        listino_servizi = payload.get("listino_servizi") or []
        tipo_contratto = payload.get("tipo_contratto")
        data_inizio_contratto = payload.get("data_inizio_contratto")
        data_fine_contratto = payload.get("data_fine_contratto")

        if not comune_id_str or not nome_interno or not nome_interno.strip():
            return {"errore": "comune_id e nome_interno sono obbligatori"}

        record = {
            "comune_id": comune_id_str,
            "nome_interno": nome_interno.strip(),
            "categoria_servizio": categoria_servizio,
            "listino_servizi": listino_servizi,
            "tipo_contratto": tipo_contratto,
            "data_inizio_contratto": data_inizio_contratto,
            "data_fine_contratto": data_fine_contratto,
        }
        creato_resp = supabase.table("fornitori_logistica").insert(record).execute()
        return {"status": "salvato", "fornitore": creato_resp.data[0] if creato_resp.data else None}
    except Exception as e:
        print(f"Errore creazione fornitore logistica: {e}")
        return {"errore": str(e)}


def get_fornitori(comune_id):
    try:
        resp = supabase.table("fornitori_logistica").select("*").eq("comune_id", comune_id).eq("attivo", True).order("nome_interno").execute()
        return {"comune_id": comune_id, "fornitori": resp.data or []}
    except Exception as e:
        print(f"Errore get fornitori logistica comune {comune_id}: {e}")
        return {"errore": str(e)}


def aggiorna_fornitore(fornitore_id, payload):
    try:
        campi_consentiti = {"nome_interno", "categoria_servizio", "listino_servizi", "tipo_contratto", "data_inizio_contratto", "data_fine_contratto", "affidabilita"}
        aggiornamento = {k: v for k, v in payload.items() if k in campi_consentiti}
        if not aggiornamento:
            return {"errore": "Nessun campo valido da aggiornare"}
        supabase.table("fornitori_logistica").update(aggiornamento).eq("id", fornitore_id).execute()
        return {"status": "aggiornato"}
    except Exception as e:
        print(f"Errore aggiornamento fornitore logistica {fornitore_id}: {e}")
        return {"errore": str(e)}


def disattiva_fornitore(fornitore_id):
    try:
        supabase.table("fornitori_logistica").update({"attivo": False}).eq("id", fornitore_id).execute()
        return {"status": "disattivato"}
    except Exception as e:
        print(f"Errore disattivazione fornitore logistica {fornitore_id}: {e}")
        return {"errore": str(e)}


def crea_scheda_tecnica(payload):
    try:
        comune_id_str = payload.get("comune_id")
        titolo = payload.get("titolo")
        tipo_origine = payload.get("tipo_origine")
        mercato_id = payload.get("mercato_id")
        riferimento_libero = payload.get("riferimento_libero")
        ricavi_previsti = payload.get("ricavi_previsti")
        fabbisogni_input = payload.get("fabbisogni") or []

        if not comune_id_str or not titolo or not titolo.strip():
            return {"errore": "comune_id e titolo sono obbligatori"}

        record = {
            "comune_id": comune_id_str,
            "titolo": titolo.strip(),
            "tipo_origine": tipo_origine,
            "mercato_id": mercato_id,
            "riferimento_libero": riferimento_libero,
            "ricavi_previsti": ricavi_previsti,
            "stato": "bozza",
        }
        creato_resp = supabase.table("schede_tecniche").insert(record).execute()
        scheda = creato_resp.data[0] if creato_resp.data else None
        if not scheda:
            return {"errore": "Errore nel salvataggio della scheda tecnica"}

        fabbisogni_creati = []
        for f in fabbisogni_input:
            fabbisogno_record = {
                "scheda_tecnica_id": scheda["id"],
                "categoria_servizio": f.get("categoria_servizio"),
                "descrizione": f.get("descrizione"),
                "quantita": f.get("quantita"),
            }
            fabbisogno_resp = supabase.table("fabbisogni_logistici").insert(fabbisogno_record).execute()
            if fabbisogno_resp.data:
                fabbisogni_creati.append(fabbisogno_resp.data[0])

        return {"status": "salvato", "scheda": scheda, "fabbisogni": fabbisogni_creati}
    except Exception as e:
        print(f"Errore creazione scheda tecnica: {e}")
        return {"errore": str(e)}


def get_schede_tecniche(comune_id):
    try:
        resp = supabase.table("schede_tecniche").select("*").eq("comune_id", comune_id).order("creato_il", desc=True).execute()
        return {"comune_id": comune_id, "schede": resp.data or []}
    except Exception as e:
        print(f"Errore get schede tecniche comune {comune_id}: {e}")
        return {"errore": str(e)}


def get_scheda_tecnica_dettaglio(scheda_id):
    try:
        scheda_resp = supabase.table("schede_tecniche").select("*").eq("id", scheda_id).single().execute()
        scheda = scheda_resp.data
        if not scheda:
            return {"errore": "Scheda tecnica non trovata"}

        fabbisogni_resp = supabase.table("fabbisogni_logistici").select("*").eq("scheda_tecnica_id", scheda_id).order("creato_il").execute()
        fabbisogni = fabbisogni_resp.data or []

        fabbisogno_ids = [f["id"] for f in fabbisogni]
        preventivi_resp = supabase.table("preventivi_logistici").select("*, fornitori_logistica(nome_interno)") \
            .in_("fabbisogno_id", fabbisogno_ids).execute() if fabbisogno_ids else None
        tutti_preventivi = preventivi_resp.data if preventivi_resp else []

        for f in fabbisogni:
            f["preventivi"] = [p for p in tutti_preventivi if p["fabbisogno_id"] == f["id"]]

        return {"scheda": scheda, "fabbisogni": fabbisogni}
    except Exception as e:
        print(f"Errore dettaglio scheda tecnica {scheda_id}: {e}")
        return {"errore": str(e)}


def aggiungi_fabbisogno(scheda_id, payload):
    try:
        record = {
            "scheda_tecnica_id": scheda_id,
            "categoria_servizio": payload.get("categoria_servizio"),
            "descrizione": payload.get("descrizione"),
            "quantita": payload.get("quantita"),
        }
        creato_resp = supabase.table("fabbisogni_logistici").insert(record).execute()
        return {"status": "salvato", "fabbisogno": creato_resp.data[0] if creato_resp.data else None}
    except Exception as e:
        print(f"Errore aggiunta fabbisogno scheda {scheda_id}: {e}")
        return {"errore": str(e)}


def elimina_fabbisogno(fabbisogno_id):
    try:
        supabase.table("fabbisogni_logistici").delete().eq("id", fabbisogno_id).execute()
        return {"status": "eliminato"}
    except Exception as e:
        print(f"Errore eliminazione fabbisogno {fabbisogno_id}: {e}")
        return {"errore": str(e)}


def aggiungi_preventivo(fabbisogno_id, payload):
    try:
        comune_id_str = payload.get("comune_id")
        fornitore_id = payload.get("fornitore_id")
        fornitore_nome_libero = payload.get("fornitore_nome_libero")
        importo = payload.get("importo")
        note = payload.get("note")

        if not comune_id_str or importo is None:
            return {"errore": "comune_id e importo sono obbligatori"}
        if not fornitore_id and not (fornitore_nome_libero and fornitore_nome_libero.strip()):
            return {"errore": "Indica un fornitore dall'albo oppure un nome libero"}
        if importo < 0:
            return {"errore": "L'importo non può essere negativo"}

        record = {
            "fabbisogno_id": fabbisogno_id,
            "comune_id": comune_id_str,
            "fornitore_id": fornitore_id,
            "fornitore_nome_libero": fornitore_nome_libero,
            "importo": importo,
            "note": note,
            "vincitore": False,
        }
        creato_resp = supabase.table("preventivi_logistici").insert(record).execute()
        return {"status": "salvato", "preventivo": creato_resp.data[0] if creato_resp.data else None}
    except Exception as e:
        print(f"Errore aggiunta preventivo fabbisogno {fabbisogno_id}: {e}")
        return {"errore": str(e)}


def segna_preventivo_vincitore(preventivo_id):
    try:
        preventivo_resp = supabase.table("preventivi_logistici").select("fabbisogno_id").eq("id", preventivo_id).single().execute()
        preventivo = preventivo_resp.data
        if not preventivo:
            return {"errore": "Preventivo non trovato"}

        supabase.table("preventivi_logistici").update({"vincitore": False}).eq("fabbisogno_id", preventivo["fabbisogno_id"]).execute()
        supabase.table("preventivi_logistici").update({"vincitore": True}).eq("id", preventivo_id).execute()
        return {"status": "vincitore"}
    except Exception as e:
        print(f"Errore selezione vincitore preventivo {preventivo_id}: {e}")
        return {"errore": str(e)}


def elimina_preventivo(preventivo_id):
    try:
        supabase.table("preventivi_logistici").delete().eq("id", preventivo_id).execute()
        return {"status": "eliminato"}
    except Exception as e:
        print(f"Errore eliminazione preventivo {preventivo_id}: {e}")
        return {"errore": str(e)}


def conferma_scheda_tecnica(scheda_id):
    try:
        scheda_resp = supabase.table("schede_tecniche").select("stato").eq("id", scheda_id).single().execute()
        scheda = scheda_resp.data
        if not scheda:
            return {"errore": "Scheda tecnica non trovata"}
        if scheda["stato"] == "confermata":
            return {"errore": "La scheda è già confermata"}

        fabbisogni_resp = supabase.table("fabbisogni_logistici").select("id, descrizione").eq("scheda_tecnica_id", scheda_id).execute()
        fabbisogni = fabbisogni_resp.data or []
        if not fabbisogni:
            return {"errore": "Aggiungi almeno un fabbisogno prima di confermare"}

        fabbisogno_ids = [f["id"] for f in fabbisogni]
        preventivi_resp = supabase.table("preventivi_logistici").select("fabbisogno_id, vincitore").in_("fabbisogno_id", fabbisogno_ids).execute()
        preventivi = preventivi_resp.data or []

        senza_vincitore = []
        for f in fabbisogni:
            ha_vincitore = any(p["fabbisogno_id"] == f["id"] and p["vincitore"] for p in preventivi)
            if not ha_vincitore:
                senza_vincitore.append(f.get("descrizione") or f"fabbisogno #{f['id']}")

        if senza_vincitore:
            return {"errore": f"Manca un preventivo vincitore per: {', '.join(senza_vincitore)}"}

        supabase.table("schede_tecniche").update({"stato": "confermata"}).eq("id", scheda_id).execute()
        return {"status": "confermata"}
    except Exception as e:
        print(f"Errore conferma scheda tecnica {scheda_id}: {e}")
        return {"errore": str(e)}


def get_storico_schede(comune_id):
    try:
        schede_resp = supabase.table("schede_tecniche").select("*").eq("comune_id", comune_id).eq("stato", "confermata").order("creato_il", desc=True).execute()
        schede = schede_resp.data or []
        if not schede:
            return {"comune_id": comune_id, "schede": []}

        scheda_ids = [s["id"] for s in schede]
        fabbisogni_resp = supabase.table("fabbisogni_logistici").select("*").in_("scheda_tecnica_id", scheda_ids).execute()
        fabbisogni = fabbisogni_resp.data or []

        fabbisogno_ids = [f["id"] for f in fabbisogni]
        preventivi_resp = supabase.table("preventivi_logistici").select("*").in_("fabbisogno_id", fabbisogno_ids).execute() if fabbisogno_ids else None
        preventivi = preventivi_resp.data or []
        vincitori = [p for p in preventivi if p["vincitore"]]

        fabbisogni_per_scheda = defaultdict(list)
        for f in fabbisogni:
            fabbisogni_per_scheda[f["scheda_tecnica_id"]].append(f["id"])

        for s in schede:
            ids_fabbisogni = fabbisogni_per_scheda.get(s["id"], [])
            s["n_fabbisogni"] = len(ids_fabbisogni)
            s["costo_logistico"] = round(sum(p["importo"] for p in vincitori if p["fabbisogno_id"] in ids_fabbisogni), 2)
            s["margine_netto"] = round((s.get("ricavi_previsti") or 0) - s["costo_logistico"], 2)

        return {"comune_id": comune_id, "schede": schede}
    except Exception as e:
        print(f"Errore storico schede comune {comune_id}: {e}")
        return {"errore": str(e)}


def get_statistiche_appalti(comune_id):
    try:
        schede_resp = supabase.table("schede_tecniche").select("*").eq("comune_id", comune_id).execute()
        schede = schede_resp.data or []
        confermate = [s for s in schede if s["stato"] == "confermata"]

        scheda_ids = [s["id"] for s in schede]
        fabbisogni_resp = supabase.table("fabbisogni_logistici").select("*").in_("scheda_tecnica_id", scheda_ids).execute() if scheda_ids else None
        fabbisogni = fabbisogni_resp.data or []

        fabbisogno_ids = [f["id"] for f in fabbisogni]
        preventivi_resp = supabase.table("preventivi_logistici").select("*").in_("fabbisogno_id", fabbisogno_ids).execute() if fabbisogno_ids else None
        preventivi = preventivi_resp.data or []
        vincitori = [p for p in preventivi if p["vincitore"]]

        fabbisogni_per_scheda = defaultdict(list)
        for f in fabbisogni:
            fabbisogni_per_scheda[f["scheda_tecnica_id"]].append(f["id"])

        costo_per_scheda = {}
        for scheda_id, ids_fabbisogni in fabbisogni_per_scheda.items():
            costo_per_scheda[scheda_id] = sum(p["importo"] for p in vincitori if p["fabbisogno_id"] in ids_fabbisogni)

        costo_logistico_totale = round(sum(costo_per_scheda.values()), 2)
        ricavi_totali = round(sum(s.get("ricavi_previsti") or 0 for s in confermate), 2)
        margine_netto_totale = round(sum(
            (s.get("ricavi_previsti") or 0) - costo_per_scheda.get(s["id"], 0) for s in confermate
        ), 2)

        fabbisogno_categoria = {f["id"]: f.get("categoria_servizio") or "Altro" for f in fabbisogni}
        costo_per_categoria_raw = defaultdict(float)
        for p in vincitori:
            categoria = fabbisogno_categoria.get(p["fabbisogno_id"], "Altro")
            costo_per_categoria_raw[categoria] += p["importo"]
        costo_per_categoria = [
            {"tipo": cat, "valore": round(val, 2)}
            for cat, val in sorted(costo_per_categoria_raw.items(), key=lambda x: x[1], reverse=True)
        ]

        conteggio_fornitori = defaultdict(int)
        for p in vincitori:
            if p.get("fornitore_id"):
                conteggio_fornitori[p["fornitore_id"]] += 1
        fornitori_ids_top = sorted(conteggio_fornitori.items(), key=lambda x: x[1], reverse=True)[:5]
        fornitori_top = []
        for fid, conteggio in fornitori_ids_top:
            fornitore_resp = supabase.table("fornitori_logistica").select("nome_interno").eq("id", fid).single().execute()
            nome = fornitore_resp.data["nome_interno"] if fornitore_resp.data else "Fornitore rimosso"
            fornitori_top.append({"nome_interno": nome, "preventivi_vinti": conteggio})

        return {
            "comune_id": comune_id,
            "n_schede_totali": len(schede),
            "n_schede_confermate": len(confermate),
            "n_schede_bozza": len(schede) - len(confermate),
            "costo_logistico_totale": costo_logistico_totale,
            "ricavi_totali": ricavi_totali,
            "margine_netto_totale": margine_netto_totale,
            "costo_per_categoria": costo_per_categoria,
            "fornitori_top": fornitori_top,
            "nota_metodologica": (
                "Il margine netto è calcolato solo sulle schede tecniche confermate, come differenza tra i ricavi "
                "previsti indicati e la somma dei preventivi vincitori per ciascun fabbisogno logistico. L'albo "
                "fornitori non contiene dati identificativi: solo etichette interne, categorie di servizio e listini."
            ),
        }
    except Exception as e:
        print(f"Errore statistiche appalti comune {comune_id}: {e}")
        return {"errore": str(e)}