from __future__ import annotations

from pathlib import Path

from hermes_mobile.files.outbound import (
    collect_outbound_media,
    find_media_candidates,
)
from hermes_mobile.persistence.repositories import ProfileStore

PNG = b"\x89PNG\r\n\x1a\n" + b"fake-png-body"


def test_find_media_candidates_orders_and_deduplicates():
    text = (
        "Aquí tienes la imagen ![qr](https://cdn.example.test/qr.png) "
        "y el fichero MEDIA:/tmp/informe.pdf del informe. "
        "También `/var/data/notas.md` en el host."
    )
    candidates = find_media_candidates(text)
    kinds = [(item.kind, item.value) for item in candidates]
    assert kinds == [
        ("url", "https://cdn.example.test/qr.png"),
        ("path", "/tmp/informe.pdf"),
        ("path", "/var/data/notas.md"),
    ]
    # The MEDIA path is not reported twice as a bare path.
    assert sum(1 for item in candidates if item.value == "/tmp/informe.pdf") == 1


def test_find_media_candidates_ignores_remote_urls_as_bare_paths():
    text = "Mira https://example.test/static/logo.png y nada más."
    assert find_media_candidates(text) == []


async def _store(tmp_path) -> tuple[ProfileStore, str]:
    store = ProfileStore(tmp_path)
    store.initialize()
    conversation = store.ensure_conversation("native-outbound")
    return store, conversation["public_id"]


async def test_collect_outbound_media_registers_local_file_and_cleans_text(tmp_path):
    store, conversation_id = await _store(tmp_path)
    image_path = tmp_path / "qr.png"
    image_path.write_bytes(PNG)

    text = f"Escanea este código:\n\nMEDIA:{image_path}\n"
    cleaned, blocks = await collect_outbound_media(store, conversation_id, text)

    assert cleaned == "Escanea este código:"
    assert len(blocks) == 1
    assert blocks[0]["type"] == "attachment"
    assert blocks[0]["mime_type"] == "image/png"
    assert blocks[0]["status"] == "ready"
    assert blocks[0]["filename"] == "qr.png"
    stored = store.attachment(blocks[0]["attachment_id"])
    assert stored is not None and stored["status"] == "ready"
    assert Path(stored["storage_path"]).read_bytes() == PNG
    assert stored["conversation_id"] == conversation_id


async def test_collect_outbound_media_deduplicates_across_calls(tmp_path):
    store, conversation_id = await _store(tmp_path)
    image_path = tmp_path / "logo.png"
    image_path.write_bytes(PNG)
    text = f"MEDIA:{image_path}"

    _, first = await collect_outbound_media(store, conversation_id, text)
    _, second = await collect_outbound_media(store, conversation_id, text)

    assert first[0]["attachment_id"] == second[0]["attachment_id"]
    rows = store.list_attachments(conversation_id, None, 10, 0)
    assert [row["public_id"] for row in rows] == [first[0]["attachment_id"]]


async def test_collect_outbound_media_handles_local_markdown_images(tmp_path):
    store, conversation_id = await _store(tmp_path)
    image_path = tmp_path / "grafico.png"
    image_path.write_bytes(PNG)

    cleaned, blocks = await collect_outbound_media(
        store, conversation_id, f"Resultado: ![grafico]({image_path})"
    )

    assert cleaned == "Resultado:"
    assert blocks[0]["mime_type"] == "image/png"


async def test_collect_outbound_media_downloads_remote_images(tmp_path):
    store, conversation_id = await _store(tmp_path)

    async def downloader(url: str):
        assert url == "https://cdn.example.test/qr.png"
        return PNG, "codigo-qr.png", "image/png"

    cleaned, blocks = await collect_outbound_media(
        store,
        conversation_id,
        "Tu QR: ![qr](https://cdn.example.test/qr.png)",
        downloader=downloader,
    )

    assert cleaned == "Tu QR:"
    assert blocks[0]["mime_type"] == "image/png"
    assert blocks[0]["filename"] == "codigo-qr.png"
    assert store.attachment(blocks[0]["attachment_id"])["size"] == len(PNG)


async def test_collect_outbound_media_leaves_unusable_refs_untouched(tmp_path):
    store, conversation_id = await _store(tmp_path)
    missing = tmp_path / "no-existe.png"

    cleaned, blocks = await collect_outbound_media(
        store, conversation_id, f"Sin fichero: MEDIA:{missing}"
    )

    assert blocks == []
    assert cleaned == f"Sin fichero: MEDIA:{missing}"


async def test_collect_outbound_media_ignores_fenced_examples(tmp_path):
    store, conversation_id = await _store(tmp_path)
    image_path = tmp_path / "ejemplo.png"
    image_path.write_bytes(PNG)
    text = f"Para enviar una imagen escribe:\n\n```\nMEDIA:{image_path}\n```\n"

    cleaned, blocks = await collect_outbound_media(store, conversation_id, text)

    assert blocks == []
    assert f"MEDIA:{image_path}" in cleaned


async def test_collect_outbound_media_rejects_executables(tmp_path):
    store, conversation_id = await _store(tmp_path)
    binary = tmp_path / "tool.png"
    binary.write_bytes(b"MZ" + b"\x00" * 32)

    cleaned, blocks = await collect_outbound_media(
        store, conversation_id, f"Ejecutable MEDIA:{binary}"
    )

    assert blocks == []
    assert "MEDIA:" in cleaned
