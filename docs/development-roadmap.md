# Incremental development roadmap

> Historical architectural roadmap. As of September 14, 2026, the operational
> source of truth is [the area-by-area development plan](piano-sviluppo.md).
> Its tasks are planned, not authorization to implement the entire roadmap.
> See [testing notes](testing-refresh.md) for the work already implemented.

Last updated: August 27, 2026.

Jarvs evolves through small, independently verifiable increments. The default
assistant must remain useful on mid-range and lower-end computers: the window
and daemon stay light, while models, MCP servers, plugin workers, browser
automation, speech runtimes, and specialist agents start only when requested.

## Product direction

Jarvs is the desktop control plane, not a wrapper around one agent harness. Its
value comes from the combination of:

- the Spotlight interface, hotkeys, tray, attachments, and desktop context;
- direct access to interchangeable local and cloud models;
- one governed path for tools, MCP, plugins, browser actions, and routines;
- bounded personal memory and deterministic reusable workflows;
- user-visible approvals, progress, failure recovery, and audit;
- optional integrations that can be installed, disabled, or replaced.

Hermes is one possible future agent-runtime connector. It is not a prerequisite,
the default execution path, or the owner of Jarvs routing, memory, policy,
plugins, browser, or model lifecycle.

## Delivery rules

- Keep one user-visible outcome or one infrastructure improvement per change.
- Add or update tests in the same change as behavior.
- Keep the Direct path free of plugin discovery, MCP connections, subprocesses,
  browser runtimes, speech models, and specialist imports.
- Route every external effect through registry, validation, policy, approval,
  preflight, executor, and audit.
- Treat tools and metadata received from plugins or MCP servers as untrusted.
- Keep live-provider and hardware checks opt-in; deterministic CI remains
  secret-free.
- Measure resident RAM, cold-start time, warm latency, and orphaned processes for
  every optional runtime before making it part of the normal product surface.
- Give low- and medium-memory profiles at most one heavyweight GPU lease at a
  time. Queue, cancel, preempt, or reject competing work instead of relying on
  best-effort allocation.
- Keep startup idle. A warm speech set is an explicit, bounded session state,
  not permission to keep generation, ASR, and TTS permanently resident.
- Treat images, screenshots, camera frames, and audio as explicit capture and
  egress boundaries. Vision runs on attachment or event, never as continuous
  semantic surveillance.
- Benchmark cold and warm paths, worst-case RAM/VRAM, cancellation, recovery
  from memory pressure, and a 30-minute soak before adopting a resident worker.

## Execution order

### BASELINE-1: Preserve the native baseline and park the Hermes spike — in progress

Outcome: keep the useful generic work from the current experiment without
making Hermes part of the core architecture.

1. Preserve the current Hermes work as an isolated reference spike.
2. Extract generic improvements that stand on their own, especially process-tree
   containment, authenticated graceful shutdown, CI improvements, and explicit
   Direct fallback semantics.
3. Remove Hermes-specific UI and daemon coupling from the merge candidate.
4. Keep the existing native model, action, memory, routine, and voice paths
   independently testable.

`BASELINE-1a` is the first extracted slice: provider-neutral daemon cleanup,
authenticated pre-shutdown preparation, bounded process-tree termination, and
focused Python/Rust tests. The remaining Hermes adapter, settings, coordinator,
UI, installer, and fallback coupling still belong to the reference spike and do
not become prerequisites for later phases.

Exit criteria: `main` remains Hermes-free, Direct behavior is unchanged, useful
generic fixes have focused tests, and the spike can be revisited without being
required by subsequent phases.

### AUDIT-1: Finish durable run recovery — planned

Outcome: complete retention, inspection, fault injection, and guided recovery
for the existing append-only run/effect store.

Exit criteria: every started effect reaches a terminal or explicitly
reconcilable state, and interrupted runs can be inspected without persisting
raw chain of thought or secrets.

### MCP-1: Native MCP client and lifecycle — planned

Outcome: Jarvs can use MCP tools and resources without delegating to an external
agent harness.

