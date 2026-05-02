#!/usr/bin/env python3
"""
Cell Tower Scanner - rtl_power based signal scanner
No dependency on gr-gsm or grgsm_livemon_headless.
"""

import subprocess
import sqlite3
import json
import time
import os
import argparse
import requests
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "towers.db"
CONFIG_PATH = Path(__file__).parent / "config.json"

GSM_BAND_RANGES = {
    "GSM-900":  (935_000_000, 960_000_000),
    "GSM-1800": (1_805_000_000, 1_880_000_000),
    "GSM-850":  (869_000_000, 894_000_000),
    "GSM-1900": (1_930_000_000, 1_990_000_000),
}

BIN_SIZE = 100_000  # 100kHz bins

def load_config():
    defaults = {
        "lat": None, "lon": None,
        "opencellid_token": "",
        "scan_duration": 60,
        "bands": ["GSM-900", "GSM-1800"],
        "ppm": 0, "gain": 40,
        "signal_threshold": 15.0,
    }
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return {**defaults, **json.load(f)}
    return defaults

def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS towers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mcc INTEGER, mnc INTEGER, lac INTEGER, cell_id INTEGER,
            arfcn INTEGER, freq_mhz REAL, band TEXT, signal_dbm REAL,
            lat REAL, lon REAL,
            observer_lat REAL, observer_lon REAL,
            operator TEXT, country TEXT,
            first_seen TEXT, last_seen TEXT,
            seen_count INTEGER DEFAULT 1,
            UNIQUE(mcc, mnc, lac, cell_id)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS detections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            arfcn INTEGER, freq_mhz REAL, band TEXT,
            signal_dbm REAL, noise_floor REAL,
            observer_lat REAL, observer_lon REAL,
            timestamp TEXT,
            UNIQUE(arfcn, band)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mcc INTEGER, mnc INTEGER, lac INTEGER, cell_id INTEGER,
            arfcn INTEGER, signal_dbm REAL,
            observer_lat REAL, observer_lon REAL, timestamp TEXT
        )
    """)
    conn.commit()
    conn.close()

def freq_to_arfcn(freq_hz, band):
    f = freq_hz / 1e6
    if band == "GSM-900":
        a = round((f - 935.0) / 0.2) + 1
        if 1 <= a <= 124: return a
        a = round((f - 935.0) / 0.2) + 1024
        if 975 <= a <= 1023: return a
    elif band == "GSM-1800":
        a = round((f - 1805.2) / 0.2) + 512
        if 512 <= a <= 885: return a
    elif band == "GSM-850":
        a = round((f - 869.2) / 0.2) + 128
        if 128 <= a <= 251: return a
    elif band == "GSM-1900":
        a = round((f - 1930.2) / 0.2) + 512
        if 512 <= a <= 810: return a
    return None

def arfcn_to_freq_mhz(arfcn, band):
    if band == "GSM-900":
        if 1 <= arfcn <= 124:    return 935.0 + 0.2 * (arfcn - 1)
        if 975 <= arfcn <= 1023: return 935.0 + 0.2 * (arfcn - 1024)
    elif band == "GSM-1800":
        if 512 <= arfcn <= 885:  return 1805.2 + 0.2 * (arfcn - 512)
    elif band == "GSM-850":
        if 128 <= arfcn <= 251:  return 869.2 + 0.2 * (arfcn - 128)
    elif band == "GSM-1900":
        if 512 <= arfcn <= 810:  return 1930.2 + 0.2 * (arfcn - 512)
    return None

def run_rtl_power(freq_start, freq_end, bin_hz, gain, ppm, duration_sec):
    cmd = [
        "rtl_power",
        "-f", f"{freq_start}:{freq_end}:{bin_hz}",
        "-g", str(gain),
        "-p", str(ppm),
        "-i", "5",
        "-e", str(duration_sec),
        "-"
    ]
    print(f"    cmd: {' '.join(cmd)}")
    rows = []
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=duration_sec + 30)
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parts = line.split(', ')
                if len(parts) < 7:
                    continue
                freq_low  = float(parts[2])
                freq_high = float(parts[3])
                bin_size  = float(parts[4])
                powers    = [float(x) for x in parts[6:]]
                if powers:
                    rows.append({"freq_low": freq_low, "freq_high": freq_high,
                                 "bin_size": bin_size, "powers": powers})
            except (ValueError, IndexError):
                continue
    except subprocess.TimeoutExpired:
        print("    rtl_power timed out")
    except FileNotFoundError:
        print("    rtl_power not found")
    except Exception as e:
        print(f"    rtl_power error: {e}")
    return rows

def find_carriers(rows, band, threshold_db=15.0):
    freq_powers = {}
    for row in rows:
        for i, pwr in enumerate(row["powers"]):
            freq = row["freq_low"] + row["bin_size"] * i
            if freq not in freq_powers:
                freq_powers[freq] = []
            freq_powers[freq].append(pwr)

    if not freq_powers:
        return []

    avg_powers = {f: sum(v)/len(v) for f, v in freq_powers.items()}
    all_vals = sorted(avg_powers.values())
    noise_floor = all_vals[len(all_vals) // 2]

    best = {}
    for freq_hz, pwr in avg_powers.items():
        if pwr - noise_floor >= threshold_db:
            arfcn = freq_to_arfcn(freq_hz, band)
            if arfcn:
                snapped = arfcn_to_freq_mhz(arfcn, band)
                if snapped and (arfcn not in best or pwr > best[arfcn]["signal_dbm"]):
                    best[arfcn] = {
                        "freq_mhz": snapped, "signal_dbm": pwr,
                        "noise_floor": noise_floor, "arfcn": arfcn, "band": band
                    }

    return sorted(best.values(), key=lambda x: x["signal_dbm"], reverse=True)

MCC_OPERATORS = {
    (230, 1): ("T-Mobile CZ",    "Czech Republic"),
    (230, 2): ("O2 CZ",          "Czech Republic"),
    (230, 3): ("Vodafone CZ",    "Czech Republic"),
    (230, 4): ("Nordic Telecom", "Czech Republic"),
    (230, 6): ("Sazka Mobil",    "Czech Republic"),
}

def lookup_tower_mozilla(mcc, mnc, lac, cell_id):
    if not all([mcc, mnc, lac, cell_id]):
        return None
    try:
        r = requests.post("https://location.services.mozilla.com/v1/geolocate?key=test",
            json={"cellTowers": [{"radioType": "gsm", "mobileCountryCode": mcc,
                "mobileNetworkCode": mnc, "locationAreaCode": lac, "cellId": cell_id}]},
            timeout=5)
        if r.status_code == 200:
            loc = r.json().get("location", {})
            if "lat" in loc:
                return float(loc["lat"]), float(loc["lng"])
    except Exception:
        pass
    return None

def lookup_tower_opencellid(mcc, mnc, lac, cell_id, token):
    if not token or not all([mcc, mnc, lac, cell_id]):
        return None
    try:
        r = requests.get("https://opencellid.org/cell/get", timeout=5,
            params={"key": token, "mcc": mcc, "mnc": mnc,
                    "lac": lac, "cellid": cell_id, "format": "json"})
        if r.status_code == 200:
            d = r.json()
            if "lat" in d:
                return float(d["lat"]), float(d["lon"])
    except Exception:
        pass
    return None

def store_detection(carrier, observer_lat, observer_lon):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = datetime.utcnow().isoformat()
    c.execute("""
        INSERT INTO detections (arfcn, freq_mhz, band, signal_dbm, noise_floor,
            observer_lat, observer_lon, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(arfcn, band) DO UPDATE SET
            signal_dbm = excluded.signal_dbm,
            timestamp  = excluded.timestamp
    """, (carrier["arfcn"], carrier["freq_mhz"], carrier["band"],
          carrier["signal_dbm"], carrier["noise_floor"],
          observer_lat, observer_lon, now))
    conn.commit()
    conn.close()

def run_scan(config, bands=None):
    bands     = bands or config.get("bands", ["GSM-900"])
    obs_lat   = config.get("lat")
    obs_lon   = config.get("lon")
    duration  = config.get("scan_duration", 60)
    gain      = config.get("gain", 40)
    ppm       = config.get("ppm", 0)
    threshold = config.get("signal_threshold", 15.0)

    if not obs_lat or not obs_lon:
        print("WARNING: No GPS coordinates set. Use --lat and --lon.\n")

    total = 0
    for band in bands:
        freq_start, freq_end = GSM_BAND_RANGES[band]
        print(f"\n[{band}] {freq_start/1e6:.0f}–{freq_end/1e6:.0f} MHz | "
              f"{duration}s | gain={gain} | ppm={ppm}")

        rows = run_rtl_power(freq_start, freq_end, BIN_SIZE, gain, ppm, duration)
        if not rows:
            print(f"  No data from rtl_power.")
            continue

        print(f"  Received {len(rows)} sweep(s). Analysing...")
        carriers = find_carriers(rows, band, threshold_db=threshold)

        if not carriers:
            print(f"  No carriers found above noise+{threshold}dB.")
            print(f"  Try: --threshold 10  or  --gain 45")
            continue

        print(f"  {len(carriers)} carrier(s) detected:")
        for c in carriers:
            above = c['signal_dbm'] - c['noise_floor']
            print(f"    ARFCN {c['arfcn']:4d}  {c['freq_mhz']:.1f} MHz  "
                  f"{c['signal_dbm']:.1f} dBm  (+{above:.1f} dB above noise)")
            store_detection(c, obs_lat, obs_lon)
            total += 1

    print(f"\n{'─'*50}")
    print(f"Done. {total} carrier(s) stored.")
    show_stats()

def show_stats():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM towers");     t = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM detections"); d = c.fetchone()[0]
    conn.close()
    print(f"\nDatabase: {t} identified towers | {d} detected carriers")

def diagnose():
    print("=== Diagnostic ===\n")
    print("[1] RTL-SDR:")
    r = subprocess.run(["rtl_test", "-t"], capture_output=True, text=True, timeout=8)
    for line in (r.stdout + r.stderr).splitlines():
        if any(x in line for x in ["Found", "Tuner", "E4000", "R820", "error"]):
            print(f"    {line.strip()}")

    print("\n[2] rtl_power 10s test at 950 MHz:")
    r = subprocess.run(
        ["rtl_power", "-f", "949M:951M:100k", "-g", "40", "-i", "5", "-e", "10", "-"],
        capture_output=True, text=True, timeout=20)
    lines = [l for l in r.stdout.splitlines() if l.strip()]
    if lines:
        print(f"    OK — {len(lines)} line(s) of data")
        try:
            vals = [float(x) for x in lines[0].split(', ')[6:]]
            print(f"    Peak: {max(vals):.1f} dBm  Noise: {min(vals):.1f} dBm")
        except Exception:
            pass
    else:
        print(f"    No output. stderr: {r.stderr[:150]}")

def main():
    parser = argparse.ArgumentParser(description="Cell Tower Scanner")
    parser.add_argument("--scan",      action="store_true")
    parser.add_argument("--diagnose",  action="store_true")
    parser.add_argument("--stats",     action="store_true")
    parser.add_argument("--bands",     nargs="+", choices=list(GSM_BAND_RANGES.keys()))
    parser.add_argument("--lat",       type=float)
    parser.add_argument("--lon",       type=float)
    parser.add_argument("--ppm",       type=int)
    parser.add_argument("--gain",      type=int)
    parser.add_argument("--duration",  type=int)
    parser.add_argument("--threshold", type=float, help="dB above noise floor (default 15)")
    parser.add_argument("--token",     help="OpenCelliD API token")
    args = parser.parse_args()

    init_db()
    config = load_config()

    if args.lat:                  config["lat"] = args.lat
    if args.lon:                  config["lon"] = args.lon
    if args.ppm is not None:      config["ppm"] = args.ppm
    if args.gain:                 config["gain"] = args.gain
    if args.duration:             config["scan_duration"] = args.duration
    if args.threshold:            config["signal_threshold"] = args.threshold
    if args.token:                config["opencellid_token"] = args.token
    save_config(config)

    if args.diagnose:   diagnose()
    elif args.stats:    show_stats()
    elif args.scan:     run_scan(config, bands=args.bands)
    else:
        parser.print_help()
        print("\nExamples:")
        print("  python3 scan.py --diagnose")
        print("  python3 scan.py --scan --lat 50.0759 --lon 14.4378 --bands GSM-900 GSM-1800")
        print("  python3 scan.py --scan --bands GSM-900 --duration 120 --threshold 10")

if __name__ == "__main__":
    main()
