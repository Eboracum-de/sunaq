"""Incrementeller IMAP -> Nextcloud-WebDAV-Mailimport.

Zielbild:
- IMAP bleibt der maßgebliche Mail-Speicher.
- Nextcloud erhält eine flache, durchsuchbare Repräsentation pro Nachricht.
- Pro Mail wird immer eine .txt-Datei mit Headern + Body erzeugt.
- Anhangsnamen stehen immer in der TXT-Datei; Roh-.eml/Anhangsdateien sind optional.
- Kein Verzeichnis pro Mail; Ablage nach Account/Mailbox/Jahr/Monat.
- SQLite trennt Live-Sync und rückwärts laufenden historischen Backfill.

Bewusst NICHT enthalten:
- Löschungen aus IMAP nachziehen
- Moves zwischen IMAP-Ordnern auflösen
- IMAP IDLE / Push
- Mail-spezifische Felder direkt in Elasticsearch schreiben

Aufruf:
    python -m rag.mail_sync --config config.yaml

Optionale Filter:
    python -m rag.mail_sync --config config.yaml --account privat --mailbox INBOX
    python -m rag.mail_sync --config config.yaml --max-messages 50
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import imaplib
import logging
import os
import re
import shlex
import sqlite3
import ssl
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.parse import quote

import httpx
import yaml

from rag.logging_utils import get_logger
from rag.secret_env import secret_env
from rag.nextcloud_tls import nextcloud_verify_value
from rag.policy_hooks import POST_FETCH, PRE_PERSIST, apply_policy_hook
from rag.credential_store import CredentialStore, MailAccount
from rag.source_registry import register_document


log = get_logger("sync")


# ---------------------------------------------------------------------------
# Kleine Datenmodelle
# ---------------------------------------------------------------------------

@dataclass
class Attachment:
    index: int
    filename: str
    content_type: str
    data: bytes


@dataclass
class ParsedMail:
    uid: int
    date: datetime | None
    date_raw: str
    subject: str
    sender: str
    recipients: str
    cc: str
    bcc: str
    reply_to: str
    message_id: str
    in_reply_to: list[str]
    references: list[str]
    return_path: str
    received: list[str]
    authentication_results: list[str]
    arc_authentication_results: list[str]
    dkim_signatures: list[str]
    received_spf: list[str]
    body: str
    attachments: list[Attachment]


# ---------------------------------------------------------------------------
# HTML -> Text, falls eine Mail keinen text/plain-Part hat
# ---------------------------------------------------------------------------

class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in {"script", "style"}:
            self._skip += 1
        elif not self._skip and tag in {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style"} and self._skip:
            self._skip -= 1
        elif not self._skip and tag in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        value = html.unescape("".join(self.parts))
        value = value.replace("\r\n", "\n").replace("\r", "\n")
        value = re.sub(r"[ \t]+", " ", value)
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip()


def html_to_text(value: str) -> str:
    parser = _HTMLTextExtractor()
    try:
        parser.feed(value)
        parser.close()
        return parser.text()
    except Exception:
        return re.sub(r"<[^>]+>", " ", value).strip()


# ---------------------------------------------------------------------------
# Konfigurations-/Dateinamen-Helfer
# ---------------------------------------------------------------------------

def _env_secret(name: str) -> str:
    value = secret_env(str(name or ""), "")
    if not value:
        raise RuntimeError(f"Umgebungsvariable {name!r} ist nicht gesetzt")
    return value


def _config_value_or_env(cfg: dict, key: str) -> str:
    """Lese einen normalen Konfigurationswert oder <key>_env.

    Ein explizit gesetztes *_env gewinnt. Damit können Benutzername und andere
    installationsspezifische Werte aus config.yaml herausgehalten werden, ohne
    alte Konfigurationen mit z.B. ``username: demo-user`` zu brechen.
    """
    env_name = str(cfg.get(f"{key}_env") or "").strip()
    if env_name:
        value = secret_env(env_name, "")
        if not value:
            raise RuntimeError(f"Umgebungsvariable {env_name!r} ist nicht gesetzt")
        return value

    value = cfg.get(key)
    if value is None or str(value).strip() == "":
        raise RuntimeError(f"Weder {key!r} noch {key + '_env'!r} ist konfiguriert")
    return str(value)


def _parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise RuntimeError(f"Ungültiges Datum {value!r}; erwartet YYYY-MM-DD") from exc


def _subtract_years(day: date, years: int) -> date:
    if years < 0:
        raise RuntimeError("max_age_years darf nicht negativ sein")
    try:
        return day.replace(year=day.year - years)
    except ValueError:
        # 29. Februar -> 28. Februar im Zieljahr.
        return day.replace(month=2, day=28, year=day.year - years)


def _not_before(account_cfg: dict) -> date | None:
    """Ermittle die untere Importgrenze.

    ``not_before`` ist reproduzierbar und gewinnt, wenn beide Optionen gesetzt
    sind. ``max_age_years`` ist die bequeme relative Alternative.
    """
    explicit = str(account_cfg.get("not_before") or "").strip()
    if explicit:
        return _parse_iso_date(explicit)

    raw_years = account_cfg.get("max_age_years")
    if raw_years is None or str(raw_years).strip() == "":
        return None
    return _subtract_years(date.today(), int(raw_years))


def _safe_component(value: str, fallback: str = "unknown", max_len: int = 120) -> str:
    value = str(value or "").strip()
    value = value.replace("/", "_").replace("\\", "_")
    value = re.sub(r"[\x00-\x1f\x7f]+", "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value:
        value = fallback
    return value[:max_len]


def _header_text(msg: Message, name: str) -> str:
    value = msg.get(name)
    return str(value).strip() if value is not None else ""


def _header_values(msg: Message, name: str) -> list[str]:
    """Return unfolded header values without adding them to retrieval text."""
    out: list[str] = []
    for value in msg.get_all(name, []) or []:
        text = str(value or "").strip()
        if text:
            out.append(text)
    return out


def _message_ids(msg: Message, name: str) -> list[str]:
    values = msg.get_all(name, [])
    if not values:
        return []
    raw = " ".join(str(v) for v in values).strip()
    ids = re.findall(r"<[^<>]+>", raw)
    if not ids and raw:
        ids = [raw]
    out: list[str] = []
    for value in ids:
        value = str(value or "").strip()
        if value and value not in out:
            out.append(value)
    return out


def _addresses(msg: Message, name: str) -> str:
    values = msg.get_all(name, [])
    if not values:
        return ""

    rendered: list[str] = []
    for display, address in getaddresses([str(v) for v in values]):
        display = str(display or "").strip()
        address = str(address or "").strip()
        if display and address:
            rendered.append(f"{display} <{address}>")
        elif address:
            rendered.append(address)
        elif display:
            rendered.append(display)
    return ", ".join(rendered)


def _message_date(msg: Message) -> datetime | None:
    raw = _header_text(msg, "Date")
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        if dt is not None and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _decode_text_part(part: Message) -> str:
    try:
        payload = part.get_payload(decode=True)
    except Exception:
        payload = None

    if payload is None:
        raw = part.get_payload()
        return raw if isinstance(raw, str) else ""

    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Mail parsen
# ---------------------------------------------------------------------------

def parse_mail(uid: int, raw: bytes) -> ParsedMail:
    msg = BytesParser(policy=policy.default).parsebytes(raw)

    plain_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[Attachment] = []
    attachment_index = 0

    parts: Iterable[Message]
    if msg.is_multipart():
        parts = msg.walk()
    else:
        parts = [msg]

    for part in parts:
        if part.is_multipart():
            continue

        filename = str(part.get_filename() or "").strip()
        disposition = str(part.get_content_disposition() or "").lower()
        content_type = str(part.get_content_type() or "application/octet-stream")

        is_attachment = bool(filename) or disposition == "attachment"

        if is_attachment:
            attachment_index += 1
            try:
                data = part.get_payload(decode=True) or b""
            except Exception:
                data = b""
            data = apply_policy_hook(
                POST_FETCH,
                content=data,
                metadata={
                    "source": "mail_attachment",
                    "uid": uid,
                    "filename": filename,
                    "content_type": content_type,
                },
            )
            if not isinstance(data, (bytes, bytearray)):
                raise TypeError("post_fetch policy hook must return bytes content")
            data = bytes(data)

            attachments.append(
                Attachment(
                    index=attachment_index,
                    filename=_safe_component(
                        filename,
                        fallback=f"attachment-{attachment_index:02d}",
                        max_len=180,
                    ),
                    content_type=content_type,
                    data=data,
                )
            )
            continue

        if content_type == "text/plain":
            text = _decode_text_part(part).strip()
            if text:
                plain_parts.append(text)
        elif content_type == "text/html":
            text = _decode_text_part(part).strip()
            if text:
                html_parts.append(text)

    body = "\n\n".join(plain_parts).strip()
    if not body and html_parts:
        body = "\n\n".join(html_to_text(v) for v in html_parts if v).strip()

    return ParsedMail(
        uid=uid,
        date=_message_date(msg),
        date_raw=_header_text(msg, "Date"),
        subject=_header_text(msg, "Subject"),
        sender=_addresses(msg, "From"),
        recipients=_addresses(msg, "To"),
        cc=_addresses(msg, "Cc"),
        bcc=_addresses(msg, "Bcc"),
        reply_to=_addresses(msg, "Reply-To"),
        message_id=_header_text(msg, "Message-ID"),
        in_reply_to=_message_ids(msg, "In-Reply-To"),
        references=_message_ids(msg, "References"),
        return_path=_header_text(msg, "Return-Path"),
        received=_header_values(msg, "Received"),
        authentication_results=_header_values(msg, "Authentication-Results"),
        arc_authentication_results=_header_values(msg, "ARC-Authentication-Results"),
        dkim_signatures=_header_values(msg, "DKIM-Signature"),
        received_spf=_header_values(msg, "Received-SPF"),
        body=body,
        attachments=attachments,
    )


def render_mail_text(
    account: str,
    mailbox: str,
    parsed: ParsedMail,
    original_eml_path: str | None = None,
) -> str:
    if parsed.date:
        date_text = parsed.date.isoformat()
    else:
        date_text = ""

    lines = [
        "CONTENT-KIND: EMAIL",
        f"MAIL-ACCOUNT: {account}",
        f"IMAP-MAILBOX: {mailbox}",
        f"IMAP-UID: {parsed.uid}",
        f"DATE: {date_text}",
        f"FROM: {parsed.sender}",
        f"TO: {parsed.recipients}",
        f"CC: {parsed.cc}",
        f"BCC: {parsed.bcc}",
        f"REPLY-TO: {parsed.reply_to}",
        f"SUBJECT: {parsed.subject}",
        f"MESSAGE-ID: {parsed.message_id}",
        f"IN-REPLY-TO: {' '.join(parsed.in_reply_to)}",
        f"REFERENCES: {' '.join(parsed.references)}",
        f"ATTACHMENT-COUNT: {len(parsed.attachments)}",
    ]

    if original_eml_path:
        lines.append(f"ORIGINAL-EML-PATH: {original_eml_path}")

    lines.extend([
        "",
        "ATTACHMENTS:",
    ])

    if parsed.attachments:
        for attachment in parsed.attachments:
            lines.append(
                f"- {attachment.filename} ({attachment.content_type}, {len(attachment.data)} bytes)"
            )
    else:
        lines.append("- none")

    lines.extend(["", "BODY:", parsed.body or ""])
    return "\n".join(lines).rstrip() + "\n"


def render_mail_metadata(
    account: str,
    mailbox: str,
    parsed: ParsedMail,
    *,
    text_name: str,
    attachment_names: list[str],
    original_eml_path: str | None = None,
    uidvalidity: int | None = None,
    raw_message_sha256: str = "",
    raw_message_bytes: int | None = None,
    imported_at: str = "",
) -> bytes:
    """Render the shared hidden mail sidecar schema used by graph ingestion."""
    payload = {
        "schema": "nextcloud-mailmeta-v1",
        "content_kind": "email",
        "headers": {
            "message_id": parsed.message_id or None,
            "in_reply_to": list(parsed.in_reply_to),
            "references": list(parsed.references),
            "date_raw": parsed.date_raw or None,
            "date_iso": parsed.date.isoformat() if parsed.date else None,
            "subject": parsed.subject or None,
            # The built-in sync already has RFC-decoded address strings.  The
            # sidecar consumer accepts these as well as the richer object arrays
            # emitted by the legacy shell extractor.
            "from": parsed.sender or "",
            "to": parsed.recipients or "",
            "cc": parsed.cc or "",
            "bcc": parsed.bcc or "",
            "reply_to": parsed.reply_to or "",
        },
        "technical_headers": {
            "return_path": parsed.return_path or None,
            "received": list(parsed.received),
            "authentication_results": list(parsed.authentication_results),
            "arc_authentication_results": list(parsed.arc_authentication_results),
            "dkim_signature": list(parsed.dkim_signatures),
            "received_spf": list(parsed.received_spf),
        },
        "files": {
            "eml": original_eml_path,
            "text": text_name,
            "html_pdf": [],
            "readme": None,
            "attachments": list(attachment_names),
        },
        "source": {
            "account": account,
            "mailbox": mailbox,
            "imap_uid": parsed.uid,
            "imap_uidvalidity": int(uidvalidity) if uidvalidity is not None else None,
            "imported_at": imported_at or None,
        },
        "integrity": {
            "algorithm": "sha256",
            "raw_message_sha256": raw_message_sha256 or None,
            "raw_message_bytes": int(raw_message_bytes) if raw_message_bytes is not None else None,
        },
        "generator": {
            "name": "rag.mail_sync",
            "mailmeta_schema": 1,
        },
    }
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# SQLite-State
# ---------------------------------------------------------------------------

class StateDB:
    """Persistenter Live-/Backfill-Zustand pro IMAP-Mailbox.

    Das alte Schema ``mail_cursors(last_uid)`` wird gelesen, aber nicht mehr
    fortgeschrieben. Ein vorhandener alter Cursor bedeutet: alle damals
    vorhandenen UIDs bis ``last_uid`` wurden bereits importiert. Der neue
    Backfill füllt deshalb nur die Lücke zwischen diesem historischen Präfix
    und dem heutigen Mailbox-Kopf.
    """

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS mail_sync_state (
                account TEXT NOT NULL,
                mailbox TEXT NOT NULL,
                uidvalidity INTEGER NOT NULL,
                live_uid INTEGER NOT NULL,
                backfill_before_uid INTEGER NOT NULL,
                legacy_last_uid INTEGER NOT NULL DEFAULT 0,
                initialized_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (account, mailbox)
            )
            """
        )
        self.db.commit()

    def _legacy_cursor(self, account: str, mailbox: str, uidvalidity: int) -> int:
        table = self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='mail_cursors'"
        ).fetchone()
        if not table:
            return 0
        row = self.db.execute(
            "SELECT uidvalidity, last_uid FROM mail_cursors WHERE account=? AND mailbox=?",
            (account, mailbox),
        ).fetchone()
        if not row:
            return 0
        old_validity, last_uid = int(row[0]), int(row[1])
        if uidvalidity and old_validity and old_validity != uidvalidity:
            return 0
        return max(0, last_uid)

    def load_or_initialize(
        self,
        account: str,
        mailbox: str,
        uidvalidity: int,
        mailbox_max_uid: int,
        persist: bool = True,
    ) -> tuple[int, int, int]:
        row = self.db.execute(
            """
            SELECT uidvalidity, live_uid, backfill_before_uid, legacy_last_uid
            FROM mail_sync_state WHERE account=? AND mailbox=?
            """,
            (account, mailbox),
        ).fetchone()

        if row is not None:
            old_validity, live_uid, backfill_before_uid, legacy_last_uid = map(int, row)
            if not uidvalidity or not old_validity or old_validity == uidvalidity:
                return live_uid, backfill_before_uid, legacy_last_uid
            log.warning(
                "UIDVALIDITY geändert: account=%s mailbox=%s %s -> %s; initialisiere Zustand neu%s",
                account, mailbox, old_validity, uidvalidity,
                " (DRY-RUN: nicht gespeichert)" if not persist else "",
            )
            if persist:
                self.db.execute(
                    "DELETE FROM mail_sync_state WHERE account=? AND mailbox=?",
                    (account, mailbox),
                )
                self.db.commit()

        legacy_last_uid = self._legacy_cursor(account, mailbox, uidvalidity)
        live_uid = max(0, int(mailbox_max_uid))
        backfill_before_uid = live_uid + 1
        if persist:
            self.db.execute(
                """
                INSERT INTO mail_sync_state(
                    account, mailbox, uidvalidity, live_uid, backfill_before_uid,
                    legacy_last_uid, initialized_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (account, mailbox, uidvalidity, live_uid, backfill_before_uid, legacy_last_uid),
            )
            self.db.commit()
        log.info(
            "Mail-State initialisiert account=%s mailbox=%s live_uid=%s backfill_before=%s legacy_last_uid=%s",
            account, mailbox, live_uid, backfill_before_uid, legacy_last_uid,
        )
        return live_uid, backfill_before_uid, legacy_last_uid

    def save_live(self, account: str, mailbox: str, uidvalidity: int, live_uid: int) -> None:
        self.db.execute(
            """
            UPDATE mail_sync_state
            SET uidvalidity=?, live_uid=?, updated_at=CURRENT_TIMESTAMP
            WHERE account=? AND mailbox=?
            """,
            (uidvalidity, live_uid, account, mailbox),
        )
        self.db.commit()

    def save_backfill_before(
        self,
        account: str,
        mailbox: str,
        uidvalidity: int,
        before_uid: int,
    ) -> None:
        self.db.execute(
            """
            UPDATE mail_sync_state
            SET uidvalidity=?, backfill_before_uid=?, updated_at=CURRENT_TIMESTAMP
            WHERE account=? AND mailbox=?
            """,
            (uidvalidity, before_uid, account, mailbox),
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()


# ---------------------------------------------------------------------------
# Nextcloud WebDAV
# ---------------------------------------------------------------------------

class NextcloudDAV:
    def __init__(self, base_url: str, username: str, password: str, verify_tls: bool | str = True):
        self.base_url = str(base_url).rstrip("/")
        self.client = httpx.Client(
            auth=httpx.BasicAuth(username, password),
            timeout=120,
            verify=verify_tls,
            follow_redirects=True,
        )
        self._known_dirs: set[tuple[str, ...]] = set()

    def close(self) -> None:
        self.client.close()

    def _url(self, parts: Iterable[str]) -> str:
        encoded = "/".join(quote(str(part), safe="") for part in parts if str(part))
        return self.base_url + ("/" + encoded if encoded else "")

    def ensure_dir(self, parts: list[str]) -> None:
        current: list[str] = []
        for part in parts:
            current.append(part)
            key = tuple(current)
            if key in self._known_dirs:
                continue

            response = self.client.request("MKCOL", self._url(current))
            if response.status_code not in {201, 405}:
                raise RuntimeError(
                    f"WebDAV MKCOL {self._url(current)} -> "
                    f"HTTP {response.status_code}: {response.text[:500]}"
                )
            self._known_dirs.add(key)

    def put(self, parts: list[str], data: bytes, content_type: str) -> str | None:
        target = "/".join(str(part) for part in parts if str(part))
        data = apply_policy_hook(
            PRE_PERSIST,
            content=data,
            metadata={
                "source": "mail_archive",
                "target": target,
                "content_type": content_type,
            },
        )
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("pre_persist policy hook must return bytes content")
        data = bytes(data)
        url = self._url(parts)
        response = self.client.put(
            url,
            content=data,
            headers={"Content-Type": content_type},
        )
        if response.status_code not in {200, 201, 204}:
            raise RuntimeError(
                f"WebDAV PUT {url} -> "
                f"HTTP {response.status_code}: {response.text[:500]}"
            )
        # Nextcloud commonly exposes the file id directly after PUT.  Keep a
        # PROPFIND fallback so older servers can still populate the registry.
        for header in ("OC-FileId", "X-OC-FileId", "FileId"):
            value = str(response.headers.get(header) or "").strip()
            if value.isdigit():
                return value
        body = (
            '<?xml version="1.0" encoding="utf-8" ?>\n'
            '<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
            '<d:prop><oc:fileid/></d:prop></d:propfind>'
        ).encode("utf-8")
        try:
            prop = self.client.request(
                "PROPFIND", url, content=body,
                headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"},
            )
            if prop.status_code == 207:
                match = re.search(rb"<[^>]*fileid[^>]*>(\d+)</[^>]+>", prop.content)
                if match:
                    return match.group(1).decode("ascii")
        except Exception as exc:
            log.debug("mail archive fileid lookup failed for %s: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# IMAP
# ---------------------------------------------------------------------------

def _imap_connect(cfg: dict) -> imaplib.IMAP4:
    """Connect using runtime account data; legacy env-based config remains accepted."""
    host = str(cfg["host"]).strip()
    security = str(cfg.get("security") or ("tls" if cfg.get("ssl", True) else ("starttls" if cfg.get("starttls") else "plain"))).strip().lower()
    if security not in {"tls", "starttls", "plain"}:
        raise RuntimeError(f"Unbekannter IMAP security mode {security!r}")
    port = int(cfg.get("port", 993 if security == "tls" else 143))
    verify_tls = bool(cfg.get("verify_tls", True))

    context = ssl.create_default_context()
    if not verify_tls:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    if security == "tls":
        client: imaplib.IMAP4 = imaplib.IMAP4_SSL(host, port, ssl_context=context)
    else:
        client = imaplib.IMAP4(host, port)
        if security == "starttls":
            client.starttls(ssl_context=context)

    user = str(cfg.get("username") or "").strip()
    if not user:
        user = _config_value_or_env(cfg, "username")
    password = str(cfg.get("_password") or "")
    if not password:
        password_env = str(cfg.get("password_env") or "").strip()
        if not password_env:
            raise RuntimeError("IMAP credential missing")
        password = _env_secret(password_env)
    client.login(user, password)
    return client



@dataclass(frozen=True)
class MailboxSpec:
    """Selectable IMAP mailbox plus its server-advertised hierarchy delimiter."""

    name: str
    delimiter: str | None = None

    @property
    def storage_parts(self) -> tuple[str, ...]:
        if self.delimiter:
            raw_parts = [part for part in self.name.split(self.delimiter) if part]
        else:
            raw_parts = [self.name]
        return tuple(_safe_component(part, "mailbox") for part in raw_parts) or ("mailbox",)


@dataclass(frozen=True)
class MailboxInfo:
    """Mailbox returned by IMAP LIST, including special-use/selectability flags."""

    name: str
    delimiter: str | None
    flags: tuple[str, ...] = ()

    @property
    def selectable(self) -> bool:
        return "\\noselect" not in {flag.casefold() for flag in self.flags}

    @property
    def depth(self) -> int:
        if not self.delimiter:
            return 0
        return max(0, len([part for part in self.name.split(self.delimiter) if part]) - 1)

    def covered_by(self, roots: Iterable[str]) -> str:
        for root in roots:
            value = str(root or "").strip()
            if value and _is_mailbox_under_root(self.name, value, self.delimiter):
                return value
        return ""


def _imap_modified_utf7_decode(value: bytes | str) -> str:
    """Decode RFC 3501 modified UTF-7 mailbox names without extra dependencies."""
    if isinstance(value, bytes):
        text = value.decode("ascii", errors="replace")
    else:
        text = str(value)
    out: list[str] = []
    i = 0
    while i < len(text):
        if text[i] != "&":
            j = text.find("&", i)
            if j < 0:
                j = len(text)
            out.append(text[i:j])
            i = j
            continue
        j = text.find("-", i)
        if j < 0:
            out.append(text[i:])
            break
        encoded = text[i + 1:j]
        if not encoded:
            out.append("&")
        else:
            b64 = encoded.replace(",", "/")
            b64 += "=" * ((4 - len(b64) % 4) % 4)
            try:
                out.append(base64.b64decode(b64).decode("utf-16-be"))
            except Exception:
                # Preserve malformed server data visibly instead of dropping it.
                out.append(text[i:j + 1])
        i = j + 1
    return "".join(out)


def _imap_modified_utf7_encode(value: str) -> str:
    """Encode a Unicode mailbox name for classic IMAP4rev1 SELECT commands."""
    text = str(value)
    out: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if not buf:
            return
        raw = "".join(buf).encode("utf-16-be")
        encoded = base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",")
        out.append("&" + encoded + "-")
        buf.clear()

    for ch in text:
        code = ord(ch)
        if 0x20 <= code <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


def _imap_quote_mailbox(name: str) -> str:
    wire = _imap_modified_utf7_encode(name)
    return '"' + wire.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _imap_unquote_token(value: bytes) -> bytes:
    value = bytes(value).strip()
    if len(value) >= 2 and value[:1] == b'"' and value[-1:] == b'"':
        value = value[1:-1]
        out = bytearray()
        escaped = False
        for ch in value:
            if escaped:
                out.append(ch)
                escaped = False
            elif ch == 0x5C:  # backslash
                escaped = True
            else:
                out.append(ch)
        if escaped:
            out.append(0x5C)
        return bytes(out)
    return value


def _parse_imap_list_line(raw: bytes | bytearray | str) -> tuple[set[str], str | None, str] | None:
    """Parse the common RFC 3501 LIST response shape returned by imaplib."""
    if isinstance(raw, str):
        data = raw.encode("ascii", errors="replace")
    else:
        data = bytes(raw)
    match = re.match(
        rb'^\((?P<flags>[^)]*)\)\s+(?P<delimiter>NIL|"(?:\\.|[^"])*"|[^\s]+)\s+(?P<name>.+)$',
        data.strip(),
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    flags = {
        item.decode("ascii", errors="ignore").casefold()
        for item in match.group("flags").split()
        if item
    }
    delimiter_raw = match.group("delimiter")
    delimiter = None
    if delimiter_raw.upper() != b"NIL":
        delimiter = _imap_unquote_token(delimiter_raw).decode("ascii", errors="replace")
    mailbox_raw = _imap_unquote_token(match.group("name"))
    mailbox = _imap_modified_utf7_decode(mailbox_raw)
    return flags, delimiter, mailbox


def _is_mailbox_under_root(name: str, root: str, delimiter: str | None) -> bool:
    if name == root or (root.casefold() == "inbox" and name.casefold() == "inbox"):
        return True
    if not delimiter:
        return False
    prefix = root + delimiter
    if root.casefold() == "inbox":
        return name.casefold().startswith(prefix.casefold())
    return name.startswith(prefix)


def list_available_mailboxes(client: imaplib.IMAP4) -> list[MailboxInfo]:
    """Return all parseable mailboxes advertised by IMAP LIST.

    This is intentionally the same parser used by the sync path so the Admin UI
    shows exactly the names that can be configured as recursive roots.
    """
    typ, rows = client.list('""', '*')
    if typ != "OK":
        raise RuntimeError(f"IMAP LIST fehlgeschlagen: {typ}")

    out: list[MailboxInfo] = []
    seen: set[tuple[str, str | None]] = set()
    for row in rows or []:
        if row is None:
            continue
        if isinstance(row, tuple):
            candidate = row[-1] if row else b""
        else:
            candidate = row
        parsed = _parse_imap_list_line(candidate)
        if parsed is None:
            continue
        flags, delimiter, name = parsed
        key = (name.casefold() if name.casefold() == "inbox" else name, delimiter)
        if key in seen:
            continue
        seen.add(key)
        out.append(MailboxInfo(name=name, delimiter=delimiter, flags=tuple(sorted(flags))))

    if not out:
        raise RuntimeError("IMAP LIST lieferte keine parsebaren Mailboxen")
    return out


def discover_mailboxes(client: imaplib.IMAP4, roots: Iterable[str]) -> list[MailboxSpec]:
    """Expand configured mailbox roots recursively using IMAP LIST.

    Configured names are roots, not a static whitelist.  Selectable descendants
    below each root are included.  ``\\Noselect`` containers themselves are
    skipped, while their selectable children remain eligible.
    """
    configured = [str(root).strip() for root in roots if str(root).strip()]
    if not configured:
        configured = ["INBOX"]

    try:
        listed = list_available_mailboxes(client)
    except RuntimeError as exc:
        log.warning("%s; verwende konfigurierte Mailboxen ohne Rekursion", exc)
        return [MailboxSpec(root, None) for root in configured]

    selected: list[MailboxSpec] = []
    seen: set[str] = set()
    for root in configured:
        root_found = False
        root_has_descendant = False
        for info in listed:
            name = info.name
            delimiter = info.delimiter
            if not _is_mailbox_under_root(name, root, delimiter):
                continue
            exact = name == root or (root.casefold() == "inbox" and name.casefold() == "inbox")
            root_found = root_found or exact
            root_has_descendant = root_has_descendant or not exact
            if not info.selectable:
                continue
            key = name.casefold() if name.casefold() == "inbox" else name
            if key in seen:
                continue
            seen.add(key)
            selected.append(MailboxSpec(name=name, delimiter=delimiter))
        # Some servers hide special mailboxes from LIST. Preserve explicit roots
        # only if LIST showed neither that root nor descendants below it.
        if not root_found and not root_has_descendant:
            key = root.casefold() if root.casefold() == "inbox" else root
            if key not in seen:
                seen.add(key)
                selected.append(MailboxSpec(root, None))

    log.info(
        "IMAP mailbox roots=%s expanded=%s",
        configured,
        [spec.name for spec in selected],
    )
    return selected


def _uidvalidity(client: imaplib.IMAP4) -> int:
    try:
        _, values = client.response("UIDVALIDITY")
        if values and values[0]:
            match = re.search(rb"\d+", values[0])
            if match:
                return int(match.group(0))
    except Exception:
        log.debug("Could not read IMAP UIDVALIDITY", exc_info=True)
    return 0


_IMAP_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def _imap_date(value: date) -> str:
    return f"{value.day:02d}-{_IMAP_MONTHS[value.month - 1]}-{value.year:04d}"


def _all_uids(client: imaplib.IMAP4, not_before: date | None = None) -> list[int]:
    # IMAP SINCE bezieht sich auf INTERNALDATE und filtert damit bereits auf
    # dem Mailserver. Der Date:-Header einer Mail ist dafür nicht maßgeblich.
    if not_before is None:
        typ, data = client.uid("search", None, "ALL")
    else:
        typ, data = client.uid("search", None, "SINCE", _imap_date(not_before))

    if typ != "OK":
        raise RuntimeError(f"IMAP UID SEARCH fehlgeschlagen: {typ} {data}")
    if not data or not data[0]:
        return []
    return sorted(int(value) for value in data[0].split() if value.isdigit())


def _fetch_raw(client: imaplib.IMAP4, uid: int) -> bytes:
    typ, data = client.uid("fetch", str(uid), "(BODY.PEEK[])")
    if typ != "OK":
        raise RuntimeError(f"IMAP FETCH UID {uid} fehlgeschlagen: {typ} {data}")

    for item in data or []:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
            return bytes(item[1])

    raise RuntimeError(f"IMAP FETCH UID {uid} lieferte keinen Nachrichtenkörper")


# ---------------------------------------------------------------------------
# Ein Mailbox-Lauf
# ---------------------------------------------------------------------------

def _mail_base(parsed: ParsedMail) -> tuple[str, str, str]:
    if parsed.date:
        dt = parsed.date
        year = f"{dt.year:04d}"
        month = f"{dt.month:02d}"
        stamp = dt.strftime("%Y%m%d-%H%M%S")
    else:
        year = "undated"
        month = "00"
        stamp = "undated"

    return year, month, f"{stamp}_{parsed.uid}"


def sync_mailbox(
    account_cfg: dict,
    mailbox: str,
    state: StateDB,
    dav: NextcloudDAV,
    root_parts: list[str],
    eml_root_parts: list[str],
    cli_max_messages: int | None,
    dry_run: bool,
    *,
    mailbox_parts: Iterable[str] | None = None,
    client: imaplib.IMAP4 | None = None,
) -> tuple[int, int, set[tuple[str, ...]]]:
    account_name = _safe_component(str(account_cfg["name"]), "mail")
    state_account = str(account_cfg.get("_state_key") or account_name)
    storage_mailbox_parts = list(mailbox_parts or [_safe_component(mailbox, "mailbox")])

    own_client = client is None
    client = client or _imap_connect(account_cfg)
    imported = 0
    touched_dirs: set[tuple[str, ...]] = set()

    try:
        typ, _ = client.select(_imap_quote_mailbox(mailbox), readonly=True)
        if typ != "OK":
            raise RuntimeError(f"IMAP SELECT {mailbox!r} fehlgeschlagen")

        validity = _uidvalidity(client)
        cutoff = _not_before(account_cfg)

        # Ungefilterte Liste nur für den aktuellen Mailbox-Kopf. Die zweite
        # Suche wird vom Server per SINCE auf den gewünschten Zeitraum begrenzt.
        every_uid = _all_uids(client)
        eligible_uids = _all_uids(client, cutoff) if cutoff else every_uid
        mailbox_max_uid = every_uid[-1] if every_uid else 0

        live_uid, backfill_before_uid, legacy_last_uid = state.load_or_initialize(
            state_account, mailbox, validity, mailbox_max_uid, persist=not dry_run
        )

        live_pending = [uid for uid in eligible_uids if uid > live_uid]
        backfill_pending = [
            uid for uid in eligible_uids
            if legacy_last_uid < uid < backfill_before_uid
        ]
        backfill_pending.sort(reverse=True)

        configured_max = int(account_cfg.get("max_messages_per_run", 250))
        max_messages = cli_max_messages if cli_max_messages is not None else configured_max
        remaining = max_messages if max_messages > 0 else None

        # Neue Nachrichten zuerst, chronologisch vorwärts. Den Rest des Budgets
        # verwendet derselbe Lauf für den historischen Backfill (neu -> alt).
        live_batch = live_pending if remaining is None else live_pending[:remaining]
        if remaining is not None:
            remaining -= len(live_batch)
        backfill_batch = backfill_pending if remaining is None else backfill_pending[:max(0, remaining)]

        available = len(live_pending) + len(backfill_pending)
        log.info(
            "account=%s mailbox=%s uidvalidity=%s cutoff=%s live_uid=%s "
            "backfill_before=%s legacy_last_uid=%s live_neu=%s backfill_offen=%s dieser_lauf=%s",
            account_name,
            mailbox,
            validity,
            cutoff.isoformat() if cutoff else "-",
            live_uid,
            backfill_before_uid,
            legacy_last_uid,
            len(live_pending),
            len(backfill_pending),
            len(live_batch) + len(backfill_batch),
        )

        store_eml = bool(account_cfg.get("store_eml", False))
        store_attachments = bool(account_cfg.get("store_attachments", True))

        def import_one(uid: int, mode: str) -> None:
            nonlocal imported
            raw = _fetch_raw(client, uid)
            raw = apply_policy_hook(
                POST_FETCH,
                content=raw,
                metadata={
                    "source": "imap_message",
                    "account": account_name,
                    "mailbox": mailbox,
                    "uid": uid,
                },
            )
            if not isinstance(raw, (bytes, bytearray)):
                raise TypeError("post_fetch policy hook must return bytes content")
            raw = bytes(raw)
            raw_message_sha256 = hashlib.sha256(raw).hexdigest()
            imported_at = datetime.now(timezone.utc).isoformat()
            parsed = parse_mail(uid, raw)
            year, month, base = _mail_base(parsed)
            subject_part = _safe_component(parsed.subject, "", 90)
            mail_dir_name = _safe_component(
                f"{base}_{subject_part}" if subject_part else base,
                base,
                180,
            )

            month_directory = root_parts + [account_name, *storage_mailbox_parts, year, month]
            directory = month_directory + [mail_dir_name]
            txt_name = "mail.txt"
            sidecar_name = ".mailmeta.json"

            eml_path_parts = eml_root_parts + [
                account_name, *storage_mailbox_parts, year, month, mail_dir_name, "message.eml"
            ]
            original_eml_path = "/" + "/".join(eml_path_parts) if store_eml else None
            txt = render_mail_text(
                account_name,
                mailbox,
                parsed,
                original_eml_path=original_eml_path,
            ).encode("utf-8")
            attachment_names = [
                f"a{attachment.index:02d}_{_safe_component(attachment.filename, 'attachment', 180)}"
                for attachment in parsed.attachments
            ] if store_attachments else []
            sidecar = render_mail_metadata(
                account_name,
                mailbox,
                parsed,
                text_name=txt_name,
                attachment_names=attachment_names,
                original_eml_path=original_eml_path,
                uidvalidity=validity,
                raw_message_sha256=raw_message_sha256,
                raw_message_bytes=len(raw),
                imported_at=imported_at,
            )

            if not dry_run:
                dav.ensure_dir(directory)
                txt_id = dav.put(directory + [txt_name], txt, "text/plain; charset=utf-8")
                if txt_id:
                    register_document(f"files:{txt_id}", "mail_archive", source_path="/".join(directory + [txt_name]), classification_source="mail_import")
                dav.put(directory + [sidecar_name], sidecar, "application/json; charset=utf-8")
                touched_dirs.add(tuple(month_directory))

                if store_eml:
                    dav.ensure_dir(eml_path_parts[:-1])
                    eml_id = dav.put(eml_path_parts, raw, "message/rfc822")
                    if eml_id:
                        register_document(f"files:{eml_id}", "mail_archive", source_path="/".join(eml_path_parts), classification_source="mail_import")

                if store_attachments:
                    for attachment, attachment_name in zip(parsed.attachments, attachment_names):
                        attachment_id = dav.put(
                            directory + [attachment_name],
                            attachment.data,
                            attachment.content_type or "application/octet-stream",
                        )
                        if attachment_id:
                            register_document(
                                f"files:{attachment_id}", "mail_archive",
                                source_path="/".join(directory + [attachment_name]),
                                classification_source="mail_import",
                            )

                if mode == "live":
                    state.save_live(state_account, mailbox, validity, uid)
                else:
                    # Nächster Backfill darf nur kleinere UIDs betrachten.
                    state.save_backfill_before(state_account, mailbox, validity, uid)

            imported += 1
            log.info(
                "importiert mode=%s account=%s mailbox=%s uid=%s subject=%r attachments=%s bytes=%s%s",
                mode,
                account_name,
                mailbox,
                uid,
                parsed.subject[:100],
                len(parsed.attachments),
                len(raw),
                " DRY-RUN" if dry_run else "",
            )

        for uid in live_batch:
            import_one(uid, "live")
        for uid in backfill_batch:
            import_one(uid, "backfill")

        return imported, available, touched_dirs

    finally:
        if own_client:
            try:
                client.logout()
            except Exception:
                log.debug("IMAP logout failed after mailbox sync", exc_info=True)


# ---------------------------------------------------------------------------
# Optionale Post-Sync-Hooks
# ---------------------------------------------------------------------------

def _run_process(
    argv: list[str],
    *,
    timeout_seconds: int,
    fail_on_error: bool,
    label: str,
) -> bool:
    """Starte einen konfigurierten Nachlauf und protokolliere das Ergebnis."""
    log.info("post_sync %s: %s", label, " ".join(shlex.quote(v) for v in argv))

    try:
        result = subprocess.run(
            argv,
            text=True,
            capture_output=True,
            timeout=timeout_seconds if timeout_seconds > 0 else None,
            check=False,
        )
    except Exception as exc:
        if fail_on_error:
            raise RuntimeError(f"post_sync {label} konnte nicht gestartet werden: {exc}") from exc
        log.warning("post_sync %s fehlgeschlagen: %s", label, exc)
        return False

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    if stdout:
        log.info("post_sync %s stdout:\n%s", label, stdout[-8000:])
    if stderr:
        log.warning("post_sync %s stderr:\n%s", label, stderr[-8000:])

    if result.returncode != 0:
        message = f"post_sync {label} endete mit Exit-Code {result.returncode}"
        if fail_on_error:
            raise RuntimeError(message)
        log.warning(message)
        return False

    log.info("post_sync %s erfolgreich", label)
    return True


def _occ_command(cfg: dict, occ_args: list[str]) -> list[str]:
    """Baue lokalen oder SSH-basierten OCC-Aufruf ohne shell=True."""
    mode = str(cfg.get("mode", "local")).strip().lower()
    run_as = str(cfg.get("run_as", "")).strip()
    php = str(cfg.get("php", "php"))
    occ = str(cfg.get("occ", "occ"))

    remote_or_local: list[str] = []
    if run_as:
        remote_or_local.extend(["sudo", "-u", run_as])
    remote_or_local.extend([php, occ])
    remote_or_local.extend(occ_args)

    if mode == "local":
        return remote_or_local

    if mode == "ssh":
        host = str(cfg.get("host", "")).strip()
        if not host:
            raise RuntimeError("mail.post_sync.fulltextsearch.host fehlt für mode=ssh")
        ssh_options = [str(v) for v in (cfg.get("ssh_options") or [])]
        remote_command = " ".join(shlex.quote(v) for v in remote_or_local)
        return ["ssh", *ssh_options, host, remote_command]

    raise RuntimeError(
        "mail.post_sync.fulltextsearch.mode muss 'local' oder 'ssh' sein"
    )


def run_fulltextsearch_hook(
    mail_cfg: dict,
    nextcloud_user: str,
    touched_dirs: set[tuple[str, ...]],
) -> bool:
    hook = ((mail_cfg.get("post_sync") or {}).get("fulltextsearch") or {})
    if not bool(hook.get("enabled", False)):
        return True
    if not touched_dirs:
        log.info("post_sync fulltextsearch: keine neuen Pfade")
        return True

    timeout_seconds = int(hook.get("timeout_seconds", 1800))
    fail_on_error = bool(hook.get("fail_on_error", False))
    no_readline = bool(hook.get("no_readline", True))

    # Ein OCC-Lauf pro tatsächlich beschriebenem Jahr/Monat-Verzeichnis.
    # Der Files-Provider erwartet den Pfad relativ zum Files-Root des Users,
    # z.B. /Mailarchiv/main/INBOX/2026/08.
    paths = sorted({"/" + "/".join(parts) for parts in touched_dirs})

    all_ok = True
    for path in paths:
        options = {
            "user": nextcloud_user,
            "providers": ["files"],
            "path": path,
        }
        occ_args = ["fulltextsearch:index"]
        if no_readline:
            occ_args.append("--no-readline")
        occ_args.append(json.dumps(options, ensure_ascii=False, separators=(",", ":")))

        argv = _occ_command(hook, occ_args)
        ok = _run_process(
            argv,
            timeout_seconds=timeout_seconds,
            fail_on_error=fail_on_error,
            label=f"fulltextsearch path={path}",
        )
        all_ok = all_ok and ok

    return all_ok


def run_qdrant_hook(mail_cfg: dict) -> bool:
    """Optionaler generischer Nachlauf, typischerweise `python -m rag.sync`."""
    hook = ((mail_cfg.get("post_sync") or {}).get("qdrant") or {})
    if not bool(hook.get("enabled", False)):
        return True

    command = hook.get("command") or []
    if isinstance(command, str):
        # Absichtlich kein shell=True: ein einzelner String ist daher nur ein
        # Programmname. Für Argumente bitte YAML-Liste verwenden.
        command = [command]
    argv = [str(v) for v in command]
    if not argv:
        raise RuntimeError("mail.post_sync.qdrant.command ist leer")

    return _run_process(
        argv,
        timeout_seconds=int(hook.get("timeout_seconds", 3600)),
        fail_on_error=bool(hook.get("fail_on_error", False)),
        label="qdrant",
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _runtime_account_cfg(account: MailAccount, password: str) -> dict:
    return {
        "name": account.name,
        "host": account.host,
        "port": account.port,
        "security": account.security,
        "verify_tls": account.verify_tls,
        "username": account.username,
        "_password": password,
        "mailboxes": list(account.mailboxes),
        "max_messages_per_run": account.max_messages_per_run,
        "not_before": account.not_before,
        "store_eml": account.store_eml,
        "store_attachments": account.store_attachments,
        "_state_key": f"{account.canonical_user_id}:{account.account_id}",
    }


def probe_mailboxes(account: MailAccount, password: str) -> list[MailboxInfo]:
    """Login to an IMAP account and return its advertised mailbox catalogue."""
    client: imaplib.IMAP4 | None = None
    try:
        client = _imap_connect(_runtime_account_cfg(account, password))
        return list_available_mailboxes(client)
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                log.debug("IMAP logout failed after mailbox probe", exc_info=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Incrementeller admin-konfigurierter IMAP -> Nextcloud Mailimport"
    )
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--user", help="Nur diesen kanonischen Nextcloud-Login oder die canonical_user_id importieren")
    parser.add_argument("--account", help="Nur diese Account-ID oder diesen Account-Namen importieren")
    parser.add_argument("--mailbox", help="Nur diesen IMAP-Ordner als rekursive Wurzel importieren")
    parser.add_argument("--max-messages", type=int, help="Limit pro Mailbox für diesen Lauf")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(
        getattr(logging, os.getenv("HTTPX_LOG_LEVEL", "WARNING").upper(), logging.WARNING)
    )

    config_path = Path(args.config).resolve()
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    mail_cfg = cfg.get("mail") or {}

    if not bool(mail_cfg.get("enabled", False)):
        log.info("mail.enabled=false; nichts zu tun")
        return 0

    state_path = Path(mail_cfg.get("state_file", "mail_state.sqlite"))
    if not state_path.is_absolute():
        state_path = config_path.parent / state_path

    auth_cfg = cfg.get("auth") or {}
    acl_cfg = cfg.get("acl") or {}
    store_path = str(
        auth_cfg.get("credential_store") or acl_cfg.get("credential_store") or "runtime/users.sqlite"
    )
    store = CredentialStore(store_path)
    accounts = store.list_mail_accounts(enabled_only=True)
    if not accounts:
        log.info("Keine aktiven benutzerbezogenen Mailkonten konfiguriert")
        return 0

    state = StateDB(state_path)
    total_imported = 0
    had_error = False
    all_fts_ok = True
    verify_nextcloud = nextcloud_verify_value(cfg, "acl")

    try:
        for account in accounts:
            canonical = store.get_canonical_user(account.canonical_user_id)
            if canonical is None or not canonical.enabled:
                continue
            if args.user and args.user not in {canonical.canonical_user_id, canonical.nextcloud_login}:
                continue
            if args.account and args.account not in {account.account_id, account.name}:
                continue

            nc_credential = store.get_nextcloud_credential_for_canonical_user(canonical.canonical_user_id)
            mail_credential = store.get_mail_secret(account)
            if nc_credential is None:
                log.error(
                    "Mail-Sync übersprungen user=%s account=%s: kein aktuelles Nextcloud-App-Passwort",
                    canonical.nextcloud_login, account.name,
                )
                had_error = True
                continue
            if mail_credential is None:
                log.error(
                    "Mail-Sync übersprungen user=%s account=%s: IMAP-Credential fehlt",
                    canonical.nextcloud_login, account.name,
                )
                had_error = True
                continue

            server = canonical.nextcloud_server.rstrip("/")
            if not server:
                log.error("Mail-Sync übersprungen user=%s: Nextcloud-Server fehlt", canonical.nextcloud_login)
                had_error = True
                continue
            dav_url = f"{server}/remote.php/dav/files/{quote(nc_credential.username, safe='')}"
            root = account.target_path.strip("/")
            root_parts = [_safe_component(part) for part in root.split("/") if part]
            eml_root = (account.eml_target_path or account.target_path).strip("/")
            eml_root_parts = [_safe_component(part) for part in eml_root.split("/") if part]
            if not root_parts:
                log.error("Mail-Sync übersprungen user=%s account=%s: Zielpfad fehlt", canonical.nextcloud_login, account.name)
                had_error = True
                continue

            runtime_cfg = _runtime_account_cfg(account, mail_credential.secret)
            dav = NextcloudDAV(
                dav_url, nc_credential.username, nc_credential.secret, verify_tls=verify_nextcloud
            )
            touched_dirs: set[tuple[str, ...]] = set()
            imported_for_account = 0
            imap_client: imaplib.IMAP4 | None = None
            try:
                imap_client = _imap_connect(runtime_cfg)
                roots = [args.mailbox] if args.mailbox else list(account.mailboxes)
                mailbox_specs = discover_mailboxes(imap_client, roots)
                for spec in mailbox_specs:
                    imported, available, mailbox_touched_dirs = sync_mailbox(
                        runtime_cfg,
                        spec.name,
                        state,
                        dav,
                        root_parts,
                        eml_root_parts,
                        args.max_messages,
                        args.dry_run,
                        mailbox_parts=spec.storage_parts,
                        client=imap_client,
                    )
                    imported_for_account += imported
                    total_imported += imported
                    touched_dirs.update(mailbox_touched_dirs)
                    log.info(
                        "fertig user=%s account=%s mailbox=%s importiert=%s ursprünglich_neu=%s",
                        canonical.nextcloud_login, account.name, spec.name, imported, available,
                    )
            except Exception as exc:
                had_error = True
                log.exception(
                    "Mail-Sync fehlgeschlagen user=%s account=%s: %s",
                    canonical.nextcloud_login, account.name, exc,
                )
            finally:
                if imap_client is not None:
                    try:
                        imap_client.logout()
                    except Exception:
                        log.debug(
                            "IMAP logout failed user=%s account=%s",
                            canonical.nextcloud_login,
                            account.name,
                            exc_info=True,
                        )
                dav.close()

            if not args.dry_run and imported_for_account > 0:
                fts_ok = run_fulltextsearch_hook(mail_cfg, nc_credential.username, touched_dirs)
                all_fts_ok = all_fts_ok and fts_ok
    finally:
        state.close()

    log.info("GESAMT importiert=%s%s", total_imported, " DRY-RUN" if args.dry_run else "")

    if not args.dry_run and total_imported > 0:
        if all_fts_ok:
            run_qdrant_hook(mail_cfg)
        else:
            log.warning("Qdrant-Nachlauf nicht gestartet, weil mindestens ein FullTextSearch-Hook fehlschlug")

    return 1 if had_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