1. Implement stdio and Streamable HTTP transports behind a common client.
2. Add initialization, capability negotiation, timeouts, cancellation,
   reconnect, namespacing, and schema bounds.
3. Map MCP tools into the existing Jarvs registry and policy path; do not create
   an alternate executor.
4. Add per-server enablement, tool allowlists, secret references, health, and
   uninstall/disable controls.
5. Start servers only on the action path and stop them when idle or disabled.

Exit criteria: a deterministic fake server covers discovery, collisions,
timeouts, cancellation, malformed schemas, policy modes, and zero MCP activity
on Direct requests.

### PLUGIN-1: Installable capability bundles — planned

Outcome: add capabilities without growing the Jarvs core or coupling the product
to Hermes.

The initial plugin contract may contribute declarative skills, MCP server
definitions, provider adapters, browser backends, speech providers, or optional
specialist connectors. Executable plugins run out of process. Manifests declare
version compatibility, entry points, capabilities, effects, permissions,
configuration schema, secrets, and health checks.

Exit criteria: install, inspect, enable, disable, update, and remove are explicit;
plugin failure cannot break startup or Direct chat; all model-callable effects
still pass through Jarvs policy and audit.

### BROWSER-1: Browser and sourced research — planned

Outcome: web research and browser interaction become first-class Jarvs
capabilities rather than Hermes-only features.

1. Deliver sourced research with URL, title, retrieval time, and clear untrusted
   content boundaries.
2. Add a managed browser backend as an MCP server or plugin worker.
3. Add a Jarvs browser surface for navigation, screenshots, progress, and user
   takeover without granting the renderer direct tool execution.
4. Separate read/navigation operations from effectful form submission,
   download, authentication, and purchase flows.
5. Capture only a selected viewport, crop, or event-driven screenshot. Continuous
   desktop/video interpretation is out of scope, and image egress to a cloud
   provider requires the same explicit boundary as workspace content.

Exit criteria: cancellation and process cleanup work; credentials are never
captured implicitly; effectful operations are previewed and governed.

### RUNTIME-1: Resource arbitration and local worker lifecycle — planned

Outcome: optional runtimes share one bounded lifecycle and cannot silently
overcommit a consumer PC.

1. Add declarative workload estimates for device, RAM, VRAM, cold load, expected
   residency, priority, cancellation, and preemption support.
2. Implement a single-heavy-GPU lease for low/medium profiles, priority queues,
   back-pressure, timeouts, and explicit release.
3. Support four measured policies: startup idle, session-scoped warm workers,
   hybrid idle TTL, and deliberate GPU swap for batch work.
4. Supervise every local worker with loopback/authentication where applicable,
   health checks, process-tree containment, and idempotent cleanup.
5. Recover from load failure or OOM to the prior usable provider or a clear
   unloaded state; telemetry contains timings and resource peaks, never prompts.

Exit criteria: deterministic fake workers prove lease serialization,
cancellation, priority, timeout, cleanup, and OOM recovery; the 16 GB RAM / 8 GB
VRAM reference profile never admits a second heavyweight GPU job.

### MODEL-1: Provider-neutral generation and multimodal lifecycle — planned

Outcome: Jarvs can manage local generation models on demand without requiring a
specific GUI or inference server.

The first adapter targets LM Studio's native v1 API because it matches the
current development setup and already supports listing, loading, unloading, JIT
loading, idle TTL, and auto-eviction for LLM and embedding models. A measured
PoC compares LM Studio with llama.cpp and Ollama before any migration decision.
The Jarvs contract must make lifecycle control optional so a plain
OpenAI-compatible endpoint or cloud provider still works.

Required concepts:

- provider capabilities: generate, embed, list, load, unload, and health;
- declared modalities, image limits, projector/vision assets, context/KV policy,
  device placement, resource estimate, streaming, and preemption support;
- resource profiles for low, medium, and high memory machines;
- one-heavy-model-at-a-time as the conservative default;
- configurable idle TTL and explicit release before another GPU-heavy workload;
- cold-load and warm-request telemetry that never contains prompts.

