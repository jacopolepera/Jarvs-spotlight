"""Resolve opaque, Tauri-staged attachment IDs inside a per-run root."""

from __future__ import annotations

import base64
import os
import re
import stat
from pathlib import Path
from typing import Any, Iterable

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 25 * 1024 * 1024
MAX_TEXT_CHARS = 200_000
MAX_PDF_PAGES = 100
ATTACHMENT_ID = re.compile(
    r"^[0-9a-f]{32}\.(png|jpg|jpeg|webp|heic|pdf|txt|csv|json|py|md|html|xml|yaml|yml)$"
)
IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "heic"}


class AttachmentError(ValueError):
    pass


class AttachmentBroker:
    def __init__(self, root: str | os.PathLike[str] | None) -> None:
        self.root = Path(root).resolve() if root else None

    @classmethod
    def from_environment(cls) -> "AttachmentBroker":
        return cls(os.environ.get("JARVS_ATTACHMENT_ROOT"))

    def encode_many(self, attachments: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        descriptors = list(attachments)
        resolved: list[tuple[Path, str, str, int]] = []
        total = 0
        for descriptor in descriptors:
            item = self._resolve(descriptor)
            total += item[3]
            if total > MAX_TOTAL_BYTES:
                raise AttachmentError("Attachments exceed the total size limit.")
            resolved.append(item)
        return [self._encode(*item) for item in resolved]

    def _resolve(self, descriptor: dict[str, Any]) -> tuple[Path, str, str, int]:
        if self.root is None:
            raise AttachmentError("Attachment staging is not configured.")
        if "path" in descriptor:
            raise AttachmentError("Raw attachment paths are not accepted.")

        attachment_id = descriptor.get("attachmentId")
        display_name = descriptor.get("displayName")
        kind = descriptor.get("kind")
        declared_size = descriptor.get("size")
        if not isinstance(attachment_id, str) or not ATTACHMENT_ID.fullmatch(
            attachment_id
        ):
            raise AttachmentError("Invalid attachment ID.")
        if (
            not isinstance(display_name, str)
            or not display_name
            or len(display_name) > 255
            or Path(display_name).name != display_name
        ):
            raise AttachmentError("Invalid attachment display name.")
        if kind not in {"image", "file"}:
            raise AttachmentError("Invalid attachment kind.")
        if not isinstance(declared_size, int) or isinstance(declared_size, bool):
            raise AttachmentError("Invalid attachment size.")

        extension = attachment_id.rsplit(".", 1)[1]
        expected_kind = "image" if extension in IMAGE_EXTENSIONS else "file"
        if kind != expected_kind:
            raise AttachmentError("Attachment kind does not match its file type.")

        candidate = self.root / attachment_id
        try:
            raw_metadata = candidate.lstat()
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(self.root)
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise AttachmentError("Attachment is missing or outside its staging root.") from exc
        if stat.S_ISLNK(raw_metadata.st_mode) or not resolved.is_file():
            raise AttachmentError("Attachment must be a regular staged file.")
        file_attributes = getattr(raw_metadata, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if reparse_flag and file_attributes & reparse_flag:
            raise AttachmentError("Attachment reparse points are not allowed.")

        size = resolved.stat().st_size
        maximum = MAX_IMAGE_BYTES if kind == "image" else MAX_DOCUMENT_BYTES
        if size > maximum:
            raise AttachmentError("Attachment exceeds its size limit.")
        if declared_size != size:
            raise AttachmentError("Attachment metadata no longer matches the staged file.")
        self._validate_signature(resolved, extension)
        return resolved, display_name, kind, size

    @staticmethod
    def _validate_signature(path: Path, extension: str) -> None:
        with path.open("rb") as handle:
            header = handle.read(8_192)
        valid = True
        if extension == "png":
            valid = header.startswith(b"\x89PNG\r\n\x1a\n")
        elif extension in {"jpg", "jpeg"}:
            valid = header.startswith(b"\xff\xd8\xff")
        elif extension == "webp":
            valid = header.startswith(b"RIFF") and header[8:12] == b"WEBP"
        elif extension == "heic":
            valid = len(header) >= 12 and header[4:8] == b"ftyp"
        elif extension == "pdf":
            valid = header.startswith(b"%PDF-")
        else:
            valid = b"\x00" not in header
        if not valid:
            raise AttachmentError("Attachment content does not match its file type.")

    @staticmethod
    def _encode(path: Path, display_name: str, kind: str, _size: int) -> dict[str, Any]:
        if kind == "image":
            with path.open("rb") as handle:
                content = base64.b64encode(handle.read()).decode("ascii")
            extension = path.suffix.lower().lstrip(".")
            image_format = "jpeg" if extension in {"jpg", "jpeg"} else extension
            return {
                "role": "user",
                "type": "image",
                "format": f"base64.{image_format}",
                "content": content,
            }

        if path.suffix.lower() == ".pdf":
            content = AttachmentBroker._extract_pdf_text(path, display_name)
        else:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                text = handle.read(MAX_TEXT_CHARS + 1)
            if len(text) > MAX_TEXT_CHARS:
                text = f"{text[:MAX_TEXT_CHARS]}\n...[Attachment truncated]..."
            content = f"[Attached file: {display_name}]\n```\n{text}\n```"
        if len(content) > MAX_TEXT_CHARS:
            content = f"{content[:MAX_TEXT_CHARS]}\n...[Attachment truncated]..."
        return {"role": "user", "type": "message", "content": content}

    @staticmethod
    def _extract_pdf_text(path: Path, display_name: str) -> str:
        try:
            from pypdf import PdfReader

            reader = PdfReader(str(path))
            parts: list[str] = []
            length = 0
            for page_number, page in enumerate(reader.pages):
                if page_number >= MAX_PDF_PAGES or length >= MAX_TEXT_CHARS:
                    break
                text = page.extract_text() or ""
                remaining = MAX_TEXT_CHARS - length
                parts.append(text[:remaining])
                length += min(len(text), remaining)
            extracted = "".join(parts).strip()
            if extracted:
                if len(reader.pages) > MAX_PDF_PAGES or length >= MAX_TEXT_CHARS:
                    extracted += "\n...[PDF truncated]..."
                return f"[Attached PDF: {display_name}]\n```\n{extracted}\n```"
            return f"[Attached PDF: {display_name}] (No extractable text.)"
        except Exception as exc:
            raise AttachmentError("PDF text extraction failed.") from exc
