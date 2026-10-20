# Sonda dati Seveso

Test di qualche giorno per capire **quali dati sono davvero accessibili a un bot e con quale ritardo**, prima di costruire il sistema di allerta per il Seveso a Niguarda.

Non manda avvisi e non fa previsioni: ogni 10 minuti interroga le fonti e registra cosa risponde e quanto sono "freschi" i dati.

| Fonte | Cosa misura |
|---|---|
| ARPA Lombardia (dati.lombardia.it) | trova idrometri e pluviometri del bacino del Seveso, misura il ritardo di pubblicazione di ciascuno, salva gli ultimi livelli |
| Radar Protezione Civile | ritardo dell'ultimo prodotto SRI (intensità di pioggia) e CUM3 (cumulata 3 ore); una volta all'ora prova a scaricare il file completo |
| Open-Meteo, modello ICON-2I | una volta all'ora, pioggia prevista nelle prossime 24 ore su Niguarda e sul bacino a monte |

## Installazione (dal browser, circa 10 minuti)

1. Su GitHub crea un repository **pubblico** (es. `seveso-probe`). Pubblico perché i minuti di GitHub Actions sono gratuiti e illimitati; dentro ci sono solo open data.
2. Carica i file mantenendo la struttura: `probe.py`, `analyze.py`, `README.md` e `.github/workflows/probe.yml`. Con "Add file → Upload files" puoi trascinare l'intera cartella; se la cartella `.github` non viene caricata (è nascosta su alcuni sistemi), creala a mano con "Add file → Create new file" scrivendo come nome `.github/workflows/probe.yml` e incollando il contenuto.
3. Facoltativo ma consigliato: crea un account gratuito su dati.lombardia.it, genera un *App Token* nelle impostazioni sviluppatore, e aggiungilo nel repository in **Settings → Secrets and variables → Actions → New repository secret** con nome `SOCRATA_APP_TOKEN`. Senza token funziona lo stesso, ma il portale può rallentare le richieste.
4. In **Settings → Actions → General → Workflow permissions** seleziona "Read and write permissions" (serve per salvare i dati).
5. Vai nella scheda **Actions**, apri "Sonda dati Seveso" e premi **Run workflow** per la prima esecuzione manuale (fa subito anche i test orari).

## Cosa guardare dopo la prima esecuzione

Apri l'esecuzione nella scheda Actions e poi lo step "Esegui la sonda": trovi un riepilogo come questo.

```
Scoperta sensori: 23 (errore: nessuno)
ARPA: 23 sensori con dati, ritardo {'min': 18.0, 'median': 31.0, 'max': 75.0} min
Radar SRI: ritardo 9.0 min
...
```

- Se compare **"schema diverso dal previsto"**, i nomi dei campi del portale non sono quelli attesi: il file `data/probe_log.jsonl` contiene un campione dei dati e lo script va adattato.
- Se una fonte dà **HTTP 403 o 429**, quella fonte blocca o limita i client automatici: è esattamente l'informazione che cerchiamo.
- Il file `data/sensors.json` elenca i sensori trovati: controlla che ci siano le stazioni del Seveso (Cantù Asnago, Cesano Maderno, Paderno Dugnano-Palazzolo, Milano via Ornato) e se c'è anche via Valfurva.

## Dopo 3–7 giorni

Il valore vero emerge dopo qualche giorno, meglio se c'è stato almeno un episodio di pioggia. Puoi:

- lanciare `python3 analyze.py` se hai Python sul pc, oppure
- scaricare `data/probe_log.jsonl` e `data/sensors.json` e portarli a Claude per l'analisi.

Le domande a cui risponde: quanto ritardo hanno davvero i livelli ARPA? Il radar arriva sempre? Quanto è puntuale GitHub Actions? Qualche fonte blocca le richieste?

## Note

- Gli orari ARPA sono in **ora solare (UTC+1) tutto l'anno**; lo script ne tiene conto.
- GitHub può ritardare o saltare qualche esecuzione pianificata: anche questo viene misurato.
- Il repository riceve un commit ogni 10 minuti: è normale. Finito il test, puoi disattivare il workflow dalla scheda Actions ("Disable workflow").
- Progetto personale, non un sistema di allerta: per le allerte ufficiali fanno fede Regione Lombardia, ARPA e Protezione civile.
