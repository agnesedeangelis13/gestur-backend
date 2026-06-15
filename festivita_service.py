import os
from datetime import date, timedelta
from supabase import create_client

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

FESTIVITA_NAZIONALI = [
    ("01-01", "Capodanno"), ("01-06", "Epifania"),
    ("04-25", "Festa della Liberazione"), ("05-01", "Festa del Lavoro"),
    ("06-02", "Festa della Repubblica"), ("08-15", "Ferragosto"),
    ("11-01", "Ognissanti"), ("12-08", "Immacolata"),
    ("12-25", "Natale"), ("12-26", "Santo Stefano"),
]

FESTIVITA_REGIONALI = {
    "Lombardia": [("12-07", "Sant'Ambrogio")],
    "Lazio": [("06-29", "Santi Pietro e Paolo")],
    "Campania": [("09-19", "San Gennaro")],
    "Sicilia": [("09-02", "Santa Rosalia"), ("02-05", "Sant'Agata")],
    "Toscana": [("06-24", "San Giovanni Battista")],
    "Veneto": [("04-25", "San Marco")],
    "Piemonte": [("06-24", "San Giovanni")],
    "Liguria": [("12-08", "Immacolata Genova")],
    "Emilia-Romagna": [("10-04", "San Petronio")],
    "Puglia": [("12-06", "San Nicola")],
    "Sardegna": [("04-23", "San Giorgio")],
    "Calabria": [("09-08", "Madonna della Consolazione")],
    "Marche": [("12-10", "Santa Casa di Loreto")],
    "Umbria": [("01-29", "San Costanzo")],
    "Abruzzo": [("06-10", "San Massimo")],
    "Basilicata": [("07-02", "Maria SS. di Picciano")],
    "Molise": [("05-26", "San Filippo Neri")],
    "Friuli-Venezia Giulia": [("11-03", "Unita Nazionale")],
    "Trentino-Alto Adige": [("08-15", "Assunzione Maria")],
    "Valle d'Aosta": [("08-07", "Forte di Bard")],
}

def calcola_pasqua(anno: int) -> date:
    a = anno % 19
    b = anno // 100
    c = anno % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mese = (h + l - 7 * m + 114) // 31
    giorno = ((h + l - 7 * m + 114) % 31) + 1
    return date(anno, mese, giorno)

def popola_festivita(anno: int):
    records = []
    regioni = list(FESTIVITA_REGIONALI.keys())

    for regione in regioni:
        # Nazionali
        for mmdd, nome in FESTIVITA_NAZIONALI:
            records.append({
                "regione": regione, "nome": nome,
                "data": f"{anno}-{mmdd}", "tipo": "nazionale", "anno": anno
            })
        # Pasqua e Lunedi dell'Angelo
        pasqua = calcola_pasqua(anno)
        lunedi = pasqua + timedelta(days=1)
        records.append({"regione": regione, "nome": "Pasqua", "data": str(pasqua), "tipo": "nazionale", "anno": anno})
        records.append({"regione": regione, "nome": "Lunedi dell'Angelo", "data": str(lunedi), "tipo": "nazionale", "anno": anno})
        # Regionali
        for mmdd, nome in FESTIVITA_REGIONALI.get(regione, []):
            records.append({
                "regione": regione, "nome": nome,
                "data": f"{anno}-{mmdd}", "tipo": "locale", "anno": anno
            })

    supabase.table("festivita_regionali").upsert(records, on_conflict="regione,data,nome").execute()
    return len(records)