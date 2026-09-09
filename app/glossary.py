"""Client for Palabra's glossary REST API -- a separate, plain HTTP API
from the streaming palabra_ai SDK used for translation itself. Lets the
user force specific source->target term translations (e.g. proper names,
terminology) that would otherwise come out inconsistent or wrong.

See https://docs.palabra.ai/docs/glossaries/. The exact request/response
shapes below were confirmed against the real API (no public OpenAPI spec
was available to read instead): POST to create metadata, then POST once
to /upload with the CSV -- there is no PATCH/PUT anywhere, so "editing"
an existing glossary always means delete the old one (if any) + create a
fresh one + upload its CSV once. A second upload to the same glossary_id
is rejected outright ("Glossary file was already uploaded").
"""

from __future__ import annotations

import csv
import io
import json
import urllib.error
import urllib.request
import uuid

API_BASE = "https://api.palabra.ai"


class GlossaryError(Exception):
    pass


def _request(
    method: str,
    path: str,
    api_key: str,
    *,
    json_body: dict | None = None,
    csv_bytes: bytes | None = None,
) -> dict:
    headers = {"Authorization": f"Bearer {api_key}"}
    data: bytes | None = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif csv_bytes is not None:
        # Hand-rolled multipart/form-data: no HTTP client library beyond
        # urllib is available in this app (see requirements.txt). The form
        # field must be named "glossary" and MUST carry an explicit
        # text/csv Content-Type -- confirmed empirically: without it the
        # server rejects the upload ("Glossary format must be CSV") even
        # though the file really is CSV, apparently relying on the part's
        # declared type rather than sniffing content or the filename.
        # DO NOT "improve" this to "text/csv; charset=utf-8" -- confirmed
        # empirically that the server does a literal string match on the
        # Content-Type and REJECTS that variant too, with the same
        # misleading "must be CSV" error. Plain "text/csv", nothing added.
        boundary = uuid.uuid4().hex
        body = io.BytesIO()
        body.write(f"--{boundary}\r\n".encode("ascii"))
        body.write(b'Content-Disposition: form-data; name="glossary"; filename="glossary.csv"\r\n')
        body.write(b"Content-Type: text/csv\r\n\r\n")
        body.write(csv_bytes)
        body.write(f"\r\n--{boundary}--\r\n".encode("ascii"))
        data = body.getvalue()
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"

    req = urllib.request.Request(f"{API_BASE}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(detail)["errors"][0]["detail"]
        except Exception:
            # Not the server's own structured JSON error -- e.g. a gateway
            # in front of the API answering with an HTML error page on a
            # 502/504 during a flaky connection (confirmed reproducible:
            # a >1MB CSV gets a raw HTML "413 Request Entity Too Large"
            # page, not a JSON error). Truncate before it reaches a caller
            # that might render it as-is (GlossaryDialog's status QLabel
            # would otherwise interpret stray HTML tags as rich text and
            # visibly break the dialog's layout).
            detail = detail[:300]
        raise GlossaryError(f"HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise GlossaryError(str(e.reason)) from e


def create_glossary(
    api_key: str,
    name: str,
    source_lang: str,
    target_lang: str,
    glossary_type: str = "translation",
    is_enabled: bool = True,
) -> str:
    """Creates a glossary's metadata (no entries yet -- see upload_entries).
    Returns its glossary_id."""
    resp = _request(
        "POST",
        "/saas/glossary",
        api_key,
        json_body={
            "data": {
                "name": name,
                "is_enabled": is_enabled,
                "glossary_type": glossary_type,
                "source_lang": source_lang,
                "target_lang": target_lang,
            }
        },
    )
    return resp["data"]["glossary_id"]


def upload_entries(api_key: str, glossary_id: str, pairs: list[tuple[str, str]]) -> None:
    """Uploads the CSV of term pairs for a freshly created glossary. Can
    only be called ONCE per glossary_id -- the server rejects a second
    upload to the same one (see this module's docstring)."""
    buf = io.StringIO()
    csv.writer(buf).writerows(pairs)
    _request("POST", f"/saas/glossary/{glossary_id}/upload", api_key, csv_bytes=buf.getvalue().encode("utf-8"))


def list_glossaries(api_key: str) -> list[dict]:
    """Lists every glossary on the account. All active glossaries apply
    to every translation regardless of what this app knows about locally
    (see this module's docstring: there's no per-session attachment) --
    this is the only way to find and clean up one whose glossary_id was
    lost from ~/.sts_bridge/glossary.json (a partial save failure, editing
    the same account from two machines, or a bug), since an orphaned
    glossary otherwise keeps silently rewriting every future translation
    with no way to even see it, let alone remove it, from within the app.

    page_size=100 in one request: this app's use case (a handful of
    glossaries per account at most) makes multi-page listing genuinely not
    worth the complexity -- an account with more than 100 would need to
    clean some up via the Palabra web portal instead.
    """
    resp = _request("GET", "/saas/glossaries?page_size=100", api_key)
    return resp.get("data", {}).get("items", [])


def delete_glossary(api_key: str, glossary_id: str) -> None:
    """Deletes a glossary. Treats an already-gone glossary (404) as
    success, not an error -- callers use this to clean up before
    recreating, and a glossary that's already gone (deleted by hand on
    the Palabra web portal, or lost to whatever made a bad upload attempt
    orphan one during this feature's own development) means the desired
    end state -- 'no such glossary' -- is already true."""
    try:
        _request("DELETE", f"/saas/glossary/{glossary_id}", api_key)
    except GlossaryError as e:
        if "HTTP 404" not in str(e):
            raise


def sync_glossary(
    api_key: str,
    name: str,
    source_lang: str,
    target_lang: str,
    pairs: list[tuple[str, str]],
    old_glossary_id: str | None,
) -> str | None:
    """Replaces whatever glossary currently represents this (name,
    source_lang, target_lang) with one holding exactly `pairs` -- the only
    available way to "edit" entries, since the API has no update endpoint.
    Returns the new glossary_id, or None if `pairs` is empty (old_glossary_id
    is deleted and nothing is created -- the natural way to "turn it off"
    from the user's side, since is_enabled can't be toggled after creation
    either).

    Creates and uploads the NEW glossary before deleting old_glossary_id
    (not the other way around): if the new one fails to create/upload, the
    old one -- which was still correctly representing the user's
    previously-saved list -- is left untouched instead of being deleted
    first and potentially never replaced (e.g. a network drop between
    delete and create used to leave NO glossary active for this language
    pair at all, silently, with no error surfaced until the next attempt).
    The old glossary is deleted last, best-effort: if that final delete
    fails, the new one (already live and correct) is still returned as a
    success -- the caller ends up with the right content active plus one
    harmless leftover orphan, rather than losing the successful save.
    """
    if not pairs:
        if old_glossary_id:
            delete_glossary(api_key, old_glossary_id)
        return None
    glossary_id = create_glossary(api_key, name, source_lang, target_lang)
    try:
        upload_entries(api_key, glossary_id, pairs)
    except GlossaryError as upload_error:
        # Don't leave an empty, entry-less glossary dangling if the upload
        # itself failed -- there's nothing useful an empty one does (see
        # this module's own confirmed behavior: a glossary must have its
        # file uploaded to matter), and leaving it around would silently
        # occupy this language pair's slot for any future retry that
        # doesn't know about it. Suppress (not propagate) a failure in
        # THIS cleanup attempt -- it must never replace the real upload
        # error in what the caller sees.
        try:
            delete_glossary(api_key, glossary_id)
        except GlossaryError:
            pass
        raise upload_error
    if old_glossary_id and old_glossary_id != glossary_id:
        try:
            delete_glossary(api_key, old_glossary_id)
        except GlossaryError:
            pass  # best-effort -- see docstring; the new glossary is already live and correct
    return glossary_id
