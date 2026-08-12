import os

import pytest
from pypdf import PdfWriter

from core.vision.attachment_broker import AttachmentBroker, AttachmentError


def descriptor(attachment_id, name, kind, size):
    return {
        "attachmentId": attachment_id,
        "displayName": name,
        "kind": kind,
        "size": size,
    }


def test_valid_staged_text_file_is_encoded(tmp_path):
    attachment_id = f"{'a' * 32}.txt"
    staged = tmp_path / attachment_id
    staged.write_text("hello", encoding="utf-8")
    broker = AttachmentBroker(tmp_path)

    messages = broker.encode_many(
        [descriptor(attachment_id, "notes.txt", "file", staged.stat().st_size)]
    )

    assert len(messages) == 1
    assert "[Attached file: notes.txt]" in messages[0]["content"]
    assert "hello" in messages[0]["content"]


def test_valid_pdf_is_extracted_without_optional_runtime_failures(tmp_path):
    attachment_id = f"{'e' * 32}.pdf"
    staged = tmp_path / attachment_id
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with staged.open("wb") as handle:
        writer.write(handle)

    messages = AttachmentBroker(tmp_path).encode_many(
        [descriptor(attachment_id, "blank.pdf", "file", staged.stat().st_size)]
    )

    assert messages[0]["content"] == "[Attached PDF: blank.pdf] (No extractable text.)"


@pytest.mark.parametrize(
    "payload",
    [
        {"path": "C:/secret.txt", "kind": "file"},
        descriptor("../secret.txt", "secret.txt", "file", 1),
        descriptor("C:/secret.txt", "secret.txt", "file", 1),
        descriptor(f"{'a' * 32}.txt", "../secret.txt", "file", 1),
    ],
)
def test_paths_and_traversal_are_rejected(tmp_path, payload):
    with pytest.raises(AttachmentError):
        AttachmentBroker(tmp_path).encode_many([payload])


def test_kind_and_signature_mismatch_are_rejected(tmp_path):
    attachment_id = f"{'b' * 32}.png"
    staged = tmp_path / attachment_id
    staged.write_bytes(b"not really a png")
    broker = AttachmentBroker(tmp_path)

    with pytest.raises(AttachmentError, match="kind"):
        broker.encode_many(
            [descriptor(attachment_id, "fake.png", "file", staged.stat().st_size)]
        )
    with pytest.raises(AttachmentError, match="content"):
        broker.encode_many(
            [descriptor(attachment_id, "fake.png", "image", staged.stat().st_size)]
        )


def test_metadata_tampering_is_rejected(tmp_path):
    attachment_id = f"{'c' * 32}.txt"
    staged = tmp_path / attachment_id
    staged.write_text("hello", encoding="utf-8")
    with pytest.raises(AttachmentError, match="metadata"):
        AttachmentBroker(tmp_path).encode_many(
            [descriptor(attachment_id, "notes.txt", "file", 99)]
        )


def test_symlink_escape_is_rejected_when_supported(tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link_id = f"{'d' * 32}.txt"
    link = tmp_path / link_id
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks require privileges on this Windows configuration")

    with pytest.raises(AttachmentError):
        AttachmentBroker(tmp_path).encode_many(
            [descriptor(link_id, "outside.txt", "file", outside.stat().st_size)]
        )
