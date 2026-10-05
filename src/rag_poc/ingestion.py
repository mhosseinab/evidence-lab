"""Bounded text ingestion with immutable coordinates and resumable batches.

There is deliberately no OCR or model download in this module. Character
coordinates refer to the minimally normalized page text returned in ``pages``;
the original upload is retained by Store for an exact source preview.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import math
import re
import unicodedata
from pathlib import PurePath

from pypdf import PdfReader

from rag_poc.config import input_token_bound
from rag_poc.domain import CallContext, PIPELINE_VERSION, ProviderError, stable_hash

_TEXT_TYPES = {"text/plain", "text/markdown", "text/x-markdown"}
_GENERIC_TYPES = {"", "application/octet-stream"}
_PARAGRAPH = re.compile(r"\n[ \t]*\n+")
_SENTENCE = re.compile(r"[.!?][\"')\]]*(?:[ \t]+|\n)")
_WHITESPACE = re.compile(r"\s+")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def pipeline_revision(config) -> str:
    """Changing extraction or chunk rules defeats upload idempotency correctly."""
    settings = config.ingestion
    return f"{PIPELINE_VERSION}-{stable_hash({
        'normalization': 'nfc-lf-v1',
        'pdf': 'pypdf-text-conservative-image-review-v1',
        'chunk_target_chars': settings.chunk_target_chars,
        'chunk_max_chars': settings.chunk_max_chars,
        'chunk_overlap_chars': settings.chunk_overlap_chars,
        'max_page_extracted_chars': settings.max_page_extracted_chars,
        'max_extracted_chars': settings.max_extracted_chars,
    })[:16]}"


def admit_document(raw: bytes, name: str, media_type: str, config) -> str:
    """Validate cheap admission constraints; return ``text`` or ``pdf``.

    HTTP upload handlers can call this before enqueuing work. Parsing remains a
    worker operation. Exceptions contain no submitted source content.
    """
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        raise ProviderError("invalid_document", "Document content must be bytes")
    if not raw:
        raise ProviderError("invalid_document", "The uploaded document is empty")
    if len(raw) > config.ingestion.max_upload_bytes:
        raise ProviderError("document_too_large", "Document exceeds the configured upload limit")
    if not isinstance(name, str) or not name.strip():
        raise ProviderError("invalid_document", "A document filename is required")
    suffix = PurePath(name.replace("\\", "/")).suffix.lower()
    mime = (media_type or "").split(";", 1)[0].strip().lower()
    pdf_header = bytes(raw[:1024]).lstrip().startswith(b"%PDF-")
    if suffix == ".pdf":
        if mime not in _GENERIC_TYPES | {"application/pdf"} or not pdf_header:
            raise ProviderError("invalid_document", "PDF filename, media type and content do not agree")
        return "pdf"
    if suffix not in {".txt", ".md", ".markdown"}:
        raise ProviderError("unsupported_document", "Only UTF-8 TXT, Markdown and text-readable PDF files are supported")
    if mime not in _TEXT_TYPES | _GENERIC_TYPES or pdf_header:
        raise ProviderError("invalid_document", "Text filename, media type and content do not agree")
    return "text"


def _normalize(text: str) -> str:
    # NFC preserves mathematical minus signs, compatibility characters and units.
    return unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))


def _resolve(value):
    return value.get_object() if hasattr(value, "get_object") else value


def _has_images(resources, seen=None, depth=0) -> bool:
    """Inspect image references without loading/decoding image pixels.

    Unused image resources are treated conservatively as needing review. Nested
    forms are bounded; an uninspectable graph is handled by the caller as review.
    """
    if depth > 12:
        raise ValueError("PDF resource nesting exceeds inspection limit")
    resources = _resolve(resources) or {}
    seen = set() if seen is None else seen
    xobjects = _resolve(resources.get("/XObject", {})) or {}
    if len(xobjects) > 1024:
        raise ValueError("PDF resource count exceeds inspection limit")
    for reference in xobjects.values():
        obj = _resolve(reference)
        marker = id(obj)
        if marker in seen:
            continue
        seen.add(marker)
        if obj.get("/Subtype") == "/Image":
            return True
        if obj.get("/Subtype") == "/Form" and _has_images(obj.get("/Resources"), seen, depth + 1):
            return True
    return False


def _page_record(number: int, original: str, state: str, errors: list[str] | None = None) -> dict:
    normalized = _normalize(original)
    return {
        "page": number,
        "text": normalized,
        "state": state,
        "text_hash": text_hash(normalized),
        "original_text_hash": text_hash(original),
        "coordinate_system": "normalized_page_unicode_codepoints",
        "errors": errors or [],
    }


def _extract_pdf(raw: bytes, config) -> tuple[list[dict], list[str]]:
    settings = config.ingestion
    try:
        reader = PdfReader(io.BytesIO(raw), strict=False)
        if reader.is_encrypted:
            return [], ["Encrypted PDFs require an unencrypted replacement"]
        if len(reader.pages) > settings.max_pdf_pages:
            return [], ["PDF exceeds the configured page limit"]
        if not len(reader.pages):
            return [], ["PDF contains no pages"]
    except Exception:
        return [], ["The PDF could not be parsed"]

    pages: list[dict] = []
    errors: list[str] = []
    total_chars = 0
    for number, page in enumerate(reader.pages, 1):
        try:
            original = page.extract_text() or ""
            normalized = _normalize(original)
            total_chars += len(normalized)
            if len(normalized) > settings.max_page_extracted_chars or total_chars > settings.max_extracted_chars:
                # A visibly incomplete preview may be retained, but no partial
                # extraction from this document is eligible for activation.
                limit = min(settings.max_page_extracted_chars, max(0, settings.max_extracted_chars - (total_chars - len(normalized))))
                record = _page_record(number, normalized[:limit], "needs_review", ["Extracted text exceeds the configured safety limit; preview is incomplete"])
                record["preview_truncated"] = True
                record["extracted_character_count"] = len(normalized)
                pages.append(record)
                errors.append(f"Page {number}: extraction size limit exceeded")
                # Record every remaining page as unprocessed instead of implying
                # complete coverage or allocating its extraction text.
                for remainder in range(number + 1, len(reader.pages) + 1):
                    pages.append(_page_record(remainder, "", "needs_review", ["Not processed after extraction size limit was reached"]))
                break
            contents = page.get_contents()
            operations = contents.operations if contents is not None else []
            images = _has_images(page.get("/Resources")) or any(operator == b"INLINE IMAGE" for _, operator in operations)
            has_text = bool(normalized.strip())
            if images and has_text:
                state, page_errors = "needs_review", ["Page contains images as well as extracted text; image content has not been read"]
            elif images:
                state, page_errors = "needs_ocr", ["Page contains image content without extractable text; OCR is not enabled"]
            elif has_text:
                state, page_errors = "extracted", []
            elif operations:
                state, page_errors = "needs_review", ["Page has drawing/content instructions but no readable text"]
            else:
                state, page_errors = "blank", []
            if "\ufffd" in normalized or _CONTROL.search(normalized):
                state = "needs_review"
                page_errors.append("Extracted text contains unreadable or unsupported control characters")
            pages.append(_page_record(number, original, state, page_errors))
        except Exception:
            pages.append(_page_record(number, "", "needs_review", ["Page extraction or coverage inspection failed"]))
            errors.append(f"Page {number}: extraction failed")
    return pages, errors


def _best_boundary(text: str, start: int, target: int, maximum: int) -> int:
    hard_end = min(len(text), start + maximum)
    if hard_end == len(text):
        return hard_end
    wanted = start + target
    low = start + max(1, target * 3 // 5)
    segment = text[start:hard_end]
    for pattern in (_PARAGRAPH, _SENTENCE, _WHITESPACE):
        positions = [start + match.end() for match in pattern.finditer(segment) if low <= start + match.end() <= hard_end]
        if positions:
            return min(positions, key=lambda position: (abs(position - wanted), position))
    return hard_end


def chunk_pages(pages: list[dict], config, *, identity: str) -> list[dict]:
    """Create stable page-local chunks without altering substantive characters."""
    settings = config.ingestion
    target = settings.chunk_target_chars
    maximum = settings.chunk_max_chars
    overlap = settings.chunk_overlap_chars
    if not 0 <= overlap < target <= maximum:
        raise ValueError("Chunk sizes must satisfy 0 <= overlap < target <= maximum")
    revision = pipeline_revision(config)
    chunks: list[dict] = []
    for page in pages:
        if page["state"] != "extracted":
            continue
        text = page["text"]
        cursor = 0
        while cursor < len(text):
            while cursor < len(text) and text[cursor].isspace():
                cursor += 1
            if cursor >= len(text):
                break
            boundary = _best_boundary(text, cursor, target, maximum)
            end = boundary
            while end > cursor and text[end - 1].isspace():
                end -= 1
            excerpt = text[cursor:end]
            if excerpt:
                digest = text_hash(excerpt)
                chunk_id = "chunk_" + stable_hash({"identity": identity, "pipeline": revision, "page": page["page"], "start": cursor, "end": end, "text_hash": digest})[:32]
                chunks.append({"id": chunk_id, "text": excerpt, "page": page["page"], "start": cursor, "end": end, "text_hash": digest})
            if boundary >= len(text):
                break
            next_cursor = max(cursor + 1, boundary - overlap)
            if overlap:
                # Start a little earlier on a word boundary where possible; the
                # hard maximum still bounds the following chunk.
                floor = max(cursor + 1, next_cursor - min(64, max(1, overlap // 2)))
                candidates = [match.end() + floor for match in _WHITESPACE.finditer(text[floor:next_cursor])]
                if candidates:
                    next_cursor = candidates[-1]
            cursor = next_cursor
    return chunks


def extract_document(raw: bytes, name: str, media_type: str, config) -> dict:
    """Extract a document without activating partial or unreadable coverage."""
    try:
        kind = admit_document(raw, name, media_type, config)
    except ProviderError as exc:
        return {"pages": [], "chunks": [], "state": "failed", "errors": [str(exc)], "code": exc.status}
    raw = bytes(raw)
    errors: list[str] = []
    if kind == "pdf":
        pages, errors = _extract_pdf(raw, config)
    else:
        try:
            original = raw.decode("utf-8-sig", errors="strict")
        except UnicodeDecodeError:
            return {"pages": [], "chunks": [], "state": "failed", "errors": ["Text files must use valid UTF-8"], "code": "invalid_encoding"}
        normalized = _normalize(original)
        state = "extracted" if normalized.strip() else "blank"
        page_errors: list[str] = []
        if _CONTROL.search(normalized):
            state, page_errors = "needs_review", ["Text contains unsupported binary/control characters"]
        limit = min(config.ingestion.max_extracted_chars, config.ingestion.max_page_extracted_chars)
        if len(normalized) > limit:
            pages = [_page_record(1, normalized[:limit], "needs_review", ["Extracted text exceeds the configured safety limit; preview is incomplete"])]
            pages[0]["preview_truncated"] = True
            pages[0]["extracted_character_count"] = len(normalized)
        else:
            pages = [_page_record(1, original, state, page_errors)]
    if not pages:
        state = "failed"
    elif any(page["state"] == "needs_review" for page in pages):
        state = "needs_review"
    elif any(page["state"] == "needs_ocr" for page in pages):
        state = "needs_review" if any(page["state"] == "extracted" for page in pages) else "needs_ocr"
    elif not any(page["state"] == "extracted" for page in pages):
        state = "needs_review"
        errors.append("Document contains no readable text")
    else:
        state = "extracted"
    # Retain extracted chunks for review, but ingest_job never embeds/activates a
    # document unless its entire extraction state is eligible.
    identity = hashlib.sha256(raw).hexdigest()
    chunks = chunk_pages(pages, config, identity=identity)
    return {"pages": pages, "chunks": chunks, "state": state, "errors": errors, "pipeline_revision": pipeline_revision(config)}


def validate_vectors(vectors: list, expected: int, dimensions: int) -> list[list[float]]:
    """Validate cache and provider vectors before a storage mutation."""
    if not isinstance(vectors, list) or len(vectors) != expected:
        raise ProviderError("invalid_response", "Embedding response has an incorrect result count")
    validated: list[list[float]] = []
    for vector in vectors:
        if not isinstance(vector, (list, tuple)) or len(vector) != dimensions:
            raise ProviderError("invalid_response", "Embedding vector has an incorrect dimension")
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in vector):
            raise ProviderError("invalid_response", "Embedding vector contains invalid values")
        values = [float(value) for value in vector]
        if not any(value != 0 for value in values):
            raise ProviderError("invalid_response", "Embedding vector has zero norm")
        validated.append(values)
    return validated


def _embedding_batches(chunks: list[dict], profile, batch_size: int) -> list[list[dict]]:
    result: list[list[dict]] = []
    pending: list[dict] = []
    batch_limit = int(profile.effective_max_batch_input_tokens * (1 - profile.context_headroom_fraction))
    for chunk in chunks:
        if input_token_bound(chunk["text"]) > profile.usable_input_tokens:
            raise ProviderError("over_budget", "An embedding input exceeds the configured provider limit")
        candidate = pending + [chunk]
        payload = {"model": profile.model, "input": [item["text"] for item in candidate]}
        if profile.request_dimensions:
            payload["dimensions"] = profile.dimensions
        if len(candidate) > batch_size or input_token_bound(payload) > batch_limit:
            if pending:
                result.append(pending)
            pending = [chunk]
            payload["input"] = [chunk["text"]]
            if input_token_bound(payload) > batch_limit:
                raise ProviderError("over_budget", "An embedding input cannot fit the configured batch budget")
        else:
            pending = candidate
    if pending:
        result.append(pending)
    return result


async def ingest_job(job: dict, store, hub, config) -> dict:
    """Process a claimed ingest job; every publication is fenced by its lease."""
    from rag_poc.retrieval import space_manifest

    ctx = CallContext.for_seconds(job["id"], "ingestion", config.runtime.ingestion_deadline_seconds, config.runtime.max_remote_attempts_per_ingestion)
    version_id = job["payload"]["version_id"]
    version = store.get_version(version_id, include_bytes=True)
    corpus_id = version["corpus_id"]
    manifest = space_manifest(config)
    corpus = store.ensure_corpus(corpus_id, manifest)
    if corpus["space_id"] != manifest["id"]:
        raise ProviderError("space_changed", "The active embedding space does not match ingestion configuration")
    if version["state"] == "ready":
        return {"status": "ready", "document_id": version["document_id"], "version_id": version_id, "reused": True}
    if version["state"] == "extracted":
        chunks = store.chunks_for_version(version_id)
    else:
        # PDF parsing can be CPU-heavy. Keep the event loop available to renew
        # the worker lease and process cancellation while extraction runs.
        extracted = await asyncio.to_thread(extract_document, version["raw"], version["name"], version["media_type"], config)
        ctx.remaining()
        chunks = extracted["chunks"]
        for chunk in chunks:
            chunk["id"] = "chunk_" + stable_hash({"version_id": version_id, "pipeline": pipeline_revision(config), "page": chunk["page"], "start": chunk["start"], "end": chunk["end"], "text_hash": chunk["text_hash"]})[:32]
        store.save_extraction(version_id, extracted["pages"], chunks, extracted["state"], job)
        if extracted["state"] != "extracted":
            return {"status": extracted["state"], "document_id": version["document_id"], "version_id": version_id, "errors": extracted["errors"], "pages": len(extracted["pages"])}
    if not chunks:
        raise ProviderError("invalid_document", "Extracted document has no indexable chunks")
    profile = config.role_profile("embeddings")
    unique = {chunk["text_hash"]: chunk for chunk in chunks}
    cache = store.get_cached_embeddings(manifest["id"], list(unique))
    cached_vectors = {}
    for digest, vector in cache.items():
        if digest in unique:
            cached_vectors[digest] = validate_vectors([vector], 1, manifest["dimensions"])[0]
    pending = [chunk for digest, chunk in unique.items() if digest not in cached_vectors]
    batch_size = min(config.ingestion.embedding_batch_size, profile.batch_size)
    batches = _embedding_batches(pending, profile, batch_size)
    # Reattach cached vectors to this immutable version in bounded writes.
    cached_chunks = [chunk for chunk in chunks if chunk["text_hash"] in cached_vectors]
    for start in range(0, len(cached_chunks), batch_size):
        ctx.remaining()
        items = cached_chunks[start:start + batch_size]
        store.write_embeddings(version_id, manifest["id"], {chunk["id"]: cached_vectors[chunk["text_hash"]] for chunk in items}, job)
    embedded_inputs = 0
    for batch in batches:
        ctx.remaining()
        vectors = validate_vectors(await hub.embed([chunk["text"] for chunk in batch], ctx), len(batch), manifest["dimensions"])
        resolved = {chunk["text_hash"]: vector for chunk, vector in zip(batch, vectors, strict=True)}
        # Multiple chunks can have identical exact text. One paid embedding is
        # reused, with each chunk attached to its own immutable source location.
        store.write_embeddings(version_id, manifest["id"], {chunk["id"]: resolved[chunk["text_hash"]] for chunk in chunks if chunk["text_hash"] in resolved}, job)
        embedded_inputs += len(batch)
    ctx.remaining()
    store.activate_version(version_id, manifest["id"], job)
    return {"status": "ready", "document_id": version["document_id"], "version_id": version_id, "chunks": len(chunks), "embedded_inputs": embedded_inputs, "cached_inputs": len(cached_vectors), "space_id": manifest["id"]}
