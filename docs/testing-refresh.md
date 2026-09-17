# Branch di testing: refresh Jarvs

Branch: `codex/testing-refresh-2026-09-11`. Nessun merge su `main`.
Documento aggiornato il 17 settembre 2026.

## Cosa contiene

Il branch conserva anche il lavoro locale presente prima di questo intervento:
spike Hermes, gestione del processo daemon, configurazione e documentazione.
Hermes resta sperimentale e disabilitato per impostazione predefinita. Il branch
non rappresenta ancora la proposta finale da integrare integralmente in main.

Le novità di questo intervento sono:

- UI sobria, composer multilinea con Invio/Stop, impostazioni suddivise e controlli
  sperimentali meno esposti; correzioni alla selezione testo e allo scroll.
- Stop della generazione identificata, senza azzerare la sessione. Il pulsante
  attende il termine effettivo: un provider bloccato può richiedere tempo per chiudersi.
- `workspace.edit_text`: sostituzioni precise in un unico salvataggio, hash del
  file letto e numero di corrispondenze atteso. In caso di conflitto nessuna modifica
  del batch viene applicata. Restano i limiti workspace, policy e script.
- Recupero conservativo dei run interrotti: nessuna riesecuzione automatica,
  esiti incerti da verificare, lock esclusivo del daemon e API di conferma.
- Trascrizione con server locale OpenAI-compatible oppure Gemini esplicitamente
  selezionato. Nessuna installazione di server/modelli effettuata da questo lavoro.

## Avvio e prove manuali

Nel checkout già usato per lo sviluppo, verificare di essere su questo branch,
poi avviare `jarvs_dev.vbs`; in alternativa seguire Quick start nel README.
Non usare il launcher release per verificare la nuova UI finché l'eseguibile
release non è stato ricompilato: potrebbe aprire la versione precedente.

1. Aprire Jarvs con la scorciatoia e verificare composer compatto, testo su più
   righe, allegati, leggibilità e impostazioni alle dimensioni abituali di Windows.
2. Inviare una domanda lunga, premere Stop e continuare nella stessa chat.
   Verificare che il testo parziale resti leggibile e che non partano due risposte
   contemporaneamente. Ripetere durante un'approvazione in attesa.
3. Su un file di prova nel workspace chiedere una modifica precisa. Verificare
   che il tool legga il file prima di modificarlo e rifiuti una versione obsoleta.
   Una modifica concorrente immediatamente dopo l'ultimo controllo resta un limite
   del controllo ottimistico: non è un lock globale su editor esterni.
4. In impostazioni voce scegliere locale, URL del server, nome effettivo del
   modello installato e lingua facoltativa (`it` oppure vuota per rilevamento).
   Il default è `http://127.0.0.1:8000/v1` con `Systran/faster-whisper-small`.
   Il server deve offrire `/audio/transcriptions`; LM Studio configurato solo per
   chat non soddisfa automaticamente questo requisito.
5. Se si sceglie Gemini, verificare che esista una chiave configurata. Una voce
   locale non disponibile deve produrre un errore, senza inviare audio al cloud.

Le API `/run-recovery` e `/run-recovery/{run_id}/{invocation_id}/acknowledge`
sono protette dalla stessa autenticazione del daemon. La conferma è una verifica
umana, non una nuova esecuzione. Il pannello è in Settings → Recovery: carica le
operazioni quando viene aperto, offre Inspect run e registra la scelta solo dopo
conferma positiva del daemon. Refresh rilegge le operazioni ancora da verificare;
nessun controllo periodico viene avviato durante la chat. Un conflitto ricarica
la lista e richiede di verificarne lo stato aggiornato.
Non simulare crash durante operazioni su dati importanti; i test automatici usano
processi e file temporanei isolati.

## Verifiche e limiti

La suite Python integrata è stata eseguita con successo (319 test, uno escluso
per configurazione); il build frontend TypeScript/Vite e i 10 test Rust sono
riusciti. Il controllo visivo su Edge con dati simulati ha verificato home e
impostazioni, senza errori JavaScript o overflow orizzontale. I test
speech includono un server HTTP locale deterministico, non un modello reale.
L'esito finale delle verifiche e del push è riportato nel messaggio di consegna.

La validazione manuale di WebView2, microfono, scorciatoie globali e modello locale
reale resta necessaria sulla macchina d'uso. Il ramo include codice sperimentale
precedente: la selezione dei commit per main è un task distinto, REL-2 nel piano.

## Verifica del pannello Recovery

È stata eseguita una prova browser con daemon autentico e database temporaneo:
nessuna richiesta recovery prima dell'apertura del pannello, run interrotto visibile,
dettagli consultabili, una sola conferma persistita, lista aggiornata e nessuna nuova
esecuzione dello strumento. Il run resta failed anche dopo la verifica umana.
Non sono stati usati dati personali o richieste a modelli durante questa prova.

La QA browser automatizzata è ripetibile con `node scripts/ui-release-qa.cjs`
e `node scripts/recovery-ui-qa.cjs`, dopo `npm --prefix desktop run build`.
Richiede Playwright disponibile all'ambiente Node ed Edge installato. Se Playwright
si trova altrove, impostare `JARVS_PLAYWRIGHT_PATH` alla directory del pacchetto;
`JARVS_BROWSER_CHANNEL` permette di scegliere un altro canale compatibile installato.
Gli script servono il build su una porta locale temporanea e usano dati simulati.

Le correzioni UI coprono il focus iniziale sul composer, il focus trattenuto nella
conferma di esecuzione (inizialmente su Deny) e l'indicazione Response stopped
sulla risposta dell'assistente. La QA controlla anche bozza multilinea, scroll
durante lo streaming e dimensioni compatte; non sostituisce la prova nativa.
