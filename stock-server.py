#!/usr/bin/env python3
"""
Stock Portfolio Tracker — local server
Serves stock-tracker.html and proxies SAP stock price from Yahoo Finance.
Run: python3 stock-server.py
Then open: http://localhost:8765
"""
import http.server, urllib.request, urllib.parse, json, os, gzip, threading, time

PORT = 8765
DIR  = os.path.dirname(os.path.abspath(__file__))

AV_KEY       = "RIBSWKDXHM0MTYGB"   # Alpha Vantage free key (25 req/day)
_price_cache = {"data": None, "ts": 0}
_cache_lock  = threading.Lock()
CACHE_TTL    = 60  # seconds

def fetch_sap_price():
    with _cache_lock:
        now = time.time()
        if _price_cache["data"] and now - _price_cache["ts"] < CACHE_TTL:
            return _price_cache["data"]

    # Primary: Alpha Vantage
    try:
        url = f"https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol=SAP&apikey={AV_KEY}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        q = data.get("Global Quote", {})
        price = float(q.get("05. price", 0))
        prev  = float(q.get("08. previous close", 0))
        if price:
            result = {"price": price, "prev": prev, "currency": "USD", "symbol": "SAP", "source": "Alpha Vantage"}
            with _cache_lock:
                _price_cache["data"] = result
                _price_cache["ts"]   = time.time()
            return result
    except Exception as e:
        print(f"[price] Alpha Vantage failed: {e}")

    # Fallback: Yahoo Finance
    for sym in ["SAP.DE", "SAP"]:
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=1d"
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=8) as resp:
                raw = resp.read()
                if resp.info().get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                data = json.loads(raw)
            meta = data["chart"]["result"][0]["meta"]
            price = meta.get("regularMarketPrice") or meta.get("previousClose")
            prev  = meta.get("chartPreviousClose") or meta.get("previousClose")
            if price:
                result = {"price": price, "prev": prev, "currency": meta.get("currency", "EUR"), "symbol": sym, "source": "Yahoo"}
                with _cache_lock:
                    _price_cache["data"] = result
                    _price_cache["ts"]   = time.time()
                return result
        except Exception as e:
            print(f"[price] Yahoo {sym} failed: {e}")
    return None


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"  {self.address_string()} {fmt % args}")

    def send_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path

        # ── /api/price  (proxy) ──────────────────────────────────────────────
        if path == "/api/price":
            result = fetch_sap_price()
            body = json.dumps(result or {}).encode()
            self.send_response(200 if result else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-cache")
            self.send_cors()
            self.end_headers()
            self.wfile.write(body)
            return

        # ── static files ─────────────────────────────────────────────────────
        if path == "/" or path == "":
            path = "/stock-tracker.html"
        filepath = os.path.join(DIR, path.lstrip("/"))
        if os.path.isfile(filepath):
            ext = os.path.splitext(filepath)[1]
            ct  = {"html": "text/html", "js": "application/javascript",
                   "css": "text/css", "json": "application/json"}.get(ext.lstrip("."), "text/plain")
            with open(filepath, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_cors()
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_cors()
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.end_headers()


if __name__ == "__main__":
    os.chdir(DIR)
    server = http.server.ThreadingHTTPServer(("", PORT), Handler)
    print(f"\n  Stock Portfolio Tracker")
    print(f"  ─────────────────────────────────────────")
    print(f"  Open in browser → http://localhost:{PORT}")
    print(f"  Press Ctrl+C to stop\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")
