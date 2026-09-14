# Security Policy

## Reporting a vulnerability

Use GitHub's private vulnerability reporting for this repository when available. If it is not available, open an issue requesting a private contact channel without including exploit details, credentials, personal data, or a working proof of concept.

## Security model

Jarvs Spotlight can execute approved local actions. The default execution policy requires confirmation, cloud workspace egress defaults to denied, and the daemon is bound to loopback with token authentication. These controls reduce risk but do not turn arbitrary model-generated code into a sandbox.

Only use models and tools you trust, review requested actions, and keep backups of important workspaces.

## Optional runtimes, MCP servers, and plugins

Jarvs does not treat an installed integration as trusted merely because it uses
MCP or a plugin manifest. Server descriptions, tool annotations, schemas,
resources, prompts, and returned content are untrusted until they pass the
Jarvs validation, policy, approval, and executor boundaries.

Optional executable components must stay out of startup and Direct requests,
run in managed processes, declare filesystem/network/secret needs, and have
bounded health, timeout, cancellation, reset, and removal behavior. A process
tree boundary prevents ordinary orphaning but does not reduce the permissions a
component has while it is running. Strong isolation requires a separate OS
sandbox and explicit egress policy.

Remote MCP connections require authentication appropriate to the selected
transport. Local HTTP servers must bind to loopback, validate origins, and avoid
logging credentials or raw sensitive arguments. Stdio servers inherit only a
reduced environment and must not receive arbitrary application secrets.

Hermes is currently an uncommitted compatibility spike and is not required for
MCP, plugins, browser, speech, or specialist development. If it is later ported
to the connector system, its broad agent loop remains high risk because upstream
approval events do not mediate every possible internal effect. See
[the Hermes spike decision](docs/hermes.md).
