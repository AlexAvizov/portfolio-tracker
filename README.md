# Portfolio Tracker

A personal stock portfolio tracker built as a single-page web app with an optional local backend server and SQLite database.

## Features

- **Three currency sections** — EUR, USD, and ILS, each with its own summary and holdings table
- **Live market prices** — per-symbol prices fetched from Yahoo Finance, with automatic ticker resolution
- **Live FX rates** — EUR/ILS and USD/ILS exchange rates displayed next to each section header, sourced from Yahoo Finance
- **Per-holding summary** — Total Invested, Current Value, Total Gain/Loss, Overall Return %, Total Shares
- **Transaction drill-down** — click any holding row to expand all individual purchase lots inline
- **Full CRUD** — add, edit, and delete holdings and individual transactions via modal forms
- **Excel import** — upload a `.xlsx` file to replace the portfolio data
- **SQLite database** — when the local server is running, all data persists in `portfolio.db`
- **Offline fallback** — works without the server using browser `localStorage`

---

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

Data is persisted in `portfolio.db` (SQLite). The status bar shows `· DB` when connected, or `· offline` when using localStorage.

---

## Component Diagram

```mermaid
graph TB
    subgraph Browser["Browser (index.html)"]
        UI["UI Layer<br/>renderAll · summaryHtml<br/>holdingsTableHtml · expansionRowHtml"]
        State["App State<br/>rawRows · prices · tickerMap<br/>activeSymbol · activeCcy"]
        DataLayer["Data Layer<br/>loadData · persistAndRefresh<br/>saveTransaction"]
        PriceFetch["Price Layer<br/>resolveAllTickers · fetchAllPrices<br/>searchTicker · fetchYahooQuote"]
        LS["localStorage<br/>stockPortfolioData<br/>yfTickerMap"]
    end

    subgraph Server["Python Server (stock-server.py)"]
        API["REST API Handler<br/>GET/POST/PUT/DELETE /api/transactions<br/>POST /api/transactions/import"]
        PriceProxy["Price Proxy<br/>GET /api/price?symbols=<br/>GET /api/ticker-search?q=&currency="]
        Cache["In-Memory Cache<br/>_price_cache TTL 60s<br/>_ticker_cache TTL 3600s"]
        DB[("SQLite<br/>portfolio.db")]
    end

    subgraph External["External Services"]
        YFSearch["Yahoo Finance Search<br/>query1.finance.yahoo.com<br/>/v1/finance/search"]
        YFChart["Yahoo Finance Chart<br/>query1.finance.yahoo.com<br/>/v8/finance/chart"]
        SheetJS["SheetJS CDN<br/>xlsx.full.min.js"]
    end

    UI --> State
    DataLayer --> State
    PriceFetch --> State
    DataLayer -->|API mode| API
    DataLayer -->|offline| LS
    PriceFetch -->|server proxy| PriceProxy
    PriceFetch -->|direct fallback| YFSearch
    PriceFetch -->|direct fallback| YFChart
    API --> DB
    PriceProxy --> Cache
    Cache -->|miss| YFSearch
    Cache -->|miss| YFChart
    UI -->|Excel upload| SheetJS
```

---

## Page Load & Price Flow

```mermaid
sequenceDiagram
    participant Browser
    participant Server as Python Server
    participant DB as SQLite DB
    participant YF as Yahoo Finance

    Browser->>Server: GET /api/transactions
    Server->>DB: SELECT * FROM transactions
    DB-->>Server: rows[]
    Server-->>Browser: transactions JSON
    Browser->>Browser: renderAll() — first paint (no prices)

    loop For each new symbol
        Browser->>Server: GET /api/ticker-search?q=SAP+AG&currency=EUR
        Server->>YF: /v1/finance/search?q=SAP&quotesCount=8
        YF-->>Server: quotes[] with exchanges
        Server-->>Browser: { ticker: "SAP.DE" }
        Browser->>Browser: save to localStorage[yfTickerMap]
    end

    Browser->>Server: GET /api/price?symbols=SAP.DE,SAP,...
    Server->>YF: /v8/finance/chart/SAP.DE?interval=1d&range=1d
    YF-->>Server: regularMarketPrice, chartPreviousClose
    Server-->>Browser: { "SAP.DE": { price, chg, chgPct, ... } }

    Browser->>Browser: renderAll() — final paint with live prices
    Browser->>Browser: update header badge
```

---

## CRUD Flow

```mermaid
flowchart TD
    A([User clicks Add Purchase]) --> B[openAddHolding / openAddTransaction]
    B --> C[Modal opens — user fills form]
    C --> D{Validate fields}
    D -- invalid --> E[showToast warning]
    E --> C
    D -- valid --> F{useAPI?}
    F -- yes, new --> G[POST /api/transactions]
    F -- yes, edit --> H[PUT /api/transactions/:id]
    F -- no --> I[Update rawRows in memory]
    G --> J[closeModal]
    H --> J
    I --> J
    J --> K[persistAndRefresh]
    K -- API mode --> L[GET /api/transactions — reload from DB]
    K -- offline --> M[localStorage.setItem]
    L --> N[renderAll — UI reflects new state]
    M --> N
```

---

## Data Model

### `transactions` table (SQLite)

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER | Primary key, auto-increment |
| `symbol` | TEXT | Internal symbol, e.g. `50724` |
| `name` | TEXT | Display name, e.g. `SAP AG-SPONSORE` |
| `quantity` | REAL | Number of shares |
| `purchase_price` | REAL | Price per share at purchase |
| `purchase_date` | TEXT | ISO date `YYYY-MM-DD` |
| `currency` | TEXT | `EUR` / `USD` / `ILS`, default `EUR` |
| `created_at` | TEXT | Server timestamp on insert |
| `updated_at` | TEXT | Server timestamp on update |

### In-memory structures (client)

| Variable | Shape | Purpose |
|----------|-------|---------|
| `rawRows` | `[symbol, name, qty, price, date, ccy][]` | All transactions |
| `prices` | `{ internalSymbol: { price, chg, chgPct, yfTicker, currency } }` | Live quotes keyed by internal symbol |
| `tickerMap` | `{ internalSymbol: { ticker, name, resolvedAt } }` | Persisted in `localStorage` |

---

## REST API

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/transactions` | List all transactions |
| `POST` | `/api/transactions` | Add a transaction |
| `PUT` | `/api/transactions/:id` | Update a transaction |
| `DELETE` | `/api/transactions/:id` | Delete a transaction |
| `POST` | `/api/transactions/import` | Bulk replace all transactions |
| `GET` | `/api/ticker-search?q=&currency=` | Resolve stock name to Yahoo ticker |
| `GET` | `/api/price?symbols=A,B` | Live quotes keyed by ticker |
| `GET` | `/` | Serve `index.html` |

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

---

## Excel Upload Format

The app expects a sheet named `clean-table` (falls back to the first sheet):

| Stock Symbol | Stock Name | Quantity | Purchase Price | Purchase Date | Currency |
|---|---|---|---|---|---|
| 50724 | SAP AG-SPONSORE | 5.25 | 174.56 | 2024-03-06 | EUR |

`Currency` is optional and defaults to `EUR`. Valid values: `EUR`, `USD`, `ILS`.

---

## Requirements

- A modern web browser (Chrome, Firefox, Safari, Edge)
- Python 3.6+ (only needed for server / database mode)
- No external Python dependencies — stdlib only (`sqlite3`, `http.server`, `urllib`, `gzip`, `threading`)
