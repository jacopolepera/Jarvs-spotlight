# Hermes connector spike

Last updated: August 27, 2026.

## Current decision

Hermes is not the Jarvs harness, kernel, or mandatory execution path. It is one
candidate for a future installable specialist connector.

The August integration work proved that Hermes can be started lazily as a
managed sidecar and kept away from the default Direct path. It also confirmed
the reasons not to build the product around it:

- its process, dependency, prompt, tool, and context footprint is unsuitable as
  a mandatory layer for mid-range and lower-end machines;
- browser, MCP, plugins, research, and speech are product capabilities that
  Jarvs must be able to provide without Hermes;
- Hermes does not expose a complete pre-effect interception boundary for every
  internal tool, so an outer approval is not equivalent to per-action control;
- making Hermes own routing, memory, sessions, or every model call would reduce
  Jarvs to a specialized frontend.

The active roadmap therefore continues with native MCP, installable plugins,
browser/research, model lifecycle, and local-first speech. Specialist connectors
come later and remain optional.

## State of the implementation spike

The Hermes implementation is currently an uncommitted working-tree experiment.
The published `main` branch and `origin/main` do not contain it. This makes the
architecture fully reversible at Git-history level, but the working tree should
not be reset as one undifferentiated block because it also contains generic work
worth preserving.

Reusable findings and components include:

- child-process containment and authenticated graceful shutdown;
- bounded JSON-RPC event normalization and cancellation;
- explicit preflight and health contracts for an optional runtime;
- installer pinning and local runtime attestation;
- proof that Direct can remain the default and avoid importing the connector.

Hermes-specific coupling that should not enter the baseline includes:

- `Direct | Hermes` as a permanent product-level mode switch;
- Hermes settings and status embedded directly in the main application model;
- Hermes capability names in coordinator and UI contracts;
- a roadmap that blocks browser, MCP, or plugins on a Hermes live smoke test.

## Disposition

Before resuming feature development:

1. preserve the spike as a reference branch or patch;
2. extract generic lifecycle, CI, and fallback improvements into focused changes;
3. keep Hermes adapter, installer, UI, and tests out of the baseline merge;
4. implement the connector/plugin contract with a deterministic fixture first;
5. port Hermes to that contract only if a later benchmark justifies it.

No Hermes installation is required for Jarvs development, release, startup,
MCP, plugins, browser, transcription, TTS, memory, or routines.

## Future connector contract

A specialist connector must declare:

- install and compatibility metadata;
- capabilities and required permissions;
- whether Jarvs or the connector owns the model call;
- health, cold-start cost, resident memory, and child processes;
- streaming events, progress, cancellation, and terminal status;
- filesystem, network, credential, and data-egress boundaries;
- session cleanup and uninstall behavior.

Every connector remains subordinate to Jarvs policy and audit. It receives only
bounded task context, cannot write Jarvs memory or routines directly, and must
be removable without changing the Direct or Action paths.

## Conditions for reconsidering Hermes

Hermes may return as an opt-in connector after the native extension layers are
stable. Reconsideration requires measurements on representative low-, mid-, and
high-memory Windows profiles:

- cold and warm startup;
- total resident RAM and VRAM, including children;
- prompt/tool-schema overhead and time to first token;
- cancellation, crash recovery, and orphan cleanup;
- observed effects and approval coverage;
- identical-task comparison with native Jarvs MCP/browser/plugin paths.

Passing these gates can justify an optional integration. It does not justify
making Hermes the only way to use Jarvs.

## Preserved pilot baseline

For historical reproducibility, the spike targeted Hermes `0.20.0`, tag
`v2026.8.3`, commit `3c27eb6234bf91b8ceee9e9071591b31e9b148cb`, through the TUI
Gateway JSON-RPC transport and a local loopback LM Studio endpoint. These values
describe the experiment only; they are not Jarvs runtime requirements.

## Related documents

- [Development roadmap](development-roadmap.md)
- [Model runtime and speech strategy](model-runtime-and-audio.md)
- [Hermes repository](https://github.com/NousResearch/hermes-agent)
