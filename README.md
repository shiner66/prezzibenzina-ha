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
- **Previsione 7 giorni** tramite regressione lineare e media mobile ponderata (zero dipendenze esterne)
- **Indicatori statistici avanzati**: volatilità, momentum, accelerazione del prezzo
- **Analisi AI con contesto geopolitico** (Claude / OpenAI): ad ogni aggiornamento il modello analizza i fattori di mercato globali e stima il rischio di rincari
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

La previsione a 7 giorni usa un algoritmo puramente statistico (nessuna dipendenza esterna):

1. **Raccolta dati**: ultimi 30 giorni di storico locale (aggiornato ad ogni fetch MIMIT)
2. **Pulizia serie**: interpolazione lineare per gap ≤ 3 giorni consecutivi
3. **Regressione OLS**: calcolo su finestra 14 giorni → pendenza + intercetta + R²
4. **Scelta metodo**:
   - R² ≥ 0.6 e ≥ 14 punti → **regressione lineare** (trend chiaro)
   - Altrimenti → **media mobile ponderata** (WMA, peso lineare: più recente = maggiore)
5. **Clamping**: le previsioni vengono limitate a [0.5×, 2.0×] la media degli ultimi 7 giorni
6. **Confidenza**:
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

## Analisi AI con contesto geopolitico

### Panoramica

Quando si configura un provider AI (Claude o OpenAI), ad **ogni aggiornamento** dei prezzi il sensore di previsione chiama il modello LLM con un prompt strutturato che include:

- La serie storica dei prezzi degli ultimi 30 giorni
- Tutti gli indicatori statistici calcolati (trend, volatilità, momentum, accelerazione)
- Il **contesto stagionale automatico** (es. estate = picco domanda benzina, inverno = riscaldamento)
- Una richiesta esplicita di analisi geopolitica e di mercato

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

### Provider AI supportati

| Provider | Modello | Note |
|----------|---------|-------|
| Claude (Anthropic) | `claude-haiku-4-5` | Ottimo per analisi veloci e costi contenuti |
| OpenAI | `gpt-4o-mini` | Alternativa compatibile |

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

- La previsione è puramente statistica sui prezzi storici locali. Non considera variabili macroeconomiche in tempo reale.
- L'analisi AI si basa sulle conoscenze del modello fino alla sua data di training. Per eventi molto recenti la qualità dell'analisi geopolitica può essere limitata.
- I prezzi MIMIT sono aggiornati una volta al giorno (08:00). Variazioni infragiornaliere non sono rilevabili.
- La componente fiscale italiana (accise fisse) riduce l'elasticità del prezzo al pompa rispetto al greggio.

## Crediti e licenza

- Dati: **MIMIT** — Osservatorio Prezzi Carburanti, licenza IODL 2.0
- Codice: MIT License
