#!/usr/bin/env python3
"""
Comprehensive test suite for Portfolio Tracker
Tests: API endpoints, DB operations, price fetching, auth

Run:  python3 test_portfolio.py
      python3 test_portfolio.py -v   (verbose)
"""
import unittest, json, threading, time, os, sys, tempfile, shutil, urllib.request, urllib.error, unittest.mock

# ── make sure we can import the server module ─────────────────────────────────
SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SERVER_DIR)

import importlib.util
spec = importlib.util.spec_from_file_location("stock_server",
       os.path.join(SERVER_DIR, "stock-server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

import http.server as _hs
srv.http = type("_http", (), {"server": _hs})()   # expose http.server on the module
srv.PORT    = 18765          # use a separate port so we don't clash with the real server

BASE = f"http://127.0.0.1:{srv.PORT}"

# ── Global session cookie — set by each test class's setUpClass ───────────────
_session_cookie = None


# ── helpers ───────────────────────────────────────────────────────────────────
def api(method, path, body=None, port=None):
    base = f"http://127.0.0.1:{port}" if port else BASE
    url  = base + path
    data = json.dumps(body).encode() if body is not None else None
    headers = {}
    if data:
        headers["Content-Type"] = "application/json"
    if _session_cookie:
        headers["Cookie"] = f"session={_session_cookie}"
    req  = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _raw_api(port, method, path, body=None, cookie=None):
    """API call without the global session cookie (for auth setup)."""
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {}
    if data:
        headers["Content-Type"] = "application/json"
    if cookie:
        headers["Cookie"] = f"session={cookie}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read()), r.headers
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read()), e.headers


def _setup_auth(port):
    """Register testuser, login, return (user_id, session_token)."""
    _raw_api(port, "POST", "/api/register", {"username": "testuser", "password": "testpass"})
    status, body, headers = _raw_api(port, "POST", "/api/login",
                                     {"username": "testuser", "password": "testpass"})
    token = ""
    sc = headers.get("Set-Cookie", "")
    for part in sc.split(";"):
        if part.strip().startswith("session="):
            token = part.strip()[len("session="):]
            break
    return body["id"], token


TX_FIXTURE = {
    "symbol": "TEST",
    "name": "Test Corp",
    "quantity": 10.5,
    "purchase_price": 100.0,
    "purchase_date": "2025-01-15",
    "currency": "EUR",
}


# ═════════════════════════════════════════════════════════════════════════════
# 1. Unit tests — pure Python, no HTTP
# ═════════════════════════════════════════════════════════════════════════════
class TestDatabase(unittest.TestCase):

    def setUp(self):
        srv.DB_PATH = tempfile.mktemp(suffix=".db")
        srv.init_db()

    def tearDown(self):
        if os.path.exists(srv.DB_PATH):
            os.remove(srv.DB_PATH)

    # ── init ─────────────────────────────────────────────────────────────────
    def test_init_creates_table(self):
        rows = srv.db_get_all(1)
        self.assertIsInstance(rows, list)
        self.assertEqual(len(rows), 0)

    # ── insert ───────────────────────────────────────────────────────────────
    def test_insert_returns_row_with_id(self):
        row = srv.db_insert(1, "SAP", "SAP SE", 5.0, 170.0, "2025-06-01", "EUR")
        self.assertIn("id", row)
        self.assertEqual(row["symbol"], "SAP")
        self.assertEqual(row["currency"], "EUR")

    def test_insert_multiple_rows(self):
        srv.db_insert(1, "A", "Alpha", 1.0, 10.0, "2025-01-01", "USD")
        srv.db_insert(1, "B", "Beta",  2.0, 20.0, "2025-01-02", "EUR")
        rows = srv.db_get_all(1)
        self.assertEqual(len(rows), 2)

    def test_insert_preserves_floats(self):
        row = srv.db_insert(1, "X", "X Corp", 3.1415, 99.99, "2025-03-14", "ILS")
        self.assertAlmostEqual(row["quantity"],       3.1415, places=4)
        self.assertAlmostEqual(row["purchase_price"], 99.99,  places=2)

    # ── get_all ───────────────────────────────────────────────────────────────
    def test_get_all_ordered_by_date(self):
        srv.db_insert(1, "Z", "Z", 1, 1, "2025-12-01", "EUR")
        srv.db_insert(1, "A", "A", 1, 1, "2025-01-01", "EUR")
        rows = srv.db_get_all(1)
        self.assertLessEqual(rows[0]["purchase_date"], rows[1]["purchase_date"])

    # ── update ───────────────────────────────────────────────────────────────
    def test_update_existing_row(self):
        row = srv.db_insert(1, "OLD", "Old Name", 1.0, 50.0, "2025-01-01", "EUR")
        updated = srv.db_update(1, row["id"], "NEW", "New Name", 2.0, 75.0, "2025-06-01", "USD")
        self.assertEqual(updated["symbol"], "NEW")
        self.assertEqual(updated["currency"], "USD")
        self.assertAlmostEqual(updated["quantity"], 2.0)

    def test_update_nonexistent_returns_none(self):
        result = srv.db_update(1, 99999, "X", "X", 1, 1, "2025-01-01", "EUR")
        self.assertIsNone(result)

    # ── delete ────────────────────────────────────────────────────────────────
    def test_delete_existing_row(self):
        row = srv.db_insert(1, "DEL", "Del Corp", 1.0, 10.0, "2025-01-01", "EUR")
        result = srv.db_delete(1, row["id"])
        self.assertTrue(result)
        self.assertEqual(len(srv.db_get_all(1)), 0)

    def test_delete_nonexistent_returns_false(self):
        result = srv.db_delete(1, 99999)
        self.assertFalse(result)

    # ── bulk insert ───────────────────────────────────────────────────────────
    def test_bulk_insert(self):
        rows = [
            ["SAP", "SAP SE", 5.0, 170.0, "2025-01-01", "EUR"],
            ["SAP", "SAP SE", 3.0, 180.0, "2025-02-01", "EUR"],
            ["AAPL", "Apple", 2.0, 190.0, "2025-03-01", "USD"],
        ]
        srv.db_bulk_insert(1, rows)
        all_rows = srv.db_get_all(1)
        self.assertEqual(len(all_rows), 3)

    def test_bulk_insert_defaults_currency_to_eur(self):
        srv.db_bulk_insert(1, [["X", "X", 1, 1, "2025-01-01"]])  # no currency
        rows = srv.db_get_all(1)
        self.assertEqual(rows[0]["currency"], "EUR")

    def test_bulk_insert_then_clear(self):
        srv.db_bulk_insert(1, [["X", "X", 1, 1, "2025-01-01", "EUR"]])
        srv.db_clear(1)
        self.assertEqual(len(srv.db_get_all(1)), 0)


# ═════════════════════════════════════════════════════════════════════════════
# 2. Integration tests — real HTTP against test server
# ═════════════════════════════════════════════════════════════════════════════
class TestAPI(unittest.TestCase):

    _user_id = None
    _cookie  = None

    @classmethod
    def setUpClass(cls):
        """Start a test server instance on port 18765."""
        global _session_cookie
        srv.DB_PATH = tempfile.mktemp(suffix=".db")
        srv.init_db()
        srv.http.server.ThreadingHTTPServer.allow_reuse_address = True
        srv.http.server.ThreadingHTTPServer.request_queue_size = 128
        cls.server = srv.http.server.ThreadingHTTPServer(("127.0.0.1", srv.PORT), srv.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.3)
        cls._user_id, cls._cookie = _setup_auth(srv.PORT)
        _session_cookie = cls._cookie

    @classmethod
    def tearDownClass(cls):
        global _session_cookie
        _session_cookie = None
        cls.server.shutdown()
        if os.path.exists(srv.DB_PATH):
            os.remove(srv.DB_PATH)

    def setUp(self):
        """Wipe DB and block live network calls between tests."""
        srv.db_clear()
        with srv._price_lock:
            srv._price_cache.clear()
            srv._ticker_cache.clear()
        _real_urlopen = srv.urllib.request.urlopen
        def _block_yahoo(req, *args, **kwargs):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "yahoo.com" in url:
                raise Exception("Live Yahoo calls blocked in tests — pre-seed the cache")
            return _real_urlopen(req, *args, **kwargs)
        self._urlopen_patcher = unittest.mock.patch.object(
            srv.urllib.request, "urlopen", side_effect=_block_yahoo
        )
        self._urlopen_patcher.start()

    def tearDown(self):
        self._urlopen_patcher.stop()

    # ── GET /api/transactions ─────────────────────────────────────────────────
    def test_get_transactions_empty(self):
        status, body = api("GET", "/api/transactions")
        self.assertEqual(status, 200)
        self.assertEqual(body, [])

    def test_get_transactions_returns_list(self):
        srv.db_insert(self.__class__._user_id, TX_FIXTURE["symbol"], TX_FIXTURE["name"],
                      TX_FIXTURE["quantity"], TX_FIXTURE["purchase_price"],
                      TX_FIXTURE["purchase_date"], TX_FIXTURE["currency"])
        status, body = api("GET", "/api/transactions")
        self.assertEqual(status, 200)
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["symbol"], "TEST")

    # ── POST /api/transactions ────────────────────────────────────────────────
    def test_post_transaction_creates_row(self):
        status, body = api("POST", "/api/transactions", TX_FIXTURE)
        self.assertEqual(status, 201)
        self.assertIn("id", body)
        self.assertEqual(body["symbol"], "TEST")
        self.assertEqual(body["currency"], "EUR")

    def test_post_transaction_missing_field(self):
        bad = {k: v for k, v in TX_FIXTURE.items() if k != "symbol"}
        status, body = api("POST", "/api/transactions", bad)
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_post_transaction_invalid_quantity(self):
        bad = {**TX_FIXTURE, "quantity": "not-a-number"}
        status, body = api("POST", "/api/transactions", bad)
        self.assertEqual(status, 400)

    def test_post_transaction_defaults_currency(self):
        no_ccy = {k: v for k, v in TX_FIXTURE.items() if k != "currency"}
        status, body = api("POST", "/api/transactions", no_ccy)
        self.assertEqual(status, 201)
        self.assertEqual(body["currency"], "EUR")

    # ── PUT /api/transactions/:id ─────────────────────────────────────────────
    def test_put_updates_transaction(self):
        _, created = api("POST", "/api/transactions", TX_FIXTURE)
        updated_payload = {**TX_FIXTURE, "symbol": "UPDATED", "quantity": 99.0}
        status, body = api("PUT", f"/api/transactions/{created['id']}", updated_payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["symbol"], "UPDATED")
        self.assertAlmostEqual(body["quantity"], 99.0)

    def test_put_nonexistent_id(self):
        status, body = api("PUT", "/api/transactions/99999", TX_FIXTURE)
        self.assertEqual(status, 404)

    # ── DELETE /api/transactions/:id ──────────────────────────────────────────
    def test_delete_transaction(self):
        _, created = api("POST", "/api/transactions", TX_FIXTURE)
        status, body = api("DELETE", f"/api/transactions/{created['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(body["deleted"], created["id"])
        _, all_rows = api("GET", "/api/transactions")
        self.assertEqual(len(all_rows), 0)

    def test_delete_nonexistent_id(self):
        status, body = api("DELETE", "/api/transactions/99999")
        self.assertEqual(status, 404)

    # ── POST /api/transactions/import ─────────────────────────────────────────
    def test_import_bulk_replaces_all(self):
        api("POST", "/api/transactions", TX_FIXTURE)  # pre-existing row
        rows = [
            ["SAP", "SAP SE", 5.0, 170.0, "2025-01-01", "EUR"],
            ["SAP", "SAP SE", 3.0, 180.0, "2025-02-01", "EUR"],
        ]
        status, body = api("POST", "/api/transactions/import", {"rows": rows})
        self.assertEqual(status, 201)
        self.assertEqual(body["imported"], 2)
        _, all_rows = api("GET", "/api/transactions")
        self.assertEqual(len(all_rows), 2)
        self.assertTrue(all(r["symbol"] == "SAP" for r in all_rows))

    def test_import_empty_rows_returns_400(self):
        status, body = api("POST", "/api/transactions/import", {"rows": []})
        self.assertEqual(status, 400)

    # ── CORS headers ──────────────────────────────────────────────────────────
    def test_cors_header_on_get(self):
        headers = {"Cookie": f"session={self.__class__._cookie}"}
        req = urllib.request.Request(BASE + "/api/transactions", headers=headers)
        with urllib.request.urlopen(req) as r:
            self.assertEqual(r.headers.get("Access-Control-Allow-Origin"), "*")

    def test_options_preflight(self):
        req = urllib.request.Request(BASE + "/api/transactions", method="OPTIONS")
        with urllib.request.urlopen(req) as r:
            self.assertEqual(r.status, 204)
            self.assertIn("GET", r.headers.get("Access-Control-Allow-Methods", ""))

    # ── static file serving ───────────────────────────────────────────────────
    def test_root_serves_index_html(self):
        req = urllib.request.Request(BASE + "/")
        with urllib.request.urlopen(req) as r:
            self.assertEqual(r.status, 200)
            ct = r.headers.get("Content-Type", "")
            self.assertIn("text/html", ct)

    def test_404_for_unknown_path(self):
        req = urllib.request.Request(BASE + "/nonexistent.xyz")
        try:
            urllib.request.urlopen(req)
            self.fail("Expected 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

    # ── full CRUD round-trip ──────────────────────────────────────────────────
    def test_full_crud_lifecycle(self):
        status, row = api("POST", "/api/transactions", TX_FIXTURE)
        self.assertEqual(status, 201)
        tx_id = row["id"]

        status, rows = api("GET", "/api/transactions")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], tx_id)

        status, updated = api("PUT", f"/api/transactions/{tx_id}",
                              {**TX_FIXTURE, "quantity": 50.0, "purchase_price": 200.0})
        self.assertEqual(status, 200)
        self.assertAlmostEqual(updated["quantity"], 50.0)

        _, rows = api("GET", "/api/transactions")
        self.assertAlmostEqual(rows[0]["quantity"], 50.0)

        status, _ = api("DELETE", f"/api/transactions/{tx_id}")
        self.assertEqual(status, 200)

        _, rows = api("GET", "/api/transactions")
        self.assertEqual(len(rows), 0)

    # ── multiple currencies ───────────────────────────────────────────────────
    def test_multiple_currencies_stored_independently(self):
        api("POST", "/api/transactions", {**TX_FIXTURE, "currency": "EUR"})
        api("POST", "/api/transactions", {**TX_FIXTURE, "currency": "USD"})
        api("POST", "/api/transactions", {**TX_FIXTURE, "currency": "ILS"})
        _, rows = api("GET", "/api/transactions")
        currencies = {r["currency"] for r in rows}
        self.assertEqual(currencies, {"EUR", "USD", "ILS"})

    # ── data integrity ────────────────────────────────────────────────────────
    def test_large_quantity_precision(self):
        payload = {**TX_FIXTURE, "quantity": 1234567.8901, "purchase_price": 99999.99}
        _, row = api("POST", "/api/transactions", payload)
        self.assertAlmostEqual(row["quantity"],       1234567.8901, places=3)
        self.assertAlmostEqual(row["purchase_price"], 99999.99,     places=2)

    def test_concurrent_inserts(self):
        """Concurrent inserts should all succeed without DB corruption."""
        N = 20
        results = []
        def insert(i):
            try:
                s, r = api("POST", "/api/transactions", {**TX_FIXTURE, "symbol": f"S{i}"})
                results.append(s)
            except Exception as exc:
                results.append(f"ERROR: {exc}")

        threads = [threading.Thread(target=insert, args=(i,)) for i in range(N)]
        for t in threads: t.start()
        for t in threads: t.join()

        self.assertTrue(all(s == 201 for s in results), f"Some inserts failed: {results}")
        _, rows = api("GET", "/api/transactions")
        self.assertEqual(len(rows), N)

    # ── Add Purchase: brand-new symbol ────────────────────────────────────────
    def test_add_purchase_new_symbol_creates_first_transaction(self):
        """Adding a brand-new symbol via Add Purchase saves it as a transaction."""
        payload = {
            "symbol": "NEWCO",
            "name":   "New Corp",
            "quantity": 5.0,
            "purchase_price": 99.50,
            "purchase_date": "2025-06-01",
            "currency": "EUR",
        }
        status, row = api("POST", "/api/transactions", payload)
        self.assertEqual(status, 201)
        self.assertEqual(row["symbol"], "NEWCO")
        self.assertAlmostEqual(row["quantity"], 5.0)
        self.assertAlmostEqual(row["purchase_price"], 99.50)
        self.assertEqual(row["currency"], "EUR")

        _, all_rows = api("GET", "/api/transactions")
        newco = [r for r in all_rows if r["symbol"] == "NEWCO"]
        self.assertEqual(len(newco), 1)

    def test_add_purchase_new_symbol_then_second_transaction(self):
        """A second purchase for the same symbol adds a second transaction row."""
        base = {"symbol": "AAA", "name": "Alpha Corp", "quantity": 2.0,
                "purchase_price": 50.0, "purchase_date": "2025-01-01", "currency": "USD"}
        api("POST", "/api/transactions", base)
        api("POST", "/api/transactions", {**base, "quantity": 3.0, "purchase_date": "2025-02-01"})

        _, all_rows = api("GET", "/api/transactions")
        aaa = [r for r in all_rows if r["symbol"] == "AAA"]
        self.assertEqual(len(aaa), 2)
        total_qty = sum(r["quantity"] for r in aaa)
        self.assertAlmostEqual(total_qty, 5.0)


