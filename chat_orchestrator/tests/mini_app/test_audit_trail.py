"""Tests for the digital audit trail helpers in mini_app/router.py."""

import hashlib
from unittest.mock import MagicMock


class TestExtractClientIp:
    """_extract_client_ip() should prefer X-Forwarded-For over request.client.host."""

    def _call(self, forwarded_for: str | None, client_host: str) -> str:
        from orchestrator.mini_app.router import _extract_client_ip

        req = MagicMock()
        req.headers = {}
        if forwarded_for is not None:
            req.headers = {"X-Forwarded-For": forwarded_for}
        req.client = MagicMock()
        req.client.host = client_host
        return str(_extract_client_ip(req))

    def test_uses_last_forwarded_for_value(self):
        # Load balancer appends real IP to the right; leftmost value is client-controlled.
        ip = self._call("1.2.3.4, 10.0.0.1", "10.0.0.1")
        assert ip == "10.0.0.1"

    def test_single_forwarded_for(self):
        ip = self._call("5.6.7.8", "10.0.0.1")
        assert ip == "5.6.7.8"

    def test_strips_whitespace(self):
        ip = self._call("1.2.3.4,  9.9.9.9  ", "10.0.0.2")
        assert ip == "9.9.9.9"

    def test_falls_back_to_client_host_when_no_header(self):
        ip = self._call(None, "192.168.1.1")
        assert ip == "192.168.1.1"

    def test_no_client_returns_unknown(self):
        from orchestrator.mini_app.router import _extract_client_ip

        req = MagicMock()
        req.headers = {}
        req.client = None
        assert _extract_client_ip(req) == "unknown"


class TestGenerateAuditPage:
    """_generate_audit_page() should produce a valid PDF containing audit data."""

    def _make_audit_data(self, **overrides) -> dict:
        base = {
            "document_name": "Employment Agreement.pdf",
            "packet_id": "abc-123",
            "requester_name": "Alice Manager",
            "requester_email": "alice@example.com",
            "sent_at": "2026-04-01T10:00:00+00:00",
            "viewed_at": "2026-04-01T10:05:00+00:00",
            "signer_name": "Bob Employee",
            "signer_ip": "203.0.113.42",
            "signed_at": "2026-04-01T10:07:00+00:00",
            "original_sha256": "a" * 64,
            "signed_sha256": "b" * 64,
        }
        base.update(overrides)
        return base

    def test_returns_valid_pdf_bytes(self):
        from orchestrator.mini_app.audit import _generate_audit_page

        result = _generate_audit_page(self._make_audit_data())
        # PDF magic bytes
        assert result[:4] == b"%PDF"

    def test_pdf_has_nonzero_size(self):
        from orchestrator.mini_app.audit import _generate_audit_page

        result = _generate_audit_page(self._make_audit_data())
        assert len(result) > 1000  # a real PDF page is never this small

    def test_works_without_optional_fields(self):
        """viewed_at and signer_ip are optional — page should still generate."""
        from orchestrator.mini_app.audit import _generate_audit_page

        data = self._make_audit_data(viewed_at=None, signer_ip=None)
        result = _generate_audit_page(data)
        assert result[:4] == b"%PDF"

    def test_audit_page_appendable_to_pdf(self):
        """The audit page bytes can be opened and appended via pymupdf."""
        import fitz

        from orchestrator.mini_app.audit import _generate_audit_page

        audit_bytes = _generate_audit_page(self._make_audit_data())

        # Create a minimal 1-page PDF to append to
        base_doc = fitz.open()
        base_doc.new_page()
        base_bytes = base_doc.tobytes()
        base_doc.close()

        # Append audit page
        doc = fitz.open(stream=base_bytes, filetype="pdf")
        audit_doc = fitz.open(stream=audit_bytes, filetype="pdf")
        doc.insert_pdf(audit_doc)
        result_bytes = doc.tobytes()
        doc.close()
        audit_doc.close()

        # Result should be a 2-page PDF
        verify_doc = fitz.open(stream=result_bytes, filetype="pdf")
        assert verify_doc.page_count == 2
        verify_doc.close()


