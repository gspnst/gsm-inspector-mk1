#!/usr/bin/env python3
"""
Cell Tower Scanner v2
─────────────────────
Backend : rtl_power  (RF carrier detection, no gr-gsm needed)
Identity: OpenCelliD CSV database (offline ARFCN → MCC/MNC/LAC/CI lookup)
Fallback: OpenCelliD API / Mozilla Location Services (online)
Map     : server.py  →  http://<pi>:5000
"""

import subprocess
import sqlite3
import csv
import gzip
import json
import os
import argparse
import requests
from datetime import datetime, timezone
from pathlib import Path

DB_PATH     = Path(__file__).parent / "towers.db"
CONFIG_PATH = Path(__file__).parent / "config.json"
OCID_PATH   = Path(__file__).parent / "cell_towers.csv.gz"   # or .csv

# ── Band definitions ──────────────────────────────────────────────────────────

GSM_BAND_RANGES = {
    "GSM-900":  (935_000_000, 960_000_000),
    "GSM-1800": (1_805_000_000, 1_880_000_000),
    "GSM-850":  (869_000_000,   894_000_000),
    "GSM-1900": (1_930_000_000, 1_990_000_000),
}

BIN_SIZE = 100_000  # 100 kHz — resolves individual GSM 200 kHz carriers

# ── Config ────────────────────────────────────────────────────────────────────

def load_config():
    defaults = {
        "lat": None, "lon": None,
        "opencellid_token": "",
        "scan_duration": 60,
        "bands": ["GSM-900", "GSM-1800"],
        "ppm": 0, "gain": 40,
        "signal_threshold": 12.0,
    }
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return {**defaults, **json.load(f)}
    return defaults

def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)

