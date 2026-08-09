#!/usr/bin/env python3
"""
Stock Portfolio Tracker — local server
  • Serves index.html (the app)
  • SQLite database for transaction persistence
  • REST API for CRUD operations
  • Proxies live SAP stock price from Yahoo Finance

Run:  python3 stock-server.py
Open: http://localhost:8765
"""
import http.server, urllib.request, urllib.parse, json, os, gzip, threading, time, sqlite3, re

PORT    = 8765
DIR     = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(DIR, "portfolio.db")

# ─── Database ─────────────────────────────────────────────────────────────────
_db_lock = threading.Lock()

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol        TEXT    NOT NULL,
                name          TEXT    NOT NULL,
                quantity      REAL    NOT NULL,
                purchase_price REAL   NOT NULL,
                purchase_date TEXT    NOT NULL,
                currency      TEXT    NOT NULL DEFAULT 'EUR',
                created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
                updated_at    TEXT    NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.commit()
    print(f"  [db] Database ready → {DB_PATH}")

def db_get_all():
    with _db_lock:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM transactions ORDER BY purchase_date ASC, id ASC"
            ).fetchall()
    return [dict(r) for r in rows]

def db_insert(symbol, name, qty, price, date, ccy):
    with _db_lock:
        with get_conn() as conn:
            cur = conn.execute(
                """INSERT INTO transactions (symbol, name, quantity, purchase_price, purchase_date, currency)
                   VALUES (?,?,?,?,?,?)""",
                (symbol, name, qty, price, date, ccy)
            )
            conn.commit()
            row = conn.execute("SELECT * FROM transactions WHERE id=?", (cur.lastrowid,)).fetchone()
    return dict(row)

def db_update(tx_id, symbol, name, qty, price, date, ccy):
    with _db_lock:
        with get_conn() as conn:
            conn.execute(
                """UPDATE transactions
                   SET symbol=?, name=?, quantity=?, purchase_price=?, purchase_date=?,
                       currency=?, updated_at=datetime('now')
                   WHERE id=?""",
                (symbol, name, qty, price, date, ccy, tx_id)
            )
            conn.commit()
            row = conn.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
    return dict(row) if row else None

def db_delete(tx_id):
    with _db_lock:
        with get_conn() as conn:
            affected = conn.execute("DELETE FROM transactions WHERE id=?", (tx_id,)).rowcount
            conn.commit()
    return affected > 0

def db_bulk_insert(rows):
    """rows: list of [symbol, name, qty, price, date, ccy]"""
    with _db_lock:
        with get_conn() as conn:
            conn.executemany(
                """INSERT INTO transactions (symbol, name, quantity, purchase_price, purchase_date, currency)
                   VALUES (?,?,?,?,?,?)""",
                [(r[0], r[1], float(r[2]), float(r[3]), r[4], r[5] if len(r) > 5 else 'EUR')
                 for r in rows]
            )
            conn.commit()

def db_clear():
    with _db_lock:
        with get_conn() as conn:
            conn.execute("DELETE FROM transactions")
            conn.commit()

# ─── Price & ticker fetching ───────────────────────────────────────────────────
_price_cache  = {}
_ticker_cache = {}
_price_lock   = threading.Lock()
CACHE_TTL        = 60
TICKER_CACHE_TTL = 3600

YAHOO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
}

def fetch_yahoo_quote(yf_symbol, currency=None):
    with _price_lock:
        cached = _price_cache.get(yf_symbol)
        if cached and time.time() - cached["ts"] < CACHE_TTL:
            return cached["data"]

    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_symbol}?interval=1d&range=1d"
        req = urllib.request.Request(url, headers=YAHOO_HEADERS)
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read()
            if resp.info().get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            data = json.loads(raw)
        meta  = data["chart"]["result"][0]["meta"]
        price = float(meta.get("regularMarketPrice") or 0)
        prev  = float(meta.get("chartPreviousClose") or 0)
        if price:
            chg     = price - prev if prev else None
            chg_pct = ((price - prev) / prev * 100) if prev else None
            result  = {"price": price, "prev": prev, "chg": chg, "chgPct": chg_pct,
                       "currency": currency, "symbol": yf_symbol, "source": "Yahoo Finance"}
            with _price_lock:
                _price_cache[yf_symbol] = {"data": result, "ts": time.time()}
            return result
    except Exception as e:
        print(f"  [price] {yf_symbol} failed: {e}")
    return None

