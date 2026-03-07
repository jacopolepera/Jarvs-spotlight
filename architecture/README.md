# Architecture folder (Layer 1)

This folder contains Technical SOPs (Standard Operating Procedures) written in Markdown.

**Why does this exist?**
According to the A.N.T. 3-layer architecture defined in the B.L.A.S.T. protocol (`support/gemini.md`):
- LLMs are probabilistic, but UI logic must be deterministic.
- If UI thread logic changes, or we establish a new pattern for how components communicate (like the streaming from Open Interpreter to PyQt6, or how voice input is handled), we write the SOP here first before coding it.
- This ensures Jarvs (or any AI working on Jarvs) doesn't break the deterministic UI layer by guessing the logic.

*Note: Keep this file so the folder is technically not empty and gets appropriately synced/tracked.*
