#!/usr/bin/env python3
"""
aggiorna_mappa.py
-----------------
Scarica il CSV prezzi carburanti dal MIMIT e aggiorna mappa_carburanti.html.
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
HTML_FILE = Path("mappa_carburanti.html")
ANA_FILE  = Path("anagrafica_impianti_attivi.csv")
CSV_URL   = "https://www.mimit.gov.it/images/exportCSV/prezzo_alle_8.csv"

# Fix coordinate distributore di Riace (precedentemente geocodificato a Parma)
RIACE_FIXES = [
    ("servito", 11200, 38.394048639418514, 16.534445547244754),
    ("self",    14805, 38.394048639418514, 16.534445547244754),
]


# ── STEP 1: Scarica CSV ───────────────────────────────────────────────────────
def download_csv():
    print(f"Scaricando {CSV_URL} ...")
    r = requests.get(CSV_URL, timeout=30)
    r.raise_for_status()
    lines = r.text.splitlines()
    date_match = re.search(r"\d{4}-\d{2}-\d{2}", lines[0])
    date_str = date_match.group() if date_match else datetime.today().strftime("%Y-%m-%d")
    df = pd.read_csv(StringIO("\n".join(lines[1:])), sep="|")
    print(f"  → {len(df)} righe, estrazione del {date_str}")
    return df, date_str


# ── STEP 2: Carica HTML e separa le parti ────────────────────────────────────
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


# ── STEP 3: Aggiorna prezzi ───────────────────────────────────────────────────
def update_prices(main_js, df):
    ana = pd.read_csv(ANA_FILE, skiprows=1, sep="|")

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
    for row in df.itertuples():
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


# ── STEP 4: Aggiorna data nel footer ─────────────────────────────────────────
def update_footer(head, date_str):
    d = datetime.strptime(date_str, "%Y-%m-%d")
    it_date = d.strftime("%d/%m/%Y")
    return re.sub(
        r"Ultimi dati Mimit: \d{2}/\d{2}/\d{4}",
        f"Ultimi dati Mimit: {it_date}",
        head
    )


# ── STEP 5: Salva ─────────────────────────────────────────────────────────────
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
        sys.exit(f"Errore: {HTML_FILE} non trovato")
    if not ANA_FILE.exists():
        sys.exit(f"Errore: {ANA_FILE} non trovato")

    print("=== Aggiornamento mappa carburanti ===")

    df, date_str = download_csv()

    print("Caricamento HTML...")
    head, main_js, tail = load_html()

    print("Aggiornamento prezzi...")
    main_js = update_prices(main_js, df)

    head = update_footer(head, date_str)

    print("Salvataggio...")
    save_html(head, main_js, tail)

    print(f"\n✓ Completato — estrazione del {date_str}")


if __name__ == "__main__":
    main()