# ═════════════════════════════════════════════════════════════════════════════
# 3. UI Logic tests — test JS business logic via Python simulation
# ═════════════════════════════════════════════════════════════════════════════
class TestBusinessLogic(unittest.TestCase):

    def build_holdings(self, rows, ccy, prices_by_symbol=None):
        from collections import defaultdict
        if prices_by_symbol is None:
            prices_by_symbol = {}
        groups = defaultdict(list)
        for r in rows:
            sym  = r[0].replace(" ", "")
            rccy = (r[5] if len(r) > 5 else "EUR").upper()
            if rccy != ccy: continue
            groups[sym].append({"symbol": sym, "name": r[1], "qty": float(r[2]),
                                 "price": float(r[3]), "date": r[4]})
        result = []
        for sym, lots in groups.items():
            total_qty  = sum(l["qty"] for l in lots)
            total_cost = sum(l["qty"] * l["price"] for l in lots)
            avg_price  = total_cost / total_qty
            cur        = prices_by_symbol.get(sym)
            total_value = cur * total_qty if cur is not None else None
            pct_change  = ((cur * total_qty - total_cost) / total_cost * 100) if cur is not None else None
            gain_loss   = (cur * total_qty - total_cost) if cur is not None else None
            result.append({"symbol": sym, "totalQty": total_qty, "totalCost": total_cost,
                            "avgPrice": avg_price, "totalValue": total_value,
                            "pctChange": pct_change, "gainLoss": gain_loss})
        return result

    def test_total_cost_is_sum_of_lot_costs(self):
        rows = [
            ["SAP", "SAP SE", 5.0, 100.0, "2025-01-01", "EUR"],
            ["SAP", "SAP SE", 3.0, 200.0, "2025-02-01", "EUR"],
        ]
        h = self.build_holdings(rows, "EUR")[0]
        self.assertAlmostEqual(h["totalCost"], 5*100 + 3*200)

    def test_avg_price_weighted_correctly(self):
        rows = [
            ["SAP", "SAP SE", 5.0, 100.0, "2025-01-01", "EUR"],
            ["SAP", "SAP SE", 5.0, 200.0, "2025-02-01", "EUR"],
        ]
        h = self.build_holdings(rows, "EUR")[0]
        self.assertAlmostEqual(h["avgPrice"], 150.0)

    def test_total_qty_summed(self):
        rows = [["SAP","SAP SE", 3.14, 100.0, "2025-01-01", "EUR"],
                ["SAP","SAP SE", 2.86, 100.0, "2025-02-01", "EUR"]]
        h = self.build_holdings(rows, "EUR")[0]
        self.assertAlmostEqual(h["totalQty"], 6.0, places=4)

    def test_gain_when_price_rises(self):
        rows = [["SAP","SAP SE", 10.0, 100.0, "2025-01-01", "EUR"]]
        h = self.build_holdings(rows, "EUR", prices_by_symbol={"SAP": 150.0})[0]
        self.assertAlmostEqual(h["gainLoss"],  500.0)
        self.assertAlmostEqual(h["pctChange"],  50.0)

    def test_loss_when_price_falls(self):
        rows = [["SAP","SAP SE", 10.0, 100.0, "2025-01-01", "EUR"]]
        h = self.build_holdings(rows, "EUR", prices_by_symbol={"SAP": 80.0})[0]
        self.assertAlmostEqual(h["gainLoss"],  -200.0)
        self.assertAlmostEqual(h["pctChange"],  -20.0)

    def test_no_gain_when_no_current_price(self):
        rows = [["SAP","SAP SE", 10.0, 100.0, "2025-01-01", "EUR"]]
        h = self.build_holdings(rows, "EUR")[0]
        self.assertIsNone(h["gainLoss"])
        self.assertIsNone(h["pctChange"])

    def test_eur_rows_excluded_from_usd_section(self):
        rows = [
            ["SAP","SAP SE", 5.0, 100.0, "2025-01-01", "EUR"],
            ["SAP","SAP SE", 3.0, 190.0, "2025-01-02", "USD"],
        ]
        eur = self.build_holdings(rows, "EUR")
        usd = self.build_holdings(rows, "USD")
        self.assertEqual(len(eur), 1)
        self.assertEqual(len(usd), 1)
        self.assertAlmostEqual(eur[0]["totalQty"], 5.0)
        self.assertAlmostEqual(usd[0]["totalQty"], 3.0)

    def test_ils_section_empty_for_eur_rows(self):
        rows = [["SAP","SAP SE", 5.0, 100.0, "2025-01-01", "EUR"]]
        ils = self.build_holdings(rows, "ILS")
        self.assertEqual(len(ils), 0)

    def test_two_symbols_aggregated_separately(self):
        rows = [
            ["AAA","Alpha", 10.0, 50.0, "2025-01-01", "USD"],
            ["BBB","Beta",   5.0, 80.0, "2025-01-02", "USD"],
            ["AAA","Alpha",  5.0, 60.0, "2025-01-03", "USD"],
        ]
        holdings = self.build_holdings(rows, "USD")
        syms = {h["symbol"]: h for h in holdings}
        self.assertIn("AAA", syms); self.assertIn("BBB", syms)
        self.assertAlmostEqual(syms["AAA"]["totalQty"],  15.0)
        self.assertAlmostEqual(syms["BBB"]["totalQty"],   5.0)
        self.assertAlmostEqual(syms["AAA"]["avgPrice"], 800/15, places=5)

    def test_portfolio_total_invested(self):
        rows = [
            ["SAP","SAP SE", 5.0, 200.0, "2025-01-01", "EUR"],
            ["SAP","SAP SE", 3.0, 150.0, "2025-02-01", "EUR"],
        ]
        holdings = self.build_holdings(rows, "EUR")
        total_invested = sum(h["totalCost"] for h in holdings)
        self.assertAlmostEqual(total_invested, 5*200 + 3*150)

    def test_portfolio_total_gain_with_price(self):
        rows = [
            ["SAP","SAP SE", 5.0, 200.0, "2025-01-01", "EUR"],
            ["SAP","SAP SE", 5.0, 200.0, "2025-02-01", "EUR"],
        ]
        holdings = self.build_holdings(rows, "EUR", prices_by_symbol={"SAP": 250.0})
        total_gain = sum(h["gainLoss"] for h in holdings)
        self.assertAlmostEqual(total_gain, 500.0)

    def test_new_symbol_appears_as_single_holding(self):
        """A brand-new symbol added for the first time shows up as exactly one holding."""
        rows = [["NEWCO", "New Corp", 3.0, 50.0, "2025-01-01", "EUR"]]
        holdings = self.build_holdings(rows, "EUR")
        self.assertEqual(len(holdings), 1)
        self.assertEqual(holdings[0]["symbol"], "NEWCO")
        self.assertAlmostEqual(holdings[0]["totalQty"], 3.0)
        self.assertAlmostEqual(holdings[0]["totalCost"], 150.0)
        self.assertAlmostEqual(holdings[0]["avgPrice"], 50.0)

    def test_new_symbol_gain_loss_none_without_price(self):
        """A brand-new symbol with no live price has null gain/loss."""
        rows = [["NEWCO", "New Corp", 3.0, 50.0, "2025-01-01", "EUR"]]
        h = self.build_holdings(rows, "EUR")[0]
        self.assertIsNone(h["gainLoss"])
        self.assertIsNone(h["pctChange"])
        self.assertIsNone(h["totalValue"])

    def test_two_symbols_independent_prices(self):
        """Each symbol uses its own price independently."""
        rows = [
            ["AAA", "Alpha", 10.0, 100.0, "2025-01-01", "EUR"],
            ["BBB", "Beta",  10.0, 100.0, "2025-01-01", "EUR"],
        ]
        holdings = self.build_holdings(rows, "EUR", prices_by_symbol={"AAA": 150.0, "BBB": 80.0})
        by_sym = {h["symbol"]: h for h in holdings}
        self.assertAlmostEqual(by_sym["AAA"]["gainLoss"],  500.0)
        self.assertAlmostEqual(by_sym["BBB"]["gainLoss"], -200.0)

    def test_missing_price_does_not_affect_other_symbols(self):
        """A symbol without a price shows None while others still compute."""
        rows = [
            ["AAA", "Alpha", 10.0, 100.0, "2025-01-01", "EUR"],
            ["BBB", "Beta",  10.0, 100.0, "2025-01-01", "EUR"],
        ]
        holdings = self.build_holdings(rows, "EUR", prices_by_symbol={"AAA": 120.0})
        by_sym = {h["symbol"]: h for h in holdings}
        self.assertAlmostEqual(by_sym["AAA"]["gainLoss"], 200.0)
        self.assertIsNone(by_sym["BBB"]["gainLoss"])


