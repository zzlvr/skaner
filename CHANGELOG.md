# Changelog

## 0.1.0

- Extraction of receipts, invoices, acts and waybills from photos and PDFs, several documents per photo, one document across several photos.
- Checks: line totals, items vs. subtotal, totals, VAT rate, dates, Kazakh IIN/BIN and Russian INN checksums.
- Exports: JSON, Excel, CSV, Excel for 1C import, text PDF.
- Backends: Anthropic Claude and Google Gemini; bring your own key.
- Web app without sign-up (guest sessions), English, Russian and Kazakh.
- `--report FILE.html`: an HTML page with every value linked to the place it is printed on the document, plus a second, independent read of the document (PDF text or OCR) that flags values not found in it.
- OpenAI-compatible backend (`--backend openai`): OpenAI or a local model (Ollama, LM Studio, vLLM).
- Duplicate detection within a batch.
- Synthetic Russian/Kazakh/English receipt benchmark (`bench/`).
