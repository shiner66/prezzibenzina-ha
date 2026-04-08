# Changelog

Tutte le modifiche rilevanti sono documentate in questo file.
Formato: [Keep a Changelog](https://keepachangelog.com/it/1.0.0/) — versioning [semver](https://semver.org/).

---

## [2.0.0] — 2026-04-08

### Added

- **Dati di mercato in tempo reale** iniettati nel prompt AI prima di ogni chiamata:
  - Brent crude, TTF gas naturale, EU ETS carbonio (Yahoo Finance, no API key)
  - EUR/USD spot e Brent convertito in EUR (Frankfurter/BCE, no API key)
  - Ultime 5 notizie su petrolio/OPEC/greggio (Google News RSS in italiano, no API key)
  - Cache 60 minuti nel coordinator; recupero parallelo con `asyncio.gather`
- **Selezione modello AI** nella configurazione e nelle opzioni:
  - Dropdown con tutti i modelli OpenAI (gpt-4.1-mini, gpt-4.1-nano, gpt-4o-mini, gpt-5-mini, gpt-4.1, gpt-4o, gpt-5) e Claude (Haiku, Sonnet)
  - Valore personalizzato abilitato per modelli futuri
  - Default: `gpt-4.1-mini` (gruppo 2,5M token/giorno free)
- **Stima token/giorno** nella UI con tabella comparativa per tutti i gruppi di modelli:
  - Confronto stima vs limite free tier per ogni gruppo
  - Avviso quando si supera il 90% del limite (rischio billing all-or-nothing)
  - Nota esplicita: free tier richiede Data Sharing attivo nelle impostazioni OpenAI
  - Nota: la stima si aggiorna riaprendo le impostazioni (HA non supporta aggiornamenti real-time nei form)

### Changed

- **Algoritmo di previsione completamente riscritto** — da regressione OLS semplice a ensemble adattivo:
  - **EWOLS** (Exponentially Weighted OLS, α=0.15): pesi esponenziali sui dati recenti, più reattivo alle variazioni recenti
  - **Holt Double Exponential Smoothing** (α=0.3, β=0.1): cattura livello e trend, robusto su serie non lineari
  - **Ensemble 60% EWOLS + 40% Holt** quando R² ≥ 0.6 e ≥ 14 punti dati
  - **Solo Holt** quando ≥ 5 punti ma R² insufficiente
  - **WMA fallback** per serie cortissime (< 5 punti)
- **Clamping adattivo alla volatilità**: `[max(0.5×μ, μ−3σ), min(2.0×μ, μ+3σ)]` invece del clamping fisso `[0.5×, 2.0×]`; i bound si restringono automaticamente su serie stabili
- **Prompt AI** arricchito: il blocco dati di mercato precede la serie storica, il modello ha data odierna nel system message

### Fixed

- Il sensore AI non era a conoscenza di eventi successivi al training cutoff del modello; ora i dati di mercato in tempo reale vengono iniettati prima di ogni chiamata

---

## [1.0.9] — 2026-03-30

### Changed

- Bump automatico versione manifest (CI)

---

## [1.0.8] — 2026-03-30

### Added

- Station picker mostra una riga per stazione (senza duplicati per carburante)
- Cleanup sensori orfani alla rimozione di una stazione preferita

### Fixed

- Rimosso parsing legacy `id:fuel_type` dal coordinator

---

## [1.0.7] — 2026-03-29

### Fixed

- `add_suggested_values_to_schema` per pre-popolamento affidabile del form opzioni

---
