# Branch di testing: refresh Jarvs

Branch: `codex/testing-refresh-2026-09-11`. Nessun merge su `main`.
Documento aggiornato il 15 ottobre 2026.

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
  Nel turno interrotto vengono conservate le coppie chiamata/risultato degli strumenti
  conclusi; testo parziale e strumenti interrotti non entrano nel contesto successivo.
  Senza strumenti conclusi, il prompt del turno interrotto non viene conservato.
- `workspace.edit_text`: sostituzioni precise in un unico salvataggio, hash del
  file letto e numero di corrispondenze atteso. In caso di conflitto nessuna modifica
  del batch viene applicata. Restano i limiti workspace, policy e script.
  La conferma mostra percorso, versione del file, conteggi e testo prima/dopo.
  Anteprime lunghe dichiarano i limiti: la conferma riguarda comunque l'intera
  operazione. I contenuti del file vengono visualizzati come testo, non eseguiti.
- Recupero conservativo dei run interrotti: nessuna riesecuzione automatica,
  esiti incerti da verificare, lock esclusivo del daemon e API di conferma.
- Trascrizione con server locale OpenAI-compatible oppure Gemini esplicitamente
  selezionato. Nessuna installazione di server/modelli effettuata da questo lavoro.

## Avvio e prove manuali

Nel checkout già usato per lo sviluppo, verificare di essere su questo branch.
L'eseguibile release locale è stato ricompilato con il lotto del 15 ottobre:
si può avviare da `jarvs_launcher.vbs`. Dopo nuovi aggiornamenti del sorgente,
ricompilare con `npm --prefix desktop run build:desktop` prima di usare questo launcher.
Per lo sviluppo resta disponibile `jarvs_dev.vbs`; per un checkout nuovo seguire
Quick start nel README. L'eseguibile locale non è un installer pubblicato.

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

La suite Python integrata è stata eseguita con successo (331 test, uno escluso
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

La nuova anteprima file ha 8 controlli browser dedicati, ripetibili con
`node scripts/file-approval-ui-qa.cjs` con gli stessi prerequisiti. Verificano
conteggi, testo prima/dopo, contenuto HTML letterale, limiti espliciti, layout
compatto e contratto approve/deny. I 9 controlli di regressione UI restano verdi.

Le correzioni UI coprono il focus iniziale sul composer, il focus trattenuto nella
conferma di esecuzione (inizialmente su Deny) e l'indicazione Response stopped
sulla risposta dell'assistente. La QA controlla anche bozza multilinea, scroll
durante lo streaming e dimensioni compatte; non sostituisce la prova nativa.

## Stop e confine tra contesto locale e cloud

Le prove automatiche includono Stop durante la sintesi dopo una scrittura riuscita,
seconda approvazione di un batch, un processo Python effettivo e uno stream HTTP
locale bloccato. Il run interrotto termina come cancelled; le operazioni già
concluse mantengono il proprio esito. Un'approvazione interrotta non è riutilizzabile.

Con invio workspace al cloud negato, il risultato può essere mostrato localmente
ma la cronologia salva solo una notifica neutra. Se la conversazione contiene
risultati workspace precedenti o risposte da essi derivate, tutto il contesto storico
viene escluso dalle richieste cloud: la chat visibile resta disponibile e il modello
locale può ancora usarlo. Reset azzera questo vincolo; autorizzare l'invio workspace
al cloud permette nuovamente l'uso del contesto. Il comportamento conservativo evita
di dedurre dal solo testo di una risposta se contiene dati locali.

## Controllo del composer e della finestra nativa

La bozza iniziale mantiene il layout compatto: il testo su più righe aumenta solo
l'altezza necessaria al composer (fino al limite del campo), senza aprire la vista
conversazione. Svuotando il campo si torna all'altezza compatta. Le richieste native
di ridimensionamento sono ordinate e quelle superate vengono ignorate.

La QA browser include ora un simulatore del comando nativo con completamenti
ritardati: verifica la dimensione effettivamente richiesta, non solo l'attributo
CSS del layout. Copre a capo automatico sotto 160 caratteri, restringimento manuale,
svuotamento e apertura/chiusura rapida delle impostazioni.

Per le modifiche al layout, prima della consegna verificare anche la build Windows
reale con screenshot e strumenti di accessibilità: chat vuota, testo lungo senza
invio, Shift+Invio, cancellazione della bozza, ritorno dalle impostazioni. Conservare
eventuali bozze dell'utente e non inviare messaggi di prova a un modello. Le prove
browser e la build riuscita da sole non dimostrano che la finestra nativa si adatti.

Verifica Windows eseguita sulla release corretta: bozza di 139 caratteri senza invio
su quattro righe, finestra 602×170 nello screenshot (bordi inclusi), testo e pulsante
visibili; svuotamento riportato a 602×122; apertura e chiusura delle impostazioni
riportata a 602×122. Nessun messaggio è stato inviato durante queste prove. Superati
12 controlli browser, inclusi i comandi nativi simulati, e 11 test Rust.

## Zoom da tastiera

Ctrl+più (anche Ctrl+= o Ctrl+Shift+=) ingrandisce l'interfaccia di 10 punti
percentuali; Ctrl+meno la riduce. Ctrl+0 ripristina il 100%. Sono supportati anche
più/meno del tastierino numerico. I limiti restano 60–160% e Ctrl+rotella conserva
il passo del 5%. Le scorciatoie non inviano né modificano la bozza.

Sviluppo TDD: prima dell'implementazione i due nuovi controlli fallivano perché
lo zoom restava al 100%. Dopo l'implementazione passano 14 controlli UI totali,
inclusi conservazione della bozza e contenimento del composer nella finestra
ridimensionata durante lo zoom. Il fattore è condiviso con Ctrl+rotella.

Verifica nativa del 16 ottobre: Ctrl+ (tasto principale e tastierino) ha ingrandito
la bozza mantenendola intera, con finestra adattata da 602×146 a 602×157 nello
screenshot. Ctrl− e Ctrl+0 hanno ripristinato 602×146; svuotando la sola bozza
di prova la finestra è tornata a 602×122. Nessun messaggio è stato inviato.
