#!/usr/bin/env python3
"""
Stock Portfolio Tracker — local server
  • Serves index.html (the app)
  • SQLite database for transaction persistence
  • REST API for CRUD operations
  • Proxies live SAP stock price from Yahoo Finance
  • Username/password authentication with cookie sessions

Run:  python3 stock-server.py
Open: http://localhost:8765
"""
import http.server, urllib.request, urllib.parse, json, os, gzip, threading, time, sqlite3, re
import hashlib, secrets

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
                user_id       INTEGER,
                created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
                updated_at    TEXT    NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT    NOT NULL UNIQUE,
                salt          TEXT    NOT NULL,
                password_hash TEXT    NOT NULL,
                created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                token      TEXT    PRIMARY KEY,
                user_id    INTEGER NOT NULL REFERENCES users(id),
                created_at TEXT    NOT NULL DEFAULT (datetime('now'))
            )
        """)
        # migrate: add user_id column to existing transactions tables
        cols = [r[1] for r in conn.execute("PRAGMA table_info(transactions)")]
        if "user_id" not in cols:
            conn.execute("ALTER TABLE transactions ADD COLUMN user_id INTEGER")
            conn.execute("UPDATE transactions SET user_id = 1 WHERE user_id IS NULL")
        conn.commit()
    print(f"  [db] Database ready → {DB_PATH}")

# ─── Auth ─────────────────────────────────────────────────────────────────────

def hash_password(pw):
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 260_000).hex()
    return salt, h

def verify_password(pw, salt, stored_hash):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 260_000).hex() == stored_hash

def create_session(user_id):
    token = secrets.token_urlsafe(32)
    with _db_lock:
        with get_conn() as conn:
            conn.execute("INSERT INTO sessions (token, user_id) VALUES (?,?)", (token, user_id))
            conn.commit()
    return token

def get_session_user(token):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT u.id, u.username FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?",
            (token,)
        ).fetchone()
    return dict(row) if row else None

def delete_session(token):
    with _db_lock:
        with get_conn() as conn:
            conn.execute("DELETE FROM sessions WHERE token=?", (token,))
            conn.commit()

def users_exist():
    with get_conn() as conn:
        return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None

def register_user(username, password):
    """Returns user dict on success, raises ValueError on duplicate."""
    salt, h = hash_password(password)
    with _db_lock:
        with get_conn() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO users (username, salt, password_hash) VALUES (?,?,?)",
                    (username, salt, h)
                )
                conn.commit()
                row = conn.execute("SELECT id, username FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
                return dict(row)
            except sqlite3.IntegrityError:
                raise ValueError(f"Username '{username}' already exists")

def login_user(username, password):
    """Returns user dict on success, None on bad credentials."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, username, salt, password_hash FROM users WHERE username=?", (username,)
        ).fetchone()
    if not row:
        return None
    if not verify_password(password, row["salt"], row["password_hash"]):
        return None
    return {"id": row["id"], "username": row["username"]}

# ─── Transactions DB ──────────────────────────────────────────────────────────

def db_get_all(user_id):
    with _db_lock:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM transactions WHERE user_id=? ORDER BY purchase_date ASC, id ASC",
                (user_id,)
            ).fetchall()
    return [dict(r) for r in rows]

def db_insert(user_id, symbol, name, qty, price, date, ccy):
    with _db_lock:
        with get_conn() as conn:
            cur = conn.execute(
                """INSERT INTO transactions (symbol, name, quantity, purchase_price, purchase_date, currency, user_id)
                   VALUES (?,?,?,?,?,?,?)""",
                (symbol, name, qty, price, date, ccy, user_id)
            )
            conn.commit()
            row = conn.execute("SELECT * FROM transactions WHERE id=?", (cur.lastrowid,)).fetchone()
    return dict(row)

def db_update(user_id, tx_id, symbol, name, qty, price, date, ccy):
    with _db_lock:
        with get_conn() as conn:
            conn.execute(
                """UPDATE transactions
                   SET symbol=?, name=?, quantity=?, purchase_price=?, purchase_date=?,
                       currency=?, updated_at=datetime('now')
                   WHERE id=? AND user_id=?""",
                (symbol, name, qty, price, date, ccy, tx_id, user_id)
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM transactions WHERE id=? AND user_id=?", (tx_id, user_id)
            ).fetchone()
    return dict(row) if row else None

def db_delete(user_id, tx_id):
    with _db_lock:
        with get_conn() as conn:
            affected = conn.execute(
                "DELETE FROM transactions WHERE id=? AND user_id=?", (tx_id, user_id)
            ).rowcount
            conn.commit()
    return affected > 0

