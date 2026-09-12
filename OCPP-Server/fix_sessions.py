#!/usr/bin/env python3
"""
Script de diagnostic et correction des sessions corrompues.
Placer dans E:\GIT\hassio-addons\OCPP-Server\ et executer ADDON ARRETE.
Usage: python fix_sessions.py [--apply]
Sans --apply : mode dry-run, affiche seulement ce qui serait corrige.
"""
import sys, os

DATA_DIR = r"C:\addon_configs\ocppserver"  # chemin par defaut HA
# Essayer plusieurs chemins possibles
for candidate in [
    r"C:\addon_configs\ocppserver",
    r"/addon_configs/ocppserver",
    "/data",
]:
    if os.path.exists(os.path.join(candidate, "ocpp_server.sqlite")):
        DATA_DIR = candidate
        break

DB_PATH = os.path.join(DATA_DIR, "ocpp_server.sqlite")
if not os.path.exists(DB_PATH):
    # Demander le chemin
    DB_PATH = input(f"Chemin vers ocpp_server.sqlite : ").strip()

print(f"Base : {DB_PATH}")

import sqlite3
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

apply = "--apply" in sys.argv

# 1. Lister toutes les sessions avec energy_wh suspect (> 50000 Wh = 50 kWh sans MV)
print("\n=== Sessions avec energy_wh potentiellement corrompu ===")
cur.execute("""
    SELECT t.id, t.charger_id, t.connector_id, t.meter_start, t.meter_stop,
           t.energy_wh, t.status, t.start_time, t.stop_time
    FROM transactions t
    WHERE t.is_external = 0
    ORDER BY t.id DESC
    LIMIT 20
""")
rows = cur.fetchall()
for r in rows:
    # Calculer l'énergie correcte depuis meter_start/meter_stop
    if r['meter_start'] is not None and r['meter_stop'] is not None:
        correct_wh = max(0.0, r['meter_stop'] - r['meter_start'])
    else:
        correct_wh = None
    flag = ""
    if r['energy_wh'] and correct_wh is not None and abs(r['energy_wh'] - correct_wh) > 100:
        flag = " <-- CORROMPU"
    print(f"  ID={r['id']} status={r['status']} "
          f"meter_start={r['meter_start']} meter_stop={r['meter_stop']} "
          f"energy_wh_stored={r['energy_wh']:.1f if r['energy_wh'] else 'None'} "
          f"correct={correct_wh:.1f if correct_wh is not None else 'None'}{flag}")

# 2. Lister les MeterValues Energy pour chaque session
print("\n=== MeterValues Energy par session ===")
cur.execute("""
    SELECT transaction_id, measurand, unit, COUNT(*) as cnt,
           MIN(value) as vmin, MAX(value) as vmax
    FROM meter_values
    WHERE measurand = 'Energy.Active.Import.Register'
    GROUP BY transaction_id
    ORDER BY transaction_id DESC
    LIMIT 10
""")
for r in cur.fetchall():
    print(f"  txn_id={r['transaction_id']} unit={r['unit']} "
          f"count={r['cnt']} min={r['vmin']:.1f} max={r['vmax']:.1f}")

# 3. Corriger les sessions corrompues
if apply:
    print("\n=== Application des corrections ===")
    cur.execute("""
        SELECT t.id, t.meter_start, t.meter_stop, t.charger_id
        FROM transactions t
        WHERE t.is_external = 0 AND t.status = 'completed'
          AND t.meter_start IS NOT NULL AND t.meter_stop IS NOT NULL
    """)
    fixed = 0
    for r in cur.fetchall():
        correct_wh = max(0.0, r['meter_stop'] - r['meter_start'])
        cur2 = conn.cursor()
        cur2.execute("SELECT energy_wh FROM transactions WHERE id=?", (r['id'],))
        stored = cur2.fetchone()['energy_wh']
        if stored is not None and abs(stored - correct_wh) > 100:
            conn.execute(
                "UPDATE transactions SET energy_wh=? WHERE id=?",
                (correct_wh, r['id'])
            )
            print(f"  ID={r['id']}: {stored:.1f} Wh -> {correct_wh:.1f} Wh")
            fixed += 1
    conn.commit()
    print(f"\n{fixed} session(s) corrigee(s).")
    print("Relancer l'addon pour que freeze_transaction_cost recalcule les couts.")
else:
    print("\nMode dry-run. Ajouter --apply pour appliquer les corrections.")

conn.close()
