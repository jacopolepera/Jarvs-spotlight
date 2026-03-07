# Jarvs Spotlight: Future Steps & Vision

This document tracks the long-term vision, innovative features, and architectural principles required to keep Jarvs future-proof, maintainable, and definitively better than other terminal-based AI applications.

## Innovative Capabilities

*   **Ghost Mode Operations:** Continue expanding the `.tmp/` area so Jarvs can write files, scrape web data, run tests, or execute background scripts completely invisibly, returning only the final product or status to the Spotlight search bar.
*   **Persistent Memory & RAG:** Give Jarvs long-term memory about the user's projects and preferences so he remembers context across reboots and different workspaces.


## Generalization & Maintainability

To ensure Jarvs remains accessible and future-proof without requiring significant code rewrites, the architecture must adhere to the following principles:

*   **Model Agnosticism:** The application must not break if a specific LLM is discontinued or if Open Interpreter changes its default model. Users should be able to select new models or change parameters (like system prompts or temperature) easily via a configuration file (e.g., `settings.json` or `.env`).
*   **Dependency Resilience (Open Interpreter Updates):** When `open-interpreter` releases new updates, the core A.N.T. architecture (especially Layer 2) should gracefully handle deprecations. The logic routing output to the UI must remain decoupled from Open Interpreter's internal streaming logic as much as possible.
*   **Version Tracking:** We must periodically check if the installed `open-interpreter` version is the latest available. (Note: Currently running `0.4.3`, which is the latest version).

## Unresolved UI Bugs

✅ All UI bugs have been resolved as of 2026-02-24. See `problems.md` entries P-006, P-007, P-008 for details.