# ═════════════════════════════════════════════════════════════════════════════
# 4. Ticker search tests — unit tests for fetch_yahoo_search
# ═════════════════════════════════════════════════════════════════════════════
class TestTickerSearch(unittest.TestCase):

    def setUp(self):
        with srv._price_lock:
            srv._ticker_cache.clear()

    def _mock_search(self, quotes):
        import io
        body = json.dumps({"quotes": quotes}).encode()
        class FakeResp:
            def read(self): return body
            def info(self): return {}
            def __enter__(self): return self
            def __exit__(self, *a): pass
        return FakeResp()

    def test_returns_first_quote_symbol(self):
        with unittest.mock.patch.object(srv.urllib.request, "urlopen",
                                 return_value=self._mock_search([{"symbol": "SAP.DE"}, {"symbol": "SAP"}])):
            result = srv.fetch_yahoo_search("SAP AG")
        self.assertEqual(result, "SAP.DE")

    def test_returns_none_for_empty_quotes(self):
        with unittest.mock.patch.object(srv.urllib.request, "urlopen",
                                 return_value=self._mock_search([])):
            result = srv.fetch_yahoo_search("UNKNOWN CORP XYZ")
        self.assertIsNone(result)

    def test_caches_result(self):
        with unittest.mock.patch.object(srv.urllib.request, "urlopen",
                                 return_value=self._mock_search([{"symbol": "SAP.DE"}])) as m:
            srv.fetch_yahoo_search("SAP AG")
            srv.fetch_yahoo_search("SAP AG")
            self.assertEqual(m.call_count, 1)

    def test_returns_none_on_network_error(self):
        with unittest.mock.patch.object(srv.urllib.request, "urlopen", side_effect=Exception("timeout")):
            result = srv.fetch_yahoo_search("SAP AG")
        self.assertIsNone(result)


