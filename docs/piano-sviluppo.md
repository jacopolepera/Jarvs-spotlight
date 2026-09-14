# Piano di sviluppo Jarvs per area

Aggiornato: 14 settembre 2026. Stato: **pianificazione, non esecuzione automatica**.

## Obiettivo e modo di lavorare

Portare Jarvs a un assistente desktop affidabile, essenziale e utile nel lavoro
quotidiano, mantenendo avvio leggero, modelli intercambiabili e controllo sugli
effetti. Si chiude il lavoro già avviato sul branch di testing; ogni sviluppo
successivo parte da un task esplicitamente scelto. La vecchia roadmap rimane una
descrizione architetturale, non una sequenza obbligatoria di fasi da completare.

Per contenere consumo e dispersione: un task attivo per volta, contesto limitato
ai file interessati, niente sottoagenti per default. Usare delega solo per una
verifica indipendente o attività separabili quando ne vale il costo e viene
richiesta. Ogni task consegna un comportamento provabile e un breve resoconto.
Una scoperta fuori perimetro entra nel backlog; non allarga automaticamente il lavoro.

La chiusura richiede test proporzionati al rischio. Eseguire test mirati durante
le modifiche e una suite integrata prima del push; non ripetere verifiche verdi
senza nuove modifiche o un motivo concreto. Una prova con provider reale è separata
dai test deterministici e non deve consumare API o scaricare modelli implicitamente.

## Stato di partenza

Branch di prova: `codex/testing-refresh-2026-09-11`, con modifiche locali pregresse
conservate. Implementati: refresh UI, Stop senza reset, editing mirato con controllo
versione, recovery conservativo e client speech locale/Gemini. Vedere
[guida di testing](testing-refresh.md) per comportamento, prove e limiti.

Non ancora consegnati: UI del recovery, server speech locale installato, MCP nativo,
plugin installabili, browser gestito, gestione dei modelli e distribuzione autonoma.
Lo spike Hermes non è un prerequisito per queste capacità.

Priorità: P0 = chiudere o validare ciò che esiste; P1 = prossimo valore d'uso;
P2 = estensione successiva; P3 = opzione da rivalutare.
Taglie relative: S = intervento circoscritto, M = più componenti, L = integrazione
ampia da suddividere prima dell'esecuzione. Non sono stime di ore o token.

## Ordine consigliato, senza vincoli artificiali

| Ordine | Task | Priorità | Taglia | Dipendenza reale |
|---|---|---|---|---|
| 1 | REL-1: validare il branch nell'uso reale | P0 | S | Chiusura verifiche e push |
| 2 | UI-1: correggere solo difetti osservati | P1 | S/M | Feedback REL-1 |
| 3 | AUD-1: rendere visibile il recupero | P1 | M | API recovery già presenti |
| 4 | VOICE-1: provare una configurazione locale reale | P1 | M | Scelta installazione/server |
| 5 | REL-2: selezionare il candidato per main | P1 | M | REL-1 e decisione su spike |
| 6 | MCP-1: primo server utile nel percorso governato | P2 | L | Contratto e caso d'uso scelti |
| 7 | MODEL-1 oppure PLUG-1 | P2 | L | Bisogno emerso nell'uso |

AUD-1 e VOICE-1 non dipendono da MCP. Il browser non blocca voce o UI. Nessuna
attività GPU complessa va iniziata soltanto perché compare in questa lista.

## Area esperienza desktop

### UI-1 — Validare il minimalismo nel flusso reale

P1 · S/M · Dipende da REL-1. Scopo: correggere attriti osservati senza un altro redesign.

- UI-1.1: raccogliere difetti con azione, risultato atteso, dimensioni finestra e tema.
- UI-1.2: verificare composer compatto/espanso, testo lungo, allegati e impostazioni
  a 100%, 125% e 150% di scaling; correggere clipping e focus persi.
- UI-1.3: verificare Tab, Invio, Shift+Invio, Escape e lettura di messaggi lunghi;
  mantenere focus visibile e nessuno scroll forzato mentre si legge.
- UI-1.4: mostrare stati di invio, arresto ed errore con una sola indicazione utile;
  rimuovere duplicazioni solo se riscontrate durante la prova.

