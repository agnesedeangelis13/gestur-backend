import os
import httpx
from datetime import datetime

RESEND_API_KEY = os.getenv("RESEND_API_KEY")

async def invia_alert_previsioni(sito_nome: str, previsioni: list, destinatari: list):
    """Invia email di alert con le previsioni SARIMAX."""
    
    # Costruisci tabella previsioni
    righe_html = ""
    for p in previsioni:
        data = p["data"]
        presenze = p["presenze_previste"]
        # Colore in base al valore
        if presenze > 200:
            colore = "#ffebee"  # rosso chiaro = picco alto
            icona = "🔴"
        elif presenze < 20:
            colore = "#fff3e0"  # arancio = calo
            icona = "🟡"
        else:
            colore = "#f1f8e9"  # verde = normale
            icona = "🟢"
        
        righe_html += f"""
        <tr style="background:{colore}">
            <td style="padding:8px 12px">{data}</td>
            <td style="padding:8px 12px;text-align:center">{presenze}</td>
            <td style="padding:8px 12px;text-align:center">{icona}</td>
        </tr>"""

    html = f"""
    <div style="font-family:system-ui,sans-serif;max-width:600px;margin:0 auto">
        <div style="background:#1A3557;padding:24px;border-radius:12px 12px 0 0">
            <h1 style="color:white;margin:0;font-size:20px">🌍 GesTur — Previsioni Affluenza</h1>
            <p style="color:rgba(255,255,255,0.7);margin:8px 0 0;font-size:13px">
                Aggiornamento automatico SARIMAX — {datetime.now().strftime("%d/%m/%Y %H:%M")}
            </p>
        </div>
        <div style="background:white;padding:24px;border:1px solid #e0e0e0">
            <p style="color:#1A3557;font-weight:600">Sito: {sito_nome}</p>
            <table style="width:100%;border-collapse:collapse;margin-top:16px">
                <thead>
                    <tr style="background:#1A3557;color:white">
                        <th style="padding:10px 12px;text-align:left">Data</th>
                        <th style="padding:10px 12px">Presenze previste</th>
                        <th style="padding:10px 12px">Stato</th>
                    </tr>
                </thead>
                <tbody>{righe_html}</tbody>
            </table>
            <div style="margin-top:20px;padding:16px;background:#f8fafc;border-radius:8px;font-size:12px;color:#6b7280">
                🟢 Normale &nbsp;&nbsp; 🟡 Calo atteso (&lt;20) &nbsp;&nbsp; 🔴 Picco atteso (&gt;200)
            </div>
        </div>
        <div style="background:#f0f4f8;padding:16px;border-radius:0 0 12px 12px;font-size:12px;color:#6b7280;text-align:center">
            GesTur — Sistema di gestione presenze siti culturali
        </div>
    </div>
    """

    payload = {
        "from": "GesTur <onboarding@resend.dev>",
        "to": destinatari,
        "subject": f"📊 Previsioni affluenza — {sito_nome} — {datetime.now().strftime('%d/%m/%Y')}",
        "html": html
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json"
            },
            json=payload,
            timeout=10
        )
        return resp.json()