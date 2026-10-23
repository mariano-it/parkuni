#!/usr/bin/env python3
"""Respaldo de la base de ParkUni (solo librería estándar).

  python3 server/respaldo.py                  # copia a server/data/respaldos/parkuni-AAAAMMDD-HHMMSS.db
  python3 server/respaldo.py --keep 30        # conserva los últimos 30 (por defecto 14)
  python3 server/respaldo.py --dir /ruta      # otra carpeta (p. ej. una memoria USB montada)
  python3 server/respaldo.py --db /ruta.db    # otra base

Usa la API de respaldo de SQLite: es seguro con el servidor encendido (no copia el archivo a ciegas).
Se corre A MANO, desde el menú de terminal (parkuni -> b): la laptop del servidor no aguanta un respaldo automático.
"""
import argparse, glob, os, sqlite3, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("PARKUNI_DB", os.path.join(HERE, "data", "parkuni.db"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--dir", default=None)
    ap.add_argument("--keep", type=int, default=14)
    a = ap.parse_args()
    if not os.path.exists(a.db):
        sys.exit(f"No existe la base: {a.db}")
    a.dir = a.dir or os.path.join(os.path.dirname(a.db), "respaldos")
    os.makedirs(a.dir, exist_ok=True)
    dest = os.path.join(a.dir, time.strftime("parkuni-%Y%m%d-%H%M%S.db"))
    src = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True, timeout=10)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
        ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        dst.close()
        src.close()
    if ok != "ok":
        os.remove(dest)
        sys.exit(f"El respaldo salió dañado ({ok}); se descartó.")
    old = sorted(glob.glob(os.path.join(a.dir, "parkuni-*.db")))[:-a.keep]
    for f in old:
        os.remove(f)
    print(f"Respaldo: {dest} ({os.path.getsize(dest)} bytes); se conservan los últimos {a.keep}, borrados {len(old)} viejos")


if __name__ == "__main__":
    main()
