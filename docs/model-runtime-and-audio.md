# Model runtime and speech strategy

Last updated: August 27, 2026.

## Decision

Jarvs should coordinate several replaceable providers instead of searching for
one process that must serve generation, transcription, and text-to-speech.

LM Studio remains a valid first provider for local generation. Its native v1
API can list, load, and unload LLM and embedding models, while JIT loading, idle
TTL, and auto-eviction support memory-conscious switching. Current official
model-management documentation covers LLM and embedding models, not STT or TTS.
Speech therefore gets its own provider contracts and lifecycle.

The accompanying [multimodal local-assistant report](Assistente_locale_multimodale.docx)
is a dated research snapshot for a 16 GB RAM / 8 GB VRAM consumer PC. It informs
budgets and benchmark hypotheses; its named models, runtimes, and memory figures
are not compatibility promises.

```text
Jarvs resource manager
|-- GenerationProvider       LM Studio first; cloud/OpenAI-compatible fallback
|-- EmbeddingProvider        optional and lazy
|-- SpeechToTextProvider     Gemini adapter + local adapter
|-- TextToSpeechProvider     Windows voice + optional neural adapter
`-- SpecialistConnector      optional, future, never required
```

## Reference hardware envelope

The product baseline remains hardware-neutral and starts with no optional model
loaded. For planning and regression tests, the first constrained profile is:

- 16 GB system RAM, 8 GB VRAM, SSD/NVMe, and one active conversation;
- at most one heavyweight generative or batch GPU workload;
- small ASR, TTS, VAD, and wake-word components on CPU where practical;
- vision from an attached image, selected crop, or event-driven screenshot,
  never continuous semantic analysis of the desktop or camera;
- a measured safety margin: worst-case GPU allocation should remain below
  roughly 85–90% of available VRAM.

A 3B/4B quantized VLM is the initial benchmark shape for this machine, not a
required model. Actual admission uses observed allocation and provider
capabilities rather than parameter count alone.

## Provider contracts

The normal generation contract remains provider-neutral: health, capabilities,
streaming generation, cancellation, and optional tool calling. Lifecycle is a
separate optional capability with list, load, unload, current instances, and
load configuration.

Each provider or worker additionally declares:

- modalities and limits, including image formats/count/size and required
  projector or vision assets;
- supported batch/streaming modes, device placement, and context/KV policy;
- estimated RAM, VRAM, cold-load time, and residency scope (`request`, `ttl`,
  `voice_session`, or externally managed);
- cancellation, preemption, health, and cleanup behavior;
- model/runtime version, artifact hash, disk size, and license metadata.

This separation matters because:

- cloud endpoints cannot be loaded or unloaded by Jarvs;
- generic OpenAI-compatible endpoints may not expose management APIs;
- LM Studio can expose a richer management API without becoming a hard product
  dependency;
- audio engines use different model formats and resource patterns.

## LM Studio assessment

No immediate migration is required. The first implementation should add an LM
Studio lifecycle adapter around:

- `GET /api/v1/models`;
- `POST /api/v1/models/load`;
- `POST /api/v1/models/unload`;
- JIT loading for inference requests;
- per-request or server-default idle TTL;
- auto-eviction when switching JIT-loaded models.

Jarvs should still support unmanaged endpoints. If lifecycle capability is
absent, settings explain that model loading remains external.

LM Studio's headless `llmster` daemon is worth a packaging PoC because it can
run without the desktop GUI. It should be evaluated, not adopted implicitly:
installation size, update behavior, redistribution rights, Windows lifecycle,
and compatibility with the existing model catalog remain release decisions.

### Generation runtime PoC matrix

| Runtime | Role in the PoC | Decision to measure |
|---|---|---|
| LM Studio | First lifecycle adapter; already used by Jarvs development | Native management coverage, headless packaging, idle behavior, and recovery |
| llama.cpp | Fine-control reference for GGUF/VLM offload, cache, slots, and projector assets | Whether lower-level control materially improves the 16/8 profile |
| Ollama | Operational-simplicity reference with managed load/keep-alive behavior | Whether simpler lifecycle outweighs reduced tuning control |

This is a comparison, not a migration plan. Jarvs must also retain the unmanaged
OpenAI-compatible path.

## Residency and arbitration modes

The resource coordinator exposes four policies instead of one global
"loaded/unloaded" switch:

1. **Startup idle:** no optional model or worker is started merely because Jarvs
   opens.
2. **Session warm:** a small CPU speech set may remain resident only while the
   user explicitly keeps a voice session active.
3. **Hybrid TTL:** the active generation or speech model is released after a
   bounded idle interval.
4. **Batch GPU swap:** release the generative lease before starting a measured
   GPU-heavy ASR or other batch job, then restore only on demand.

Low and medium profiles default to a single heavyweight GPU lease. Priority,
cancellation, back-pressure, and timeouts are owned by Jarvs, not inferred from
provider-specific errors.

## Resource policy for consumer hardware

The conservative default is one heavyweight model at a time.

### Low-memory profile

- no model is loaded by Jarvs at startup;
- JIT-load generation and use a short idle TTL;
- run local batch STT on CPU after recording stops;
- use the installed Windows voice for TTS;
- do not keep browser workers, MCP servers, plugins, or specialists resident;
- offer cloud fallback only with explicit user choice.

### Medium-memory profile

- keep the active generation model warm only while the user is active;
- allow a small CPU speech model beside it only during an explicit voice
  session;
- unload or evict the generation model before a GPU-heavy audio workload;
- cap concurrent plugin and browser workers.

### High-memory profile

- permit multiple loaded instances only after memory estimation and preflight;
- keep user-visible controls for pinning and releasing models;
- preserve the same correctness and cleanup behavior as lower profiles.

Hardware detection should recommend a profile, not silently override the user.
Out-of-memory failures must be bounded and recoverable.

## Transcription

The microphone UI, F4 shortcut, bounded payload, and authenticated daemon route
already exist. The current backend sends audio to Gemini and therefore requires
a configured cloud key. The next change is architectural, not a UI rewrite:

1. extract `SpeechToTextProvider`;
2. preserve Gemini as an optional adapter;
3. add a fully local provider;
4. expose provider, language, model, device, `batch|streaming`, VAD/endpointing,
   residency, and retention settings;
5. keep audio ephemeral and never add it to memory automatically;
6. retain push-to-talk as the first dependable interaction, even when VAD is
   available.

Two strong PoC candidates are:

- `whisper.cpp`: focused, lightweight Whisper ASR with quantization, CPU-only
  operation, Windows support, VAD, and several acceleration backends; `base`
  and multilingual `small` are the first constrained profiles;
- `sherpa-onnx`: one local ONNX runtime supporting streaming and non-streaming
  STT, TTS, VAD, and Windows builds.

The PoC must compare Italian word error rate, first-result latency, real-time
factor, peak RAM/VRAM, model size, cold load, cancellation, and licenses for
both runtime code and selected model weights.

The low-memory path transcribes after recording. The medium profile may prewarm
a small CPU ASR model for an explicit voice session and stream partial results.
Large GPU ASR is a batch-only experiment that must first release the generation
lease.

## Text-to-speech

TTS should have a zero-cost baseline and a higher-quality optional tier.

1. Use installed Windows speech voices first. This requires no neural model,
   uses the current system voice by default, and is sufficient to restore the
   speak-response interaction.
2. Evaluate Piper as the first focused local neural PoC. Runtime and individual
   voice licenses, including redistribution compatibility, are release gates.
3. Keep `sherpa-onnx` as the first unified STT/TTS candidate; alternative
   engines and voices may be delivered by plugins.
4. Keep cloud TTS optional and clearly labelled with its data and monetary cost.

Playback must support immediate stop, sentence-level streaming where the
provider permits it, time to first audio must be measured separately from total
synthesis time, and no audio is retained by default.

## Audio interaction state machine

The conversational loop uses explicit states rather than overlapping callbacks:

```text
idle -> recording -> transcribing -> generating -> speaking -> idle
            |              |              |            |
            `--------------+--------------+----------> interrupted -> idle
```

