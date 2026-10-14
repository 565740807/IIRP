"""Disposable network and page operations; facts are published by the supervisor."""
import base64
import json
from hashlib import sha256

from iirp.config import settings
from iirp.jobs.operations import ProviderFailure
from iirp.messages import msg
from iirp.political.config import policy


def operation(kind, target):
    if kind in ("political_page", "political_manifest"):
        version = policy()["collection"]["parser_version"]
        if target.get("parser_version", version) != version:
            # The fenced publisher retires obsolete jobs without replacing facts.
            # Do not spend the OCR lane reading or recognizing their old pages.
            return {"page_count": 0} if kind == "political_manifest" else {"page_result": {"rows": []}}
        from iirp.political.parse import parse_pdf_page, parse_senate_html
        digest = target["source_hash"]
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError(msg("storage.reference_mismatch"))
        payload = (settings().runtime_dir / "objects" / digest[:2] / digest).read_bytes()
        if sha256(payload).hexdigest() != digest:
            raise ValueError(msg("storage.hash_mismatch"))
        try:
            if kind == "political_manifest":
                from iirp.political.parse import inspect_pdf
                return {"page_count":1 if target.get("media_type") == "text/html" else inspect_pdf(payload)}
            if target.get("media_type") == "text/html":
                result = {"rows":parse_senate_html(payload), "ocr":False, "seconds":0,
                          "raw_text":payload.decode("utf-8", errors="replace")}
            else:
                result = parse_pdf_page(payload, target["page"], target["source"])
            return json.loads(json.dumps({"page_result":result}, default=str))
        except Exception as exc:
            if kind == "political_manifest":
                return {"page_count":0,"parse_error":msg("political.parse_failed", error=type(exc).__name__)}
            return {"page_result":{"rows":[], "ocr":False, "seconds":0, "raw_text":None,
                                   "error":msg("political.parse_failed", error=type(exc).__name__)}}
    from iirp.political.sources import SourceUnavailable, discover_page
    from iirp.political.transport import Transport
    documents = []
    def retain(url, payload, media_type):
        if not payload:
            return
        documents.append({"url":url, "payload":base64.b64encode(payload).decode(),
                          "encoding":"base64", "media_type":media_type})
    transport = Transport()
    try:
        if kind == "political_index":
            discovered = discover_page(target["source"], transport.fetch, retain=retain,
                                       cursor=target.get("cursor"), max_pages=policy()["sources"]["index_pages_per_task"])
            error = discovered.get("partial_error")
            if error:
                discovered["unavailable"] = error.get("message") or msg("political.source_unavailable",
                    reason=msg("political.reason."+error["reason"]),status=error.get("status"))
            return {**discovered, "source_documents":documents}
        url = target["url"]
        response = transport.fetch("GET", url)
        # Redirects must also pass the official-host validation and shared budget.
        from urllib.parse import urljoin
        for _ in range(5):
            if response.status_code not in (301, 302, 303, 307, 308):
                break
            url = urljoin(url, response.headers["location"])
            response = transport.fetch("GET", url)
        response.raise_for_status()
        payload = response.content
        media = "text/html" if "html" in response.headers.get("content-type", "") else "application/pdf"
        retain(target["url"], payload, media)
        # Publish the immutable original before any PDF inspection. Even a
        # crashing or timed-out inspector must leave the downloaded file intact.
        return {"page_count": 1 if media == "text/html" else 0, "media_type": media,
                "needs_manifest": media != "text/html", "source_documents": documents}
    except (SourceUnavailable, ProviderFailure, http_error()) as exc:
        error = str(exc) if isinstance(exc, ProviderFailure) else msg(
            "political.source_unavailable", reason=msg("political.reason."+getattr(exc,"reason", "http_error")),
            status=getattr(exc,"status_code", None))
        return {"unavailable":error, "source_documents":documents}
    finally:
        transport.close()


def http_error():
    import httpx
    return httpx.HTTPError