# ── Database ──────────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS towers (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            mcc         INTEGER,
            mnc         INTEGER,
            lac         INTEGER,
            cell_id     INTEGER,
            arfcn       INTEGER,
            freq_mhz    REAL,
            band        TEXT,
            signal_dbm  REAL,
            lat         REAL,
            lon         REAL,
            range_m     INTEGER,
            observer_lat REAL,
            observer_lon REAL,
            operator    TEXT,
            country     TEXT,
            first_seen  TEXT,
            last_seen   TEXT,
            seen_count  INTEGER DEFAULT 1,
            UNIQUE(mcc, mnc, lac, cell_id)
        );

        CREATE TABLE IF NOT EXISTS detections (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            arfcn       INTEGER,
            freq_mhz    REAL,
            band        TEXT,
            signal_dbm  REAL,
            noise_floor REAL,
            observer_lat REAL,
            observer_lon REAL,
            timestamp   TEXT,
            UNIQUE(arfcn, band)
        );

        CREATE TABLE IF NOT EXISTS observations (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            mcc         INTEGER,
            mnc         INTEGER,
            lac         INTEGER,
            cell_id     INTEGER,
            arfcn       INTEGER,
            signal_dbm  REAL,
            observer_lat REAL,
            observer_lon REAL,
            timestamp   TEXT
        );
    """)
    conn.commit()
    conn.close()

# ── ARFCN ↔ Frequency ─────────────────────────────────────────────────────────

def freq_to_arfcn(freq_hz, band):
    f = freq_hz / 1e6
    if band == "GSM-900":
        a = round((f - 935.0) / 0.2) + 1
        if 1 <= a <= 124:    return a
        a = round((f - 935.0) / 0.2) + 1024
        if 975 <= a <= 1023: return a
    elif band == "GSM-1800":
        a = round((f - 1805.2) / 0.2) + 512
        if 512 <= a <= 885:  return a
    elif band == "GSM-850":
        a = round((f - 869.2) / 0.2) + 128
        if 128 <= a <= 251:  return a
    elif band == "GSM-1900":
        a = round((f - 1930.2) / 0.2) + 512
        if 512 <= a <= 810:  return a
    return None

def arfcn_to_freq_mhz(arfcn, band):
    if band == "GSM-900":
        if 1 <= arfcn <= 124:    return round(935.0 + 0.2 * (arfcn - 1),    1)
        if 975 <= arfcn <= 1023: return round(935.0 + 0.2 * (arfcn - 1024), 1)
    elif band == "GSM-1800":
        if 512 <= arfcn <= 885:  return round(1805.2 + 0.2 * (arfcn - 512), 1)
    elif band == "GSM-850":
        if 128 <= arfcn <= 251:  return round(869.2 + 0.2 * (arfcn - 128),  1)
    elif band == "GSM-1900":
        if 512 <= arfcn <= 810:  return round(1930.2 + 0.2 * (arfcn - 512), 1)
    return None

# ── rtl_power ─────────────────────────────────────────────────────────────────

def run_rtl_power(freq_start, freq_end, bin_hz, gain, ppm, duration_sec):
    cmd = [
        "rtl_power",
        "-f", f"{freq_start}:{freq_end}:{bin_hz}",
        "-g", str(gain),
        "-p", str(ppm),
        "-i", "5",
        "-e", str(duration_sec),
        "-",
    ]
    print(f"    {' '.join(cmd)}")
    rows = []
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=duration_sec + 30
        )
        for line in result.stdout.splitlines():
            try:
                parts = line.strip().split(", ")
                if len(parts) < 7:
                    continue
                rows.append({
                    "freq_low":  float(parts[2]),
                    "freq_high": float(parts[3]),
                    "bin_size":  float(parts[4]),
                    "powers":    [float(x) for x in parts[6:]],
                })
            except (ValueError, IndexError):
                continue
    except subprocess.TimeoutExpired:
        print("    rtl_power timed out")
    except FileNotFoundError:
        print("    rtl_power not found — is rtl-sdr installed?")
    except Exception as e:
        print(f"    rtl_power error: {e}")
    return rows

def find_carriers(rows, band, threshold_db=12.0):
    freq_powers = {}
    for row in rows:
        for i, pwr in enumerate(row["powers"]):
            freq = row["freq_low"] + row["bin_size"] * i
            freq_powers.setdefault(freq, []).append(pwr)

    if not freq_powers:
        return []

    avg = {f: sum(v) / len(v) for f, v in freq_powers.items()}
    vals = sorted(avg.values())
    noise = vals[len(vals) // 2]

    best = {}
    for freq_hz, pwr in avg.items():
        if pwr - noise >= threshold_db:
            arfcn = freq_to_arfcn(freq_hz, band)
            if arfcn:
                snapped = arfcn_to_freq_mhz(arfcn, band)
                if snapped and (arfcn not in best or pwr > best[arfcn]["signal_dbm"]):
                    best[arfcn] = {
                        "arfcn": arfcn, "freq_mhz": snapped, "band": band,
                        "signal_dbm": round(pwr, 1),
                        "noise_floor": round(noise, 1),
                    }

    return sorted(best.values(), key=lambda x: x["signal_dbm"], reverse=True)

# ── OpenCelliD CSV lookup ─────────────────────────────────────────────────────

def load_ocid_csv(path):
    """
    Load OpenCelliD CSV (or .gz) into a dict keyed by ARFCN for fast lookup.
    CSV columns: radio,mcc,net,area,cell,unit,lon,lat,range,samples,changeable,
                 created,updated,averageSignal
    We only keep GSM rows and index by (arfcn-equivalent freq).
    Since OCID doesn't store ARFCN directly, we compute it from the cell/area.
    Primary lookup is by (mcc, mnc, lac, cell_id).
    Returns list of dicts for proximity search.
    """
    print(f"Loading OpenCelliD database from {path}...")
    towers = []
    opener = gzip.open if str(path).endswith(".gz") else open

    try:
        with opener(path, "rt", encoding="utf-8", errors="ignore") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("radio", "").upper() not in ("GSM", "UMTS", "LTE"):
                    continue
                try:
                    towers.append({
                        "radio":   row["radio"].upper(),
                        "mcc":     int(row["mcc"]),
                        "mnc":     int(row["net"]),
                        "lac":     int(row["area"]),
                        "cell_id": int(row["cell"]),
                        "lon":     float(row["lon"]),
                        "lat":     float(row["lat"]),
                        "range_m": int(row.get("range", 0) or 0),
                    })
                except (ValueError, KeyError):
                    continue
        print(f"  Loaded {len(towers):,} tower records.")
    except FileNotFoundError:
        print(f"  File not found: {path}")
    except Exception as e:
        print(f"  Error loading CSV: {e}")

    return towers

def find_nearby_gsm_towers(ocid_towers, observer_lat, observer_lon,
                            radius_km=10, radio_filter="GSM"):
    """Return all towers within radius_km of observer position."""
    import math
    results = []
    for t in ocid_towers:
        if radio_filter and t["radio"] != radio_filter:
            continue
        # Haversine distance
        dlat = math.radians(t["lat"] - observer_lat)
        dlon = math.radians(t["lon"] - observer_lon)
        a = (math.sin(dlat/2)**2 +
             math.cos(math.radians(observer_lat)) *
             math.cos(math.radians(t["lat"])) *
             math.sin(dlon/2)**2)
        dist_km = 6371 * 2 * math.asin(math.sqrt(a))
        if dist_km <= radius_km:
            results.append({**t, "distance_km": round(dist_km, 2)})

    return sorted(results, key=lambda x: x["distance_km"])

# ── Operator table ────────────────────────────────────────────────────────────

MCC_MNC_OPERATORS = {
    (230, 1):  ("T-Mobile CZ",    "Czech Republic"),
    (230, 2):  ("O2 CZ",          "Czech Republic"),
    (230, 3):  ("Vodafone CZ",    "Czech Republic"),
    (230, 4):  ("Nordic Telecom", "Czech Republic"),
    (230, 6):  ("Sazka Mobil",    "Czech Republic"),
    (230, 98): ("Eltodo",         "Czech Republic"),
    (234, 10): ("O2 UK",          "United Kingdom"),
    (234, 20): ("3 UK",           "United Kingdom"),
    (234, 30): ("EE UK",          "United Kingdom"),
    (262, 1):  ("T-Mobile DE",    "Germany"),
    (262, 2):  ("Vodafone DE",    "Germany"),
    (262, 7):  ("O2 DE",          "Germany"),
    (208, 10): ("SFR",            "France"),
    (208, 20): ("Bouygues",       "France"),
    (208, 1):  ("Orange FR",      "France"),
}

def get_operator(mcc, mnc):
    return MCC_MNC_OPERATORS.get((mcc, mnc), (f"MCC{mcc}/MNC{mnc}", "Unknown"))

# ── Online fallback lookups ───────────────────────────────────────────────────

def lookup_opencellid_api(mcc, mnc, lac, cell_id, token):
    if not token or not all([mcc, mnc, lac, cell_id]):
        return None
    try:
        r = requests.get("https://opencellid.org/cell/get", timeout=5, params={
            "key": token, "mcc": mcc, "mnc": mnc,
            "lac": lac, "cellid": cell_id, "format": "json"})
        if r.status_code == 200:
            d = r.json()
            if "lat" in d:
                return float(d["lat"]), float(d["lon"]), int(d.get("range", 0))
    except Exception:
        pass
    return None

def lookup_mozilla(mcc, mnc, lac, cell_id):
    if not all([mcc, mnc, lac, cell_id]):
        return None
    try:
        r = requests.post(
            "https://location.services.mozilla.com/v1/geolocate?key=test",
            json={"cellTowers": [{"radioType": "gsm",
                "mobileCountryCode": mcc, "mobileNetworkCode": mnc,
                "locationAreaCode": lac, "cellId": cell_id}]},
            timeout=5)
        if r.status_code == 200:
            loc = r.json().get("location", {})
            if "lat" in loc:
                return float(loc["lat"]), float(loc["lng"]), 0
    except Exception:
        pass
    return None

# ── Store results ─────────────────────────────────────────────────────────────

def store_detection(carrier, obs_lat, obs_lon):
    conn = sqlite3.connect(DB_PATH)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        INSERT INTO detections
            (arfcn, freq_mhz, band, signal_dbm, noise_floor,
             observer_lat, observer_lon, timestamp)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(arfcn, band) DO UPDATE SET
            signal_dbm  = excluded.signal_dbm,
            timestamp   = excluded.timestamp
    """, (carrier["arfcn"], carrier["freq_mhz"], carrier["band"],
          carrier["signal_dbm"], carrier["noise_floor"], obs_lat, obs_lon, now))
    conn.commit()
    conn.close()

