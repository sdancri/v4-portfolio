"""Parse all *_sweep.log din /tmp/ + sortare globala dupa PnL.

Format Top 10 row:
  hull kj   snkB SL     TP      n    WR     PF     Ret       DD
  10   60   26   3.0  %20%     138  40.6  %1.54   +2013.5  %-41.0  %

Output: tabel cu TOATE variantele unice, sortate dupa Return descrescator.
"""

from __future__ import annotations

import re
from pathlib import Path

LOG_DIR = Path("/tmp")
LINE_RE = re.compile(
    r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+([\d.]+)\s*%\s*(—|\d+%|\d+\s*%)?\s+"
    r"(\d+)\s+([\d.]+)\s*%\s*([\d.]+)\s+\+?(-?[\d.]+)\s*%\s*([+-]?[\d.]+)\s*%\s*$"
)


def parse_log(fname: Path) -> tuple[str, list[dict]]:
    sym = fname.stem.replace("_sweep", "").upper()
    rows: list[dict] = []
    in_table = False
    for line in fname.read_text().splitlines():
        if "Top 10 by" in line:
            in_table = True
            continue
        if in_table and line.strip().startswith("---"):
            in_table = False
            continue
        if not in_table:
            continue
        if not line.strip() or line.strip().startswith("hull") or line.strip().startswith("symbol"):
            continue
        m = LINE_RE.match(line)
        if not m:
            continue
        try:
            hull, kj, snkb = int(m.group(1)), int(m.group(2)), int(m.group(3))
            sl_pct = float(m.group(4))
            tp_raw = (m.group(5) or "").strip().rstrip("%").strip()
            tp_pct = None if tp_raw in ("", "—", "-") else float(tp_raw)
            n = int(m.group(6))
            wr = float(m.group(7))
            pf = float(m.group(8))
            ret = float(m.group(9))
            dd = float(m.group(10))
            rows.append({
                "symbol": sym, "hull": hull, "kj": kj, "snkb": snkb,
                "sl": sl_pct, "tp": tp_pct, "n": n, "wr": wr, "pf": pf,
                "ret": ret, "dd": dd, "final": 100 * (1 + ret / 100),
            })
        except (ValueError, AttributeError):
            continue
    return sym, rows


def main() -> int:
    all_rows: list[dict] = []
    seen: set[tuple] = set()
    for log in sorted(LOG_DIR.glob("*_sweep.log")):
        sym, rows = parse_log(log)
        for r in rows:
            key = (r["symbol"], r["hull"], r["kj"], r["snkb"], r["sl"], r["tp"])
            if key in seen:
                continue
            seen.add(key)
            all_rows.append(r)

    if not all_rows:
        print("Nu s-au gasit randuri. Verifica /tmp/*_sweep.log existenta + format.")
        return 1

    # Sort descrescator dupa return
    all_rows.sort(key=lambda r: -r["ret"])

    print(f"\n{len(all_rows)} variante unice din {len(set(r['symbol'] for r in all_rows))} perechi\n")
    print(f"{'Rank':<5}{'Symbol':<14}{'H/Kj/SnkB':<12}{'SL':<6}{'TP':<7}"
          f"{'n':<5}{'WR':<7}{'PF':<7}{'Return':<11}{'PnL$':<10}{'DD':<8}")
    print("─" * 96)
    for i, r in enumerate(all_rows, 1):
        tp = f"{int(r['tp'])}%" if r['tp'] is not None else "—"
        cfg = f"{r['hull']}/{r['kj']}/{r['snkb']}"
        pnl = r["final"] - 100
        print(f"{i:<5}{r['symbol']:<14}{cfg:<12}{r['sl']:<5.1f}%{tp:<7}"
              f"{r['n']:<5}{r['wr']:<6.1f}%{r['pf']:<7.2f}"
              f"{r['ret']:<+10.1f}%${pnl:<+9.2f}{r['dd']:<+7.1f}%")

    # Save CSV
    import csv
    out = Path("/tmp/all_sweep_variants.csv")
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(all_rows[0].keys()))
        w.writeheader()
        for r in all_rows:
            w.writerow(r)
    print(f"\nCSV: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
