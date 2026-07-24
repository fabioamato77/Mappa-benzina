#!/usr/bin/env python3
"""
aggiorna_mappa.py
-----------------
Scarica il CSV prezzi carburanti e l'anagrafica dal MIMIT,
aggiorna mappa_carburanti.html e salva.
Viene eseguito ogni mattina da GitHub Actions.

Dipendenze: pandas requests
"""

import json
import re
import sys
import requests
import pandas as pd
from datetime import datetime
from io import StringIO
from pathlib import Path

# ── CONFIG ────────────────────────────────────────────────────────────────────
HTML_FILE    = Path("mappa_carburanti.html")
PREZZI_URL   = "https://www.mimit.gov.it/images/exportCSV/prezzo_alle_8.csv"
ANA_URL      = "https://www.mimit.gov.it/images/exportCSV/anagrafica_impianti_attivi.csv"

# Fix coordinate distributore di Riace (precedentemente geocodificato a Parma)
RIACE_FIXES = [
    ("servito", 11200, 38.394048639418514, 16.534445547244754),
    ("self",    14805, 38.394048639418514, 16.534445547244754),
]


# ── Scarica un CSV dal MIMIT (prima riga = metadata, skippa) ─────────────────
def download_mimit_csv(url, sep="|"):
    print(f"  Scaricando {url} ...")
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    lines = r.text.splitlines()
    # Prima riga è "Estrazione del YYYY-MM-DD" — la saltiamo
    df = pd.read_csv(StringIO("\n".join(lines[1:])), sep=sep,
                     on_bad_lines="skip", engine="python")
    print(f"    → {len(df)} righe, colonne: {df.columns.tolist()[:4]}...")
    return df, lines[0]


# ── Scarica prezzi ────────────────────────────────────────────────────────────
def download_prezzi():
    df, header = download_mimit_csv(PREZZI_URL)
    date_match = re.search(r"\d{4}-\d{2}-\d{2}", header)
    date_str = date_match.group() if date_match else datetime.today().strftime("%Y-%m-%d")
    print(f"  → Estrazione del {date_str}")
    return df, date_str


# ── Scarica anagrafica ────────────────────────────────────────────────────────
def download_anagrafica():
    df, _ = download_mimit_csv(ANA_URL)
    # Verifica colonne attese
    needed = {"idImpianto", "Latitudine", "Longitudine"}
    if not needed.issubset(df.columns):
        raise ValueError(f"Colonne anagrafica inattese: {df.columns.tolist()}")
    return df


# ── Carica HTML e separa le parti ────────────────────────────────────────────
def load_html():
    with open(HTML_FILE, encoding="utf-8") as f:
        c = f.read()
    assert c.startswith("<!DOCTYPE html>"), "File HTML non valido"
    script_tag_pos   = c.find("<script>\nconst DATA")
    js_content_start = script_tag_pos + len("<script>\n")
    js_content_end   = c.rfind("\n</script>\n</body>") + 1
    assert script_tag_pos > 0, "Tag <script> con DATA non trovato"
    head    = c[:script_tag_pos]
    main_js = c[js_content_start:js_content_end]
    tail    = c[js_content_end:]
    return head, main_js, tail


# ── Aggiorna prezzi ───────────────────────────────────────────────────────────
def update_prices(main_js, df_prezzi, ana):
    idx  = main_js.find("const DATA = ")
    raw  = main_js[idx + len("const DATA = "):]
    end  = raw.find(";\nconst ")
    data = json.loads(raw[:end])

    fuels        = data["fuels"]
    fuel_idx_map = {f: i for i, f in enumerate(fuels)}

    def r5(x): return round(float(x), 5)

    si = {}
    for i, s in enumerate(data["servito"]): si.setdefault((r5(s[0]), r5(s[1])), i)
    ei = {}
    for i, s in enumerate(data["self"]):    ei.setdefault((r5(s[0]), r5(s[1])), i)

    ana["lr"]   = ana["Latitudine"].apply(r5)
    ana["lonr"] = ana["Longitudine"].apply(r5)
    id2c = dict(zip(ana["idImpianto"], zip(ana["lr"], ana["lonr"])))

    # Azzera prezzi
    for s in data["servito"]: s[7] = {}; s[8] = {}
    for s in data["self"]:    s[7] = {}; s[8] = {}

    # Fix Riace
    for arr_name, idx_fix, lat, lon in RIACE_FIXES:
        data[arr_name][idx_fix][0] = lat
        data[arr_name][idx_fix][1] = lon

    # Carica nuovi prezzi
    upd = 0
    for row in df_prezzi.itertuples():
        coord = id2c.get(row.idImpianto)
        if coord is None: continue
        fi = fuel_idx_map.get(row.descCarburante)
        if fi is None: continue
        pi = round(row.prezzo * 1000)
        sf = str(fi)
        if row.isSelf == 0:
            mi = si.get(coord)
            if mi is None: continue
            data["servito"][mi][7][sf] = pi
            data["servito"][mi][8][sf] = row.dtComu
        else:
            mi = ei.get(coord)
            if mi is None: continue
            data["self"][mi][7][sf] = pi
            data["self"][mi][8][sf] = row.dtComu
        upd += 1

    print(f"  → {upd} prezzi aggiornati")

    new_data_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    main_js = (
        main_js[:idx + len("const DATA = ")]
        + new_data_json
        + main_js[idx + len("const DATA = ") + end:]
    )
    return main_js


# ── Aggiorna data nel footer ──────────────────────────────────────────────────
def update_footer(head, date_str):
    d = datetime.strptime(date_str, "%Y-%m-%d")
    it_date = d.strftime("%d/%m/%Y")
    return re.sub(
        r"Ultimi dati Mimit: \d{2}/\d{2}/\d{4}",
        f"Ultimi dati Mimit: {it_date}",
        head
    )


# ── Salva ─────────────────────────────────────────────────────────────────────
def save_html(head, main_js, tail):
    final = head + "<script>\n" + main_js + tail
    opens  = len(re.findall(r"<script", final))
    closes = len(re.findall(r"</script>", final))
    assert opens == 2 and closes == 2, f"Struttura script anomala: {opens} open, {closes} close"
    assert final.startswith("<!DOCTYPE html>")
    assert final.endswith("</html>")
    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(final)
    print(f"  → Salvato: {HTML_FILE} ({len(final):,} bytes)")


# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    if not HTML_FILE.exists():
        sys.exit(f"Errore: {HTML_FILE} non trovato nel repository")

    print("=== Aggiornamento mappa carburanti ===")

    print("Download prezzi...")
    df_prezzi, date_str = download_prezzi()

    print("Download anagrafica...")
    ana = download_anagrafica()

    print("Caricamento HTML...")
    head, main_js, tail = load_html()

    print("Aggiornamento prezzi...")
    main_js = update_prices(main_js, df_prezzi, ana)

    head = update_footer(head, date_str)

    print("Salvataggio...")
    save_html(head, main_js, tail)

    print(f"\n✓ Completato — estrazione del {date_str}")


if __name__ == "__main__":
    main()