def store_tower(t, carrier, obs_lat, obs_lon, config):
    """Store a fully identified tower."""
    conn = sqlite3.connect(DB_PATH)
    now = datetime.now(timezone.utc).isoformat()

    # Try to get coordinates if not already in OCID record
    lat = t.get("lat")
    lon = t.get("lon")
    range_m = t.get("range_m", 0)

    if not lat:
        coords = lookup_opencellid_api(
            t["mcc"], t["mnc"], t["lac"], t["cell_id"],
            config.get("opencellid_token", ""))
        if not coords:
            coords = lookup_mozilla(t["mcc"], t["mnc"], t["lac"], t["cell_id"])
        if coords:
            lat, lon, range_m = coords

    operator, country = get_operator(t["mcc"], t["mnc"])

    conn.execute("""
        INSERT INTO towers
            (mcc, mnc, lac, cell_id, arfcn, freq_mhz, band, signal_dbm,
             lat, lon, range_m, observer_lat, observer_lon,
             operator, country, first_seen, last_seen)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(mcc, mnc, lac, cell_id) DO UPDATE SET
            last_seen   = excluded.last_seen,
            seen_count  = seen_count + 1,
            signal_dbm  = excluded.signal_dbm,
            lat         = COALESCE(excluded.lat, towers.lat),
            lon         = COALESCE(excluded.lon, towers.lon)
    """, (t["mcc"], t["mnc"], t["lac"], t["cell_id"],
          carrier["arfcn"], carrier["freq_mhz"], carrier["band"],
          carrier["signal_dbm"], lat, lon, range_m,
          obs_lat, obs_lon, operator, country, now, now))

    conn.execute("""
        INSERT INTO observations
            (mcc, mnc, lac, cell_id, arfcn, signal_dbm,
             observer_lat, observer_lon, timestamp)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (t["mcc"], t["mnc"], t["lac"], t["cell_id"],
          carrier["arfcn"], carrier["signal_dbm"], obs_lat, obs_lon, now))

    conn.commit()
    conn.close()
    return lat, lon

# ── Main scan ─────────────────────────────────────────────────────────────────

def run_scan(config, bands=None):
    bands     = bands or config.get("bands", ["GSM-900", "GSM-1800"])
    obs_lat   = config.get("lat")
    obs_lon   = config.get("lon")
    duration  = config.get("scan_duration", 60)
    gain      = config.get("gain", 40)
    ppm       = config.get("ppm", 0)
    threshold = config.get("signal_threshold", 12.0)

    if not obs_lat or not obs_lon:
        print("WARNING: No observer coordinates set. Use --lat and --lon.\n")

    # Load OCID CSV if available
    ocid_towers = []
    if OCID_PATH.exists():
        ocid_towers = load_ocid_csv(OCID_PATH)
    else:
        # Check for uncompressed version
        uncompressed = OCID_PATH.with_suffix("")
        if uncompressed.exists():
            ocid_towers = load_ocid_csv(uncompressed)
        else:
            print(f"No OpenCelliD CSV found at {OCID_PATH}")
            print("Tower identity lookup will use online API only.\n")

    total_carriers = 0
    total_towers   = 0

    for band in bands:
        freq_start, freq_end = GSM_BAND_RANGES[band]
        print(f"\n[{band}] {freq_start/1e6:.0f}–{freq_end/1e6:.0f} MHz | "
              f"{duration}s | gain={gain} | ppm={ppm} | threshold={threshold}dB")

        rows = run_rtl_power(freq_start, freq_end, BIN_SIZE, gain, ppm, duration)
        if not rows:
            print("  No data from rtl_power.")
            continue

        print(f"  {len(rows)} sweep(s) received. Analysing...")
        carriers = find_carriers(rows, band, threshold_db=threshold)

        if not carriers:
            print(f"  No carriers above noise+{threshold}dB. "
                  f"Try --threshold {threshold-3:.0f} or --gain {gain+5}")
            continue

        print(f"  {len(carriers)} carrier(s) detected:")
        for c in carriers:
            above = c["signal_dbm"] - c["noise_floor"]
            print(f"    ARFCN {c['arfcn']:4d}  {c['freq_mhz']:.1f} MHz  "
                  f"{c['signal_dbm']:.1f} dBm  (+{above:.1f} dB above noise)")

            store_detection(c, obs_lat, obs_lon)
            total_carriers += 1

            # Try OCID CSV lookup first (nearby towers on this band)
            matched = False
            if ocid_towers and obs_lat and obs_lon:
                nearby = find_nearby_gsm_towers(
                    ocid_towers, obs_lat, obs_lon, radius_km=15, radio_filter="GSM")
                if nearby:
                    # Pick the closest tower as the likely match for this carrier
                    # In a real deployment you'd match by ARFCN — but OCID CSV
                    # doesn't include ARFCN. So we list all nearby towers.
                    print(f"      → {len(nearby)} GSM tower(s) within 15km in OCID database:")
                    for nt in nearby[:5]:  # show top 5 closest
                        op, country = get_operator(nt["mcc"], nt["mnc"])
                        lat_lon = store_tower(nt, c, obs_lat, obs_lon, config)
                        print(f"        {op} | MCC={nt['mcc']} MNC={nt['mnc']} "
                              f"LAC={nt['lac']} CI={nt['cell_id']} | "
                              f"{nt['distance_km']}km away")
                        total_towers += 1
                    matched = True

            if not matched:
                print(f"      → No OCID CSV match. Carrier stored as detection only.")

    print(f"\n{'─'*54}")
    print(f"Scan complete.")
    print(f"  Carriers detected : {total_carriers}")
    print(f"  Towers identified : {total_towers}")
    show_stats()

# ── Import only nearby towers from OCID CSV ───────────────────────────────────

def import_ocid(config, radius_km=15):
    """
    Import all towers from OCID CSV within radius_km of observer.
    Useful for pre-populating the map without running a scan.
    """
    obs_lat = config.get("lat")
    obs_lon = config.get("lon")

    if not obs_lat or not obs_lon:
        print("ERROR: --lat and --lon required for import.")
        return

    ocid_towers = []
    if OCID_PATH.exists():
        ocid_towers = load_ocid_csv(OCID_PATH)
    else:
        uncompressed = OCID_PATH.with_suffix("")
        if uncompressed.exists():
            ocid_towers = load_ocid_csv(uncompressed)

    if not ocid_towers:
        print("No OCID database found.")
        return

    print(f"\nSearching within {radius_km}km of {obs_lat:.4f}, {obs_lon:.4f}...")
    nearby = find_nearby_gsm_towers(ocid_towers, obs_lat, obs_lon,
                                     radius_km=radius_km, radio_filter="GSM")
    print(f"Found {len(nearby)} GSM towers nearby.\n")

    conn = sqlite3.connect(DB_PATH)
    now = datetime.now(timezone.utc).isoformat()
    inserted = 0
    for t in nearby:
        operator, country = get_operator(t["mcc"], t["mnc"])
        try:
            conn.execute("""
                INSERT INTO towers
                    (mcc, mnc, lac, cell_id, freq_mhz, band,
                     lat, lon, range_m, operator, country,
                     first_seen, last_seen)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(mcc, mnc, lac, cell_id) DO UPDATE SET
                    lat      = COALESCE(excluded.lat, towers.lat),
                    lon      = COALESCE(excluded.lon, towers.lon),
                    last_seen = excluded.last_seen
            """, (t["mcc"], t["mnc"], t["lac"], t["cell_id"],
                  None, "GSM", t["lat"], t["lon"], t["range_m"],
                  operator, country, now, now))
            inserted += 1
        except Exception as e:
            pass

    conn.commit()
    conn.close()
    print(f"Imported {inserted} towers into database.")
    show_stats()

# ── Stats ─────────────────────────────────────────────────────────────────────

def show_stats():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM towers");          total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM towers WHERE lat IS NOT NULL"); mapped = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM detections");      dets  = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM observations");    obs   = c.fetchone()[0]
    c.execute("""SELECT operator, COUNT(*) n FROM towers
                 GROUP BY operator ORDER BY n DESC LIMIT 8""")
    ops = c.fetchall()
    conn.close()

    print(f"\n{'═'*40}")
    print(f" Database summary")
    print(f"{'─'*40}")
    print(f" Towers (identified) : {total} ({mapped} with GPS)")
    print(f" Raw carrier detects : {dets}")
    print(f" Observations        : {obs}")
    if ops:
        print(f"\n Top operators:")
        for op, n in ops:
            print(f"   {op:<28} {n}")
    print(f"{'═'*40}")

# ── Diagnostics ───────────────────────────────────────────────────────────────

def diagnose():
    print("=== Diagnostic ===\n")

    print("[1] RTL-SDR device:")
    try:
        r = subprocess.run(["rtl_test", "-t"], capture_output=True,
                           text=True, timeout=5)
        out = r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        # rtl_test -t runs forever; timeout is expected — grab what we got
        out = ""
    # Re-run just for device info
    try:
        p = subprocess.Popen(["rtl_test"], capture_output=True, text=True)
        import time; time.sleep(3); p.terminate()
        out = p.stdout.read() + p.stderr.read() if p.stdout else ""
    except Exception as e:
        out = str(e)
    for kw in ["Found", "Tuner", "E4000", "R820", "Crystal", "error", "No supported"]:
        for line in out.splitlines():
            if kw.lower() in line.lower():
                print(f"    {line.strip()}")
                break

    print("\n[2] rtl_power 10s test at 950 MHz:")
    try:
        r = subprocess.run(
            ["rtl_power", "-f", "949M:951M:100k", "-g", "40",
             "-i", "5", "-e", "10", "-"],
            capture_output=True, text=True, timeout=20)
        lines = [l for l in r.stdout.splitlines() if l.strip()]
        if lines:
            print(f"    OK — {len(lines)} data line(s)")
            vals = [float(x) for x in lines[0].split(", ")[6:]]
            print(f"    Peak: {max(vals):.1f} dBm  Noise: {min(vals):.1f} dBm  "
                  f"Δ: {max(vals)-min(vals):.1f} dB")
        else:
            print(f"    No output. stderr: {r.stderr[:200]}")
    except subprocess.TimeoutExpired:
        print("    Timed out")
    except FileNotFoundError:
        print("    rtl_power not found")

    print("\n[3] OpenCelliD CSV:")
    if OCID_PATH.exists():
        size_mb = OCID_PATH.stat().st_size / 1e6
        print(f"    Found: {OCID_PATH} ({size_mb:.1f} MB)")
    else:
        uncompressed = OCID_PATH.with_suffix("")
        if uncompressed.exists():
            size_mb = uncompressed.stat().st_size / 1e6
            print(f"    Found (uncompressed): {uncompressed} ({size_mb:.1f} MB)")
        else:
            print(f"    Not found at {OCID_PATH}")
            print(f"    Download: see README.md for instructions")

    print("\n[4] gr-gsm (optional decoder):")
    for tool in ["grgsm_scanner", "grgsm_livemon_headless"]:
        r = subprocess.run(["which", tool], capture_output=True, text=True)
        status = r.stdout.strip() if r.returncode == 0 else "not found"
        print(f"    {tool}: {status}")

    print("\nDiagnostic complete.\n")

# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Cell Tower Scanner v2",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 scan.py --diagnose
  python3 scan.py --import-ocid --lat 50.0759 --lon 14.4378 --radius 10
  python3 scan.py --scan --lat 50.0759 --lon 14.4378 --bands GSM-900 GSM-1800
  python3 scan.py --scan --duration 120 --threshold 10 --gain 45
  python3 scan.py --stats
        """
    )
    parser.add_argument("--scan",        action="store_true", help="Run RF scan")
    parser.add_argument("--import-ocid", action="store_true", help="Import nearby towers from OCID CSV")
    parser.add_argument("--diagnose",    action="store_true", help="Hardware diagnostic")
    parser.add_argument("--stats",       action="store_true", help="Database stats")
    parser.add_argument("--bands",       nargs="+", choices=list(GSM_BAND_RANGES.keys()))
    parser.add_argument("--lat",         type=float, help="Observer latitude")
    parser.add_argument("--lon",         type=float, help="Observer longitude")
    parser.add_argument("--ppm",         type=int,   help="SDR frequency correction (PPM)")
    parser.add_argument("--gain",        type=int,   help="SDR gain 0-50 (default 40)")
    parser.add_argument("--duration",    type=int,   help="Scan duration seconds (default 60)")
    parser.add_argument("--threshold",   type=float, help="dB above noise floor (default 12)")
    parser.add_argument("--radius",      type=float, default=15, help="OCID import radius km (default 15)")
    parser.add_argument("--token",       help="OpenCelliD API token")
    args = parser.parse_args()

    init_db()
    config = load_config()

    if args.lat:              config["lat"] = args.lat
    if args.lon:              config["lon"] = args.lon
    if args.ppm is not None:  config["ppm"] = args.ppm
    if args.gain:             config["gain"] = args.gain
    if args.duration:         config["scan_duration"] = args.duration
    if args.threshold:        config["signal_threshold"] = args.threshold
    if args.token:            config["opencellid_token"] = args.token
    save_config(config)

    if args.diagnose:
        diagnose()
    elif args.stats:
        show_stats()
    elif args.import_ocid:
        import_ocid(config, radius_km=args.radius)
    elif args.scan:
        run_scan(config, bands=args.bands)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
