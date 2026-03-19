"""
core.vision.attachments — File and image attachment management.

Handles copying user-selected files into the workspace attachment store
and encoding them into the format expected by open-interpreter messages.
"""
import os
import shutil
import base64
from datetime import datetime


def store_attachment(source_path: str, workspace_path: str) -> str:
    """
    Copies source_path into <workspace_path>/.attachments/ with a timestamp prefix.
    Returns the destination path.
    """
    attach_dir = os.path.join(workspace_path, ".attachments")
    os.makedirs(attach_dir, exist_ok=True)
    basename = os.path.basename(source_path)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(attach_dir, f"{ts}_{basename}")
    shutil.copy2(source_path, dest)
    return dest


def encode_attachment_for_engine(att: dict) -> dict | None:
    """
    Converts an attachment dict {path, kind} into an open-interpreter message dict.

    - kind == "image"  → base64-encoded image message
    - kind == "file"   → text message with file contents (PDF text-extracted or raw text)

    Returns None if encoding fails (error already printed).
    """
    path = att["path"]
    kind = att["kind"]
    fname = os.path.basename(path)

    try:
        if kind == "image":
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("utf-8")
            return {
                "role": "user",
                "type": "image",
                "format": "base64.png",
                "content": b64,
            }
        else:
            ext = os.path.splitext(fname)[1].lower()
            if ext == ".pdf":
                content = _extract_pdf_text(path, fname)
            else:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    content = f"[Attached file: {fname}]\n```\n{f.read()}\n```"
            return {"role": "user", "type": "message", "content": content}

    except Exception as e:
        print(f"Failed to encode attachment {path}: {e}")
        return None


def _extract_pdf_text(path: str, fname: str) -> str:
    """Extracts text from a PDF. Falls back to an error note if PyPDF2 is unavailable."""
    try:
        from PyPDF2 import PdfReader
        reader = PdfReader(path)
        pdf_text = "".join(page.extract_text() or "" for page in reader.pages)
        if pdf_text.strip():
            return f"[Attached PDF: {fname}]\n```\n{pdf_text.strip()}\n```"
        return f"[Attached PDF: {fname}] (No extractable text.)"
    except Exception as pdf_err:
        return f"[Attached PDF: {fname}] (Failed to extract text: {pdf_err})"
