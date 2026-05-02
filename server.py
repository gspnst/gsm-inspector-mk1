#!/usr/bin/env python3
"""
Cell Tower Map Server v2
Serves detected carriers AND identified towers on a Leaflet map.
Run:  python3 server.py
Open: http://localhost:5000  (or http://<pi-ip>:5000)
"""

import sqlite3
import json
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

DB_PATH = Path(__file__).parent / "towers.db"
PORT    = 5000

# ── Data access ───────────────────────────────────────────────────────────────

def query(sql, params=()):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    conn.close()
    return rows

def scalar(sql, params=(), default=0):
    conn = sqlite3.connect(DB_PATH)
    try:
        val = conn.execute(sql, params).fetchone()[0]
    except Exception:
        val = default
    conn.close()
    return val or default

def get_towers(operator=None, band=None, mapped_only=False):
    where, params = [], []
    if operator:
        where.append("operator LIKE ?"); params.append(f"%{operator}%")
    if band:
        where.append("band = ?"); params.append(band)
    if mapped_only:
        where.append("lat IS NOT NULL AND lon IS NOT NULL")
    sql = "SELECT * FROM towers"
    if where:
        sql += " WHERE " + " AND ".join(where)
    return query(sql + " ORDER BY last_seen DESC", params)

def get_detections(band=None):
    sql = "SELECT * FROM detections"
    if band:
        return query(sql + " WHERE band=? ORDER BY signal_dbm DESC", (band,))
    return query(sql + " ORDER BY signal_dbm DESC")

def get_stats():
    return {
        "total":      scalar("SELECT COUNT(*) FROM towers"),
        "mapped":     scalar("SELECT COUNT(*) FROM towers WHERE lat IS NOT NULL"),
        "detections": scalar("SELECT COUNT(*) FROM detections"),
        "observations": scalar("SELECT COUNT(*) FROM observations"),
        "operators":  scalar("SELECT COUNT(DISTINCT operator) FROM towers"),
        "last_scan":  scalar("SELECT MAX(last_seen) FROM towers", default="never"),
    }

def get_operators():
    return [r["operator"] for r in query(
        "SELECT DISTINCT operator FROM towers ORDER BY operator")]

def get_bands():
    return [r["band"] for r in query(
        "SELECT DISTINCT band FROM towers WHERE band != '' ORDER BY band")]

# ── HTML ──────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TowerScan</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600&family=Space+Mono:wght@400;700&display=swap">
<style>
:root {
  --bg:#0d0f14; --surface:#161a22; --surface2:#1e2430;
  --border:rgba(255,255,255,0.07);
  --accent:#00e5ff; --accent2:#ff6b35;
  --text:#e2e8f0; --muted:#718096;
  --green:#00ff88; --yellow:#ffd700; --red:#ff4757;
  --font:'Space Grotesk',sans-serif; --mono:'Space Mono',monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:var(--font);
     height:100vh;display:flex;flex-direction:column;overflow:hidden}

header{background:var(--surface);border-bottom:1px solid var(--border);
       padding:0 20px;height:52px;display:flex;align-items:center;gap:16px;
       flex-shrink:0;z-index:1000}
header h1{font-size:15px;font-weight:600;letter-spacing:.08em;
          text-transform:uppercase;color:var(--accent)}
header h1 span{color:var(--text)}
.hstats{display:flex;gap:20px;margin-left:auto;font-size:12px;font-family:var(--mono)}
.stat{display:flex;flex-direction:column;align-items:flex-end}
.stat-val{color:var(--accent);font-weight:700;font-size:14px}
.stat-lbl{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.05em}

main{display:flex;flex:1;overflow:hidden}

#sidebar{width:300px;background:var(--surface);border-right:1px solid var(--border);
         display:flex;flex-direction:column;overflow:hidden;flex-shrink:0}
.filter-panel{padding:14px 16px;border-bottom:1px solid var(--border);
              display:flex;flex-direction:column;gap:10px}
.filter-panel label{font-size:11px;color:var(--muted);text-transform:uppercase;
                    letter-spacing:.05em;margin-bottom:3px;display:block}
select{width:100%;background:var(--surface2);border:1px solid var(--border);
       color:var(--text);padding:7px 10px;border-radius:6px;
       font-family:var(--font);font-size:13px;cursor:pointer}