def db_bulk_insert(user_id, rows):
    """rows: list of [symbol, name, qty, price, date, ccy]"""
    with _db_lock:
        with get_conn() as conn:
            conn.executemany(
                """INSERT INTO transactions (symbol, name, quantity, purchase_price, purchase_date, currency, user_id)
                   VALUES (?,?,?,?,?,?,?)""",
                [(r[0], r[1], float(r[2]), float(r[3]), r[4],
                  r[5] if len(r) > 5 else 'EUR', user_id)
                 for r in rows]
            )
            conn.commit()

def db_clear(user_id=None):
    with _db_lock:
        with get_conn() as conn:
            if user_id is not None:
                conn.execute("DELETE FROM transactions WHERE user_id=?", (user_id,))
            else:
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
        # TASE prices are quoted in agorot (ILA = 1/100 ILS) — normalise to ILS
        if meta.get("currency") == "ILA":
            price /= 100
            prev  /= 100
            currency = "ILS"
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
    name = re.sub(r'\s*[-–]\s*\S.*$', '', name)
    name = re.sub(r'\s*\(.*?\)\s*$', '', name)
    name = re.sub(r'\s+\b(AG|SE|SA|NV|PLC|LTD|LLC|INC|CORP|GmbH|KGaA)\b\.?\s*$', '', name, flags=re.I)
    return name.strip()

def _tase_isin(security_id):
    """Convert a numeric TASE security ID to its full Israeli ISIN (e.g. '1150283' → 'IL0011502833')."""
    padded = security_id.zfill(9)
    base   = "IL" + padded
    # ISIN Luhn check digit
    s = "".join(str(ord(c) - 55) if c.isalpha() else c for c in base)
    total = 0
    for i, d in enumerate(reversed(s)):
        n = int(d)
        if i % 2 == 0:
            n *= 2
            if n > 9: n -= 9
        total += n
    return base + str((10 - total % 10) % 10)