Completamento: i flussi riprodotti passano su Tauri/WebView2, senza cambiare modello,
policy o contenuto della conversazione. Esclusi nuovi temi, animazioni e sidebar completa.

### UI-2 — Gestione delle conversazioni

P2 · M/L · Richiede una decisione su persistenza e privacy.

- UI-2.1: definire se servono chat persistenti, cronologia temporanea o solo ripristino.
- UI-2.2: definire conservazione, cancellazione, allegati scaduti ed esportazione.
- UI-2.3: implementare prima elenco/apertura/nuova chat con un modello dati stabile.
- UI-2.4: aggiungere ricerca soltanto se la quantità di conversazioni lo giustifica.

Completamento: riaprire una chat ripristina il contesto corretto; cancellarla elimina
i dati previsti. Nessuna persistenza implicita di segreti o ragionamento interno.

## Area esecuzione, file e affidabilità

### EXEC-1 — Arresto e ripresa affidabili

P1 · M · Parte dall'endpoint Stop implementato.

- EXEC-1.1: provare arresto durante streaming, attesa approvazione e subprocesso.
- EXEC-1.2: misurare ritardo di rilascio con provider lento o connessione interrotta.
- EXEC-1.3: se necessario correggere la chiusura del trasporto, senza dichiarare
  terminata un'operazione che sta ancora producendo effetti.
- EXEC-1.4: definire cosa entra nel contesto dopo Stop, distinguendo risposta parziale
  da strumenti già eseguiti; aggiungere test solo per le transizioni modificate.

Completamento: nessuna generazione sovrapposta, nessuna approvazione riutilizzata,
chat ancora utilizzabile. Esclusa la promessa di rollback degli effetti già avvenuti.

### FILE-1 — Editing mirato robusto

P1 · S/M · Parte da `workspace.edit_text`.

- FILE-1.1: provare batch di sostituzioni, CRLF, testo Unicode e file modificato
  dall'utente tra lettura e approvazione.
- FILE-1.2: mostrare in approvazione una sintesi delle sostituzioni e del file coinvolto,
  senza aggiungere un secondo percorso di scrittura.
- FILE-1.3: decidere se servono anteprima diff e undo; in tal caso definire prima
  limiti e conservazione delle copie, poi un task distinto.
- FILE-1.4: valutare errori recuperabili affinché il modello rilegga un file in conflitto
  invece di ripetere indefinitamente una richiesta obsoleta.

Completamento: mismatch non modifica il file; nessun aggiramento delle esclusioni
di workspace e script. Il controllo resta ottimistico rispetto agli editor esterni.

## Area audit e recupero

### AUD-1 — Pannello delle operazioni da verificare

P1 · M · API e store sono già disponibili.

- AUD-1.1: aggiungere nelle impostazioni una sezione essenziale alimentata da
  `/run-recovery`, vuota senza notifiche invadenti quando non c'è nulla da verificare.
- AUD-1.2: mostrare strumento, momento dell'interruzione ed esito incerto; non dedurre
  dal crash se un effetto è avvenuto o meno.
- AUD-1.3: offrire conferma applicato/non applicato/verificato tramite endpoint esistente,
  con stato aggiornato dopo risposta positiva del server.
- AUD-1.4: consentire ispezione del run tramite `/runs/{run_id}` senza esporre contenuti
  esclusi dall'audit. Nessun pulsante che riesegua automaticamente operazioni incerte.

Completamento: un crash simulato su fixture appare nella UI e può essere verificato
una sola volta in modo coerente; autenticazione ed errori restano gestiti.

### AUD-2 — Conservazione e dimensione dell'audit

P2 · M · Dopo AUD-1 e una decisione sulla durata di conservazione.

- AUD-2.1: misurare crescita su carico realistico e distinguere run conclusi da incerti.
- AUD-2.2: definire soglie configurabili di età/dimensione e protezione degli esiti incerti.
- AUD-2.3: implementare pulizia transazionale e ispezionabile, con recovery invariato
  se la pulizia viene interrotta.

Completamento: database limitato secondo la policy scelta, nessuna cancellazione
silenziosa di operazioni ancora da verificare. Nessuna pulizia automatica in questa consegna.

## Area voce

### VOICE-1 — Rendere utilizzabile la trascrizione locale sulla macchina

