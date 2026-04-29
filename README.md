# Cell Tower Scanner & Mapper

Passive GSM cell tower scanner for Raspberry Pi 5 + Nooelec NESDR SMArt XTR RTL-SDR.

## How It Works

Your RTL-SDR dongle receives **GSM broadcast channels (BCCH)** — unencrypted beacon
signals that every GSM base station transmits continuously. These contain:

- **MCC** (Mobile Country Code) — identifies the country
- **MNC** (Mobile Network Code) — identifies the operator
- **LAC** (Location Area Code) — identifies a zone
- **Cell ID** — uniquely identifies the tower

Tower GPS coordinates are looked up via **OpenCelliD** (free API) or Mozilla Location
Services. Everything is stored in a local SQLite database and displayed on a Leaflet map.

```
RTL-SDR → gr-gsm (BCCH decode) → SQLite DB → Leaflet map
              ↓
         OpenCelliD API → GPS coordinates
```

## Files

| File | Purpose |
|------|---------|
| `scan.py` | Main scanner — captures and stores tower data |
| `server.py` | Web server — serves the interactive map on port 5000 |
| `setup.sh` | Install dependencies on Raspberry Pi OS |
| `config.json` | Saved configuration (auto-created on first run) |
| `towers.db` | SQLite database (auto-created on first run) |

## Quick Start

### 1. Install dependencies
```bash
chmod +x setup.sh
./setup.sh
# Reboot if prompted
sudo reboot
```

### 2. Calibrate PPM offset (do once per dongle)
```bash
kal -s GSM-900 -d 0
# Note the ppm error value shown (e.g. "average absolute error: -12.3 ppm")
# Use that value for --ppm in scans
```

### 3. Scan for towers
```bash
python3 scan.py --scan \
  --lat 50.0755 --lon 14.4378 \
  --bands GSM-900 GSM-1800 \
  --ppm -12 \
  --gain 40
```

### 4. Open the map
```bash
python3 server.py
# Open http://localhost:5000
# Or from another device: http://<pi-ip-address>:5000
```

## Options

### scan.py
```
--scan            Run a scan
--stats           Show database statistics
--lat FLOAT       Your latitude (observer location)
--lon FLOAT       Your longitude (observer location)
--bands BAND...   GSM-900 GSM-1800 GSM-850 GSM-1900
--ppm INT         SDR frequency correction in PPM (from kal calibration)
--gain INT        SDR gain 0-50 (try 40 for most setups)
--duration INT    Seconds to scan each frequency (default: 15)
--token TOKEN     OpenCelliD API token for GPS lookup
```

### Recommended scanning strategy
```bash
# Quick scan — common European bands
python3 scan.py --scan --lat LAT --lon LON --bands GSM-900 GSM-1800

# Full scan — all bands (takes longer)
python3 scan.py --scan --lat LAT --lon LON --bands GSM-900 GSM-1800 GSM-850 GSM-1900

# Continuous scanning loop (run in screen or tmux)
while true; do
  python3 scan.py --scan --lat LAT --lon LON --bands GSM-900 GSM-1800
  sleep 60
done
```

## Dependencies

| Package | Purpose |
|---------|---------|
| `rtl-sdr` | RTL-SDR hardware driver |
| `gr-gsm` | GNU Radio GSM decoder (grgsm_livemon_headless) |
| `kalibrate-rtl` | GSM channel scanner + PPM calibration |
| `python3-requests` | HTTP library for tower API lookups |

## Frequency Bands

| Band | Frequencies | Coverage |
|------|------------|----------|
| GSM-900 | 935–960 MHz | Europe, Africa, Asia, Oceania |
| GSM-1800 | 1805–1880 MHz | Europe, Asia (DCS-1800) |
| GSM-850 | 869–894 MHz | Americas, some Asia |
| GSM-1900 | 1930–1990 MHz | North America (PCS-1900) |

The Nooelec XTR covers 25 MHz – 2.2 GHz so it handles all of these.

## Tower Coordinate Sources

1. **OpenCelliD** (preferred) — register free at https://opencellid.org for an API token
2. **Mozilla Location Services** (automatic fallback, no key needed)

Without coordinates, towers still appear in the list but not on the map.

## Legal Notice

This tool only receives the unencrypted public broadcast channel (BCCH) that
cell towers are legally required to transmit. No calls, SMS, or user data are
intercepted. Passive reception of public broadcasts is legal in most countries.
Check your local regulations before use.