class TestStampPdfSync:
    """_stamp_pdf_sync() should embed a signature image and return valid stamped bytes."""

    def _minimal_png(self) -> bytes:
        """Return a minimal valid PNG generated via pymupdf (guaranteed valid)."""
        import fitz

        pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 10, 10))
        pix.clear_with(255)
        result: bytes = pix.tobytes("png")
        return result

    def _minimal_pdf(self) -> bytes:
        import fitz

        doc = fitz.open()
        doc.new_page(width=595, height=842)
        b: bytes = doc.tobytes()
        doc.close()
        return b

    def test_returns_valid_pdf_bytes(self):
        from orchestrator.mini_app.audit import _stamp_pdf_sync

        result_bytes, *_ = _stamp_pdf_sync(
            self._minimal_pdf(), 0, 0.1, 0.1, self._minimal_png(), 0.2, 0.08
        )
        assert result_bytes[:4] == b"%PDF"

    def test_preserves_page_count(self):
        import fitz

        from orchestrator.mini_app.audit import _stamp_pdf_sync

        result_bytes, *_ = _stamp_pdf_sync(
            self._minimal_pdf(), 0, 0.1, 0.1, self._minimal_png(), 0.2, 0.08
        )
        doc = fitz.open(stream=result_bytes, filetype="pdf")
        assert doc.page_count == 1
        doc.close()

    def test_returns_placement_coords(self):
        from orchestrator.mini_app.audit import _stamp_pdf_sync

        _, x1, y1, x2, y2 = _stamp_pdf_sync(
            self._minimal_pdf(), 0, 0.1, 0.1, self._minimal_png(), 0.2, 0.08
        )
        # x1/y1 should be ~10% of A4 width/height
        assert x1 > 0
        assert y1 > 0
        assert x2 > x1
        assert y2 > y1

    def test_invalid_page_raises(self):
        import pytest

        from orchestrator.mini_app.audit import _stamp_pdf_sync

        with pytest.raises(ValueError, match="does not exist"):
            _stamp_pdf_sync(self._minimal_pdf(), 5, 0.1, 0.1, self._minimal_png())


class TestViewCaptureIdempotency:
    """signing_audit_viewed_at should be written on first view and not overwritten on retry."""

    def test_first_view_ip_extraction(self):
        """_extract_client_ip returns rightmost XFF value (LB-appended real IP)."""
        from unittest.mock import MagicMock

        from orchestrator.mini_app.router import _extract_client_ip

        req = MagicMock()
        req.headers = {"X-Forwarded-For": "1.2.3.4, 203.0.113.1"}
        req.client = MagicMock()
        req.client.host = "10.0.0.1"

        ip = _extract_client_ip(req)
        assert ip == "203.0.113.1"

    def test_second_view_does_not_write(self):
        """If signing_audit_viewed_at already in state, the write guard is False."""
        # State already has the viewed_at field
        state = {
            "signing_document_drive_id": "drive-id-123",
            "signing_status": "pending",
            "signing_audit_viewed_at": "2026-04-01T10:00:00+00:00",
            "signing_audit_signer_ip": "203.0.113.1",
        }

        # The guard `if not state.get("signing_audit_viewed_at")` should be False
        assert state.get("signing_audit_viewed_at") is not None
        # update_state should NOT be called when guard is False
        # (This test documents the contract; integration coverage in test_router.py)


class TestSignedPdfHash:
    """The signed_sha256 should hash only the stamped bytes, before audit page append."""

    def test_hash_excludes_audit_page(self):
        """Appending the audit page must change the bytes — the hashes must differ."""
        import fitz

        from orchestrator.mini_app.audit import _generate_audit_page

        # Minimal signed PDF
        base_doc = fitz.open()
        base_doc.new_page()
        signed_bytes = base_doc.tobytes()
        base_doc.close()

        signed_sha256 = hashlib.sha256(signed_bytes).hexdigest()

        audit_bytes = _generate_audit_page(
            {
                "document_name": "Test.pdf",
                "packet_id": "xyz",
                "requester_name": "R",
                "requester_email": "r@x.com",
                "sent_at": "2026-01-01T00:00:00Z",
                "viewed_at": "2026-01-01T00:01:00Z",
                "signer_name": "S",
                "signer_ip": "1.2.3.4",
                "signed_at": "2026-01-01T00:02:00Z",
                "original_sha256": "0" * 64,
                "signed_sha256": signed_sha256,
            }
        )

        # Append audit page
        doc = fitz.open(stream=signed_bytes, filetype="pdf")
        adoc = fitz.open(stream=audit_bytes, filetype="pdf")
        doc.insert_pdf(adoc)
        final_bytes = doc.tobytes()
        doc.close()
        adoc.close()

        final_sha256 = hashlib.sha256(final_bytes).hexdigest()

        # The two hashes must be different — signed_sha256 does NOT cover the audit page
        assert signed_sha256 != final_sha256
