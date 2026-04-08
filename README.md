# Carburanti MIMIT — Home Assistant Integration

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![HA Version](https://img.shields.io/badge/Home%20Assistant-2024.1+-blue.svg)](https://www.home-assistant.io/)
[![License: IODL 2.0](https://img.shields.io/badge/Data%20License-IODL%202.0-green.svg)](https://www.dati.gov.it/iodl/2.0/)

Integrazione Home Assistant per i prezzi dei carburanti italiani, basata sui dati ufficiali del **MIMIT (Ministero delle Imprese e del Made in Italy)**.

## Caratteristiche

- **Prezzi in tempo reale** dei distributori entro un raggio configurabile
- **Classifica dei più economici** con dettagli (nome, indirizzo, distanza, self/servito)
- **Storico 90 giorni** con grafici nativi in Lovelace (Long-Term Statistics)
- **Variazione prezzi**: settimanale, mensile, 7 giorni
- **Previsione 7 giorni** con algoritmo ensemble (EWOLS + Holt Double Exponential Smoothing) e clamping adattivo alla volatilità
- **Indicatori statistici avanzati**: volatilità, momentum, accelerazione del prezzo
- **Analisi AI con dati di mercato in tempo reale** (Claude / OpenAI): prima di ogni chiamata AI vengono iniettati i prezzi live di Brent, TTF gas, ETS carbonio, EUR/USD e le ultime notizie da Google News — il modello risponde sempre con contesto aggiornato
- **Scelta del modello AI** nella configurazione, con stima del consumo token/giorno per tutti i modelli e confronto con i limiti del free tier OpenAI
- **Servizi HA** per aggiornamenti forzati e ricerche ad-hoc da automazioni

## Fonte dati

I dati provengono dall'**Osservatorio Prezzi Carburanti MIMIT**:
- Aggiornamento quotidiano alle 08:00 (ora italiana)
- Licenza **IODL 2.0** (Open Data ufficiale italiano)
- Nessuna autenticazione richiesta

## Installazione tramite HACS

1. In HACS → Integrations → Menu (3 punti) → **Custom repositories**
2. Aggiungi: `https://github.com/shiner66/prezzibenzina-ha` → Category: **Integration**
3. Cerca "Carburanti MIMIT" e installa
4. Riavvia Home Assistant
5. Vai in **Impostazioni → Dispositivi e servizi → Aggiungi integrazione** → cerca "Carburanti MIMIT"

## Installazione manuale

```bash
cp -r custom_components/carburanti_mimit /config/custom_components/
```
Riavvia Home Assistant.

## Configurazione

La configurazione avviene tramite UI in 3 step:

### Step 1 — Posizione
| Campo | Default | Descrizione |
|-------|---------|-------------|
| Latitudine | Coordinate home HA | Punto di riferimento |
| Longitudine | Coordinate home HA | Punto di riferimento |
| Raggio (km) | 10 | Raggio di ricerca (1–100 km) |
| Nome | Casa | Etichetta per questa posizione |

### Step 2 — Tipi carburante
- Selezione multipla tra: **Benzina, Gasolio, GPL, Metano, HVO, Gasolio Riscaldamento**
- Toggle self-service / servito

### Step 3 — Avanzate
| Campo | Default | Descrizione |
|-------|---------|-------------|
| Distributori in classifica | 5 | Quanti distributori mostrare negli attributi |
| Intervallo aggiornamento | 24 h | Frequenza di polling |
| Provider AI | Nessuno | Claude (Anthropic) o OpenAI |
| Chiave API AI | — | Necessaria solo se si usa AI |
| Modello AI | gpt-4.1-mini | Modello da usare per l'analisi (con stima token/giorno e confronto limiti free tier) |

La schermata mostra automaticamente la stima di token/giorno per tutti i gruppi di modelli in base all'intervallo e ai carburanti scelti, con avviso se si rischia di superare il limite free tier OpenAI. La stima si aggiorna riaprendo le impostazioni.

> **Nota free tier OpenAI**: il limite gratuito (2,5M o 250K token/giorno a seconda del modello) è attivo **solo con il Data Sharing abilitato** nelle impostazioni dell'account OpenAI. Il billing è **all-or-nothing**: se una singola richiesta supera il limite residuo, l'intera richiesta viene fatturata.

## Sensori creati

Per ogni tipo di carburante selezionato vengono creati **4 sensori**:

| Sensore | Stato | Unità |
|---------|-------|-------|
| `sensor.carburanti_<fuel>_prezzo_minimo` | Prezzo più basso in area | EUR/L |
| `sensor.carburanti_<fuel>_prezzo_medio` | Media prezzi in area | EUR/L |
| `sensor.carburanti_<fuel>_tendenza` | `up` / `down` / `stable` | — |
| `sensor.carburanti_<fuel>_previsione` | Prezzo previsto domani | EUR/L |

### Attributi del sensore "Prezzo Minimo"

```yaml
station_name: "ENI Via Roma 12"
address: "VIA ROMA 12, 20100"
comune: "MILANO"
provincia: "MI"
distance_km: 2.34
is_self_service: true
bandiera: "Agip Eni"
reported_at: "2026-03-30T08:00:20"
self_service_cheapest: 1.728
full_service_cheapest: 1.989
stations_in_radius: 47
top_stations:
  - name: "ENI Via Roma 12"
    price: 1.728
    distance_km: 2.34
    is_self_service: true
    ...
```

### Attributi del sensore "Tendenza"

```yaml
weekly_change_pct: +1.2       # % rispetto a 7 giorni fa
monthly_change_pct: -0.8      # % rispetto a 30 giorni fa
trend_pct_7d: +1.7            # % variazione prevista nei prossimi 7 giorni
price_volatility: 0.00821     # coefficiente di variazione (σ/μ) — più alto = più instabile
price_momentum: +0.45         # % media 7gg vs settimana precedente — indica accelerazione
price_acceleration: 0.000012  # EUR/giorno² — seconda derivata della tendenza
```

### Attributi del sensore "Previsione"

```yaml
predicted_7d: [1.735, 1.742, 1.748, 1.751, 1.753, 1.755, 1.758]
confidence: "medium"          # high / medium / low
method: "linear_regression"   # linear_regression / moving_average
trend_direction: "up"
trend_pct_7d: +1.7
price_volatility: 0.00821
price_momentum: +0.45
price_acceleration: 0.000012
ai_analysis: "Il Brent è sotto pressione rialzista a causa delle tensioni nel
  Mar Rosso che allungano le rotte di approvvigionamento europee. L'OPEC+ ha
  confermato tagli alla produzione fino a fine trimestre. Il cambio EUR/USD
  stabile limita l'effetto valutario, ma la componente di accise italiane
  (≈65% del prezzo) attenua parzialmente le oscillazioni del greggio.
  Il rischio di ulteriori rincari nelle prossime due settimane è moderato.
  [RISCHIO:medio]"
ai_risk_level: "medio"        # basso / medio / alto — estratto dalla risposta AI
```

## Previsione prezzi — Logica statistica

La previsione a 7 giorni usa un algoritmo ensemble puramente statistico (nessuna dipendenza esterna):

1. **Raccolta dati**: ultimi 30 giorni di storico locale (aggiornato ad ogni fetch MIMIT)
2. **Pulizia serie**: interpolazione lineare per gap ≤ 3 giorni consecutivi
3. **EWOLS** (Exponentially Weighted OLS, α=0.15): regressione con peso esponenziale sui dati recenti → pendenza + intercetta + R²
4. **Holt Double Exponential Smoothing** (α=0.3, β=0.1): cattura sia livello che trend, robusta su serie non lineari
5. **Selezione metodo**:
   - R² ≥ 0.6 e ≥ 14 punti → **ensemble 60% EWOLS + 40% Holt** (trend chiaro e dati sufficienti)
   - ≥ 5 punti → **solo Holt** (trend incerto ma abbastanza dati)
   - Altrimenti → **media mobile ponderata** (WMA fallback)
6. **Clamping adattivo alla volatilità**: `[max(0.5×μ, μ−3σ), min(2.0×μ, μ+3σ)]` — i bound si restringono automaticamente su serie stabili e si allargano su serie volatili
7. **Confidenza**:
   - `high`: ≥ 30 giorni di storico e R² ≥ 0.7
   - `medium`: ≥ 14 giorni
   - `low`: < 14 giorni (sensore `unavailable` sotto 7 giorni)

### Indicatori statistici aggiuntivi

| Indicatore | Formula | Interpretazione |
|------------|---------|-----------------|
| `price_volatility` | σ/μ degli ultimi 14 prezzi | > 0.01 = prezzi instabili |
| `price_momentum` | (media7gg − media7gg_prec) / media7gg_prec × 100 | Positivo = accelerazione rialzista |
| `price_acceleration` | pendenza_seconda_metà − pendenza_prima_metà | Positivo = tendenza si sta irripidendo |

---

## Analisi AI con contesto geopolitico e dati di mercato in tempo reale

### Panoramica

Quando si configura un provider AI (Claude o OpenAI), ad **ogni aggiornamento** dei prezzi il sensore di previsione chiama il modello LLM con un prompt strutturato che include:

- La serie storica dei prezzi degli ultimi 30 giorni
- Tutti gli indicatori statistici calcolati (trend, volatilità, momentum, accelerazione)
- **Dati di mercato in tempo reale** recuperati in parallelo prima di ogni chiamata AI (senza API key)
- Il **contesto stagionale automatico** (es. estate = picco domanda benzina, inverno = riscaldamento)
- Una richiesta esplicita di analisi geopolitica e di mercato

### Dati di mercato iniettati in tempo reale

| Fonte | Dati | API key |
|-------|------|---------|
| Yahoo Finance | Brent crude (BZ=F): prezzo corrente e variazione % | No |
| Yahoo Finance | TTF gas naturale (TTF=F): prezzo e variazione % | No |
| Yahoo Finance | EU ETS carbonio (EUAU.DE): prezzo e variazione % | No |
| Frankfurter (BCE) | EUR/USD spot + Brent convertito in EUR | No |
| Google News RSS | Ultime 5 notizie su petrolio/OPEC/greggio (hl=it) | No |

I dati vengono recuperati in parallelo con `asyncio.gather` e cachati per 60 minuti nel coordinator. Il fallimento di una singola fonte non blocca le altre. Grazie a questi dati il modello AI può rispondere a eventi recenti (es. cessate il fuoco, decisioni OPEC, shock di cambio) anche se avvenuti dopo la sua data di training.

### Cosa analizza l'AI

Il modello viene istruito a considerare i seguenti fattori per ogni analisi:

| Fattore | Dettaglio |
|---------|-----------|
| **Petrolio Brent/WTI** | Livello e direzione del prezzo del greggio di riferimento europeo |
| **Decisioni OPEC+** | Tagli o aumenti produzione che impattano l'offerta globale |
| **Tensioni geopolitiche** | Conflitti o instabilità che coinvolgono paesi produttori o rotte di transito (Russia/Ucraina, Medio Oriente, Nord Africa, stretto di Hormuz, Mar Rosso) |
| **Cambio EUR/USD** | Un euro debole aumenta il costo d'importazione del greggio (quotato in dollari) |
| **Fiscalità italiana** | Accise (fisse, ~0.73 €/L benzina) + IVA 22% = circa il 65% del prezzo finale; attenuano ma non eliminano l'impatto del greggio |
| **Stagionalità** | Estate: picco domanda benzina. Inverno: picco gasolio riscaldamento. Primavera: manutenzione raffinerie |
| **Raffinerie europee** | Manutenzione stagionale riduce temporaneamente l'offerta di prodotti raffinati |

### Output dell'AI

La risposta del modello contiene:

1. **Analisi testuale** (3–5 frasi in italiano) che spiega i fattori in gioco
2. **Tag di rischio** strutturato, estratto automaticamente:
   - `[RISCHIO:basso]` → mercato stabile, nessun fattore di pressione rilevante
   - `[RISCHIO:medio]` → alcuni segnali di tensione, possibili variazioni moderate
   - `[RISCHIO:alto]` → forti pressioni rialziste, rincari probabili a breve

Il tag viene estratto con regex e salvato nell'attributo `ai_risk_level` separatamente dal testo completo (`ai_analysis`), così puoi usarlo direttamente in automazioni:

```yaml
# Esempio: notifica se il rischio AI è alto
automation:
  - alias: "Allerta rischio rincaro carburante"
    trigger:
      - platform: template
        value_template: >
          {{ state_attr('sensor.carburanti_benzina_previsione', 'ai_risk_level') == 'alto' }}
    action:
      - service: notify.mobile_app
        data:
          title: "⚠️ Rischio rincaro benzina"
          message: >
            {{ state_attr('sensor.carburanti_benzina_previsione', 'ai_analysis') }}
```

### Frequenza di aggiornamento AI

L'analisi AI viene ricalcolata **ad ogni aggiornamento del coordinator** (default 24h), ma solo se:
- È configurato un provider AI con chiave valida
- Sono disponibili almeno 7 giorni di storico

Le chiamate AI sono **fire-and-forget**: HA non aspetta la risposta per aggiornare gli altri sensori. Se la chiamata fallisce (rete, rate limit, ecc.) viene loggato un messaggio di debug e il valore precedente viene mantenuto.

### Provider AI supportati e selezione modello

| Provider | Modelli supportati | Free tier |
|----------|--------------------|-----------|
| OpenAI | gpt-4.1-mini *(default)*, gpt-4.1-nano, gpt-4o-mini, gpt-5-mini | 2.500.000 tok/giorno |
| OpenAI | gpt-4.1, gpt-4o, gpt-5 | 250.000 tok/giorno |
| Claude (Anthropic) | claude-haiku-4-5, claude-sonnet-4-6 | Nessun free tier |

Il modello si seleziona nella configurazione/opzioni. La UI mostra la stima di token/giorno per tutti i gruppi in base alle impostazioni salvate (intervallo × numero carburanti × ~3.500 token/chiamata).

> **Free tier OpenAI**: richiede il Data Sharing attivo nelle impostazioni account. Il billing è **all-or-nothing** per richiesta: se il saldo residuo non copre l'intera chiamata, viene fatturata per intero.

Imposta la chiave API nelle opzioni dell'integrazione (non viene mai inviata al server MIMIT).

---

## Servizi HA

### `carburanti_mimit.force_refresh`
Aggiorna immediatamente i prezzi.
```yaml
action: carburanti_mimit.force_refresh
data:
  entry_id: "abc123"  # opzionale
```

### `carburanti_mimit.get_cheapest_near`
Trova i distributori più economici vicino a qualsiasi coordinata.
```yaml
action: carburanti_mimit.get_cheapest_near
data:
  latitude: 45.4642
  longitude: 9.1900
  fuel_type: Benzina
  radius_km: 5
  top_n: 3
response_variable: result
```
Risposta:
```json
{
  "fuel_type": "Benzina",
  "results": [{"name": "...", "price": 1.729, "distance_km": 0.8, ...}],
  "data_age_seconds": 3600
}
```

### `carburanti_mimit.clear_history`
Cancella lo storico locale.
```yaml
action: carburanti_mimit.clear_history
data:
  entry_id: "abc123"
  fuel_type: Benzina  # opzionale
```

## Grafici Lovelace

L'integrazione inietta dati nelle **Long-Term Statistics** di HA. Puoi usare la **Statistics Card** con:
- `statistic_id: carburanti_mimit:<entry_prefix>_benzina_cheapest`
- `statistic_id: carburanti_mimit:<entry_prefix>_benzina_average`

(L'`entry_prefix` è visibile nei log di avvio.)

## Esempio di automazione completa

```yaml
automation:
  - alias: "Report mattutino carburante"
    trigger:
      - platform: time
        at: "08:30:00"
    action:
      - service: notify.mobile_app
        data:
          title: "Carburante oggi"
          message: >
            Benzina: {{ states('sensor.carburanti_benzina_prezzo_minimo') }} EUR/L
            da {{ state_attr('sensor.carburanti_benzina_prezzo_minimo', 'station_name') }}
            ({{ state_attr('sensor.carburanti_benzina_prezzo_minimo', 'distance_km') }} km)

            Tendenza 7gg: {{ state_attr('sensor.carburanti_benzina_previsione', 'trend_pct_7d') }}%
            Rischio AI: {{ state_attr('sensor.carburanti_benzina_previsione', 'ai_risk_level') | upper }}

            {{ state_attr('sensor.carburanti_benzina_previsione', 'ai_analysis') }}
```

## Limitazioni e avvertenze

- La previsione statistica si basa sui prezzi storici locali; il modello ensemble migliora con almeno 14 giorni di dati.
- I dati di mercato in tempo reale (Brent, TTF, ETS, EUR/USD, notizie) vengono iniettati nell'AI ma la qualità dell'analisi dipende comunque dalla capacità del modello di ragionare su di essi.
- I prezzi MIMIT sono aggiornati una volta al giorno (08:00). Variazioni infragiornaliere non sono rilevabili.
- La componente fiscale italiana (accise fisse) riduce l'elasticità del prezzo al pompa rispetto al greggio.
- Il free tier OpenAI richiede il Data Sharing attivo; senza di esso ogni chiamata è fatturata normalmente.

## Crediti e licenza

- Dati: **MIMIT** — Osservatorio Prezzi Carburanti, licenza IODL 2.0
- Codice: MIT License
