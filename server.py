#!/usr/bin/env python3
"""
Cell Tower Map Server
Serves a Leaflet.js map of all detected cell towers.
Run: python3 server.py
Then open: http://localhost:5000
"""

import sqlite3
import json
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

DB_PATH = Path(__file__).parent / "towers.db"
PORT = 5000

def get_towers(operator=None, band=None, mapped_only=False):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    where = []
    params = []
    if operator:
        where.append("operator LIKE ?")
        params.append(f"%{operator}%")
    if band:
        where.append("band = ?")
        params.append(band)
    if mapped_only:
        where.append("lat IS NOT NULL AND lon IS NOT NULL")

    sql = "SELECT * FROM towers"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY last_seen DESC"

    c.execute(sql, params)
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

def get_observations(limit=200):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""
        SELECT * FROM observations
        WHERE observer_lat IS NOT NULL AND observer_lon IS NOT NULL
        ORDER BY timestamp DESC LIMIT ?
    """, (limit,))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

def get_operators():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT DISTINCT operator FROM towers ORDER BY operator")
    ops = [r[0] for r in c.fetchall()]
    conn.close()
    return ops

def get_bands():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT DISTINCT band FROM towers WHERE band != '' ORDER BY band")
    bands = [r[0] for r in c.fetchall()]
    conn.close()
    return bands

def get_detections(band=None):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if band:
        c.execute("SELECT * FROM detections WHERE band=? ORDER BY signal_dbm DESC", (band,))
    else:
        c.execute("SELECT * FROM detections ORDER BY signal_dbm DESC")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

def get_stats():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    stats = {}
    c.execute("SELECT COUNT(*) FROM towers"); stats["total"] = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM towers WHERE lat IS NOT NULL"); stats["mapped"] = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM observations"); stats["observations"] = c.fetchone()[0]
    c.execute("SELECT COUNT(DISTINCT operator) FROM towers"); stats["operators"] = c.fetchone()[0]
    c.execute("SELECT MAX(last_seen) FROM towers"); stats["last_scan"] = c.fetchone()[0] or "never"
    try:
        c.execute("SELECT COUNT(*) FROM detections"); stats["detections"] = c.fetchone()[0]
    except Exception:
        stats["detections"] = 0
    conn.close()
    return stats

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Cell Tower Map</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600&family=Space+Mono:wght@400;700&display=swap">
<style>
  :root {
    --bg: #0d0f14;
    --surface: #161a22;
    --surface2: #1e2430;
    --border: rgba(255,255,255,0.07);
    --accent: #00e5ff;
    --accent2: #ff6b35;
    --text: #e2e8f0;
    --muted: #718096;
    --green: #00ff88;
    --yellow: #ffd700;
    --red: #ff4757;
    --font: 'Space Grotesk', sans-serif;
    --mono: 'Space Mono', monospace;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: var(--font); height: 100vh; display: flex; flex-direction: column; overflow: hidden; }

  header {
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 0 20px;
    height: 52px;
    display: flex;
    align-items: center;
    gap: 16px;
    flex-shrink: 0;
    z-index: 1000;
  }
  header h1 {
    font-size: 15px;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--accent);
  }
  header h1 span { color: var(--text); }
  .header-stats {
    display: flex;
    gap: 20px;
    margin-left: auto;
    font-size: 12px;
    font-family: var(--mono);
  }
  .stat { display: flex; flex-direction: column; align-items: flex-end; }
  .stat-val { color: var(--accent); font-weight: 700; font-size: 14px; }
  .stat-lbl { color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: 0.05em; }

  main { display: flex; flex: 1; overflow: hidden; }

  #sidebar {
    width: 300px;
    background: var(--surface);
    border-right: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    overflow: hidden;
    flex-shrink: 0;
  }

  .filter-panel {
    padding: 14px 16px;
    border-bottom: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .filter-panel label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 3px; display: block; }
  .filter-panel select, .filter-panel input[type=checkbox] { accent-color: var(--accent); }
  select {
    width: 100%;
    background: var(--surface2);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 7px 10px;
    border-radius: 6px;
    font-family: var(--font);
    font-size: 13px;
    cursor: pointer;
  }
  .toggle-row { display: flex; align-items: center; gap: 8px; font-size: 13px; cursor: pointer; }

  #tower-list {
    flex: 1;
    overflow-y: auto;
    scrollbar-width: thin;
    scrollbar-color: var(--surface2) transparent;
  }
  .tower-item {
    padding: 12px 16px;
    border-bottom: 1px solid var(--border);
    cursor: pointer;
    transition: background 0.15s;
  }
  .tower-item:hover { background: var(--surface2); }
  .tower-item.active { background: rgba(0,229,255,0.07); border-left: 2px solid var(--accent); }
  .tower-name { font-size: 13px; font-weight: 500; color: var(--text); }
  .tower-meta { font-size: 11px; color: var(--muted); font-family: var(--mono); margin-top: 3px; }
  .tower-badges { display: flex; gap: 5px; margin-top: 5px; flex-wrap: wrap; }
  .badge {
    font-size: 10px;
    padding: 2px 7px;
    border-radius: 3px;
    font-family: var(--mono);
    font-weight: 700;
  }
  .badge-band { background: rgba(0,229,255,0.12); color: var(--accent); }
  .badge-signal { background: rgba(0,255,136,0.12); color: var(--green); }
  .badge-signal.weak { background: rgba(255,71,87,0.12); color: var(--red); }
  .badge-signal.mid { background: rgba(255,215,0,0.12); color: var(--yellow); }
  .badge-nomapped { background: rgba(255,107,53,0.12); color: var(--accent2); }

  #map { flex: 1; }

  #info-panel {
    position: absolute;
    bottom: 20px;
    right: 20px;
    width: 280px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 16px;
    z-index: 900;
    display: none;
    box-shadow: 0 8px 32px rgba(0,0,0,0.5);
  }
  #info-panel h3 { font-size: 14px; color: var(--accent); margin-bottom: 10px; }
  #info-panel table { width: 100%; border-collapse: collapse; font-size: 12px; }
  #info-panel td { padding: 4px 0; vertical-align: top; }
  #info-panel td:first-child { color: var(--muted); width: 100px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; }
  #info-panel td:last-child { color: var(--text); font-family: var(--mono); }
  #info-close { position: absolute; top: 10px; right: 12px; cursor: pointer; color: var(--muted); font-size: 18px; line-height: 1; }
  #info-close:hover { color: var(--text); }

  .leaflet-popup-content-wrapper {
    background: var(--surface) !important;
    color: var(--text) !important;
    border: 1px solid var(--border) !important;
    border-radius: 8px !important;
    box-shadow: 0 4px 20px rgba(0,0,0,0.5) !important;
  }
  .leaflet-popup-tip { background: var(--surface) !important; }
  .leaflet-popup-content { font-family: var(--font); }

  .scan-indicator {
    display: inline-block;
    width: 7px; height: 7px;
    border-radius: 50%;
    background: var(--green);
    margin-right: 6px;
    animation: pulse 2s infinite;
  }
  @keyframes pulse {
    0%, 100% { opacity: 1; box-shadow: 0 0 0 0 rgba(0,255,136,0.4); }
    50% { opacity: 0.6; box-shadow: 0 0 0 5px rgba(0,255,136,0); }
  }

  .empty-state { padding: 32px 16px; text-align: center; color: var(--muted); font-size: 13px; }
  .empty-state code { display: block; margin-top: 8px; font-family: var(--mono); font-size: 11px; color: var(--accent); background: var(--surface2); padding: 8px; border-radius: 4px; text-align: left; }
</style>
</head>
<body>

<header>
  <span class="scan-indicator"></span>
  <h1>TOWER<span>SCAN</span></h1>
  <div class="header-stats">
    <div class="stat">
      <span class="stat-val" id="h-total">-</span>
      <span class="stat-lbl">Towers</span>
    </div>
    <div class="stat">
      <span class="stat-val" id="h-mapped">-</span>
      <span class="stat-lbl">Mapped</span>
    </div>
    <div class="stat">
      <span class="stat-val" id="h-obs">-</span>
      <span class="stat-lbl">Observations</span>
    </div>
    <div class="stat">
      <span class="stat-val" id="h-ops">-</span>
      <span class="stat-lbl">Operators</span>
    </div>
  </div>
</header>

<main>
  <div id="sidebar">
    <div class="filter-panel">
      <div>
        <label>Operator</label>
        <select id="filter-op"><option value="">All operators</option></select>
      </div>
      <div>
        <label>Band</label>
        <select id="filter-band"><option value="">All bands</option></select>
      </div>
      <label class="toggle-row">
        <input type="checkbox" id="filter-mapped">
        Only show towers with GPS coordinates
      </label>
    </div>
    <div id="tower-list"></div>
  </div>

  <div style="position:relative;flex:1;">
    <div id="map"></div>
    <div id="info-panel">
      <span id="info-close">×</span>
      <h3 id="info-title">Tower Details</h3>
      <table id="info-table"></table>
    </div>
  </div>
</main>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
let allTowers = [];
let markers = {};
let map;
let layerGroup;

// Dark tile layer
const darkTiles = L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
  maxZoom: 19,
  subdomains: 'abcd',
});

const streetTiles = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '&copy; OpenStreetMap contributors',
  maxZoom: 19,
});

function signalStrength(dbm) {
  if (!dbm) return { cls: '', label: 'N/A' };
  if (dbm > -70) return { cls: 'badge-signal', label: `${dbm} dBm ▲` };
  if (dbm > -85) return { cls: 'badge-signal mid', label: `${dbm} dBm ►` };
  return { cls: 'badge-signal weak', label: `${dbm} dBm ▼` };
}

function towerIcon(tower) {
  const colors = {
    'GSM-900':  '#00e5ff',
    'GSM-1800': '#ff6b35',
    'GSM-850':  '#00ff88',
    'GSM-1900': '#ffd700',
  };
  const color = tower._is_detection ? '#ffd700' : (colors[tower.band] || '#a0aec0');
  const hasCoords = tower.lat && tower.lon;
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="28" height="36" viewBox="0 0 28 36">
    <circle cx="14" cy="14" r="12" fill="${hasCoords ? color : '#a0aec0'}" fill-opacity="0.2" stroke="${hasCoords ? color : '#a0aec0'}" stroke-width="1.5"/>
    <circle cx="14" cy="14" r="5" fill="${hasCoords ? color : '#a0aec0'}"/>
    ${hasCoords ? `<circle cx="14" cy="14" r="9" fill="none" stroke="${color}" stroke-width="1" opacity="0.5"/>` : ''}
    <line x1="14" y1="26" x2="14" y2="34" stroke="${hasCoords ? color : '#718096'}" stroke-width="1.5"/>
  </svg>`;
  return L.divIcon({
    html: svg,
    className: '',
    iconSize: [28, 36],
    iconAnchor: [14, 34],
    popupAnchor: [0, -30],
  });
}

function buildPopup(t) {
  return `
    <div style="font-family:'Space Grotesk',sans-serif;min-width:180px;">
      <div style="font-size:14px;font-weight:600;color:#00e5ff;margin-bottom:8px;">${t.operator || 'Unknown'}</div>
      <div style="font-size:11px;color:#718096;font-family:'Space Mono',monospace;">
        MCC ${t.mcc} / MNC ${t.mnc}<br>
        LAC ${t.lac} / CI ${t.cell_id}<br>
        ${t.freq_mhz ? `${t.freq_mhz} MHz &bull; ` : ''}${t.band || ''}<br>
        Seen ${t.seen_count}× &bull; ${t.signal_dbm ? t.signal_dbm + ' dBm' : 'no signal data'}
        ${t.lat ? `<br><span style="color:#00ff88">📍 ${t.lat.toFixed(5)}, ${t.lon.toFixed(5)}</span>` : '<br><span style="color:#ff6b35">⚠ No GPS coordinates</span>'}
      </div>
    </div>
  `;
}

function showInfo(t) {
  document.getElementById('info-title').textContent = t.operator || 'Unknown Operator';
  const rows = [
    ['MCC / MNC', `${t.mcc} / ${t.mnc}`],
    ['LAC', t.lac],
    ['Cell ID', t.cell_id],
    ['Band', t.band || 'N/A'],
    ['Frequency', t.freq_mhz ? `${t.freq_mhz} MHz` : 'N/A'],
    ['Signal', t.signal_dbm ? `${t.signal_dbm} dBm` : 'N/A'],
    ['Country', t.country || 'N/A'],
    ['GPS', t.lat ? `${t.lat.toFixed(5)}, ${t.lon.toFixed(5)}` : 'Not found'],
    ['First seen', t.first_seen ? t.first_seen.slice(0, 16) : 'N/A'],
    ['Seen count', t.seen_count],
  ];
  document.getElementById('info-table').innerHTML = rows.map(([k,v]) =>
    `<tr><td>${k}</td><td>${v ?? 'N/A'}</td></tr>`
  ).join('');
  document.getElementById('info-panel').style.display = 'block';
}

function renderTowerList(towers) {
  const el = document.getElementById('tower-list');
  if (!towers.length) {
    el.innerHTML = `<div class="empty-state">
      No towers found yet.<br>Run a scan first:<br>
      <code>python3 scan.py --scan --lat 50.0755 --lon 14.4378</code>
    </div>`;
    return;
  }
  el.innerHTML = towers.map((t, i) => {
    const sig = signalStrength(t.signal_dbm);
    return `<div class="tower-item" data-idx="${i}" onclick="selectTower(${i})">
      <div class="tower-name">${t.operator || 'Unknown Operator'}</div>
      <div class="tower-meta">LAC ${t.lac} / CI ${t.cell_id} &bull; ${t.freq_mhz || '?'} MHz</div>
      <div class="tower-badges">
        ${t.band ? `<span class="badge badge-band">${t.band}</span>` : ''}
        ${t.signal_dbm ? `<span class="badge ${sig.cls}">${sig.label}</span>` : ''}
        ${!t.lat ? `<span class="badge badge-nomapped">no GPS</span>` : ''}
      </div>
    </div>`;
  }).join('');
}

function renderMarkers(towers) {
  layerGroup.clearLayers();
  markers = {};
  towers.forEach((t, i) => {
    if (t.lat && t.lon) {
      const m = L.marker([t.lat, t.lon], { icon: towerIcon(t) })
        .bindPopup(buildPopup(t))
        .addTo(layerGroup);
      m.on('click', () => selectTower(i));
      markers[i] = m;
    }
  });
}

let selectedIdx = null;
function selectTower(idx) {
  // Deselect previous
  document.querySelectorAll('.tower-item').forEach(el => el.classList.remove('active'));
  const item = document.querySelector(`.tower-item[data-idx="${idx}"]`);
  if (item) { item.classList.add('active'); item.scrollIntoView({ block: 'nearest' }); }

  const t = allTowers[idx];
  showInfo(t);
  if (t.lat && t.lon) {
    map.setView([t.lat, t.lon], 14);
    markers[idx]?.openPopup();
  }
  selectedIdx = idx;
}

function applyFilters() {
  const op = document.getElementById('filter-op').value;
  const band = document.getElementById('filter-band').value;
  const mappedOnly = document.getElementById('filter-mapped').checked;

  let filtered = allTowers.filter(t => {
    if (op && t.operator !== op) return false;
    if (band && t.band !== band) return false;
    if (mappedOnly && (!t.lat || !t.lon)) return false;
    return true;
  });

  renderTowerList(filtered.map((t, i) => ({ ...t, _origIdx: i })));
  renderMarkers(filtered);
}

async function loadData() {
  const [towersRes, statsRes, opsRes, bandsRes, detectionsRes] = await Promise.all([
    fetch('/api/towers').then(r => r.json()),
    fetch('/api/stats').then(r => r.json()),
    fetch('/api/operators').then(r => r.json()),
    fetch('/api/bands').then(r => r.json()),
    fetch('/api/detections').then(r => r.json()),
  ]);

  allTowers = towersRes;

  // Merge detections as partial towers (no MCC/MNC yet)
  const knownArfcns = new Set(allTowers.map(t => t.arfcn));
  detectionsRes.forEach(d => {
    if (!knownArfcns.has(d.arfcn)) {
      allTowers.push({
        operator: `ARFCN ${d.arfcn} (unidentified)`,
        band: d.band,
        freq_mhz: d.freq_mhz,
        arfcn: d.arfcn,
        signal_dbm: d.signal_dbm,
        lat: d.observer_lat,
        lon: d.observer_lon,
        mcc: null, mnc: null, lac: null, cell_id: null,
        seen_count: 1,
        first_seen: d.timestamp,
        last_seen: d.timestamp,
        _is_detection: true,
      });
    }
  });

  // Stats
  document.getElementById('h-total').textContent = statsRes.total + (statsRes.detections ? ` (+${statsRes.detections} raw)` : '');
  document.getElementById('h-mapped').textContent = statsRes.mapped;
  document.getElementById('h-obs').textContent = statsRes.observations;
  document.getElementById('h-ops').textContent = statsRes.operators;

  // Filters
  const opSel = document.getElementById('filter-op');
  opsRes.forEach(op => {
    const o = document.createElement('option');
    o.value = op; o.textContent = op;
    opSel.appendChild(o);
  });

  const bandSel = document.getElementById('filter-band');
  bandsRes.forEach(b => {
    const o = document.createElement('option');
    o.value = b; o.textContent = b;
    bandSel.appendChild(o);
  });

  // Fit map to data if we have mapped towers
  const mapped = allTowers.filter(t => t.lat && t.lon);
  if (mapped.length) {
    const lats = mapped.map(t => t.lat);
    const lons = mapped.map(t => t.lon);
    map.fitBounds([
      [Math.min(...lats) - 0.05, Math.min(...lons) - 0.05],
      [Math.max(...lats) + 0.05, Math.max(...lons) + 0.05],
    ]);
  }

  renderTowerList(allTowers);
  renderMarkers(allTowers);
}

// Init map
map = L.map('map', {
  center: [50.07, 14.43], // Prague default (override by user location)
  zoom: 11,
  layers: [darkTiles],
  zoomControl: false,
});

L.control.zoom({ position: 'bottomright' }).addTo(map);
L.control.layers({ 'Dark': darkTiles, 'Street': streetTiles }).addTo(map);

layerGroup = L.layerGroup().addTo(map);

document.getElementById('filter-op').addEventListener('change', applyFilters);
document.getElementById('filter-band').addEventListener('change', applyFilters);
document.getElementById('filter-mapped').addEventListener('change', applyFilters);
document.getElementById('info-close').addEventListener('click', () => {
  document.getElementById('info-panel').style.display = 'none';
});

loadData();

// Auto-refresh every 30s
setInterval(loadData, 30000);
</script>
</body>
</html>
"""

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress default logging

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html):
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            self.send_html(HTML_PAGE)

        elif path == "/api/towers":
            op = qs.get("operator", [None])[0]
            band = qs.get("band", [None])[0]
            mapped = qs.get("mapped_only", [""])[0] == "1"
            towers = get_towers(op, band, mapped)
            self.send_json(towers)

        elif path == "/api/stats":
            self.send_json(get_stats())

        elif path == "/api/operators":
            self.send_json(get_operators())

        elif path == "/api/bands":
            self.send_json(get_bands())

        elif path == "/api/observations":
            obs = get_observations()
            self.send_json(obs)

        elif path == "/api/detections":
            band = qs.get("band", [None])[0]
            self.send_json(get_detections(band))

        else:
            self.send_response(404)
            self.end_headers()

if __name__ == "__main__":
    if not DB_PATH.exists():
        print("No database found. Run scan.py first to collect data.")
        print(f"Expected: {DB_PATH}")
    else:
        stats = get_stats()
        print(f"Cell Tower Map Server")
        print(f"Database: {DB_PATH}")
        print(f"  {stats['total']} towers ({stats['mapped']} with GPS) | {stats.get('detections',0)} detected carriers | {stats['observations']} observations")
        print(f"\nOpen: http://localhost:{PORT}")
        print("Press Ctrl+C to stop.\n")

    server = HTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