# ═════════════════════════════════════════════════════════════════════════════
# 5. Ticker-search & multi-symbol price — API integration tests
# ═════════════════════════════════════════════════════════════════════════════
TICKER_PORT = 18767

class TestTickerAPI(unittest.TestCase):

    _user_id = None
    _cookie  = None

    @classmethod
    def setUpClass(cls):
        global _session_cookie
        srv.DB_PATH = tempfile.mktemp(suffix=".db")
        srv.init_db()
        srv.http.server.ThreadingHTTPServer.allow_reuse_address = True
        cls.server = srv.http.server.ThreadingHTTPServer(("127.0.0.1", TICKER_PORT), srv.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.3)
        cls._user_id, cls._cookie = _setup_auth(TICKER_PORT)
        _session_cookie = cls._cookie

    @classmethod
    def tearDownClass(cls):
        global _session_cookie
        _session_cookie = None
        cls.server.shutdown()
        if os.path.exists(srv.DB_PATH):
            os.remove(srv.DB_PATH)

    def setUp(self):
        _real_urlopen = srv.urllib.request.urlopen
        def _block_yahoo(req, *args, **kwargs):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "yahoo.com" in url:
                raise Exception("Live Yahoo calls blocked in tests — pre-seed the cache")
            return _real_urlopen(req, *args, **kwargs)
        self._urlopen_patcher = unittest.mock.patch.object(
            srv.urllib.request, "urlopen", side_effect=_block_yahoo
        )
        self._urlopen_patcher.start()
        with srv._price_lock:
            srv._ticker_cache.clear()
            srv._price_cache.clear()

    def tearDown(self):
        self._urlopen_patcher.stop()

    def _api(self, path):
        url = f"http://127.0.0.1:{TICKER_PORT}{path}"
        headers = {}
        if self.__class__._cookie:
            headers["Cookie"] = f"session={self.__class__._cookie}"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    # ── /api/ticker-search ────────────────────────────────────────────────────
    def test_ticker_search_missing_q_returns_400(self):
        status, body = self._api("/api/ticker-search")
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_ticker_search_returns_ticker(self):
        with srv._price_lock:
            srv._ticker_cache["sap|EUR"] = {"data": "SAP.DE", "ts": time.time()}
        status, body = self._api("/api/ticker-search?q=SAP+AG&currency=EUR")
        self.assertEqual(status, 200)
        self.assertEqual(body["ticker"], "SAP.DE")

    def test_ticker_search_returns_null_on_no_match(self):
        with srv._price_lock:
            srv._ticker_cache["noresult|"] = {"data": None, "ts": time.time()}
        status, body = self._api("/api/ticker-search?q=NORESULT")
        self.assertEqual(status, 200)
        self.assertIsNone(body["ticker"])

    # ── /api/price?symbols= ───────────────────────────────────────────────────
    def _seed_price(self, ticker, price=172.3, prev=169.26):
        result = {"price": price, "prev": prev,
                  "chg": price - prev, "chgPct": (price - prev) / prev * 100,
                  "currency": None, "symbol": ticker, "source": "Yahoo Finance"}
        with srv._price_lock:
            srv._price_cache[ticker] = {"data": result, "ts": time.time()}

    def test_price_with_symbols_param_returns_ticker_keyed_dict(self):
        self._seed_price("SAP.DE")
        status, body = self._api("/api/price?symbols=SAP.DE")
        self.assertEqual(status, 200)
        self.assertIn("SAP.DE", body)
        self.assertAlmostEqual(body["SAP.DE"]["price"], 172.3)

    def test_price_no_params_returns_legacy_eur_usd(self):
        self._seed_price("SAP.DE")
        self._seed_price("SAP", price=197.3, prev=195.5)
        status, body = self._api("/api/price")
        self.assertEqual(status, 200)
        self.assertIn("EUR", body)
        self.assertIn("USD", body)

    def test_price_symbols_capped_at_20(self):
        for i in range(25):
            self._seed_price(f"T{i}", price=10.0 + i, prev=9.0 + i)
        symbols = ",".join([f"T{i}" for i in range(25)])
        status, body = self._api(f"/api/price?symbols={symbols}")
        self.assertEqual(status, 200)
        self.assertLessEqual(len(body), 20)


