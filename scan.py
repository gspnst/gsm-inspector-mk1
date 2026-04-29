#!/usr/bin/env python3
"""
Cell Tower Scanner - Passive GSM BCCH scanner using RTL-SDR
Requires: gr-gsm, rtl-sdr, sqlite3
"""

import subprocess
import sqlite3
import json
import time
import re
import os
import sys
import argparse
import requests
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "towers.db"
CONFIG_PATH = Path(__file__).parent / "config.json"

# GSM frequency bands (MHz) for Europe + global
GSM_BANDS = {
    "GSM-900":  list(range(935, 960, 2)),   # Downlink
    "GSM-1800": list(range(1805, 1880, 2)),  # DCS-1800 downlink
    "GSM-850":  list(range(869, 894, 2)),    # US/Americas
    "GSM-1900": list(range(1930, 1990, 2)),  # PCS-1900 US
}

def load_config():
    defaults = {
        "lat": None,
        "lon": None,
        "opencellid_token": "",
        "scan_duration": 15,
        "bands": ["GSM-900", "GSM-1800"],
        "ppm": 0,
        "gain": 40,
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
            mcc INTEGER,
            mnc INTEGER,
            lac INTEGER,
            cell_id INTEGER,
            arfcn INTEGER,
            freq_mhz REAL,
            band TEXT,
            signal_dbm REAL,
            lat REAL,
            lon REAL,
            observer_lat REAL,
            observer_lon REAL,
            operator TEXT,
            country TEXT,
            first_seen TEXT,
            last_seen TEXT,
            seen_count INTEGER DEFAULT 1,
            UNIQUE(mcc, mnc, lac, cell_id)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mcc INTEGER,
            mnc INTEGER,
            lac INTEGER,
            cell_id INTEGER,
            arfcn INTEGER,
            signal_dbm REAL,
            observer_lat REAL,
            observer_lon REAL,
            timestamp TEXT
        )
    """)
    conn.commit()
    conn.close()

def arfcn_to_freq(arfcn, band="GSM-900"):
    """Convert ARFCN to downlink frequency in MHz"""
    if band == "GSM-900":
        if 1 <= arfcn <= 124:
            return 935.0 + 0.2 * (arfcn - 1)
        elif 975 <= arfcn <= 1023:
            return 935.0 + 0.2 * (arfcn - 1024)
    elif band == "GSM-1800":
        if 512 <= arfcn <= 885:
            return 1805.2 + 0.2 * (arfcn - 512)
    elif band == "GSM-850":
        if 128 <= arfcn <= 251:
            return 869.2 + 0.2 * (arfcn - 128)
    elif band == "GSM-1900":
        if 512 <= arfcn <= 810:
            return 1930.2 + 0.2 * (arfcn - 512)
    return None

def scan_frequency(freq_mhz, duration=15, ppm=0, gain=40):
    """Run grgsm_livemon_headless and parse output"""
    print(f"  Scanning {freq_mhz:.1f} MHz for {duration}s...", end="", flush=True)

    cmd = [
        "grgsm_livemon_headless",
        f"--fc={int(freq_mhz * 1e6)}",
        f"--samp-rate=2000000",
        f"--ppm={ppm}",
        f"--gain={gain}",
    ]

    results = []
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        # Also pipe grgsm output to Wireshark decoder
        decoder_cmd = ["grgsm_decode", "--cfile", "/dev/stdin", "--mode=BCCH"]

        timeout = time.time() + duration
        output_lines = []
        while time.time() < timeout:
            try:
                line = proc.stdout.readline()
                if line:
                    output_lines.append(line)
            except:
                break

        proc.terminate()
        proc.wait(timeout=3)

        results = parse_grgsm_output(output_lines, freq_mhz)
        print(f" found {len(results)} cell(s)")
    except FileNotFoundError:
        print(" [grgsm not found - using kalibrate-rtl fallback]")
        results = scan_with_kalibrate(freq_mhz, duration, ppm, gain)
    except Exception as e:
        print(f" [error: {e}]")

    return results

def scan_with_kalibrate(freq_mhz, duration, ppm, gain):
    """
    Fallback: use kalibrate-rtl (kal) to detect GSM channels.
    Returns basic ARFCN info without full decode.
    """
    band_flag = "-b GSM900"
    if freq_mhz > 1800:
        band_flag = "-b DCS1800"
    elif freq_mhz > 1900:
        band_flag = "-b PCS1900"
    elif freq_mhz < 900:
        band_flag = "-b GSM850"

    try:
        result = subprocess.run(
            f"kal -s {band_flag} -e {ppm} -g {gain} -d 0",
            shell=True, capture_output=True, text=True, timeout=duration + 5
        )
        return parse_kal_output(result.stdout, freq_mhz)
    except Exception:
        return []

def parse_grgsm_output(lines, freq_mhz):
    """Parse gr-gsm BCCH broadcast output"""
    cells = {}
    # Patterns for BCCH System Info
    mcc_re = re.compile(r"MCC[:\s]+(\d+)")
    mnc_re = re.compile(r"MNC[:\s]+(\d+)")
    lac_re = re.compile(r"LAC[:\s]+(\d+)")
    ci_re  = re.compile(r"CI[:\s]+(\d+)")
    arfcn_re = re.compile(r"ARFCN[:\s]+(\d+)")
    pwr_re = re.compile(r"(-\d+)\s*dBm")

    current = {}
    for line in lines:
        if m := mcc_re.search(line):   current["mcc"] = int(m.group(1))
        if m := mnc_re.search(line):   current["mnc"] = int(m.group(1))
        if m := lac_re.search(line):   current["lac"] = int(m.group(1))
        if m := ci_re.search(line):    current["cell_id"] = int(m.group(1))
        if m := arfcn_re.search(line): current["arfcn"] = int(m.group(1))
        if m := pwr_re.search(line):   current["signal_dbm"] = float(m.group(1))

        # Complete record when we have the essentials
        if all(k in current for k in ("mcc", "mnc", "lac", "cell_id")):
            key = (current["mcc"], current["mnc"], current["lac"], current["cell_id"])
            if key not in cells:
                cells[key] = {**current, "freq_mhz": freq_mhz}
                current = {}

    return list(cells.values())

def parse_kal_output(text, freq_mhz):
    """Parse kalibrate-rtl output for ARFCN detection"""
    results = []
    for line in text.splitlines():
        m = re.search(r"chan:\s*(\d+)\s+.*power:\s*([\d.]+)", line)
        if m:
            results.append({
                "arfcn": int(m.group(1)),
                "freq_mhz": freq_mhz,
                "signal_dbm": -50.0,  # kalibrate doesn't give exact dBm
                "mcc": None,
                "mnc": None,
                "lac": None,
                "cell_id": None,
            })
    return results

def lookup_tower_opencellid(mcc, mnc, lac, cell_id, token):
    """Query OpenCelliD API for tower coordinates"""
    if not token or not all([mcc, mnc, lac, cell_id]):
        return None
    url = "https://opencellid.org/cell/get"
    params = {
        "key": token,
        "mcc": mcc,
        "mnc": mnc,
        "lac": lac,
        "cellid": cell_id,
        "format": "json",
    }
    try:
        r = requests.get(url, params=params, timeout=5)
        if r.status_code == 200:
            data = r.json()
            if "lat" in data and "lon" in data:
                return float(data["lat"]), float(data["lon"])
    except Exception:
        pass
    return None

def lookup_tower_mozilla(mcc, mnc, lac, cell_id):
    """Fallback: Mozilla Location Services (free, no key)"""
    if not all([mcc, mnc, lac, cell_id]):
        return None
    url = "https://location.services.mozilla.com/v1/geolocate?key=test"
    payload = {
        "cellTowers": [{
            "radioType": "gsm",
            "mobileCountryCode": mcc,
            "mobileNetworkCode": mnc,
            "locationAreaCode": lac,
            "cellId": cell_id,
        }]
    }
    try:
        r = requests.post(url, json=payload, timeout=5)
        if r.status_code == 200:
            loc = r.json().get("location", {})
            if "lat" in loc and "lng" in loc:
                return float(loc["lat"]), float(loc["lng"])
    except Exception:
        pass
    return None

MCC_OPERATORS = {
    (230, 1): ("T-Mobile CZ", "Czech Republic"),
    (230, 2): ("O2 CZ", "Czech Republic"),
    (230, 3): ("Vodafone CZ", "Czech Republic"),
    (230, 4): ("Nordic Telecom", "Czech Republic"),
    (230, 6): ("Sazka Mobil", "Czech Republic"),
    (234, 10): ("O2 UK", "United Kingdom"),
    (234, 20): ("3 UK", "United Kingdom"),
    (262, 1): ("T-Mobile DE", "Germany"),
    (262, 2): ("Vodafone DE", "Germany"),
    (208, 10): ("SFR", "France"),
    (208, 20): ("Bouygues", "France"),
}

def get_operator(mcc, mnc):
    return MCC_OPERATORS.get((mcc, mnc), (f"MCC{mcc}/MNC{mnc}", "Unknown"))

def store_tower(tower, observer_lat, observer_lon, config):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = datetime.utcnow().isoformat()

    mcc, mnc, lac, cell_id = tower.get("mcc"), tower.get("mnc"), tower.get("lac"), tower.get("cell_id")

    # Try to get tower coordinates
    tower_lat, tower_lon = None, None
    if all([mcc, mnc, lac, cell_id]):
        # Try OpenCelliD first
        coords = lookup_tower_opencellid(mcc, mnc, lac, cell_id, config.get("opencellid_token", ""))
        if coords:
            tower_lat, tower_lon = coords
        else:
            # Fallback to Mozilla
            coords = lookup_tower_mozilla(mcc, mnc, lac, cell_id)
            if coords:
                tower_lat, tower_lon = coords

    operator, country = get_operator(mcc, mnc) if mcc and mnc else ("Unknown", "Unknown")

    if mcc and mnc and lac and cell_id:
        c.execute("""
            INSERT INTO towers (mcc, mnc, lac, cell_id, arfcn, freq_mhz, band, signal_dbm,
                lat, lon, observer_lat, observer_lon, operator, country, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(mcc, mnc, lac, cell_id) DO UPDATE SET
                last_seen = excluded.last_seen,
                seen_count = seen_count + 1,
                signal_dbm = excluded.signal_dbm,
                lat = COALESCE(excluded.lat, towers.lat),
                lon = COALESCE(excluded.lon, towers.lon)
        """, (
            mcc, mnc, lac, cell_id,
            tower.get("arfcn"), tower.get("freq_mhz"), tower.get("band", ""),
            tower.get("signal_dbm"), tower_lat, tower_lon,
            observer_lat, observer_lon,
            operator, country, now, now
        ))

    c.execute("""
        INSERT INTO observations (mcc, mnc, lac, cell_id, arfcn, signal_dbm, observer_lat, observer_lon, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (mcc, mnc, lac, cell_id, tower.get("arfcn"), tower.get("signal_dbm"), observer_lat, observer_lon, now))

    conn.commit()
    conn.close()
    return tower_lat, tower_lon

def run_scan(config, bands=None):
    bands = bands or config.get("bands", ["GSM-900"])
    observer_lat = config.get("lat")
    observer_lon = config.get("lon")
    duration = config.get("scan_duration", 15)

    if not observer_lat or not observer_lon:
        print("WARNING: No GPS coordinates set. Run with --lat and --lon or edit config.json")
        print("         Tower positions will still be looked up but observer location won't be stored.\n")

    total_found = 0
    total_new = 0

    for band in bands:
        freqs = GSM_BANDS.get(band, [])
        print(f"\n[{band}] Scanning {len(freqs)} frequencies...")
        for freq in freqs:
            towers = scan_frequency(freq, duration=duration, ppm=config.get("ppm", 0), gain=config.get("gain", 40))
            for t in towers:
                t["band"] = band
                tlat, tlon = store_tower(t, observer_lat, observer_lon, config)
                total_found += 1
                loc_str = f"{tlat:.4f},{tlon:.4f}" if tlat else "coords unknown"
                print(f"    + Tower: MCC={t.get('mcc')} MNC={t.get('mnc')} "
                      f"LAC={t.get('lac')} CI={t.get('cell_id')} "
                      f"@ {t.get('freq_mhz'):.1f}MHz  [{loc_str}]")

    print(f"\nScan complete. Found {total_found} tower signals.")
    print(f"Database: {DB_PATH}")

def show_stats():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM towers")
    total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM towers WHERE lat IS NOT NULL")
    mapped = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM observations")
    obs = c.fetchone()[0]
    c.execute("SELECT operator, COUNT(*) as n FROM towers GROUP BY operator ORDER BY n DESC LIMIT 10")
    ops = c.fetchall()
    conn.close()

    print(f"\n=== Cell Tower Database Stats ===")
    print(f"Total unique towers : {total}")
    print(f"Towers with GPS     : {mapped}")
    print(f"Total observations  : {obs}")
    print(f"\nTop operators:")
    for op, n in ops:
        print(f"  {op:<30} {n} tower(s)")

def main():
    parser = argparse.ArgumentParser(description="Passive GSM Cell Tower Scanner")
    parser.add_argument("--scan", action="store_true", help="Run a scan")
    parser.add_argument("--stats", action="store_true", help="Show database stats")
    parser.add_argument("--bands", nargs="+", choices=list(GSM_BANDS.keys()), help="Bands to scan")
    parser.add_argument("--lat", type=float, help="Observer latitude")
    parser.add_argument("--lon", type=float, help="Observer longitude")
    parser.add_argument("--ppm", type=int, help="SDR frequency correction in PPM")
    parser.add_argument("--gain", type=int, help="SDR gain (0-50)")
    parser.add_argument("--duration", type=int, help="Seconds per frequency")
    parser.add_argument("--token", help="OpenCelliD API token")
    args = parser.parse_args()

    init_db()
    config = load_config()

    if args.lat: config["lat"] = args.lat
    if args.lon: config["lon"] = args.lon
    if args.ppm is not None: config["ppm"] = args.ppm
    if args.gain is not None: config["gain"] = args.gain
    if args.duration: config["scan_duration"] = args.duration
    if args.token: config["opencellid_token"] = args.token
    save_config(config)

    if args.stats:
        show_stats()
    elif args.scan:
        run_scan(config, bands=args.bands)
    else:
        parser.print_help()
        print("\nQuick start:")
        print("  python3 scan.py --scan --lat 50.0755 --lon 14.4378 --bands GSM-900 GSM-1800")
        print("  python3 scan.py --stats")
        print("  python3 server.py   (then open http://localhost:5000)")

if __name__ == "__main__":
    main()
