#!/usr/bin/env python3
"""
Web dashboard for the live anomaly scanner.

Serves a self-contained HTML page that auto-refreshes with scanner
state and detected anomalies from the shared SQLite database.

Usage:
  python monitor.py                  # http://localhost:8080
  python monitor.py --port 9000      # custom port
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from kalshi_trader.config import DB_PATH

STATE_FILE = Path("scanner_state.json")

# ---------------------------------------------------------------------------
# HTML dashboard (self-contained)
# ---------------------------------------------------------------------------

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Anomaly Scanner</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0d1117;color:#c9d1d9;font-family:'SF Mono','Cascadia Code','Consolas',monospace;padding:20px 24px}
h1{font-size:18px;color:#58a6ff;margin-bottom:4px;display:flex;align-items:center;gap:8px}
h1 .dot{width:8px;height:8px;border-radius:50%;display:inline-block}
h1 .dot.on{background:#3fb950;box-shadow:0 0 6px #3fb950}
h1 .dot.off{background:#f85149}
.subtitle{font-size:11px;color:#484f58;margin-bottom:20px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin-bottom:24px}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px 16px}
.card-label{font-size:10px;color:#8b949e;text-transform:uppercase;letter-spacing:1px}
.card-value{font-size:22px;font-weight:700;margin-top:2px}
.card-value.g{color:#3fb950}.card-value.y{color:#d29922}.card-value.r{color:#f85149}.card-value.b{color:#58a6ff}
h2{font-size:13px;color:#8b949e;margin-bottom:10px;text-transform:uppercase;letter-spacing:1px}
.tbl-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;background:#161b22;border-radius:8px;overflow:hidden;font-size:13px}
th{background:#21262d;color:#8b949e;text-transform:uppercase;font-size:10px;letter-spacing:1px;padding:10px 14px;text-align:left;white-space:nowrap}
td{padding:10px 14px;border-top:1px solid #21262d;white-space:nowrap}
tr:hover td{background:#1c2128}
.badge{display:inline-block;padding:1px 7px;border-radius:10px;font-size:10px;font-weight:700}
.badge-kal{background:#1f6feb22;color:#58a6ff}.badge-ply{background:#3fb95022;color:#3fb950}
.score{font-weight:700}
.s-hi{color:#f85149}.s-md{color:#d29922}.s-lo{color:#c9d1d9}
td.title-cell{max-width:340px;overflow:hidden;text-overflow:ellipsis}
.empty{text-align:center;color:#484f58;padding:48px;font-size:14px}
.foot{color:#484f58;font-size:10px;text-align:right;margin-top:12px}
.signal-tag{display:inline-block;background:#30363d;color:#c9d1d9;padding:1px 6px;border-radius:4px;font-size:10px;margin:1px 2px}
</style>
</head>
<body>
<h1><span class="dot off" id="dot"></span> Anomaly Scanner Dashboard</h1>
<div class="subtitle" id="subtitle">Connecting…</div>

<div class="cards" id="cards"></div>

<h2>Detected Anomalies</h2>
<div class="tbl-wrap">
<table>
<thead><tr>
<th>Time</th><th>Plat</th><th>Score</th><th>Side</th><th>Notional</th><th>Mkt Vol</th><th>Signals</th><th>Market</th>
</tr></thead>
<tbody id="tbody"><tr><td colspan="8" class="empty">Waiting for data…</td></tr></tbody>
</table>
</div>
<div class="foot" id="foot"></div>

<script>
function $(id){return document.getElementById(id)}
function fmtN(v){if(v==null)return'—';v=+v;return v>=1e6?'$'+(v/1e6).toFixed(1)+'M':v>=1e3?'$'+(v/1e3).toFixed(1)+'K':'$'+v.toFixed(2)}
function fmtK(v){if(v==null)return'0';v=+v;return v>=1e6?(v/1e6).toFixed(1)+'M':v>=1e3?(v/1e3).toFixed(1)+'K':v.toString()}
function fmtT(iso){if(!iso)return'—';try{let d=new Date(iso);return d.toLocaleTimeString()}catch(e){return iso}}
function fmtDur(iso){if(!iso)return'—';let s=Math.floor((Date.now()-new Date(iso).getTime())/1000);
  if(s<60)return s+'s';if(s<3600)return Math.floor(s/60)+'m '+s%60+'s';let h=Math.floor(s/3600);return h+'h '+Math.floor((s%3600)/60)+'m'}
function scoreClass(s){return s>=0.5?'s-hi':s>=0.35?'s-md':'s-lo'}

async function refresh(){
  try{
    let [sr,ar]=await Promise.all([fetch('/api/state'),fetch('/api/anomalies')]);
    let state=await sr.json(), anomalies=await ar.json();
    renderState(state);
    renderAnomalies(anomalies);
    $('foot').textContent='Last refresh: '+new Date().toLocaleTimeString()+' — auto-refresh every 5s';
  }catch(e){
    $('foot').textContent='Connection error: '+e.message;
    $('dot').className='dot off';
  }
}

function renderState(s){
  if(!s||!s.started_at){$('subtitle').textContent='Scanner not running';$('dot').className='dot off';return}
  $('dot').className='dot on';
  let ago=fmtDur(s.last_cycle);
  $('subtitle').textContent='Running since '+fmtT(s.started_at)+' — last cycle '+ago+' ago';
  let cards=[
    {l:'Cycles',v:fmtK(s.cycle_count),c:'b'},
    {l:'Trades Scanned',v:fmtK(s.total_trades_scanned),c:'b'},
    {l:'Anomalies',v:s.total_anomalies||'0',c:s.total_anomalies>0?'r':'g'},
    {l:'Skip: Low Vol',v:fmtK(s.total_filtered_volume),c:'y'},
    {l:'Skip: Closed',v:fmtK(s.total_filtered_closed),c:'y'},
    {l:'Min Volume',v:fmtN(s.min_market_volume),c:'b'},
    {l:'Threshold',v:s.threshold||'—',c:'b'},
    {l:'Cache',v:fmtK(s.market_cache_size),c:'b'},
  ];
  $('cards').innerHTML=cards.map(c=>`<div class="card"><div class="card-label">${c.l}</div><div class="card-value ${c.c}">${c.v}</div></div>`).join('');
}

function renderAnomalies(items){
  if(!items||!items.length){$('tbody').innerHTML='<tr><td colspan="8" class="empty">No anomalies detected yet — scanner is running</td></tr>';return}
  $('tbody').innerHTML=items.map(a=>{
    let plat=a.platform==='kalshi'?'<span class="badge badge-kal">KAL</span>':'<span class="badge badge-ply">PLY</span>';
    let sc=a.composite_score||0;
    let signals=[];
    if(a.score_size>0.1)signals.push('size='+fmtN(a.sig_size_usd));
    if(a.score_deadline>0.1){let m=a.sig_minutes_to_close;signals.push(m!=null?'deadline='+Math.round(m)+'min':'deadline')}
    if(a.score_price>0.1)signals.push('price='+(a.sig_price_move_pct||0).toFixed(1)+'%');
    if(a.score_volume>0.1)signals.push('vol×'+(a.sig_volume_ratio||0).toFixed(1));
    let sigHtml=signals.map(s=>`<span class="signal-tag">${s}</span>`).join(' ');
    let title=(a.market_title||'—').substring(0,80);
    let mvol=a.notional_usd?fmtN(a.notional_usd):'—';
    return `<tr>
      <td>${fmtT(a.detected_at)}</td>
      <td>${plat}</td>
      <td class="score ${scoreClass(sc)}">${sc.toFixed(3)}</td>
      <td>${a.trade_side||'—'}</td>
      <td>${fmtN(a.notional_usd)}</td>
      <td>${fmtN(a.sig_size_usd)}</td>
      <td>${sigHtml||'—'}</td>
      <td class="title-cell">${title}</td>
    </tr>`;
  }).join('');
}

setInterval(refresh,5000);
refresh();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class DashboardHandler(BaseHTTPRequestHandler):
    """Serves the dashboard HTML and JSON API endpoints."""

    def log_message(self, format, *args):
        pass  # Suppress default access log noise

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/" or path == "/index.html":
            self._respond(200, "text/html", HTML_TEMPLATE.encode())
        elif path == "/api/state":
            self._respond_json(self._get_state())
        elif path == "/api/anomalies":
            self._respond_json(self._get_anomalies())
        else:
            self._respond(404, "text/plain", b"Not found")

    def _respond(self, code: int, content_type: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _respond_json(self, data):
        body = json.dumps(data, default=str).encode()
        self._respond(200, "application/json", body)

    def _get_state(self) -> dict:
        try:
            return json.loads(STATE_FILE.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _get_anomalies(self) -> list[dict]:
        try:
            conn = sqlite3.connect(str(DB_PATH), timeout=5)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM anomalies ORDER BY id DESC LIMIT 100"
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception:
            return []


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Anomaly scanner web dashboard")
    parser.add_argument("--port", type=int, default=8080, help="HTTP port (default: 8080)")
    args = parser.parse_args()

    server = HTTPServer(("0.0.0.0", args.port), DashboardHandler)
    print(f"Dashboard running at http://localhost:{args.port}")
    print("Press Ctrl+C to stop\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
