# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Opis projektu

CLI do przyrostowego pobierania faktur z KSeF (Krajowy System e-Faktur) — polskiego systemu faktur elektronicznych. Python 3.8+, licencja GPLv3.

## Uruchamianie

```bash
pip install -r requirements.txt
cp .env.example .env  # uzupełnij KSEF_TOKEN i CONTEXT_NIP
python fetch_invoices.py              # JSON output
python fetch_invoices.py --format text  # tekstowy output
```

## Testy i linting

Brak infrastruktury testowej i lintingu — nie ma pytest, flake8 ani żadnych narzędzi CI.

## Architektura

Trzy moduły, prosta hierarchia zależności:

```
fetch_invoices.py → client.py → crypto.py
```

- **`fetch_invoices.py`** — orkiestrator (`InvoiceFetcher`). Pobiera faktury przyrostowo dla trzech typów podmiotów (Subject1/2/3) z deduplikacją. Persystuje stan w `.ksef_state.json` (mechanizm High Water Mark). Zapisuje XML-e do `faktury/`.
- **`client.py`** — klient REST API KSeF (`KSeFClient`). Wieloetapowe uwierzytelnianie (challenge → RSA-encrypted token → polling → redeem). Eksport faktur jako zaszyfrowane paczki ZIP (AES-256-CBC) pobierane z Azure Storage (signed URLs).
- **`crypto.py`** — moduł kryptograficzny (`Crypto`). Pobiera certyfikaty publiczne z API KSeF przy inicjalizacji. RSA-OAEP (SHA-256) do szyfrowania tokenów i kluczy symetrycznych. AES-256-CBC do deszyfrowania paczek z fakturami.

## Kluczowe wzorce

- **Przyrostowe pobieranie (HWM)** — stan per subject type w `.ksef_state.json`, kontynuacja od ostatniego `permanentStorageHwmDate`
- **Polling** — operacje asynchroniczne (auth, export) odpytywane w pętli z opóźnieniami (2s auth, 5s export)
- **Szyfrowanie dwupoziomowe** — klucz symetryczny AES szyfrowany RSA, faktury szyfrowane AES-256-CBC
- **Deduplikacja** — in-memory set + sprawdzenie pliku na dysku

## Konfiguracja

Zmienne w `.env`: `KSEF_TOKEN` (token uwierzytelniający), `CONTEXT_NIP` (NIP podatnika). API wskazuje na produkcję (`https://api.ksef.mf.gov.pl/v2`).
