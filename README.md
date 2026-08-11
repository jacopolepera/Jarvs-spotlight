# Jarvs Spotlight

Jarvs Spotlight is a Windows-first desktop assistant designed for fast, local access. It combines a Spotlight-style Tauri interface with a policy-governed Python agent runtime, configurable local or cloud models, file attachments, voice input, explicit memory, and reusable routines.

The project is local-first: the desktop app talks to a loopback-only daemon, execution defaults to approval-required, and workspace content is not sent to a cloud model unless the user explicitly allows it.

## Features

- Global `Win+Alt+Space` shortcut and system-tray access
- Streaming chat with local or cloud OpenAI-compatible model endpoints
- File and image attachments with bounded local staging
- Push-to-talk with `F4` and optional Gemini transcription
- Structured workspace, script, system, and routine tools
- Three execution modes: always ask, auto-run within policy, or deny all
- Separate controls for local tool execution and cloud workspace egress
- User-managed memory, routines, and run audit data
- Dark, light, glass, and vaporwave themes

## Current distribution

Jarvs Spotlight is currently distributed as a source release. The steps below produce a working development or release build on Windows. Prebuilt installers are not published yet because the Tauri bundle does not currently embed a Python interpreter; a source-built release uses the project's virtual environment.

## Requirements

- Windows 10 or Windows 11
- Python 3.11 or newer
- Node.js `^20.19.0` or `>=22.12.0`
- Rust stable with the MSVC toolchain
- Microsoft C++ Build Tools and WebView2
- A model endpoint:
  - LM Studio or another loopback OpenAI-compatible server, or
  - a supported cloud model and API key

## Quick start

Open PowerShell and run:

```powershell
git clone https://github.com/jacopolepera/Jarvs-spotlight.git
Set-Location Jarvs-spotlight

py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

Set-Location desktop
npm ci
$env:JARVS_PYTHON = (Resolve-Path ..\.venv\Scripts\python.exe).Path
npm run dev:desktop
```

The root-level `jarvs_dev.vbs` launcher performs the final two steps after `.venv` and the desktop dependencies have been installed.

## Configure a model

Jarvs Spotlight starts with an LM Studio profile that expects an OpenAI-compatible server at `http://localhost:1234/v1`.

1. Start LM Studio's local server and load a model that supports tool calling.
2. Open Jarvs Spotlight settings.
3. Select the local profile, confirm the model identifier and server URL, then save.

Cloud profiles can be configured from the same settings panel. API keys are written only to local ignored configuration files and are never returned to the renderer. They are not yet stored in the operating system credential vault, so protect the local application-data directory accordingly.

Voice transcription currently requires a configured Gemini key even when the chat model is local.

## Build a release executable

From PowerShell:

```powershell
Set-Location desktop
$env:JARVS_PYTHON = (Resolve-Path ..\.venv\Scripts\python.exe).Path
npm run build:desktop
Set-Location ..
.\jarvs_launcher.vbs
```

The executable is written to `desktop\src-tauri\target\release\jarvs-spotlight.exe`. The launcher intentionally checks for both the executable and its bundled Python source resources. The local `.venv` remains the Python runtime for this source-built release.

## Data and privacy

- The daemon listens only on `127.0.0.1` and requires an ephemeral authentication token.
- Settings, model profiles, secrets, memory, routines, and audit data stay in local application state.
- A selected workspace contains only files the user or approved tools create there; personal databases are stored separately.
- Chat context is session-scoped. Long-term memory is explicit, inspectable, editable, and removable from settings.
- `settings.json`, `user_models.json`, `.env`, `data/`, and `workspace/` are ignored by Git.
- Cloud workspace egress defaults to denied. Enabling a cloud model does not automatically authorize sending local tool output.

Review every tool request before approval, especially scripts and system commands. `auto_run` removes the interactive approval step only for actions still allowed by the central policy; hard denials remain authoritative.

## Development

Install development dependencies:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
Set-Location desktop
npm ci
```

Run the validation suite from the repository root:

```powershell
.\.venv\Scripts\python.exe -m pytest
npm --prefix desktop run build
cargo test --locked --manifest-path desktop\src-tauri\Cargo.toml --lib
```

Project layout:

```text
core/                 Python agent, policy, tools, and loopback daemon
desktop/              React frontend and Tauri desktop host
tests/                Python unit and integration tests
models.json           Secret-free starter model catalog
system_prompt.txt     Product-level assistant behavior
jarvs_dev.vbs         Windows development launcher
jarvs_launcher.vbs    Windows source-release launcher
```

## License

Jarvs Spotlight is available under the [MIT License](LICENSE).
