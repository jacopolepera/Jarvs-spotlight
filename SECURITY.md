# Security Policy

## Reporting a vulnerability

Use GitHub's private vulnerability reporting for this repository when available. If it is not available, open an issue requesting a private contact channel without including exploit details, credentials, personal data, or a working proof of concept.

## Security model

Jarvs Spotlight can execute approved local actions. The default execution policy requires confirmation, cloud workspace egress defaults to denied, and the daemon is bound to loopback with token authentication. These controls reduce risk but do not turn arbitrary model-generated code into a sandbox.

Only use models and tools you trust, review requested actions, and keep backups of important workspaces.
