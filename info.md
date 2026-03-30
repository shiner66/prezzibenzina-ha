## Carburanti MIMIT

Integrazione Home Assistant per i **prezzi dei carburanti italiani** basata sui dati ufficiali del MIMIT (Osservatorio Prezzi Carburanti).

**Funzionalità:**
- Prezzi in tempo reale entro un raggio configurabile (1–100 km)
- Classifica dei distributori più economici con distanza, indirizzo e tipo (self/servito)
- Storico 90 giorni con grafici nativi in Lovelace (Long-Term Statistics)
- Variazione prezzi settimanale e mensile
- Previsione a 7 giorni (regressione lineare + media mobile ponderata)
- Analisi AI opzionale via Claude o OpenAI

**Tipi carburante:** Benzina · Gasolio · GPL · Metano · HVO · Gasolio Riscaldamento

**Fonte dati:** MIMIT open data — aggiornamento quotidiano alle 08:00, licenza IODL 2.0, nessuna autenticazione richiesta.
