# Contributing to Jarvs Spotlight

Thank you for contributing. Keep changes focused, explain their user impact, and include tests for behavior that can regress.

## Development setup

Follow the source setup in the root README, then install `requirements-dev.txt` and run `npm ci` in `desktop/`.

## Before opening a pull request

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest
npm --prefix desktop run build
cargo test --locked --manifest-path desktop\src-tauri\Cargo.toml --lib
```

Do not commit API keys, local settings, workspaces, build outputs, personal data, desktop captures, or internal planning notes. Public UI text, documentation, code comments, commit messages, and pull request descriptions should be written in English.

Security-sensitive changes must preserve the central tool registry, validation, policy, approval, and executor path. A UI preference must never bypass a hard safety denial.
