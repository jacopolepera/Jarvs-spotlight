# QA reale LM Studio — 16 ottobre 2026

## Ambiente e metodo

Release Windows di Jarvs sul branch `codex/testing-refresh-2026-09-11`.
LM Studio 0.4.26 build 4, server locale `127.0.0.1:1234`, modello caricato
`qwen3.5-4b`: `lmstudio-community/Qwen3.5-4B-GGUF`, Q4_K_M, 3,38 GB.
La UI dichiara supporto a reasoning, tool use e vision; vision non provata.

Prompt inseriti e inviati nell'app nativa con Computer Use, osservando screenshot,
accessibilità e pannello Activity. L'audit locale è stato letto come controllo
aggiuntivo di routing, tempi e tool realmente conclusi. Nessun prompt di questa
campagna è stato inviato direttamente alle API bypassando Jarvs.

Sessioni azzerate con New chat fra gruppi di prove. Dati sintetici, nessun comando
Python/PowerShell eseguito dal modello. Le azioni riguardano esclusivamente
`qa_model_20261016/prova.txt` nel workspace configurato. Policy di esecuzione
originale `auto_run`, egress cloud `deny`, entrambe conservate.

Thinking inizialmente attivo, Reasoning Budget unrestricted in LM Studio.
Temporaneamente disattivato dalla UI per i run 20–25, poi ripristinato con
Discard unsaved. Nessun preset salvato. Le misure sono singoli campioni di durata
modello, non benchmark di throughput o tempo al primo token; cache e contesto
variano e il primo prompt include la cache fredda.

## Risultati prima delle correzioni

Gli ordinali sono quelli del database audit locale; non sono identificatori
stabili per altre installazioni.

| Run | Prova | Thinking | Osservazione |
| --- | --- | --- | --- |
| 15 | «Rispondi soltanto con: PRONTO.» | On | `PRONTO`, nessun tool, 17,63 s; 242 caratteri nel canale reasoning. |
| 16 | Penna + quaderno 2,20 €, differenza 2 € | On | Corretto: 0,10 €, con verifica; nessun tool, 20,56 s. |
| 17 | Tradurre un comando citato senza eseguirlo | On | Nessun tool, 10,14 s. Traduce anche `NON_CREARE.txt` in `NON_CREATE.txt`: routing corretto, fedeltà agli identificatori imperfetta. |
| 18 | Creare il file con `ALFA 17` | On | Profilo automate e `workspace.write_text` corretti; rifiuto `workspace_unavailable`: cartella configurata assente. Audit erroneamente ancora running. |
| 19 | Ripetere dopo il ripristino della cartella | On | Scrittura reale riuscita, contenuto verificato su disco. 72,06 s cumulativi di modello, 5 chiamate: due risposte reasoning-only dopo il tool, poi risposta finale senza catalogo. |
| 20 | Leggi, sostituisci ALFA con BETA, rileggi | Off | Profilo inspect errato per l'intero compito. Sette letture identiche, nessuna modifica; fallisce per `tool_iteration_limit`, 64,61 s di modello. File ancora `ALFA 17`. |
| 21 | Stesso prompt PRONTO in nuova chat | Off | `PRONTO.`: 3,41 s, zero reasoning e zero tool. Aggiunge un punto, quindi non identico carattere per carattere all'atteso. |
| 22 | Modifica isolata ALFA → BETA | Off | Profilo automate, lettura + `workspace.edit_text`; file realmente `BETA 17`. 32,22 s, 4 chiamate modello, nessun recupero reasoning-only. La risposta ripete anche PRONTO dalla richiesta precedente. |
| 23 | Codice GHIANDA-42 solo per questa chat | Off | Risponde `OK`, nessun tool, 2,95 s. |
| 24 | Richiamare il codice nella stessa chat | Off | `GHIANDA-42`, nessun tool, 3,64 s. |
| 25 | Richiamare il codice dopo New chat | Off | `NON PRESENTE`, nessun tool, 2,87 s: isolamento della sessione verificato. |
| 26 | Elenco 1–500, Stop durante thinking | On | UI torna disponibile e mostra Response stopped, ma audit failed/AttributeError: cancellazione classificata male. |

Il tentativo di Stop sul run 20 è arrivato dopo la conclusione per limite di
iterazioni: non è contato come test riuscito di cancellazione.

## Difetti di Jarvs corretti in TDD