P1 · M · Il client è pronto; installazione del server resta da scegliere.

- VOICE-1.1: inventariare server/modelli già installati senza download; scegliere
  un endpoint compatibile e una dimensione di modello adeguata.
- VOICE-1.2: concordare posizione, spazio disco e modalità di avvio; installare soltanto
  ciò che è stato scelto, senza rendere lo speech obbligatorio all'avvio di Jarvs.
- VOICE-1.3: configurare URL/modello/lingua e provare registrazioni italiane brevi,
  silenzio, rumore e durata al limite.
- VOICE-1.4: rilevare latenza a freddo/caldo, RAM e comportamento con modello chat attivo.
- VOICE-1.5: documentare avvio/arresto e diagnosi di server assente o modello mancante.

Completamento: F4/microfono produce testo reale senza chiavi cloud; errori locali
non provocano fallback. La qualità/latency accettabile va valutata dall'utente.

### VOICE-2 — Sintesi vocale opzionale

P3 · M · Solo dopo una richiesta d'uso concreta.

- VOICE-2.1: verificare voci Windows disponibili e prova minima di qualità italiana.
- VOICE-2.2: aggiungere Riproduci/Stop espliciti, con interruzione immediata e nessun autoplay.
- VOICE-2.3: valutare modelli neurali solo se le voci di sistema non bastano, includendo
  licenze, peso e consumo memoria prima di distribuire componenti.

Completamento: audio interrompibile senza bloccare chat. Wake word, ascolto continuo
e conversazione hands-free restano esclusi e richiedono un piano separato.

## Area MCP e plugin

### MCP-1 — Un'integrazione nativa completa e utile

P2 · L · Scegliere un solo caso d'uso e trasporto iniziale prima di implementare.

- MCP-1.1: definire un server concreto utile, tool consentiti, configurazione e confini
  di fiducia; verificare documentazione ufficiale aggiornata all'avvio del task.
- MCP-1.2: implementare handshake, discovery limitata, namespace, timeout e cancellazione
  per il trasporto scelto. Non implementare entrambi i trasporti per principio.
- MCP-1.3: adattare schema e metadati al registry, negando ciò che non si può validare;
  dati e descrizioni del server rimangono non fidati.
- MCP-1.4: portare le chiamate nello stesso percorso policy → approvazione → executor
  → audit; nessun esecutore parallelo che salti le regole.
- MCP-1.5: attivazione su richiesta, arresto e diagnosi; nessuna discovery su chat Direct.
- MCP-1.6: fake server per errori di schema, collisioni, timeout, crash e cancellazione;
  poi una prova esplicita sul server reale scelto.

Completamento: un task utente usa davvero il server, può essere negato e arrestato,
e disabilitare l'integrazione lascia funzionante la chat. Il secondo trasporto è MCP-2.

### PLUG-1 — Bundle installabili minimi

P2 · L · Dopo MCP-1 se esiste necessità di distribuire più capacità.

- PLUG-1.1: definire manifest/versione, compatibilità, permessi e configurazione segreti.
- PLUG-1.2: implementare ispezione, abilitazione e disabilitazione prima dell'installazione
  automatica; codice eseguibile fuori processo.
- PLUG-1.3: installazione da sorgente esplicita, validazione path/manifest e rimozione pulita.
- PLUG-1.4: aggiornamento con rollback della configurazione e gestione incompatibilità.

Completamento: plugin guasto non rompe avvio o Direct; uninstall rimuove processi e
registrazioni. Marketplace, aggiornamenti automatici e compatibilità universale esclusi.

## Area modelli e risorse

### MODEL-1 — Diagnosi e cambio modello utilizzabili

P2 · M · Indipendente da MCP, utile se il cambio manuale è un attrito reale.

- MODEL-1.1: mostrare endpoint raggiungibile, modello disponibile ed errore utile,
  distinguendo configurazione di chat da speech.
- MODEL-1.2: definire contratto opzionale list/load/unload/health; i provider non gestiti
  continuano a funzionare senza implementarlo.
- MODEL-1.3: scegliere il primo adattatore locale in base al server realmente usato e
  verificare API ufficiali prima dell'implementazione.
- MODEL-1.4: testare cambio riuscito/fallito e ripristino del precedente stato utilizzabile.