def _clean_stock_name(name):
    """Strip custodian/fund suffixes and corporate entity words to get a Yahoo-searchable name."""
    import re
    # Remove trailing custodian/product suffixes after hyphen (e.g. -SPONSORE, -ADR)
    name = re.sub(r'\s*[-–]\s*\S.*$', '', name)
    # Remove trailing parenthetical (e.g. (ADR))
    name = re.sub(r'\s*\(.*?\)\s*$', '', name)
    # Remove trailing corporate entity words that confuse Yahoo
    name = re.sub(r'\s+\b(AG|SE|SA|NV|PLC|LTD|LLC|INC|CORP|GmbH|KGaA)\b\.?\s*$', '', name, flags=re.I)
    return name.strip()

def fetch_yahoo_search(query, currency=None):
    query = _clean_stock_name(query)
    key = f"{query.lower().strip()}|{currency or ''}"
    with _price_lock:
        cached = _ticker_cache.get(key)
        if cached and time.time() - cached["ts"] < TICKER_CACHE_TTL:
            return cached["data"]

    # Exchanges preferred per currency
    EUR_EXCHANGES = {"GER", "FRA", "VIE", "MIL", "AMS", "PAR", "MCE", "BRU", "LIS", "HEL"}
    USD_EXCHANGES = {"NYQ", "NAS", "PCX", "ASE"}

    try:
        q   = urllib.parse.quote(query)
        url = f"https://query1.finance.yahoo.com/v1/finance/search?q={q}&quotesCount=8&newsCount=0"
        req = urllib.request.Request(url, headers=YAHOO_HEADERS)
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read()
            if resp.info().get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            data = json.loads(raw)
        quotes = [q for q in data.get("quotes", []) if q.get("symbol")]
        ticker = None
        if quotes:
            if currency == "EUR":
                ticker = next((q["symbol"] for q in quotes if q.get("exchange") in EUR_EXCHANGES), None)
            elif currency == "USD":
                ticker = next((q["symbol"] for q in quotes if q.get("exchange") in USD_EXCHANGES), None)
            if not ticker:
                ticker = quotes[0]["symbol"]
        with _price_lock:
            _ticker_cache[key] = {"data": ticker, "ts": time.time()}
        return ticker
    except Exception as e:
        print(f"  [search] {query!r} failed: {e}")
    return None