1. **Bootstrap del workspace della release.** Le impostazioni puntavano a
   `python-runtime/workspace`, ma il bootstrap non creava la cartella. Ora crea
   e ripristina solo la directory gestita del runtime; conserva impostazioni,
   policy e percorsi personalizzati. Il test su installazione nuova falliva
   prima della modifica e passa dopo, insieme alla conservazione della
   configurazione esistente. Ripristinata anche la cartella dell'installazione
   corrente, poi confermate scrittura e modifica reali.
2. **Audit dei rifiuti.** Il coordinatore emetteva `execution_denied` prima di
   persistere `run.failed`; il trasporto interrompeva la lettura al rifiuto,
   lasciando il run running. Ora il terminale viene persistito prima del
   messaggio di errore, anche per il percorso specialist. Due regressioni
   riproducono il consumatore che si ferma sul primo rifiuto, sia per chiamata
   esplicita sia per tool scelto dal modello: rosse prima, verdi dopo.
3. **Stop sullo stream del provider.** Durante la chiusura concorrente urllib
   può sollevare AttributeError o ValueError dalla lettura. Se la cancellazione
   è attiva, questi errori vengono normalizzati in `generation_cancelled`;
   altrimenti continuano a propagarsi. Due varianti del test di lettura
   bloccata fallivano prima e passano dopo. Non viene nascosto un errore di
   programmazione quando nessuno ha richiesto Stop.

## Riprova della release corretta

Ricompilata con `npm --prefix desktop run build:desktop` e riavviata tramite
Computer Use, mantenendo LM Studio caricato e Thinking nella configurazione On
originale. La campagna comprende complessivamente 14 prompt reali.

| Run | Prova | Esito |
| --- | --- | --- |
| 27 | Elenco 1–500 e Stop durante lo streaming visibile | UI: Response stopped; audit: `cancelled`, motivo `generation_cancelled`, nessun errore generico. |
| 28 | Subito dopo Stop: risultato di 2 + 2, senza riprendere l'elenco | `4`, nessun tool, completed, 6,83 s; nessuna ripresa dell'output interrotto. |

Verifiche automatiche finali: 92 test backend mirati superati (uno smoke test
LM Studio escluso, perché i prompt reali sono stati eseguiti tramite UI), 13 test
Rust superati, `cargo fmt --check`, `git diff --check` e build Windows riusciti.
I test backend includono socket HTTP reali locali, cancellazione, audit, recovery,
routing, strumenti nativi e integrazione specialist. Le prove con socket sono
state eseguite fuori dal sandbox, che altrimenti ne impedisce la connessione.

Jarvs è lasciato aperto con una chat nuova; LM Studio continua a servire lo stesso
modello con Thinking On. Il file sintetico `qa_model_20261016/prova.txt` resta
disponibile nel workspace con `BETA 17` per eventuali riprove. Nessun file utente
è stato usato nelle operazioni del modello.

## Questioni aperte e prossimi task

- **Routing composto, priorità alta.** Il modello sceglie inspect se il compito
  inizia con una lettura anche quando deve poi scrivere. Riprodurre il prompt
  del run 20 in un piccolo corpus italiano/inglese con risultati attesi per
  l'intero compito; aggiungere casi di traduzione, negazione e sola lettura.
  Valutare una nuova selezione del profilo quando quello scelto è insufficiente,
  mantenendo invariati validazione, policy e approvazioni. Non ampliare tutti
  i cataloghi indiscriminatamente.
- **Letture ripetute, priorità alta.** Il limite globale evita un ciclo infinito,
  ma sette letture identiche sono già uno spreco. Aggiungere una regressione con
  chiamate identiche e contenuto invariato; introdurre un recupero limitato o un
  errore esplicito prima di consumare l'intero budget. Non bloccare le riletture
  necessarie dopo una modifica reale.
- **Thinking per fase, priorità media.** `/no_think` nel prompt di Jarvs non
  ha eliminato il reasoning nei turni operativi con questa configurazione.
  Provare un controllo esplicito supportato dal provider, opzionale per profilo,
  con test del payload e confronto A/B sugli stessi prompt. La maggiore velocità
  osservata con Off non dimostra maggiore affidabilità del routing.
- **Precisione testuale, priorità media.** Aggiungere valutazioni di nomi file,
  formati esatti e istruzioni limitate al singolo turno. Sono limiti osservati
  del comportamento modello; non considerarli risolti dai fix del runtime.

I risultati descrivono questa configurazione e questi prompt. Non dimostrano
affidabilità generale del modello o di tutti i workflow multi-step.