.toggle-row{display:flex;align-items:center;gap:8px;font-size:13px;cursor:pointer}
input[type=checkbox]{accent-color:var(--accent)}

#tower-list{flex:1;overflow-y:auto;scrollbar-width:thin;
            scrollbar-color:var(--surface2) transparent}
.tower-item{padding:12px 16px;border-bottom:1px solid var(--border);
            cursor:pointer;transition:background .15s}
.tower-item:hover{background:var(--surface2)}
.tower-item.active{background:rgba(0,229,255,.07);border-left:2px solid var(--accent)}
.tower-name{font-size:13px;font-weight:500;color:var(--text)}
.tower-meta{font-size:11px;color:var(--muted);font-family:var(--mono);margin-top:3px}
.badges{display:flex;gap:5px;margin-top:5px;flex-wrap:wrap}
.badge{font-size:10px;padding:2px 7px;border-radius:3px;font-family:var(--mono);font-weight:700}
.b-band{background:rgba(0,229,255,.12);color:var(--accent)}
.b-sig{background:rgba(0,255,136,.12);color:var(--green)}
.b-sig.mid{background:rgba(255,215,0,.12);color:var(--yellow)}
.b-sig.weak{background:rgba(255,71,87,.12);color:var(--red)}
.b-raw{background:rgba(255,215,0,.12);color:var(--yellow)}
.b-nogps{background:rgba(255,107,53,.12);color:var(--accent2)}

#map-wrap{position:relative;flex:1}
#map{width:100%;height:100%}

#info{position:absolute;bottom:20px;right:20px;width:280px;
      background:var(--surface);border:1px solid var(--border);
      border-radius:10px;padding:16px;z-index:900;display:none;
      box-shadow:0 8px 32px rgba(0,0,0,.5)}
#info h3{font-size:14px;color:var(--accent);margin-bottom:10px}
#info table{width:100%;border-collapse:collapse;font-size:12px}
#info td{padding:4px 0;vertical-align:top}
#info td:first-child{color:var(--muted);width:100px;font-size:11px;
                     text-transform:uppercase;letter-spacing:.05em}
#info td:last-child{color:var(--text);font-family:var(--mono)}
#info-close{position:absolute;top:10px;right:12px;cursor:pointer;
            color:var(--muted);font-size:18px;line-height:1}
#info-close:hover{color:var(--text)}

.pulse{display:inline-block;width:7px;height:7px;border-radius:50%;
       background:var(--green);margin-right:6px;animation:pulse 2s infinite}
@keyframes pulse{
  0%,100%{opacity:1;box-shadow:0 0 0 0 rgba(0,255,136,.4)}
  50%{opacity:.6;box-shadow:0 0 0 5px rgba(0,255,136,0)}
}

.empty{padding:32px 16px;text-align:center;color:var(--muted);font-size:13px}
.empty code{display:block;margin-top:8px;font-family:var(--mono);font-size:11px;
            color:var(--accent);background:var(--surface2);padding:8px;
            border-radius:4px;text-align:left}

.leaflet-popup-content-wrapper{background:var(--surface)!important;
  color:var(--text)!important;border:1px solid var(--border)!important;
  border-radius:8px!important;box-shadow:0 4px 20px rgba(0,0,0,.5)!important}
.leaflet-popup-tip{background:var(--surface)!important}
.leaflet-popup-content{font-family:var(--font)}
</style>
</head>
<body>
<header>
  <span class="pulse"></span>
  <h1>TOWER<span>SCAN</span></h1>
  <div class="hstats">
    <div class="stat"><span class="stat-val" id="h-towers">–</span><span class="stat-lbl">Towers</span></div>
    <div class="stat"><span class="stat-val" id="h-det">–</span><span class="stat-lbl">Detections</span></div>
    <div class="stat"><span class="stat-val" id="h-mapped">–</span><span class="stat-lbl">Mapped</span></div>
    <div class="stat"><span class="stat-val" id="h-ops">–</span><span class="stat-lbl">Operators</span></div>
  </div>