Completamento: nessun caricamento nascosto all'avvio; fallimento non perde la conversazione.

### RESOURCE-1 — Coordinare carichi pesanti solo quando necessario

P2 · L · Richiede almeno due workload reali concorrenti e misure VOICE-1/MODEL-1.

- RESOURCE-1.1: registrare RAM/VRAM e durata delle combinazioni realmente usate.
- RESOURCE-1.2: stabilire budget e una lease GPU pesante per il profilo 16 GB/8 GB,
  se questo è l'hardware confermato; definire priorità e cancellazione.
- RESOURCE-1.3: implementare coda limitata, timeout e rilascio garantito dopo errore/OOM.
- RESOURCE-1.4: provare carico prolungato e verificare assenza di processi orfani.

Completamento: il sistema rifiuta o accoda carichi fuori budget con messaggio utile.
Nessuna migrazione di runtime basata soltanto su benchmark teorici.

## Area browser e ricerca

### WEB-1 — Lettura web con fonti, poi interazione

P3 · L · Da attivare quando serve un caso d'uso preciso; preferire MCP già validato.

- WEB-1.1: scegliere lettura di pagine/ricerca come primo risultato e definire fonti,
  limiti di contenuto e trattamento di istruzioni presenti nelle pagine.
- WEB-1.2: restituire URL, titolo e data di recupero, senza confondere testo esterno
  con istruzioni dell'utente.
- WEB-1.3: se serve browser gestito, definire sessione, cookie, acquisizione esplicita
  e presa di controllo dell'utente.
- WEB-1.4: isolare submit, download e altre azioni con effetti nel percorso di approvazione.

Completamento: risultato verificabile con fonti e cancellazione. Login, acquisti,
invii e sorveglianza continua non sono autorizzati da questo piano.

## Area rilascio e manutenzione

### REL-1 — Accettazione del branch attuale

P0 · S · Prossima attività raccomandata dopo questa consegna.

- REL-1.1: seguire le prove in testing-refresh.md sull'app desktop effettiva.
- REL-1.2: registrare soltanto difetti riproducibili, separando bug da preferenze UI.
- REL-1.3: verificare CI del branch e confermare quali novità mantenere.

Completamento: elenco breve di correzioni necessarie o accettazione esplicita del candidato.

### REL-2 — Preparare il merge senza trascinare lo spike

P1 · M · Dopo REL-1. Nessun merge implicito in questa consegna.

- REL-2.1: confrontare main e branch, distinguendo lavoro precedente da questo refresh.
- REL-2.2: decidere se Hermes resta in un ramo sperimentale; preservare i fix generici
  di processo e shutdown quando indipendenti.
- REL-2.3: preparare commit/PR coerenti, descrivendo cambiamento, compatibilità e prove.
- REL-2.4: eseguire CI sul candidato finale e integrare solo dopo prova/decisione utente.

Completamento: main riceve un perimetro leggibile e provato; spike e report locali non
diventano dipendenze accidentali. Nessun reset delle modifiche dell'utente.

### REL-3 — Distribuzione Windows riproducibile

P2 · L · Dopo stabilizzazione del candidato main.

- REL-3.1: provare build da checkout pulito e documentare tutte le dipendenze.
- REL-3.2: scegliere runtime Python incluso o requisito esterno, valutando peso e aggiornamenti.
- REL-3.3: verificare percorsi dati, migrazione e avvio senza cartella sorgente originale.
- REL-3.4: testare pacchetto su ambiente Windows pulito; firma e aggiornamenti sono
  decisioni separate, non prerequisiti per una prima build di prova.

Completamento: installazione e disinstallazione riproducibili, senza chiavi o dati
personali nel pacchetto e senza download di modelli impliciti.

## Decisioni da prendere al momento giusto

Prima di REL-2: quali esperimenti conservare su main. Prima di VOICE-1: server,
spazio e modello locale. Prima di MCP-1: primo caso d'uso utile. Prima di UI-2:
persistenza e conservazione delle chat. Queste decisioni non bloccano il testing
del lavoro esistente e non richiedono una risposta immediata.

Per avviare un task basta indicarne l'ID e il risultato desiderato. Il suo perimetro
viene confermato nel primo aggiornamento operativo; eventuali estensioni restano
pianificate finché non vengono scelte.