def fetch_yahoo_search(query, currency=None, sym=None):
    query = _clean_stock_name(query)
    key = f"{query.lower().strip()}|{currency or ''}|{sym or ''}"
    with _price_lock:
        cached = _ticker_cache.get(key)
        if cached and time.time() - cached["ts"] < TICKER_CACHE_TTL:
            return cached["data"]

    EUR_EXCHANGES = {"GER", "FRA", "VIE", "MIL", "AMS", "PAR", "MCE", "BRU", "LIS", "HEL"}
    USD_EXCHANGES = {"NYQ", "NAS", "PCX", "ASE"}
    ILS_EXCHANGES = {"TLV", "TAV"}

    # For ILS: try symbol-based .TA construction before name search.
    if currency == "ILS" and sym:
        s = sym.strip()
        # Pattern A — TASE fund format: "TCH.F1" → "TCH-F1.TA"
        ta_sym = re.sub(r'\.([A-Za-z])(\d+)$', r'-\1\2.TA', s.upper())
        if ta_sym.endswith('.TA') and ta_sym != s.upper() + '.TA':
            with _price_lock:
                _ticker_cache[key] = {"data": ta_sym, "ts": time.time()}
            return ta_sym
        # Pattern B — symbol is already an ISIN (e.g. "IL0011502833") — search Yahoo directly
        isin_candidate = s.upper()
        if re.match(r'^IL\d{10}$', isin_candidate):
            isin = isin_candidate
        # Pattern C — numeric TASE security ID: build ISIN and search Yahoo
        elif s.isdigit():
            isin = _tase_isin(s)
        else:
            isin = None
        if isin:
            try:
                q   = urllib.parse.quote(isin)
                url = f"https://query1.finance.yahoo.com/v1/finance/search?q={q}&quotesCount=5&newsCount=0"
                req = urllib.request.Request(url, headers=YAHOO_HEADERS)
                with urllib.request.urlopen(req, timeout=8) as resp:
                    raw = resp.read()
                    if resp.info().get("Content-Encoding") == "gzip":
                        raw = gzip.decompress(raw)
                    data = json.loads(raw)
                quotes = [q for q in data.get("quotes", []) if q.get("symbol")]
                ticker = next((q["symbol"] for q in quotes if q.get("exchange") in ILS_EXCHANGES), None)
                if ticker:
                    print(f"  [search] {s!r} → ISIN {isin} → {ticker}")
                    with _price_lock:
                        _ticker_cache[key] = {"data": ticker, "ts": time.time()}
                    return ticker
            except Exception as e:
                print(f"  [search] ISIN lookup {isin!r} failed: {e}")

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
            elif currency == "ILS":
                ticker = next((q["symbol"] for q in quotes if q.get("exchange") in ILS_EXCHANGES), None)
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

    def _session_token(self):
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k.strip() == "session":
                return v.strip()
        return None

    def _get_user(self):
        """Returns user dict {id, username} or None."""
        token = self._session_token()
        return get_session_user(token) if token else None

    def _require_auth(self):
        """Returns user dict or sends 401 and returns None."""
        user = self._get_user()
        if user is None:
            self.json_response({"error": "unauthorized"}, 401)
        return user

    def _set_session_cookie(self, token):
        self.send_header("Set-Cookie", f"session={token}; HttpOnly; SameSite=Strict; Path=/")

    def _clear_session_cookie(self):
        self.send_header("Set-Cookie", "session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")

    def do_OPTIONS(self):
        self.send_response(204)
        self.cors_headers()
        self.end_headers()

    # ── GET ───────────────────────────────────────────────────────────────────
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path

        # /api/me — current user info (public endpoint, returns setup/401/user)
        if path == "/api/me":
            if not users_exist():
                self.json_response({"setup": True})
                return
            user = self._get_user()
            if user is None:
                self.json_response({"error": "unauthorized"}, 401)
                return
            self.json_response({"id": user["id"], "username": user["username"]})
            return

        # /api/transactions — list all
        if path == "/api/transactions":
            user = self._require_auth()
            if user is None: return
            self.json_response(db_get_all(user["id"]))
            return

        # /api/ticker-search?q=<name>&currency=EUR&sym=<internalSymbol>
        if path == "/api/ticker-search":
            user = self._require_auth()
            if user is None: return
            params   = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            q        = (params.get("q")        or [""])[0].strip()
            currency = (params.get("currency") or [""])[0].strip().upper() or None
            sym      = (params.get("sym")      or [""])[0].strip() or None
            if not q:
                self.json_response({"error": "q param required"}, 400)
                return
            ticker = fetch_yahoo_search(q, currency, sym=sym)
            self.json_response({"ticker": ticker})
            return

        # /api/price — live prices
        if path == "/api/price":
            user = self._require_auth()
            if user is None: return
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

        # /api/forex — EUR/ILS and USD/ILS live exchange rates
        if path == "/api/forex":
            user = self._require_auth()
            if user is None: return
            eur = fetch_yahoo_quote("EURILS=X")
            usd = fetch_yahoo_quote("USDILS=X")
            result = {}
            if eur: result["EUR"] = {"rate": round(eur["price"], 4), "source": eur["source"]}
            if usd: result["USD"] = {"rate": round(usd["price"], 4), "source": usd["source"]}
            self.json_response(result)
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

        # /api/register — create user (open if no users exist; else requires auth)
        if path == "/api/register":
            d = self.read_body()
            username = str(d.get("username", "")).strip()
            password = str(d.get("password", "")).strip()
            if not username or not password:
                self.json_response({"error": "username and password required"}, 400)
                return
            if users_exist():
                # only authenticated users can create more accounts
                user = self._require_auth()
                if user is None: return
            try:
                new_user = register_user(username, password)
                self.json_response(new_user, 201)
            except ValueError as e:
                self.json_response({"error": str(e)}, 400)
            return

        # /api/login — authenticate and set session cookie
        if path == "/api/login":
            d = self.read_body()
            username = str(d.get("username", "")).strip()
            password = str(d.get("password", "")).strip()
            user = login_user(username, password)
            if user is None:
                self.json_response({"error": "invalid username or password"}, 401)
                return
            token = create_session(user["id"])
            body  = json.dumps({"id": user["id"], "username": user["username"]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-cache")
            self.cors_headers()
            self._set_session_cookie(token)
            self.end_headers()
            self.wfile.write(body)
            return

        # /api/logout — delete session
        if path == "/api/logout":
            token = self._session_token()
            if token:
                delete_session(token)
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.cors_headers()
            self._clear_session_cookie()
            self.end_headers()
            self.wfile.write(body)
            return

        # /api/transactions — insert one
        if path == "/api/transactions":
            user = self._require_auth()
            if user is None: return
            d = self.read_body()
            try:
                row = db_insert(
                    user["id"],
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
            user = self._require_auth()
            if user is None: return
            d = self.read_body()
            rows = d.get("rows", [])
            if not rows:
                self.json_response({"error": "no rows"}, 400)
                return
            db_clear(user["id"])
            db_bulk_insert(user["id"], rows)
            self.json_response({"imported": len(rows)}, 201)
            return

        self.send_response(404); self.end_headers()

    # ── PUT ───────────────────────────────────────────────────────────────────
    def do_PUT(self):
        path = urllib.parse.urlparse(self.path).path
        m = re.match(r"^/api/transactions/(\d+)$", path)
        if m:
            user = self._require_auth()
            if user is None: return
            tx_id = int(m.group(1))
            d = self.read_body()
            try:
                row = db_update(
                    user["id"],
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
            user = self._require_auth()
            if user is None: return
            tx_id = int(m.group(1))
            if db_delete(user["id"], tx_id):
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