</header>
<main>
  <div id="sidebar">
    <div class="filter-panel">
      <div><label>Operator</label><select id="f-op"><option value="">All operators</option></select></div>
      <div><label>Band</label><select id="f-band"><option value="">All bands</option></select></div>
      <label class="toggle-row">
        <input type="checkbox" id="f-mapped"> Only towers with GPS
      </label>
      <label class="toggle-row">
        <input type="checkbox" id="f-detections" checked> Show raw detections
      </label>
    </div>
    <div id="tower-list"></div>
  </div>
  <div id="map-wrap">
    <div id="map"></div>
    <div id="info">
      <span id="info-close">×</span>
      <h3 id="info-title">Details</h3>
      <table id="info-table"></table>
    </div>
  </div>
</main>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
let allItems = [];   // towers + detections merged
let markers  = {};
let map, layers;

const BAND_COLORS = {
  'GSM-900':'#00e5ff','GSM-1800':'#ff6b35','GSM-850':'#00ff88','GSM-1900':'#ffd700'
};

// ── Map init ──────────────────────────────────────────────────────────────────
const dark   = L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
  {attribution:'© OpenStreetMap © CARTO',maxZoom:19,subdomains:'abcd'});
const street = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
  {attribution:'© OpenStreetMap',maxZoom:19});

map = L.map('map',{center:[50.07,14.43],zoom:11,layers:[dark],zoomControl:false});
L.control.zoom({position:'bottomright'}).addTo(map);
L.control.layers({'Dark':dark,'Street':street}).addTo(map);
layers = L.layerGroup().addTo(map);

