"""Observed functional evidence, without expected-value comparisons or verdicts."""
from shared.document_metrics import (
    writer_document, writer_document_visible_text, writer_document_headings,
)


def functional_facts(artifact, *, revision, streams, ui, use_ui):
    """Summarize observations; raw artifacts and SSE remain the review authority."""
    document = writer_document(artifact) if isinstance(artifact, dict) else {}
    text = artifact if isinstance(artifact, str) else writer_document_visible_text(artifact)
    return {
        "draft": {
            "available": artifact is not None,
            "revision": revision,
            "characters": len(text or ""),
            "character_method": "raw_markdown" if isinstance(artifact, str) else "writer_visible_text",
            "headings": writer_document_headings(artifact) if document else [],
            "ui_editable": document.get("ui_editable") if document else None,
        },
        "streams": [{
            "stream_id": stream.stream_id,
            "events": list(stream.events),
            "delta_count": stream.events.count("artifact_stream_delta"),
            "characters": len(stream.text),
            "aborted": stream.aborted,
        } for stream in streams],
        "ui": {
            "captured": use_ui,
            **({key: ui.get(key) for key in (
                "final_panel_visible", "final_text", "final_ui_diagnostics",
                "rendered_images", "failed_images", "placeholder_images",
            )} if use_ui else {}),
        },
    }