Exit criteria: Jarvs can switch two local models without manual UI operations,
recover from load failure, and fall back to an unmanaged endpoint without
changing the conversation contract. A 3B/4B quantized VLM on the 16/8 reference
machine is a benchmark profile, not a required product model.

### AUDIO-1: Provider-neutral transcription — planned

Outcome: keep the existing F4/microphone experience while removing the hard
dependency on Gemini.

1. Extract a bounded `SpeechToTextProvider` contract from the current Gemini
   implementation.
2. Keep Gemini as an optional cloud adapter, never the only route.
3. Define both bounded batch and streaming modes. `whisper.cpp` `base`/`small`
   is the focused CPU ASR baseline; `sherpa-onnx` is the unified STT/TTS
   candidate.
4. In the low-memory profile, load ASR after recording and release it on a short
   idle TTL. A medium profile may prewarm a small CPU model only for an explicit
   voice session. GPU ASR is reserved for measured batch work after releasing
   the generation lease.
5. Keep push-to-talk as the first UX; add VAD/endpointing without making a wake
   word part of the initial dependency set.

Exit criteria: local transcription works without a paid API, cloud use remains
explicit, and the local speech runtime is absent from startup and ordinary chat.

### AUDIO-2: Text-to-speech — future

Outcome: responses can be spoken without making a neural voice model mandatory.

Start with installed Windows system voices as the zero-download, zero-inference
baseline. Add an optional local neural TTS provider only after measuring Italian
quality, latency, memory, redistribution terms, and model licenses. STT and TTS
share lifecycle and resource-budget contracts, not necessarily the same engine.

Piper is the first focused neural PoC and `sherpa-onnx` remains the unified
alternative. Piper runtime and voice licenses must pass an explicit
redistribution gate before bundling. Providers should emit the first sentence or
clause as soon as possible instead of waiting for a complete long response.

Exit criteria: speech can be interrupted immediately, audio is not retained by
default, and generating speech cannot leave the chat model and TTS model
unbounded in GPU memory at the same time.

### AUDIO-3: Conversational voice loop — future

Outcome: optional hands-free conversation composes the proven STT and TTS
providers without changing the Direct, Action, or connector contracts.

Add VAD/endpointing, echo handling, sentence-level synthesis, immediate stop,
barge-in that cancels playback and generation, and an optional wake word. Model
free thresholds and push-to-talk remain valid fallbacks on constrained machines.

Exit criteria: the audio state machine is deterministic across idle, recording,
transcribing, generating, speaking, and interrupted states; a 30-minute session
leaves no orphan worker and records no audio by default.

### CONNECTOR-1: Optional specialist runtimes — future

Outcome: installable agent runtimes can handle open-ended work through a uniform
connector interface after MCP, plugins, browser, and resource governance are
stable.

Hermes may be the first compatibility test, but the contract must also work with
a fixture connector and another runtime. A connector declares health,
capabilities, model ownership, cancellation, events, resource cost, and trust
boundary. It never owns Jarvs memory, policy, plugin catalog, or default model
path.

Exit criteria: removing a connector loses only that optional capability; Jarvs
still starts, chats, uses native MCP/plugins/browser, transcribes, and runs
routines.

### RELEASE-1: Reproducible Windows distribution — parallel track

Outcome: produce a clean, documented source release first, followed by a
self-contained and signed Windows package. Optional runtimes and models remain
separate downloads unless their size, license, and update model justify bundling.

## References

- [Multimodal local-assistant research snapshot](Assistente_locale_multimodale.docx)
- [LM Studio REST API](https://lmstudio.ai/docs/developer/rest)
- [LM Studio model loading](https://lmstudio.ai/docs/developer/rest/load)
- [LM Studio TTL and auto-eviction](https://lmstudio.ai/docs/developer/core/ttl-and-auto-evict)
- [MCP transports](https://modelcontextprotocol.io/specification/draft/basic/transports)
- [MCP security and consent principles](https://modelcontextprotocol.io/specification/2025-06-18/index)
- [whisper.cpp](https://github.com/ggml-org/whisper.cpp)
- [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)
- [Windows speech synthesis](https://learn.microsoft.com/en-us/uwp/api/windows.media.speechsynthesis)
