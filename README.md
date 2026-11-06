# Skaner (docstruc)

Turn photos, scans and PDFs of receipts, invoices, acts and waybills into **validated structured data**: JSON, Excel, CSV, 1C import files, or a text PDF.

The point is not only reading the document, but **catching the silent mistakes**. Every result is checked automatically (line totals, items vs. subtotal, subtotal + tax vs. total, VAT rate, dates, Kazakh BIN/IIN checksums), and only the fields that look wrong are flagged for a human.

Hosted version: **https://skaner.app** · License: **AGPL-3.0**

## Bring your own key

The engine does not run on a model of its own. You give it your own API key (Anthropic Claude or Google Gemini) and pay your provider directly. Your documents go to your provider under your agreement with it; there is no middleman account.

- **Run it yourself**: free, the key never leaves your machine (see below).
- **Use the hosted site**: paste your own key on the upload page (kept in the server's memory for that upload only, never stored or logged), or pay for pages and skip the key.

## Features

- Input: PDF, JPG, PNG, WEBP, HEIC. English, Russian, Kazakh and other languages.
- Document types: `receipt`, `invoice`, `act`, `waybill`. New types are plug-in modules (`docstruc/doctypes/`).
- Output: JSON, Excel (Documents / Line items / Review sheets, flagged cells highlighted), CSV (opens correctly in Russian-locale Excel), Excel prepared for 1C import, text PDF.
- Several documents on one photo are found automatically; one document across several photos is supported.
- Provider-agnostic model layer (`docstruc/llm/`). Backends: Anthropic Claude, Google Gemini.
- Web app (`web/`, FastAPI): sign-in by email code, batches, history, company accounts, admin. Telegram bot (`bot/`).

## Quick start (command line)

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env               # put your ANTHROPIC_API_KEY or GEMINI_API_KEY into .env

docstruc extract receipts/ --type receipt --backend claude --out receipts.xlsx
docstruc extract invoices/ --type invoice --backend claude --out invoices_1c.xlsx --format 1c
```

## Run the web app

```bash
pip install -e ".[web]"
cp .env.dev.example .env.dev       # demo sign-in: demo@example.com, code 482915
python -m web --dev                # local development, http://127.0.0.1:8000. No server key needed:
                                   # paste your own provider key on the upload page.
```

or with Docker:

```bash
docker build -t skaner .
docker run -p 8000:8000 -e WEB_DEV=1 -e WEB_DEMO_EMAIL=demo@example.com -e WEB_DEMO_CODE=482915 -v skaner-data:/data skaner
```

For a real deployment set `WEB_SECRET`, `WEB_BASE_URL`, a mail key (`RESEND_API_KEY`, `WEB_MAIL_FROM`) and the rest of `.env.example`. Put it behind a reverse proxy over HTTPS and block `/internal/` from the internet.

## How accurate is it?

Honestly: measured on a **small** set. On 8 real Kazakhstan receipts (286 fields, one of them a long receipt photographed in 4 parts) the strongest model matched the hand-made reference on every field, and the checks flagged the 3 receipts where a date was under a fold or a price under a finger. A cheaper model scored 97.6% with 2 silent tax errors. Synthetic acts and waybills and public form samples also came out right. This is **not a benchmark**; run it on your own documents and keep a person on the flagged fields. The model you choose matters (see `docs` of your provider for cost).

## Status and roadmap

Working: extraction, checks, exports, web app, bot. Planned: OpenAI backend, QuickBooks and Xero exports, API keys for programmatic access, pay-per-request endpoint for AI agents (x402).

## License

AGPL-3.0, see `LICENSE`. In short: you may use, study, change and run it, including commercially; if you offer a modified version to others over a network, you must publish your changes under the same license. Contributions are welcome.
