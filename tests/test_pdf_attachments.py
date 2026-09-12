"""PDF attachment boundaries, durable replay, and native provider payloads."""

import base64
import json
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfWriter

from ava.app.attach import PDF_BYTE_LIMIT, load_attachment, load_pdf
from ava.app.web.events import blocks_json
from ava.app.web.registry import _restored_chat_details
from ava.app.web.routes import ATTACHMENT_BYTE_LIMIT, decode_attachments
from ava.base import AvaError
from ava.llm import Context, Item, Role, Selection
from ava.llm.anthropic import request_body as anthropic_request_body
from ava.llm.codex import codex_request_body
from ava.llm.openai import openai_request_body
from ava.session import Log, UserMessage
from ava.session.codec import block_from_wire, block_to_wire
from ava.session.compaction import estimate_block_tokens

FIXTURES = Path(__file__).parent / "fixtures"


def test_pdf_native_requests_and_durable_replay(home, project):
    data = (FIXTURES / "preview.pdf").read_bytes()
    path = project / "报告.PDF"
    path.write_bytes(data)
    block = load_attachment(project, path.name, image=False, root=project)
    assert block.page_count == 3
    assert block_from_wire(block_to_wire(block)) == block
    assert estimate_block_tokens(block) >= 6000
    item = Item(role=Role.user, blocks=[block])
    context = Context(items=[item])
    encoded = base64.b64encode(data).decode()
    uri = "data:application/pdf;base64," + encoded
    assert json.loads(openai_request_body(context, "gpt-4.1", None))["messages"][0]["content"] == [{
        "type": "file", "file": {"filename": path.name, "file_data": uri},
    }]
    assert json.loads(anthropic_request_body(context, "claude-sonnet-4", 1024))["messages"][0]["content"] == [{
        "type": "document", "title": path.name,
        "source": {"type": "base64", "media_type": "application/pdf", "data": encoded},
    }]
    assert json.loads(codex_request_body(context, Selection("codex", "gpt-5")))["input"][0]["content"] == [{
        "type": "input_file", "filename": path.name, "file_data": uri,
    }]
    log = Log.create_default(project, "scripted", "scripted-model")
    log.append(UserMessage(item=item))
    log_path = log.path
    log.close()
    restored = Log.open(log_path)
    try:
        assert _restored_chat_details(restored) == (path.name, len(data), 0)
        message = next(event.payload for event in restored.loaded_events if isinstance(event.payload, UserMessage))
        assert message.item.blocks == [block]
        assert blocks_json(message.item) == blocks_json(item)
    finally:
        restored.close()


def pdf_with_pages(count):
    writer = PdfWriter()
    for _ in range(count):
        writer.add_blank_page(width=72, height=72)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.mark.parametrize(("data", "error"), [
    (b"not a PDF", "not a valid PDF"),
    (b"%PDF-1.7\nmalformed", "not a valid PDF"),
    ((FIXTURES / "preview-locked.pdf").read_bytes(), "password protected"),
    (b"%PDF-" + b"x" * PDF_BYTE_LIMIT, "8 MiB"),
    (pdf_with_pages(0), "1–100 pages"),
    (pdf_with_pages(101), "1–100 pages"),
])
def test_pdf_rejects_unusable_documents(data, error):
    with pytest.raises(AvaError, match=error):
        load_pdf("document.pdf", data)


def test_pdf_uses_binary_quota_and_preserves_text_limits():
    # Larger than the text limit, including a missing filename extension.
    data = (FIXTURES / "preview.pdf").read_bytes() + b"\n" * (60 * 1024)
    entry = {"kind": "file", "name": "document", "data_base64": base64.b64encode(data).decode()}
    decoded = decode_attachments([entry], ATTACHMENT_BYTE_LIMIT - len(data), 10)
    assert decoded.decoded_bytes == len(data) and decoded.images == 0
    assert decoded.blocks[0].bytes == data
    with pytest.raises(AvaError, match="8 MiB lifetime"):
        decode_attachments([entry], ATTACHMENT_BYTE_LIMIT - len(data) + 1, 0)
    text = {"kind": "file", "name": "notes.txt", "data_base64": base64.b64encode(b"x" * (51 * 1024)).decode()}
    with pytest.raises(AvaError, match="50 KiB"):
        decode_attachments([text], 0, 0)