VAD may end recording; barge-in stops playback, cancels outstanding generation,
and returns control to recording. Echo handling prevents Jarvs output from being
treated as new input. Wake word is optional and follows push-to-talk plus VAD,
not the other way around.

## Vision and capture policy

- An already attached image can use one direct multimodal provider call.
- Capturing a screenshot/crop/camera frame is an Action-path effect with visible
  scope, retention, and cloud-egress policy.
- Continuous frame sampling or semantic desktop surveillance is not a baseline
  capability.
- Prefer accessibility trees, selectors, coordinates, OCR, and deterministic
  image operations when semantic vision is unnecessary.

## Benchmark protocol

The initial local corpus contains at least 20 short Italian turns, 10 noisy
turns, 10 representative screenshots, and 5 long spoken responses. Run it after
a cold start and with normal desktop applications open. Record:

- idle, post-load, image, and generation peak RAM/VRAM;
- generation time to first token and tokens/s at 4k and 8k where supported;
- ASR real-time factor and observed errors on Italian names;
- TTS time to first audio chunk and real-time factor;
- end-to-end time from end of speech to first spoken response;
- false endpointing, barge-in success, cancellation latency, cleanup, and a
  30-minute voice-session soak.

Published model sizes are inputs, not memory guarantees: runtime, projector,
KV cache, activations, display usage, and driver behavior must be included.

## Acceptance gates

| Area | Gate |
|---|---|
| Startup | no optional model loaded unless the user explicitly restores a bounded warm voice session |
| Switching | failed load leaves the previous usable state or a clear unloaded state |
| Memory | low/medium profiles serialize heavyweight GPU work and stay below the measured 85–90% VRAM ceiling |
| Multimodal | provider declares image/projector/context limits; a text fallback remains available |
| Privacy | local speech sends no audio or text off-device |
| Cost | cloud provider selection is explicit and visible |
| Cancellation | recording, transcription, synthesis, playback, and loading can stop |
| Cleanup | idle or disabled providers release model/process resources |
| Portability | Direct chat still works with an unmanaged OpenAI-compatible endpoint |
| Soak | a 30-minute voice session leaves no orphan worker or unbounded memory growth |

## Official references

- [Multimodal local-assistant research snapshot](Assistente_locale_multimodale.docx)
- [LM Studio native REST API](https://lmstudio.ai/docs/developer/rest)
- [Load a model](https://lmstudio.ai/docs/developer/rest/load)
- [Unload a model](https://lmstudio.ai/docs/developer/rest/unload)
- [JIT loading, TTL, and auto-eviction](https://lmstudio.ai/docs/developer/core/ttl-and-auto-evict)
- [LM Studio headless operation](https://lmstudio.ai/docs/developer/core/headless)
- [whisper.cpp](https://github.com/ggml-org/whisper.cpp)
- [llama.cpp server](https://github.com/ggml-org/llama.cpp/tree/master/tools/server)
- [Ollama](https://docs.ollama.com/)
- [Piper](https://github.com/OHF-Voice/piper1-gpl)
- [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)
- [Windows speech synthesis](https://learn.microsoft.com/en-us/uwp/api/windows.media.speechsynthesis)