# ═════════════════════════════════════════════════════════════════════════════
# 6. Authentication tests
# ═════════════════════════════════════════════════════════════════════════════
AUTH_PORT = 18768

class TestAuth(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        srv.http.server.ThreadingHTTPServer.allow_reuse_address = True
        cls._db_path = tempfile.mktemp(suffix=".db")
        srv.DB_PATH = cls._db_path
        srv.init_db()
        cls.server = srv.http.server.ThreadingHTTPServer(("127.0.0.1", AUTH_PORT), srv.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        if os.path.exists(cls._db_path):
            os.remove(cls._db_path)

    def setUp(self):
        # Reset auth state between tests
        srv.DB_PATH = self.__class__._db_path
        with srv.get_conn() as conn:
            conn.execute("DELETE FROM sessions")
            conn.execute("DELETE FROM users")
            conn.execute("DELETE FROM transactions")
            conn.commit()

    def _api(self, method, path, body=None, cookie=None):
        status, data, headers = _raw_api(AUTH_PORT, method, path, body=body, cookie=cookie)
        return status, data, headers

    def _extract_cookie(self, headers):
        sc = headers.get("Set-Cookie", "")
        for part in sc.split(";"):
            if part.strip().startswith("session="):
                return part.strip()[len("session="):]
        return None

    # ── /api/me ───────────────────────────────────────────────────────────────
    def test_me_returns_setup_when_no_users(self):
        status, body, _ = self._api("GET", "/api/me")
        self.assertEqual(status, 200)
        self.assertTrue(body.get("setup"))

    def test_me_returns_401_when_users_exist_but_no_session(self):
        srv.register_user("alice", "secret")
        status, body, _ = self._api("GET", "/api/me")
        self.assertEqual(status, 401)

    def test_me_returns_user_when_authenticated(self):
        srv.register_user("alice", "secret")
        _, login_body, headers = self._api("POST", "/api/login", {"username": "alice", "password": "secret"})
        cookie = self._extract_cookie(headers)
        status, body, _ = self._api("GET", "/api/me", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(body["username"], "alice")

    # ── /api/register ─────────────────────────────────────────────────────────
    def test_register_first_user_succeeds(self):
        status, body, _ = self._api("POST", "/api/register",
                                    {"username": "alice", "password": "secret"})
        self.assertEqual(status, 201)
        self.assertEqual(body["username"], "alice")
        self.assertIn("id", body)

    def test_register_duplicate_username_fails(self):
        # Register alice (no auth needed — first user)
        self._api("POST", "/api/register", {"username": "alice", "password": "secret"})
        # Login to get a session cookie
        _, _, h = self._api("POST", "/api/login", {"username": "alice", "password": "secret"})
        cookie = self._extract_cookie(h)
        # Try to register alice again WITH auth — should be 400 (duplicate), not 401
        status, body, _ = _raw_api(AUTH_PORT, "POST", "/api/register",
                                   {"username": "alice", "password": "other"}, cookie=cookie)
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_register_missing_fields_returns_400(self):
        status, body, _ = self._api("POST", "/api/register", {"username": "alice"})
        self.assertEqual(status, 400)

    def test_register_second_user_requires_auth(self):
        # First user exists — registering a second without auth should be rejected
        srv.register_user("alice", "secret")
        status, body, _ = self._api("POST", "/api/register",
                                    {"username": "bob", "password": "pass"})
        self.assertEqual(status, 401)

    # ── /api/login ────────────────────────────────────────────────────────────
    def test_login_correct_credentials_sets_cookie(self):
        srv.register_user("alice", "secret")
        status, body, headers = self._api("POST", "/api/login",
                                          {"username": "alice", "password": "secret"})
        self.assertEqual(status, 200)
        self.assertEqual(body["username"], "alice")
        cookie = self._extract_cookie(headers)
        self.assertIsNotNone(cookie)
        self.assertTrue(len(cookie) > 10)

    def test_login_wrong_password_returns_401(self):
        srv.register_user("alice", "secret")
        status, body, _ = self._api("POST", "/api/login",
                                    {"username": "alice", "password": "wrong"})
        self.assertEqual(status, 401)

    def test_login_nonexistent_user_returns_401(self):
        status, body, _ = self._api("POST", "/api/login",
                                    {"username": "nobody", "password": "pass"})
        self.assertEqual(status, 401)

    # ── /api/logout ───────────────────────────────────────────────────────────
    def test_logout_clears_session(self):
        srv.register_user("alice", "secret")
        _, _, headers = self._api("POST", "/api/login", {"username": "alice", "password": "secret"})
        cookie = self._extract_cookie(headers)
        self._api("POST", "/api/logout", cookie=cookie)
        status, _, _ = self._api("GET", "/api/me", cookie=cookie)
        self.assertEqual(status, 401)

    # ── protected endpoints ───────────────────────────────────────────────────
    def test_transactions_requires_auth(self):
        srv.register_user("alice", "secret")
        status, body, _ = self._api("GET", "/api/transactions")
        self.assertEqual(status, 401)

    def test_transactions_accessible_with_valid_session(self):
        srv.register_user("alice", "secret")
        _, _, headers = self._api("POST", "/api/login", {"username": "alice", "password": "secret"})
        cookie = self._extract_cookie(headers)
        status, body, _ = self._api("GET", "/api/transactions", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(body, [])

    # ── data isolation ────────────────────────────────────────────────────────
    def test_user_data_isolation(self):
        """User A's transactions are invisible to user B."""
        # Register and login as Alice
        srv.register_user("alice", "pass")
        _, _, h_a = self._api("POST", "/api/login", {"username": "alice", "password": "pass"})
        cookie_a = self._extract_cookie(h_a)

        # Alice creates a transaction
        self._api("POST", "/api/transactions", TX_FIXTURE, cookie=cookie_a)

        # Register and login as Bob
        _, _, h_admin = self._api("POST", "/api/login", {"username": "alice", "password": "pass"})
        cookie_admin = self._extract_cookie(h_admin)
        self._api("POST", "/api/register", {"username": "bob", "password": "pass"},
                  cookie=cookie_admin)
        _, _, h_b = self._api("POST", "/api/login", {"username": "bob", "password": "pass"})
        cookie_b = self._extract_cookie(h_b)

        # Bob should see zero transactions
        status, rows, _ = self._api("GET", "/api/transactions", cookie=cookie_b)
        self.assertEqual(status, 200)
        self.assertEqual(len(rows), 0)

        # Alice still sees her transaction
        status, rows, _ = self._api("GET", "/api/transactions", cookie=cookie_a)
        self.assertEqual(status, 200)
        self.assertEqual(len(rows), 1)


# ═════════════════════════════════════════════════════════════════════════════
# 7. Edge-case / regression tests
# ═════════════════════════════════════════════════════════════════════════════
EDGE_PORT = 18766

class TestEdgeCases(unittest.TestCase):

    _user_id = None
    _cookie  = None

    @classmethod
    def setUpClass(cls):
        global _session_cookie
        srv.DB_PATH = tempfile.mktemp(suffix=".db")
        srv.init_db()
        srv.http.server.ThreadingHTTPServer.allow_reuse_address = True
        cls.server = srv.http.server.ThreadingHTTPServer(("127.0.0.1", EDGE_PORT), srv.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.3)
        cls._user_id, cls._cookie = _setup_auth(EDGE_PORT)
        _session_cookie = cls._cookie

    @classmethod
    def tearDownClass(cls):
        global _session_cookie
        _session_cookie = None
        cls.server.shutdown()
        if os.path.exists(srv.DB_PATH):
            os.remove(srv.DB_PATH)

    def setUp(self):
        srv.db_clear()
        with srv._price_lock:
            srv._price_cache.clear()
            srv._ticker_cache.clear()
        _real_urlopen = srv.urllib.request.urlopen
        def _block_yahoo(req, *args, **kwargs):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "yahoo.com" in url:
                raise Exception("Live Yahoo calls blocked in tests — pre-seed the cache")
            return _real_urlopen(req, *args, **kwargs)
        self._urlopen_patcher = unittest.mock.patch.object(
            srv.urllib.request, "urlopen", side_effect=_block_yahoo
        )
        self._urlopen_patcher.start()

    def tearDown(self):
        self._urlopen_patcher.stop()

    def _api(self, method, path, body=None):
        url  = f"http://127.0.0.1:{EDGE_PORT}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {}
        if data:
            headers["Content-Type"] = "application/json"
        if self.__class__._cookie:
            headers["Cookie"] = f"session={self.__class__._cookie}"
        req  = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_symbol_with_whitespace_is_stored(self):
        payload = {**TX_FIXTURE, "symbol": "  50724  "}
        _, row = self._api("POST", "/api/transactions", payload)
        self.assertIn("50724", row["symbol"])

    def test_very_small_quantity(self):
        payload = {**TX_FIXTURE, "quantity": 0.0001}
        status, row = self._api("POST", "/api/transactions", payload)
        self.assertEqual(status, 201)
        self.assertAlmostEqual(row["quantity"], 0.0001, places=4)

    def test_import_then_get_round_trip(self):
        rows = [[f"S{i}", f"Stock {i}", float(i), float(i*10), "2025-01-01", "EUR"]
                for i in range(1, 11)]
        self._api("POST", "/api/transactions/import", {"rows": rows})
        _, fetched = self._api("GET", "/api/transactions")
        self.assertEqual(len(fetched), 10)
        symbols = {r["symbol"] for r in fetched}
        self.assertEqual(symbols, {f"S{i}" for i in range(1, 11)})

    def test_delete_one_of_many_leaves_others(self):
        ids = []
        for i in range(5):
            _, r = self._api("POST", "/api/transactions", {**TX_FIXTURE, "symbol": f"X{i}"})
            ids.append(r["id"])
        self._api("DELETE", f"/api/transactions/{ids[2]}")
        _, rows = self._api("GET", "/api/transactions")
        self.assertEqual(len(rows), 4)
        self.assertNotIn(ids[2], [r["id"] for r in rows])

    def test_update_does_not_affect_other_rows(self):
        _, r1 = self._api("POST", "/api/transactions", {**TX_FIXTURE, "symbol": "AAA"})
        _, r2 = self._api("POST", "/api/transactions", {**TX_FIXTURE, "symbol": "BBB"})
        self._api("PUT", f"/api/transactions/{r1['id']}", {**TX_FIXTURE, "symbol": "AAA-MOD"})
        _, rows = self._api("GET", "/api/transactions")
        syms = {r["symbol"] for r in rows}
        self.assertIn("AAA-MOD", syms)
        self.assertIn("BBB", syms)


# ─── runner ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("\n" + "═"*60)
    print("  Portfolio Tracker — Test Suite")
    print("═"*60 + "\n")

    loader = unittest.TestLoader()
    suite  = unittest.TestSuite()
    for cls in [TestDatabase, TestAPI, TestBusinessLogic, TestTickerSearch,
                TestTickerAPI, TestAuth, TestEdgeCases]:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    verbosity = 2 if "-v" in sys.argv else 1
    runner = unittest.TextTestRunner(verbosity=verbosity)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
