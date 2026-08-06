# 📈 Portfolio Tracker

A personal stock portfolio tracker built as a single-page web app with an optional local backend server and SQLite database.

## Features

- **Three currency sections** — EUR, USD, and ILS, each with its own summary and holdings table
- **Live market prices** — SAP stock price fetched from Alpha Vantage (Xetra EUR via `SAP.DEX`, NYSE USD via `SAP`)
- **Per-holding summary** — Total Invested, Current Value, Total Gain/Loss, Overall Return %, Total Shares
- **Transaction drill-down** — click any holding row to expand all individual purchase lots
- **Full CRUD** — add, edit, and delete holdings and individual transactions via modal forms
- **Excel import** — upload a `.xlsx` file in the same format to replace the portfolio data
- **SQLite database** — when the local server is running, all data persists in `portfolio.db`
- **Offline fallback** — works without the server using browser `localStorage`

## Quick Start

### Option A — Open directly in browser (offline mode)

```bash
open index.html
```

Data is saved to `localStorage`. No server required.

### Option B — Run with local server (database mode)

```bash
python3 stock-server.py
```

Then open **http://localhost:8765** in your browser.

Data is persisted in `portfolio.db` (SQLite) in the same directory. The status bar in the app shows `· DB` when connected to the server, or `· offline` when using localStorage.

## Project Structure

```
portfolio-tracker/
├── index.html        # Single-page app (HTML + CSS + JS)
├── stock-server.py   # Local Python server: REST API + price proxy
├── portfolio.db      # SQLite database (auto-created on first run)
└── README.md
```

## REST API (when server is running)

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/transactions` | List all transactions |
| `POST` | `/api/transactions` | Add a transaction |
| `PUT` | `/api/transactions/:id` | Update a transaction |
| `DELETE` | `/api/transactions/:id` | Delete a transaction |
| `POST` | `/api/transactions/import` | Bulk replace all transactions |
| `GET` | `/api/price` | Live SAP price (EUR + USD) |

### Transaction object

```json
{
  "id": 1,
  "symbol": "50724",
  "name": "SAP AG-SPONSORE",
  "quantity": 5.25,
  "purchase_price": 174.56,
  "purchase_date": "2024-03-06",
  "currency": "EUR",
  "created_at": "2026-08-06 10:00:00",
  "updated_at": "2026-08-06 10:00:00"
}
```

## Excel Upload Format

The app expects a sheet named `clean-table` (falls back to the first sheet) with the following columns:

| Stock Symbol | Stock Name | Quantity | Purchase Price | Purchase Date | Currency |
|---|---|---|---|---|---|
| 50724 | SAP AG-SPONSORE | 5.25 | 174.56 | 2024-03-06 | EUR |

The `Currency` column is optional and defaults to `EUR` if omitted. Valid values: `EUR`, `USD`, `ILS`.

## Live Price Source

Prices are fetched from [Alpha Vantage](https://www.alphavantage.co/) (free tier, 25 requests/day).

- **EUR** — `SAP.DEX` (Xetra)
- **USD** — `SAP` (NYSE)
- **ILS** — no live price available; cost basis is shown

## Requirements

- A modern web browser (Chrome, Firefox, Safari, Edge)
- Python 3.6+ (only needed for the local server / database mode)
- No external Python dependencies — uses only the standard library (`sqlite3`, `http.server`, `urllib`)
