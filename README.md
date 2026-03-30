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
- **Analisi AI opzionale** (Claude / OpenAI) per spiegazioni testuali della tendenza
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

### Attributi del sensore "Previsione"

```yaml
predicted_7d: [1.735, 1.742, 1.748, 1.751, 1.753, 1.755, 1.758]
confidence: "medium"
method: "linear_regression"
trend_direction: "up"
trend_pct_7d: 1.7
ai_analysis: "Il rialzo potrebbe essere legato all'aumento del prezzo del petrolio..."
```

## Servizi HA

### `carburanti_mimit.force_refresh`
Aggiorna immediatamente i prezzi.
```yaml
action: carburanti_mimit.force_refresh
data:
  entry_id: "abc123"  # opzionale
```

### `carburanti_mimit.get_cheapest_near`
Trova i distributori più economici vicino a qualsiasi coordinata (utile nelle automazioni).
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

## Esempio di automazione

```yaml
automation:
  - alias: "Notifica quando la benzina scende sotto 1.70"
    trigger:
      - platform: numeric_state
        entity_id: sensor.carburanti_benzina_prezzo_minimo
        below: 1.70
    action:
      - service: notify.mobile_app
        data:
          title: "Carburante economico!"
          message: >
            Benzina a {{ states('sensor.carburanti_benzina_prezzo_minimo') }} EUR/L
            da {{ state_attr('sensor.carburanti_benzina_prezzo_minimo', 'station_name') }}
            ({{ state_attr('sensor.carburanti_benzina_prezzo_minimo', 'distance_km') }} km)
```

## Previsione prezzi

La previsione a 7 giorni usa un algoritmo puramente statistico (nessuna dipendenza esterna):

1. Raccoglie gli ultimi 30 giorni di storico locale
2. Interpola i gap fino a 3 giorni consecutivi
3. Calcola la regressione lineare OLS sugli ultimi 14 giorni → R²
4. Se R² > 0.6 → usa la regressione lineare per la previsione
5. Altrimenti → media mobile ponderata (pesi lineari, più recente = peso maggiore)
6. Limita le previsioni a [0.5×, 2.0×] della media degli ultimi 7 giorni

Il livello di **confidenza** dipende dalla quantità di dati storici:
- `high`: ≥ 30 giorni e R² ≥ 0.7
- `medium`: ≥ 14 giorni
- `low`: < 14 giorni (sensore mostra `unavailable` sotto 7 giorni)

## Crediti e licenza

- Dati: **MIMIT** — Osservatorio Prezzi Carburanti, licenza IODL 2.0
- Codice: MIT License