# ─── HTTP Handler ─────────────────────────────────────────────────────────────
class Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f"  {self.address_string()} {fmt % args}")

    def cors_headers(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def json_response(self, data, status=200):
        body = json.dumps(data, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-cache")
        self.cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def do_OPTIONS(self):
        self.send_response(204)
        self.cors_headers()
        self.end_headers()

    # ── GET ───────────────────────────────────────────────────────────────────
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path

        # /api/transactions — list all
        if path == "/api/transactions":
            self.json_response(db_get_all())
            return

        # /api/ticker-search?q=<name>&currency=EUR — resolve stock name to Yahoo ticker
        if path == "/api/ticker-search":
            params   = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            q        = (params.get("q")        or [""])[0].strip()
            currency = (params.get("currency") or [""])[0].strip().upper() or None
            if not q:
                self.json_response({"error": "q param required"}, 400)
                return
            ticker = fetch_yahoo_search(q, currency)
            self.json_response({"ticker": ticker})
            return

        # /api/price — live prices
        # ?symbols=SAP.DE,SAP  → { "SAP.DE": {...}, "SAP": {...} }  (per-symbol, no currency)
        # (no params)          → { "EUR": {...}, "USD": {...} }      (legacy)
        if path == "/api/price":
            params  = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            symbols = (params.get("symbols") or [""])[0].strip()
            if symbols:
                tickers = [s.strip() for s in symbols.split(",") if s.strip()][:20]
                result  = {}
                for t in tickers:
                    q = fetch_yahoo_quote(t)
                    if q:
                        result[t] = q
                self.json_response(result)
            else:
                eur = fetch_yahoo_quote("SAP.DE", "EUR")
                usd = fetch_yahoo_quote("SAP",    "USD")
                self.json_response({"EUR": eur, "USD": usd})
            return

        # static files
        if path in ("/", ""):
            path = "/index.html"
        filepath = os.path.join(DIR, path.lstrip("/"))
        if os.path.isfile(filepath):
            ext = os.path.splitext(filepath)[1].lstrip(".")
            ct  = {"html":"text/html","js":"application/javascript",
                   "css":"text/css","json":"application/json"}.get(ext, "text/plain")
            with open(filepath, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.cors_headers()
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404); self.end_headers()

    # ── POST ──────────────────────────────────────────────────────────────────
    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path

        # /api/transactions — insert one
        if path == "/api/transactions":
            d = self.read_body()
            try:
                row = db_insert(
                    str(d["symbol"]).strip(),
                    str(d["name"]).strip(),
                    float(d["quantity"]),
                    float(d["purchase_price"]),
                    str(d["purchase_date"]),
                    str(d.get("currency", "EUR")).upper(),
                )
                self.json_response(row, 201)
            except (KeyError, ValueError) as e:
                self.json_response({"error": str(e)}, 400)
            return

        # /api/transactions/import — replace all with bulk data
        if path == "/api/transactions/import":
            d = self.read_body()
            rows = d.get("rows", [])
            if not rows:
                self.json_response({"error": "no rows"}, 400)
                return
            db_clear()
            db_bulk_insert(rows)
            self.json_response({"imported": len(rows)}, 201)
            return

        self.send_response(404); self.end_headers()

    # ── PUT ───────────────────────────────────────────────────────────────────
    def do_PUT(self):
        path = urllib.parse.urlparse(self.path).path
        m = re.match(r"^/api/transactions/(\d+)$", path)
        if m:
            tx_id = int(m.group(1))
            d = self.read_body()
            try:
                row = db_update(
                    tx_id,
                    str(d["symbol"]).strip(),
                    str(d["name"]).strip(),
                    float(d["quantity"]),
                    float(d["purchase_price"]),
                    str(d["purchase_date"]),
                    str(d.get("currency", "EUR")).upper(),
                )
                if row:
                    self.json_response(row)
                else:
                    self.json_response({"error": "not found"}, 404)
            except (KeyError, ValueError) as e:
                self.json_response({"error": str(e)}, 400)
            return
        self.send_response(404); self.end_headers()

    # ── DELETE ────────────────────────────────────────────────────────────────
    def do_DELETE(self):
        path = urllib.parse.urlparse(self.path).path
        m = re.match(r"^/api/transactions/(\d+)$", path)
        if m:
            tx_id = int(m.group(1))
            if db_delete(tx_id):
                self.json_response({"deleted": tx_id})
            else:
                self.json_response({"error": "not found"}, 404)
            return
        self.send_response(404); self.end_headers()


# ─── Main ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    os.chdir(DIR)
    init_db()
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    server = http.server.ThreadingHTTPServer(("", PORT), Handler)
    print(f"\n  Stock Portfolio Tracker")
    print(f"  ──────────────────────────────────────────────")
    print(f"  Open in browser  → http://localhost:{PORT}")
    print(f"  Database         → {DB_PATH}")
    print(f"  Press Ctrl+C to stop\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")