// ── Icons ─────────────────────────────────────────────────────────────────────
function makeIcon(color, isDetection, hasGps) {
  const opacity = hasGps ? 1 : 0.6;
  const rings   = hasGps && !isDetection
    ? `<circle cx="14" cy="14" r="9" fill="none" stroke="${color}" stroke-width="1" opacity="0.4"/>`
    : '';
  const shape   = isDetection
    ? `<polygon points="14,4 22,22 6,22" fill="${color}" fill-opacity="0.3"
         stroke="${color}" stroke-width="1.5"/>`
    : `<circle cx="14" cy="14" r="12" fill="${color}" fill-opacity="0.15"
         stroke="${color}" stroke-width="1.5"/>
       <circle cx="14" cy="14" r="5" fill="${color}"/>`;
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="28" height="36" viewBox="0 0 28 36"
    style="opacity:${opacity}">${shape}${rings}
    <line x1="14" y1="${isDetection?22:26}" x2="14" y2="34"
          stroke="${color}" stroke-width="1.5"/></svg>`;
  return L.divIcon({html:svg,className:'',iconSize:[28,36],
                    iconAnchor:[14,34],popupAnchor:[0,-30]});
}

// ── Signal badge ──────────────────────────────────────────────────────────────
function sigBadge(dbm) {
  if (!dbm) return '';
  const cls = dbm > -70 ? 'b-sig' : dbm > -85 ? 'b-sig mid' : 'b-sig weak';
  return `<span class="badge ${cls}">${dbm} dBm</span>`;
}

// ── Popup ─────────────────────────────────────────────────────────────────────
function popup(item) {
  const title = item._raw
    ? `ARFCN ${item.arfcn} (unidentified)`
    : (item.operator || 'Unknown');
  const id = item._raw
    ? `${item.freq_mhz} MHz &bull; ${item.band}`
    : `MCC ${item.mcc} / MNC ${item.mnc}<br>LAC ${item.lac} / CI ${item.cell_id}`;
  const gps = item.lat
    ? `<span style="color:var(--green)">📍 ${item.lat.toFixed(5)}, ${item.lon.toFixed(5)}</span>`
    : `<span style="color:var(--accent2)">⚠ No GPS</span>`;
  return `<div style="font-family:var(--font);min-width:180px">
    <div style="font-size:14px;font-weight:600;color:var(--accent);margin-bottom:8px">${title}</div>
    <div style="font-size:11px;color:var(--muted);font-family:var(--mono)">
      ${id}<br>${gps}
      ${item.signal_dbm?`<br>${item.signal_dbm} dBm`:''}
      ${item._raw?`<br><em style="color:var(--yellow)">Raw detection — no identity yet</em>`:''}
    </div></div>`;
}

// ── Info panel ────────────────────────────────────────────────────────────────
function showInfo(item) {
  document.getElementById('info-title').textContent =
    item._raw ? `ARFCN ${item.arfcn}` : (item.operator || 'Unknown');
  const rows = item._raw
    ? [['Type','Raw detection'],['ARFCN',item.arfcn],['Band',item.band],
       ['Frequency',`${item.freq_mhz} MHz`],['Signal',`${item.signal_dbm} dBm`],
       ['Noise floor',`${item.noise_floor} dBm`],
       ['Observer',item.lat?`${item.lat.toFixed(4)},${item.lon.toFixed(4)}`:'N/A'],
       ['Detected',item.timestamp?.slice(0,16)||'N/A']]
    : [['Operator',item.operator],['Country',item.country],
       ['MCC/MNC',`${item.mcc}/${item.mnc}`],['LAC',item.lac],['Cell ID',item.cell_id],
       ['Band',item.band||'N/A'],['Frequency',item.freq_mhz?`${item.freq_mhz} MHz`:'N/A'],
       ['Signal',item.signal_dbm?`${item.signal_dbm} dBm`:'N/A'],
       ['GPS',item.lat?`${item.lat.toFixed(5)}, ${item.lon.toFixed(5)}`:'not found'],
       ['Seen',item.seen_count||1],['Last seen',item.last_seen?.slice(0,16)||'N/A']];
  document.getElementById('info-table').innerHTML =
    rows.map(([k,v])=>`<tr><td>${k}</td><td>${v??'N/A'}</td></tr>`).join('');
  document.getElementById('info').style.display = 'block';
}

// ── Render list ───────────────────────────────────────────────────────────────
function renderList(items) {
  const el = document.getElementById('tower-list');
  if (!items.length) {
    el.innerHTML = `<div class="empty">No data yet.<br>Run a scan first:
      <code>python3 scan.py --scan --lat LAT --lon LON</code>
      or import OCID data:
      <code>python3 scan.py --import-ocid --lat LAT --lon LON</code></div>`;
    return;
  }
  el.innerHTML = items.map((item,i) => {
    const name = item._raw
      ? `ARFCN ${item.arfcn} — ${item.band}`
      : (item.operator || 'Unknown');
    const meta = item._raw
      ? `${item.freq_mhz} MHz &bull; ${item.signal_dbm} dBm`
      : `LAC ${item.lac} / CI ${item.cell_id} &bull; ${item.freq_mhz||'?'} MHz`;
    return `<div class="tower-item" data-i="${i}" onclick="select(${i})">
      <div class="tower-name">${name}</div>
      <div class="tower-meta">${meta}</div>
      <div class="badges">
        ${item.band?`<span class="badge b-band">${item.band}</span>`:''}
        ${sigBadge(item.signal_dbm)}
        ${item._raw?`<span class="badge b-raw">unidentified</span>`:''}
        ${!item.lat&&!item._raw?`<span class="badge b-nogps">no GPS</span>`:''}
      </div></div>`;
  }).join('');
}

// ── Render markers ────────────────────────────────────────────────────────────
function renderMarkers(items) {
  layers.clearLayers(); markers = {};
  items.forEach((item, i) => {
    if (!item.lat || !item.lon) return;
    const color = BAND_COLORS[item.band] || '#a0aec0';
    const m = L.marker([item.lat, item.lon],
      {icon: makeIcon(color, !!item._raw, true)})
      .bindPopup(popup(item))
      .addTo(layers);
    m.on('click', () => select(i));
    markers[i] = m;
  });
}

// ── Select item ───────────────────────────────────────────────────────────────
function select(i) {
  document.querySelectorAll('.tower-item').forEach(e=>e.classList.remove('active'));
  const el = document.querySelector(`.tower-item[data-i="${i}"]`);
  if (el) { el.classList.add('active'); el.scrollIntoView({block:'nearest'}); }
  const item = allItems[i];
  showInfo(item);
  if (item.lat && item.lon) {
    map.setView([item.lat, item.lon], 14);
    markers[i]?.openPopup();
  }
}

// ── Filters ───────────────────────────────────────────────────────────────────
function applyFilters() {
  const op      = document.getElementById('f-op').value;
  const band    = document.getElementById('f-band').value;
  const mapped  = document.getElementById('f-mapped').checked;
  const showDet = document.getElementById('f-detections').checked;

  const filtered = allItems.filter(item => {
    if (!showDet && item._raw)           return false;
    if (op   && item.operator !== op)    return false;
    if (band && item.band !== band)      return false;
    if (mapped && (!item.lat||!item.lon)) return false;
    return true;
  });
  renderList(filtered);
  renderMarkers(filtered);
}

['f-op','f-band','f-mapped','f-detections'].forEach(id =>
  document.getElementById(id).addEventListener('change', applyFilters));
document.getElementById('info-close').addEventListener('click', () =>
  document.getElementById('info').style.display = 'none');

// ── Load data ─────────────────────────────────────────────────────────────────
async function loadData() {
  try {
    const [towers, stats, ops, bands, dets] = await Promise.all([
      fetch('/api/towers').then(r=>r.json()),
      fetch('/api/stats').then(r=>r.json()),
      fetch('/api/operators').then(r=>r.json()),
      fetch('/api/bands').then(r=>r.json()),
      fetch('/api/detections').then(r=>r.json()),
    ]);

    // Merge: identified towers first, then unidentified detections
    const knownArfcns = new Set(towers.map(t=>t.arfcn).filter(Boolean));
    const rawDets = dets
      .filter(d => !knownArfcns.has(d.arfcn))
      .map(d => ({
        ...d, _raw: true,
        operator: null, lat: d.observer_lat, lon: d.observer_lon,
      }));

    allItems = [...towers, ...rawDets];

    // Header stats
    document.getElementById('h-towers').textContent  = stats.total;
    document.getElementById('h-det').textContent     = stats.detections;
    document.getElementById('h-mapped').textContent  = stats.mapped;
    document.getElementById('h-ops').textContent     = stats.operators;

    // Populate filter dropdowns
    const opSel = document.getElementById('f-op');
    opSel.innerHTML = '<option value="">All operators</option>' +
      ops.map(o=>`<option value="${o}">${o}</option>`).join('');

    const bSel = document.getElementById('f-band');
    bSel.innerHTML = '<option value="">All bands</option>' +
      bands.map(b=>`<option value="${b}">${b}</option>`).join('');

    applyFilters();

    // Fit map to data
    const pts = allItems.filter(i=>i.lat&&i.lon);
    if (pts.length) {
      const lats = pts.map(i=>i.lat), lons = pts.map(i=>i.lon);
      map.fitBounds([
        [Math.min(...lats)-0.05, Math.min(...lons)-0.05],
        [Math.max(...lats)+0.05, Math.max(...lons)+0.05],
      ]);
    }
  } catch(e) {
    console.error('Load error:', e);
  }
}

loadData();
setInterval(loadData, 30000);
</script>
</body>
</html>"""

# ── HTTP handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_json(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
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
        p = urlparse(self.path)
        qs = parse_qs(p.query)

        routes = {
            "/":                lambda: self.send_html(HTML),
            "/index.html":      lambda: self.send_html(HTML),
            "/api/stats":       lambda: self.send_json(get_stats()),
            "/api/operators":   lambda: self.send_json(get_operators()),
            "/api/bands":       lambda: self.send_json(get_bands()),
            "/api/detections":  lambda: self.send_json(
                get_detections(qs.get("band",[None])[0])),
            "/api/towers":      lambda: self.send_json(
                get_towers(
                    qs.get("operator",[None])[0],
                    qs.get("band",[None])[0],
                    qs.get("mapped_only",[""])[0] == "1")),
        }

        handler = routes.get(p.path)
        if handler:
            handler()
        else:
            self.send_response(404)
            self.end_headers()

# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not DB_PATH.exists():
        print(f"Database not found at {DB_PATH}")
        print("Run scan.py first:\n  python3 scan.py --scan --lat LAT --lon LON")
    else:
        s = get_stats()
        print(f"TowerScan Server")
        print(f"  {s['total']} towers | {s['detections']} detections | "
              f"{s['mapped']} with GPS | {s['operators']} operators")
    print(f"\nOpen: http://localhost:{PORT}")
    print("Ctrl+C to stop.\n")
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
