"""Server-side trusted-client, identity and per-user configuration store.

Reversible Nextcloud/IMAP credentials and Nextcloud Login Flow poll tokens
are encrypted with AES-256-GCM.  The master key is kept outside SQLite; provider
client API keys remain one-way SHA-256 digests. Callers use CredentialStore so
secret handling stays centralized and future key backends can be swapped without
changing retrieval, mail or web-research code.

Two identities are deliberately separated:

* ``client_id::external_user_id`` is a frontend-scoped transport identity.
* ``(nextcloud_server, nextcloud_login)`` is the canonical user identity.

Bindings connect both. User-specific mail/web settings attach only to the
canonical Nextcloud identity, so the same user receives the same settings from
multiple trusted frontends without allowing one frontend to reuse another
frontend's Nextcloud credential.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit, urlunsplit

from rag.secret_crypto import SecretCrypto, is_encrypted, key_file_status


_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_SCOPE_SEPARATOR = "::"
_CANONICAL_CREDENTIAL_PREFIX = "canonical-user:"


def scope_identity(client_id: str, external_user_id: str) -> str:
    """Return the internal credential lookup key for one trusted client/user pair."""
    client = str(client_id or "").strip()
    user = str(external_user_id or "").strip()
    if not client or not _CLIENT_ID_RE.fullmatch(client):
        raise ValueError("invalid client_id")
    if not user:
        raise ValueError("external_user_id is required")
    return f"{client}{_SCOPE_SEPARATOR}{user}"


def split_scoped_identity(value: str) -> tuple[str, str] | None:
    text = str(value or "").strip()
    if _SCOPE_SEPARATOR not in text:
        return None
    client, user = text.split(_SCOPE_SEPARATOR, 1)
    if not client or not user:
        return None
    return client, user


def normalize_nextcloud_server(value: str) -> str:
    """Normalize the canonical Nextcloud base URL without changing a sub-path."""
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    parts = urlsplit(text)
    if not parts.scheme or not parts.netloc:
        return text
    # Host/scheme are case-insensitive.  Keep a possible Nextcloud sub-path.
    netloc = parts.netloc.lower()
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), netloc, path, "", ""))


def canonical_credential_owner(canonical_user_id: str) -> str:
    user_id = str(canonical_user_id or "").strip()
    if not user_id:
        raise ValueError("canonical_user_id is required")
    return _CANONICAL_CREDENTIAL_PREFIX + user_id


@dataclass(frozen=True)
class StoredCredential:
    rag_user_id: str
    service: str
    account_id: str
    server: str
    username: str
    secret: str


@dataclass(frozen=True)
class ProviderClient:
    client_id: str
    name: str
    enabled: bool
    created_at: float
    updated_at: float
    last_used_at: float | None


@dataclass(frozen=True)
class CanonicalUser:
    canonical_user_id: str
    nextcloud_server: str
    nextcloud_login: str
    enabled: bool
    created_at: float
    updated_at: float
    last_seen_at: float
    findings_curation_enabled: bool = False


@dataclass(frozen=True)
class IdentityBinding:
    rag_user_id: str
    canonical_user_id: str
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class CurationSession:
    session_id_hash: str
    canonical_user_id: str
    nextcloud_server: str
    nextcloud_login: str
    app_password: str
    csrf_token: str
    state: str
    created_at: float
    expires_at: float
    last_seen_at: float


@dataclass(frozen=True)
class MailAccount:
    account_id: str
    canonical_user_id: str
    name: str
    enabled: bool
    host: str
    port: int
    security: str
    verify_tls: bool
    username: str
    mailboxes: tuple[str, ...]
    max_messages_per_run: int
    not_before: str
    store_eml: bool
    store_attachments: bool
    target_path: str
    eml_target_path: str
    created_at: float
    updated_at: float
    has_secret: bool = False


@dataclass(frozen=True)
class UserWebSettings:
    canonical_user_id: str
    enabled: bool
    archive_enabled: bool
    target_path: str
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class UserChatSettings:
    canonical_user_id: str
    enabled: bool
    target_path: str
    updated_at: float


@dataclass(frozen=True)
class UserModelSettings:
    canonical_user_id: str
    default_model_id: str
    allowed_model_ids: tuple[str, ...]
    updated_at: float


@dataclass(frozen=True)
class ContactSyncSettings:
    canonical_user_id: str
    enabled: bool
    include_addressbooks: tuple[str, ...]
    exclude_addressbooks: tuple[str, ...]
    last_sync_at: float | None
    last_status: str
    contacts_seen: int
    contacts_written: int
    error_count: int
    last_error: str
    created_at: float
    updated_at: float


class CredentialStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.is_absolute():
            self.path = Path(__file__).resolve().parent.parent / self.path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._crypto = SecretCrypto.from_environment()
        self._init_db()

    @staticmethod
    def _credential_aad(rag_user_id: str, service: str, account_id: str) -> str:
        return f"credential|{rag_user_id}|{service}|{account_id}"

    @staticmethod
    def _flow_aad(flow_id: str, rag_user_id: str) -> str:
        return f"nextcloud-flow|{flow_id}|{rag_user_id}"

    @staticmethod
    def _curation_session_aad(session_id_hash: str, canonical_user_id: str) -> str:
        return f"curation-session|{session_id_hash}|{canonical_user_id}"

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=15)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=15000")
        return con

    @staticmethod
    def _key_hash(api_key: str) -> str:
        return hashlib.sha256(str(api_key or "").encode("utf-8")).hexdigest()

    def _init_db(self) -> None:
        with self._connect() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS credentials (
                    rag_user_id TEXT NOT NULL,
                    service TEXT NOT NULL,
                    account_id TEXT NOT NULL DEFAULT 'primary',
                    server TEXT NOT NULL DEFAULT '',
                    username TEXT NOT NULL,
                    secret TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (rag_user_id, service, account_id)
                );
                CREATE TABLE IF NOT EXISTS nextcloud_login_flows (
                    flow_id TEXT PRIMARY KEY,
                    rag_user_id TEXT NOT NULL,
                    poll_endpoint TEXT NOT NULL,
                    poll_token TEXT NOT NULL,
                    login_url TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS provider_clients (
                    client_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    api_key_hash TEXT NOT NULL UNIQUE,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    last_used_at REAL
                );
                CREATE TABLE IF NOT EXISTS canonical_users (
                    canonical_user_id TEXT PRIMARY KEY,
                    nextcloud_server TEXT NOT NULL,
                    nextcloud_login TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    UNIQUE(nextcloud_server, nextcloud_login)
                );
                CREATE TABLE IF NOT EXISTS identity_bindings (
                    rag_user_id TEXT PRIMARY KEY,
                    canonical_user_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY(canonical_user_id) REFERENCES canonical_users(canonical_user_id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_identity_bindings_canonical
                    ON identity_bindings(canonical_user_id);
                CREATE TABLE IF NOT EXISTS curation_sessions (
                    session_id_hash TEXT PRIMARY KEY,
                    canonical_user_id TEXT NOT NULL,
                    nextcloud_server TEXT NOT NULL,
                    nextcloud_login TEXT NOT NULL,
                    app_password TEXT NOT NULL,
                    csrf_token TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT 'active',
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    FOREIGN KEY(canonical_user_id) REFERENCES canonical_users(canonical_user_id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_curation_sessions_user
                    ON curation_sessions(canonical_user_id,state,expires_at);
                CREATE TABLE IF NOT EXISTS mail_accounts (
                    account_id TEXT PRIMARY KEY,
                    canonical_user_id TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT 'primary',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    host TEXT NOT NULL,
                    port INTEGER NOT NULL DEFAULT 993,
                    security TEXT NOT NULL DEFAULT 'tls',
                    verify_tls INTEGER NOT NULL DEFAULT 1,
                    username TEXT NOT NULL,
                    mailboxes_json TEXT NOT NULL DEFAULT '["INBOX"]',
                    max_messages_per_run INTEGER NOT NULL DEFAULT 50,
                    not_before TEXT NOT NULL DEFAULT '',
                    store_eml INTEGER NOT NULL DEFAULT 0,
                    store_attachments INTEGER NOT NULL DEFAULT 0,
                    target_path TEXT NOT NULL,
                    eml_target_path TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY(canonical_user_id) REFERENCES canonical_users(canonical_user_id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_mail_accounts_user
                    ON mail_accounts(canonical_user_id, enabled);
                CREATE TABLE IF NOT EXISTS mail_archive_roots (
                    target_path TEXT PRIMARY KEY,
                    first_seen_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS user_web_settings (
                    canonical_user_id TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    archive_enabled INTEGER NOT NULL DEFAULT 1,
                    target_path TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY(canonical_user_id) REFERENCES canonical_users(canonical_user_id)
                        ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS user_chat_settings (
                    canonical_user_id TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    target_path TEXT NOT NULL DEFAULT 'SunaQ-Chats',
                    updated_at REAL NOT NULL,
                    FOREIGN KEY(canonical_user_id) REFERENCES canonical_users(canonical_user_id)
                        ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS chat_archive_roots (
                    target_path TEXT PRIMARY KEY,
                    first_seen_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS user_model_settings (
                    canonical_user_id TEXT PRIMARY KEY,
                    default_model_id TEXT NOT NULL DEFAULT '',
                    allowed_model_ids_json TEXT NOT NULL DEFAULT '[]',
                    updated_at REAL NOT NULL,
                    FOREIGN KEY(canonical_user_id) REFERENCES canonical_users(canonical_user_id)
                        ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS web_archive_roots (
                    target_path TEXT PRIMARY KEY,
                    first_seen_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS contact_sync_settings (
                    canonical_user_id TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    include_addressbooks_json TEXT NOT NULL DEFAULT '[]',
                    exclude_addressbooks_json TEXT NOT NULL DEFAULT '[]',
                    last_sync_at REAL,
                    last_status TEXT NOT NULL DEFAULT 'never',
                    contacts_seen INTEGER NOT NULL DEFAULT 0,
                    contacts_written INTEGER NOT NULL DEFAULT 0,
                    error_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY(canonical_user_id) REFERENCES canonical_users(canonical_user_id)
                        ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS store_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

            # Forward-compatible mail schema migration. CREATE TABLE IF NOT EXISTS
            # does not add columns to long-lived installations.
            mail_columns = {str(row[1]) for row in con.execute("PRAGMA table_info(mail_accounts)").fetchall()}
            if "eml_target_path" not in mail_columns:
                con.execute("ALTER TABLE mail_accounts ADD COLUMN eml_target_path TEXT NOT NULL DEFAULT ''")

            curation_columns = {str(row[1]) for row in con.execute("PRAGMA table_info(curation_sessions)").fetchall()}
            if "csrf_token" not in curation_columns:
                con.execute("ALTER TABLE curation_sessions ADD COLUMN csrf_token TEXT NOT NULL DEFAULT ''")

            user_columns = {str(row[1]) for row in con.execute("PRAGMA table_info(canonical_users)").fetchall()}
            if "findings_curation_enabled" not in user_columns:
                con.execute("ALTER TABLE canonical_users ADD COLUMN findings_curation_enabled INTEGER NOT NULL DEFAULT 0")

            chat_columns = {str(row[1]) for row in con.execute("PRAGMA table_info(user_chat_settings)").fetchall()}
            if "enabled" not in chat_columns:
                # Existing rc1.1 rows remain enabled unless an administrator disables them.
                con.execute("ALTER TABLE user_chat_settings ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")

            # Preserve every mail archive root ever used. Existing installations
            # are backfilled here; deleting/reconfiguring an IMAP account must not
            # make already archived mail indistinguishable from ordinary files.
            now = time.time()
            rows = con.execute("SELECT target_path,eml_target_path FROM mail_accounts").fetchall()
            for row in rows:
                for raw in row:
                    root = str(raw or "").strip(" /")
                    if root:
                        con.execute(
                            """
                            INSERT INTO mail_archive_roots(target_path,first_seen_at,last_seen_at)
                            VALUES(?,?,?)
                            ON CONFLICT(target_path) DO UPDATE SET last_seen_at=excluded.last_seen_at
                            """,
                            (root, now, now),
                        )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def validate_writable(self) -> None:
        """Raise a useful error if the configured SQLite store is not usable."""
        try:
            with self._connect() as con:
                con.execute("BEGIN IMMEDIATE")
                con.execute(
                    "INSERT INTO store_meta(key,value) VALUES('writable_probe',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(time.time()),),
                )
                con.rollback()
        except Exception as exc:
            raise RuntimeError(f"credential store is not writable: {self.path}: {exc}") from exc

    # ------------------------------------------------------------------
    # Encryption status / migration
    # ------------------------------------------------------------------
    def secret_security_status(self) -> dict[str, object]:
        with self._connect() as con:
            credential_rows = con.execute(
                "SELECT rag_user_id,service,account_id,secret FROM credentials"
            ).fetchall()
            flow_rows = con.execute(
                "SELECT flow_id,rag_user_id,poll_token FROM nextcloud_login_flows"
            ).fetchall()
            curation_rows = con.execute(
                "SELECT session_id_hash,canonical_user_id,app_password FROM curation_sessions"
            ).fetchall()
        encrypted_credentials = sum(1 for row in credential_rows if is_encrypted(str(row["secret"])))
        encrypted_flows = sum(1 for row in flow_rows if is_encrypted(str(row["poll_token"])))
        encrypted_curation = sum(1 for row in curation_rows if is_encrypted(str(row["app_password"])))
        return {
            "encryption_mode": self._crypto.mode,
            "master_key": key_file_status(self._crypto.key_path),
            "credentials_total": len(credential_rows),
            "credentials_encrypted": encrypted_credentials,
            "credentials_plaintext": len(credential_rows) - encrypted_credentials,
            "flows_total": len(flow_rows),
            "flows_encrypted": encrypted_flows,
            "flows_plaintext": len(flow_rows) - encrypted_flows,
            "curation_sessions_total": len(curation_rows),
            "curation_sessions_encrypted": encrypted_curation,
            "curation_sessions_plaintext": len(curation_rows) - encrypted_curation,
        }

    def migrate_plaintext_secrets(self) -> dict[str, int]:
        if not self._crypto.enabled:
            raise RuntimeError("master key is required before plaintext secrets can be migrated")
        credentials_changed = 0
        flows_changed = 0
        curation_changed = 0
        with self._connect() as con:
            rows = con.execute("SELECT rag_user_id,service,account_id,secret FROM credentials").fetchall()
            for row in rows:
                value = str(row["secret"] or "")
                if is_encrypted(value):
                    continue
                user = str(row["rag_user_id"]); service = str(row["service"]); account = str(row["account_id"])
                encrypted = self._crypto.encrypt(value, aad=self._credential_aad(user, service, account))
                con.execute(
                    "UPDATE credentials SET secret=?,updated_at=? WHERE rag_user_id=? AND service=? AND account_id=?",
                    (encrypted, time.time(), user, service, account),
                )
                credentials_changed += 1
            rows = con.execute("SELECT flow_id,rag_user_id,poll_token FROM nextcloud_login_flows").fetchall()
            for row in rows:
                value = str(row["poll_token"] or "")
                if is_encrypted(value):
                    continue
                flow_id = str(row["flow_id"]); user = str(row["rag_user_id"])
                encrypted = self._crypto.encrypt(value, aad=self._flow_aad(flow_id, user))
                con.execute("UPDATE nextcloud_login_flows SET poll_token=? WHERE flow_id=?", (encrypted, flow_id))
                flows_changed += 1
            rows = con.execute(
                "SELECT session_id_hash,canonical_user_id,app_password FROM curation_sessions"
            ).fetchall()
            for row in rows:
                value = str(row["app_password"] or "")
                if is_encrypted(value):
                    continue
                digest = str(row["session_id_hash"]); user_id = str(row["canonical_user_id"])
                encrypted = self._crypto.encrypt(
                    value, aad=self._curation_session_aad(digest, user_id)
                )
                con.execute(
                    "UPDATE curation_sessions SET app_password=? WHERE session_id_hash=?",
                    (encrypted, digest),
                )
                curation_changed += 1
        return {"credentials": credentials_changed, "flows": flows_changed, "curation_sessions": curation_changed}

    def verify_secret_encryption(self) -> dict[str, object]:
        status = self.secret_security_status()
        errors: list[str] = []
        with self._connect() as con:
            for row in con.execute("SELECT * FROM credentials").fetchall():
                try:
                    self._credential_from_row(row)
                except Exception as exc:
                    errors.append(f"credential {row['service']}/{row['account_id']}: {exc}")
            for row in con.execute("SELECT * FROM nextcloud_login_flows").fetchall():
                try:
                    self._flow_from_row(row)
                except Exception as exc:
                    errors.append(f"login flow {row['flow_id']}: {exc}")
            for row in con.execute("SELECT * FROM curation_sessions").fetchall():
                try:
                    self._curation_session_from_row(row)
                except Exception as exc:
                    errors.append(f"curation session {row['session_id_hash']}: {exc}")
        status["errors"] = errors
        status["ok"] = (
            not errors
            and int(status["credentials_plaintext"]) == 0
            and int(status["flows_plaintext"]) == 0
            and int(status["curation_sessions_plaintext"]) == 0
        )
        return status

    # ------------------------------------------------------------------
    # Trusted provider clients
    # ------------------------------------------------------------------
    def register_client(
        self,
        client_id: str,
        api_key: str,
        *,
        name: str = "",
        enabled: bool = True,
        replace: bool = False,
    ) -> None:
        client = str(client_id or "").strip()
        if not _CLIENT_ID_RE.fullmatch(client):
            raise ValueError("client_id must match [A-Za-z0-9._-]{1,80}")
        secret = str(api_key or "").strip()
        if len(secret) < 24:
            raise ValueError("provider client API key must be at least 24 characters")
        display = str(name or client).strip() or client
        now = time.time()
        key_hash = self._key_hash(secret)
        with self._connect() as con:
            if replace:
                con.execute(
                    """
                    INSERT INTO provider_clients(client_id,name,api_key_hash,enabled,created_at,updated_at,last_used_at)
                    VALUES(?,?,?,?,?,?,NULL)
                    ON CONFLICT(client_id) DO UPDATE SET
                        name=excluded.name,
                        api_key_hash=excluded.api_key_hash,
                        enabled=excluded.enabled,
                        updated_at=excluded.updated_at
                    """,
                    (client, display, key_hash, int(bool(enabled)), now, now),
                )
            else:
                con.execute(
                    """
                    INSERT INTO provider_clients(client_id,name,api_key_hash,enabled,created_at,updated_at,last_used_at)
                    VALUES(?,?,?,?,?,?,NULL)
                    """,
                    (client, display, key_hash, int(bool(enabled)), now, now),
                )

    def create_client(self, client_id: str, *, name: str = "") -> str:
        api_key = secrets.token_urlsafe(32)
        self.register_client(client_id, api_key, name=name, replace=False)
        return api_key

    def authenticate_client(self, api_key: str, *, touch: bool = True) -> ProviderClient | None:
        secret = str(api_key or "").strip()
        if not secret:
            return None
        key_hash = self._key_hash(secret)
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM provider_clients WHERE api_key_hash=? AND enabled=1",
                (key_hash,),
            ).fetchone()
            if row is None:
                return None
            if touch:
                now = time.time()
                con.execute(
                    "UPDATE provider_clients SET last_used_at=? WHERE client_id=?",
                    (now, row["client_id"]),
                )
                last_used = now
            else:
                last_used = row["last_used_at"]
        return ProviderClient(
            client_id=row["client_id"],
            name=row["name"],
            enabled=bool(row["enabled"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            last_used_at=None if last_used is None else float(last_used),
        )

    def list_clients(self) -> list[ProviderClient]:
        with self._connect() as con:
            rows = con.execute("SELECT * FROM provider_clients ORDER BY client_id").fetchall()
        return [
            ProviderClient(
                client_id=row["client_id"], name=row["name"], enabled=bool(row["enabled"]),
                created_at=float(row["created_at"]), updated_at=float(row["updated_at"]),
                last_used_at=None if row["last_used_at"] is None else float(row["last_used_at"]),
            )
            for row in rows
        ]

    def set_client_enabled(self, client_id: str, enabled: bool) -> bool:
        with self._connect() as con:
            cur = con.execute(
                "UPDATE provider_clients SET enabled=?, updated_at=? WHERE client_id=?",
                (int(bool(enabled)), time.time(), str(client_id or "").strip()),
            )
            return cur.rowcount > 0

    def delete_client(self, client_id: str, *, purge_identity_state: bool = True) -> bool:
        """Delete one trusted frontend client.

        By default, also remove the frontend-scoped identity bindings, Nextcloud
        credentials and pending login flows belonging to ``client_id::...``.
        Canonical Nextcloud users and their Mail/Web settings are deliberately
        retained because they may still be used through other trusted clients.
        """
        client = str(client_id or "").strip()
        if not _CLIENT_ID_RE.fullmatch(client):
            raise ValueError("invalid client_id")
        prefix = client + _SCOPE_SEPARATOR
        with self._connect() as con:
            cur = con.execute(
                "DELETE FROM provider_clients WHERE client_id=?",
                (client,),
            )
            if cur.rowcount <= 0:
                return False
            if purge_identity_state:
                con.execute("DELETE FROM nextcloud_login_flows WHERE substr(rag_user_id,1,?)=?", (len(prefix), prefix))
                con.execute("DELETE FROM credentials WHERE substr(rag_user_id,1,?)=?", (len(prefix), prefix))
                con.execute("DELETE FROM identity_bindings WHERE substr(rag_user_id,1,?)=?", (len(prefix), prefix))
            return True

    def client_count(self) -> int:
        with self._connect() as con:
            return int(con.execute("SELECT COUNT(*) FROM provider_clients").fetchone()[0])

    def migrate_legacy_identities(self, client_id: str) -> int:
        """Scope pre-r1 unqualified credentials/flows to one trusted client exactly once."""
        client = str(client_id or "").strip()
        if not _CLIENT_ID_RE.fullmatch(client):
            raise ValueError("invalid client_id")
        meta_key = "identity_scope_migration_v1"
        prefix = f"{client}{_SCOPE_SEPARATOR}"
        with self._connect() as con:
            marker = con.execute("SELECT value FROM store_meta WHERE key=?", (meta_key,)).fetchone()
            if marker is not None:
                return 0
            credential_rows = con.execute("SELECT * FROM credentials").fetchall()
            flow_rows = con.execute("SELECT * FROM nextcloud_login_flows").fetchall()
            changed = 0
            for row in credential_rows:
                old = str(row["rag_user_id"])
                if split_scoped_identity(old) is not None or old.startswith(_CANONICAL_CREDENTIAL_PREFIX):
                    continue
                new = prefix + old
                service = str(row["service"]); account = str(row["account_id"])
                value = str(row["secret"])
                if is_encrypted(value):
                    clear = self._crypto.decrypt(value, aad=self._credential_aad(old, service, account))
                    value = self._crypto.encrypt(clear, aad=self._credential_aad(new, service, account))
                con.execute(
                    "UPDATE credentials SET rag_user_id=?,secret=? WHERE rag_user_id=? AND service=? AND account_id=?",
                    (new, value, old, service, account),
                )
                changed += 1
            for row in flow_rows:
                old = str(row["rag_user_id"])
                if split_scoped_identity(old) is not None:
                    continue
                new = prefix + old
                flow_id = str(row["flow_id"]); value = str(row["poll_token"])
                if is_encrypted(value):
                    clear = self._crypto.decrypt(value, aad=self._flow_aad(flow_id, old))
                    value = self._crypto.encrypt(clear, aad=self._flow_aad(flow_id, new))
                con.execute(
                    "UPDATE nextcloud_login_flows SET rag_user_id=?,poll_token=? WHERE flow_id=?",
                    (new, value, flow_id),
                )
            con.execute("INSERT INTO store_meta(key,value) VALUES(?,?)", (meta_key, client))
        return changed

    # ------------------------------------------------------------------
    # Generic secrets
    # ------------------------------------------------------------------
    def set_credential(
        self, rag_user_id: str, service: str, username: str, secret: str,
        *, account_id: str = "primary", server: str = "",
    ) -> None:
        user = str(rag_user_id or "").strip()
        service_name = str(service or "").strip().lower()
        username = str(username or "").strip()
        account = str(account_id or "primary").strip() or "primary"
        if not user or not service_name or not username or not secret:
            raise ValueError("rag_user_id, service, username and secret are required")
        stored_secret = self._crypto.encrypt(
            str(secret), aad=self._credential_aad(user, service_name, account)
        )
        now = time.time()
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO credentials(rag_user_id,service,account_id,server,username,secret,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(rag_user_id,service,account_id) DO UPDATE SET
                    server=excluded.server, username=excluded.username,
                    secret=excluded.secret, updated_at=excluded.updated_at
                """,
                (user, service_name, account, str(server or ""), username, stored_secret, now, now),
            )

    def get_credential(self, rag_user_id: str, service: str, *, account_id: str = "primary") -> StoredCredential | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM credentials WHERE rag_user_id=? AND service=? AND account_id=?",
                (str(rag_user_id or "").strip(), str(service or "").strip().lower(), str(account_id or "primary")),
            ).fetchone()
        return self._credential_from_row(row)

    def _credential_from_row(self, row: sqlite3.Row | None, *, allow_plaintext: bool = False) -> StoredCredential | None:
        if row is None:
            return None
        user = str(row["rag_user_id"])
        service = str(row["service"])
        account = str(row["account_id"])
        secret = self._crypto.decrypt(
            str(row["secret"]),
            aad=self._credential_aad(user, service, account),
            allow_plaintext=allow_plaintext,
        )
        return StoredCredential(
            rag_user_id=user, service=service, account_id=account,
            server=row["server"], username=row["username"], secret=secret,
        )

    def delete_credential(self, rag_user_id: str, service: str, *, account_id: str = "primary") -> bool:
        with self._connect() as con:
            cur = con.execute(
                "DELETE FROM credentials WHERE rag_user_id=? AND service=? AND account_id=?",
                (str(rag_user_id or "").strip(), str(service or "").strip().lower(), str(account_id or "primary")),
            )
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Canonical Nextcloud users and frontend bindings
    # ------------------------------------------------------------------
    @staticmethod
    def _canonical_user_from_row(row: sqlite3.Row | None) -> CanonicalUser | None:
        if row is None:
            return None
        return CanonicalUser(
            canonical_user_id=str(row["canonical_user_id"]),
            nextcloud_server=str(row["nextcloud_server"]),
            nextcloud_login=str(row["nextcloud_login"]),
            enabled=bool(row["enabled"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            last_seen_at=float(row["last_seen_at"]),
            findings_curation_enabled=bool(row["findings_curation_enabled"]) if "findings_curation_enabled" in row.keys() else False,
        )

    def ensure_canonical_user(self, server: str, login: str) -> CanonicalUser:
        server_value = normalize_nextcloud_server(server)
        login_value = str(login or "").strip()
        if not server_value or not login_value:
            raise ValueError("nextcloud server and login are required")
        now = time.time()
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM canonical_users WHERE nextcloud_server=? AND nextcloud_login=?",
                (server_value, login_value),
            ).fetchone()
            if row is None:
                canonical_user_id = uuid.uuid4().hex
                con.execute(
                    """
                    INSERT INTO canonical_users(
                        canonical_user_id,nextcloud_server,nextcloud_login,enabled,
                        created_at,updated_at,last_seen_at
                    ) VALUES(?,?,?,?,?,?,?)
                    """,
                    (canonical_user_id, server_value, login_value, 1, now, now, now),
                )
                row = con.execute(
                    "SELECT * FROM canonical_users WHERE canonical_user_id=?", (canonical_user_id,)
                ).fetchone()
            else:
                con.execute(
                    "UPDATE canonical_users SET last_seen_at=?, updated_at=? WHERE canonical_user_id=?",
                    (now, now, row["canonical_user_id"]),
                )
                row = con.execute(
                    "SELECT * FROM canonical_users WHERE canonical_user_id=?", (row["canonical_user_id"],)
                ).fetchone()
        result = self._canonical_user_from_row(row)
        assert result is not None
        return result

    def bind_identity(self, rag_user_id: str, server: str, login: str) -> CanonicalUser:
        identity = str(rag_user_id or "").strip()
        if not identity:
            raise ValueError("rag_user_id is required")
        user = self.ensure_canonical_user(server, login)
        now = time.time()
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO identity_bindings(rag_user_id,canonical_user_id,created_at,updated_at)
                VALUES(?,?,?,?)
                ON CONFLICT(rag_user_id) DO UPDATE SET
                    canonical_user_id=excluded.canonical_user_id,
                    updated_at=excluded.updated_at
                """,
                (identity, user.canonical_user_id, now, now),
            )
        return user

    def get_canonical_user(self, canonical_user_id: str) -> CanonicalUser | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM canonical_users WHERE canonical_user_id=?",
                (str(canonical_user_id or "").strip(),),
            ).fetchone()
        return self._canonical_user_from_row(row)

    def get_canonical_user_for_identity(self, rag_user_id: str) -> CanonicalUser | None:
        with self._connect() as con:
            row = con.execute(
                """
                SELECT u.* FROM identity_bindings b
                JOIN canonical_users u ON u.canonical_user_id=b.canonical_user_id
                WHERE b.rag_user_id=?
                """,
                (str(rag_user_id or "").strip(),),
            ).fetchone()
        return self._canonical_user_from_row(row)

    def list_canonical_users(self) -> list[CanonicalUser]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM canonical_users ORDER BY nextcloud_server,nextcloud_login"
            ).fetchall()
        return [self._canonical_user_from_row(row) for row in rows if row is not None]  # type: ignore[list-item]

    def find_canonical_users(self, login: str, *, server: str = "") -> list[CanonicalUser]:
        login_value = str(login or "").strip()
        if not login_value:
            return []
        params: list[object] = [login_value]
        where = "nextcloud_login=?"
        if server:
            where += " AND nextcloud_server=?"
            params.append(normalize_nextcloud_server(server))
        with self._connect() as con:
            rows = con.execute(
                f"SELECT * FROM canonical_users WHERE {where} ORDER BY nextcloud_server,nextcloud_login",
                params,
            ).fetchall()
        return [self._canonical_user_from_row(row) for row in rows if row is not None]  # type: ignore[list-item]

    def canonical_user_count(self) -> int:
        with self._connect() as con:
            return int(con.execute("SELECT COUNT(*) FROM canonical_users").fetchone()[0])

    def set_canonical_user_enabled(self, canonical_user_id: str, enabled: bool) -> bool:
        with self._connect() as con:
            cur = con.execute(
                "UPDATE canonical_users SET enabled=?,updated_at=? WHERE canonical_user_id=?",
                (int(bool(enabled)), time.time(), str(canonical_user_id or "").strip()),
            )
            return cur.rowcount > 0

    def set_findings_curation_enabled(self, canonical_user_id: str, enabled: bool) -> bool:
        with self._connect() as con:
            cur = con.execute(
                "UPDATE canonical_users SET findings_curation_enabled=?,updated_at=? WHERE canonical_user_id=?",
                (int(bool(enabled)), time.time(), str(canonical_user_id or "").strip()),
            )
            return cur.rowcount > 0

    def get_model_settings(self, canonical_user_id: str) -> UserModelSettings | None:
        user_id = str(canonical_user_id or "").strip()
        if not user_id:
            return None
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM user_model_settings WHERE canonical_user_id=?",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            raw_allowed = json.loads(str(row["allowed_model_ids_json"] or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            raw_allowed = []
        allowed = tuple(
            dict.fromkeys(
                str(value or "").strip()
                for value in (raw_allowed if isinstance(raw_allowed, list) else [])
                if str(value or "").strip()
            )
        )
        return UserModelSettings(
            canonical_user_id=user_id,
            default_model_id=str(row["default_model_id"] or "").strip(),
            allowed_model_ids=allowed,
            updated_at=float(row["updated_at"] or 0.0),
        )

    def set_model_settings(
        self,
        canonical_user_id: str,
        *,
        default_model_id: str,
        allowed_model_ids: list[str] | tuple[str, ...],
    ) -> UserModelSettings:
        user_id = str(canonical_user_id or "").strip()
        if not user_id or self.get_canonical_user(user_id) is None:
            raise ValueError("unknown canonical user")
        allowed = list(
            dict.fromkeys(
                str(value or "").strip()
                for value in allowed_model_ids
                if str(value or "").strip()
            )
        )
        default_id = str(default_model_id or "").strip()
        if not allowed:
            raise ValueError("at least one SunaQ model must be allowed")
        if default_id not in allowed:
            raise ValueError("default SunaQ model must be in allowed models")
        now = time.time()
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO user_model_settings(
                    canonical_user_id,default_model_id,allowed_model_ids_json,updated_at
                ) VALUES(?,?,?,?)
                ON CONFLICT(canonical_user_id) DO UPDATE SET
                    default_model_id=excluded.default_model_id,
                    allowed_model_ids_json=excluded.allowed_model_ids_json,
                    updated_at=excluded.updated_at
                """,
                (user_id, default_id, json.dumps(allowed, ensure_ascii=False), now),
            )
        settings = self.get_model_settings(user_id)
        assert settings is not None
        return settings

    def clear_model_settings(self, canonical_user_id: str) -> bool:
        with self._connect() as con:
            cur = con.execute(
                "DELETE FROM user_model_settings WHERE canonical_user_id=?",
                (str(canonical_user_id or "").strip(),),
            )
            return cur.rowcount > 0

    @staticmethod
    def curation_session_hash(session_token: str) -> str:
        return hashlib.sha256(str(session_token or "").encode("utf-8")).hexdigest()

    def create_curation_session(
        self,
        *,
        canonical_user_id: str,
        nextcloud_server: str,
        nextcloud_login: str,
        app_password: str,
        lifetime_seconds: float,
    ) -> tuple[str, CurationSession]:
        if not self._crypto.enabled:
            raise RuntimeError("encrypted credential master key is required for curation sessions")
        user = self.get_canonical_user(canonical_user_id)
        if user is None:
            raise ValueError("unknown canonical user")
        if not user.enabled:
            raise ValueError("canonical user is disabled")
        if not user.findings_curation_enabled:
            raise PermissionError("findings curation is not enabled for this user")
        token = secrets.token_urlsafe(32)
        digest = self.curation_session_hash(token)
        now = time.time()
        expires = now + max(60.0, float(lifetime_seconds))
        encrypted = self._crypto.encrypt(
            str(app_password),
            aad=self._curation_session_aad(digest, canonical_user_id),
        )
        csrf_token = secrets.token_urlsafe(24)
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO curation_sessions(
                    session_id_hash,canonical_user_id,nextcloud_server,nextcloud_login,
                    app_password,csrf_token,state,created_at,expires_at,last_seen_at
                ) VALUES(?,?,?,?,?,?,'active',?,?,?)
                """,
                (
                    digest, canonical_user_id, normalize_nextcloud_server(nextcloud_server),
                    str(nextcloud_login or "").strip(), encrypted, csrf_token, now, expires, now,
                ),
            )
        session = self.get_curation_session(token, touch=False)
        assert session is not None
        return token, session

    def _curation_session_from_row(self, row: sqlite3.Row | None) -> CurationSession | None:
        if row is None:
            return None
        digest = str(row["session_id_hash"])
        user_id = str(row["canonical_user_id"])
        secret = self._crypto.decrypt(
            str(row["app_password"]),
            aad=self._curation_session_aad(digest, user_id),
        )
        return CurationSession(
            session_id_hash=digest,
            canonical_user_id=user_id,
            nextcloud_server=str(row["nextcloud_server"]),
            nextcloud_login=str(row["nextcloud_login"]),
            app_password=secret,
            csrf_token=str(row["csrf_token"] or ""),
            state=str(row["state"]),
            created_at=float(row["created_at"]),
            expires_at=float(row["expires_at"]),
            last_seen_at=float(row["last_seen_at"]),
        )

    def get_curation_session_any(self, session_token: str) -> CurationSession | None:
        digest = self.curation_session_hash(session_token)
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM curation_sessions WHERE session_id_hash=?",
                (digest,),
            ).fetchone()
        return self._curation_session_from_row(row)

    def get_curation_session(self, session_token: str, *, touch: bool = True) -> CurationSession | None:
        digest = self.curation_session_hash(session_token)
        now = time.time()
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM curation_sessions WHERE session_id_hash=?",
                (digest,),
            ).fetchone()
            if row is None:
                return None
            if str(row["state"]) != "active" or float(row["expires_at"]) <= now:
                return None
            if touch:
                con.execute(
                    "UPDATE curation_sessions SET last_seen_at=? WHERE session_id_hash=?",
                    (now, digest),
                )
                row = con.execute(
                    "SELECT * FROM curation_sessions WHERE session_id_hash=?",
                    (digest,),
                ).fetchone()
        return self._curation_session_from_row(row)

    def mark_curation_session_revocation_pending(self, session_id_hash: str) -> None:
        with self._connect() as con:
            con.execute(
                "UPDATE curation_sessions SET state='revocation_pending' WHERE session_id_hash=?",
                (str(session_id_hash or "").strip(),),
            )

    def delete_curation_session_by_hash(self, session_id_hash: str) -> bool:
        with self._connect() as con:
            cur = con.execute(
                "DELETE FROM curation_sessions WHERE session_id_hash=?",
                (str(session_id_hash or "").strip(),),
            )
            return cur.rowcount > 0

    def list_curation_sessions(self) -> list[CurationSession]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM curation_sessions ORDER BY created_at"
            ).fetchall()
        return [x for x in (self._curation_session_from_row(row) for row in rows) if x is not None]

    def list_curation_sessions_for_cleanup(self) -> tuple[list[CurationSession], list[str]]:
        """Read startup-cleanup sessions without letting one corrupt secret abort startup."""
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM curation_sessions ORDER BY created_at"
            ).fetchall()
        sessions: list[CurationSession] = []
        undecryptable: list[str] = []
        for row in rows:
            try:
                session = self._curation_session_from_row(row)
            except Exception:
                undecryptable.append(str(row["session_id_hash"]))
                continue
            if session is not None:
                sessions.append(session)
        return sessions, undecryptable

    def list_bindings(self, canonical_user_id: str) -> list[IdentityBinding]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM identity_bindings WHERE canonical_user_id=? ORDER BY rag_user_id",
                (str(canonical_user_id or "").strip(),),
            ).fetchall()
        return [
            IdentityBinding(
                rag_user_id=str(row["rag_user_id"]),
                canonical_user_id=str(row["canonical_user_id"]),
                created_at=float(row["created_at"]), updated_at=float(row["updated_at"]),
            )
            for row in rows
        ]

    def get_nextcloud_credential_for_canonical_user(self, canonical_user_id: str) -> StoredCredential | None:
        """Return the newest current frontend credential bound to a canonical user."""
        with self._connect() as con:
            row = con.execute(
                """
                SELECT c.* FROM identity_bindings b
                JOIN credentials c ON c.rag_user_id=b.rag_user_id
                WHERE b.canonical_user_id=? AND c.service='nextcloud' AND c.account_id='primary'
                ORDER BY c.updated_at DESC
                LIMIT 1
                """,
                (str(canonical_user_id or "").strip(),),
            ).fetchone()
        return self._credential_from_row(row)

    def clear_nextcloud_credentials_for_canonical_user(self, canonical_user_id: str) -> dict[str, int]:
        """Remove frontend-scoped Nextcloud secrets/flows for controlled re-auth.

        Identity bindings and the canonical user are retained. The next request
        from each trusted frontend therefore starts Login Flow v2 again.
        """
        user_id = str(canonical_user_id or "").strip()
        bindings = self.list_bindings(user_id)
        credentials_deleted = 0
        flows_deleted = 0
        with self._connect() as con:
            for binding in bindings:
                cur = con.execute(
                    "DELETE FROM credentials WHERE rag_user_id=? AND service='nextcloud'",
                    (binding.rag_user_id,),
                )
                credentials_deleted += max(0, int(cur.rowcount or 0))
                cur = con.execute(
                    "DELETE FROM nextcloud_login_flows WHERE rag_user_id=?",
                    (binding.rag_user_id,),
                )
                flows_deleted += max(0, int(cur.rowcount or 0))
        return {
            "bindings": len(bindings),
            "credentials_deleted": credentials_deleted,
            "flows_deleted": flows_deleted,
        }

    # ------------------------------------------------------------------
    # Per-user mail configuration (schema is 1:n; beta UI exposes one account)
    # ------------------------------------------------------------------
    @staticmethod
    def _clean_mailboxes(values: Iterable[str] | str | None) -> tuple[str, ...]:
        if isinstance(values, str):
            raw = values.replace("\r", "\n").replace(",", "\n").split("\n")
        else:
            raw = list(values or [])
        out: list[str] = []
        seen: set[str] = set()
        for value in raw:
            item = str(value or "").strip()
            if item and item not in seen:
                seen.add(item)
                out.append(item)
        return tuple(out or ["INBOX"])

    @classmethod
    def _mail_account_from_row(cls, row: sqlite3.Row | None, *, has_secret: bool = False) -> MailAccount | None:
        if row is None:
            return None
        try:
            mailboxes = cls._clean_mailboxes(json.loads(str(row["mailboxes_json"] or "[]")))
        except Exception:
            mailboxes = ("INBOX",)
        return MailAccount(
            account_id=str(row["account_id"]), canonical_user_id=str(row["canonical_user_id"]),
            name=str(row["name"]), enabled=bool(row["enabled"]), host=str(row["host"]),
            port=int(row["port"]), security=str(row["security"]), verify_tls=bool(row["verify_tls"]),
            username=str(row["username"]), mailboxes=mailboxes,
            max_messages_per_run=int(row["max_messages_per_run"]), not_before=str(row["not_before"] or ""),
            store_eml=bool(row["store_eml"]), store_attachments=bool(row["store_attachments"]),
            target_path=str(row["target_path"]), eml_target_path=str(row["eml_target_path"] or ""),
            created_at=float(row["created_at"]), updated_at=float(row["updated_at"]),
            has_secret=bool(has_secret),
        )

    def save_mail_account(
        self,
        canonical_user_id: str,
        *,
        account_id: str = "",
        name: str = "primary",
        enabled: bool = True,
        host: str,
        port: int = 993,
        security: str = "tls",
        verify_tls: bool = True,
        username: str,
        password: str = "",
        mailboxes: Iterable[str] | str | None = None,
        max_messages_per_run: int = 50,
        not_before: str = "",
        store_eml: bool = False,
        store_attachments: bool = False,
        target_path: str,
        eml_target_path: str = "",
    ) -> MailAccount:
        canonical = self.get_canonical_user(canonical_user_id)
        if canonical is None:
            raise ValueError("unknown canonical user")
        host_value = str(host or "").strip()
        username_value = str(username or "").strip()
        target = str(target_path or "").strip(" /")
        if not host_value or not username_value or not target:
            raise ValueError("mail host, username and target_path are required")
        security_value = str(security or "tls").strip().lower()
        if security_value not in {"tls", "starttls", "plain"}:
            raise ValueError("mail security must be tls, starttls or plain")
        port_value = int(port)
        if not 1 <= port_value <= 65535:
            raise ValueError("mail port out of range")
        account = str(account_id or "").strip() or uuid.uuid4().hex
        boxes = self._clean_mailboxes(mailboxes)
        now = time.time()
        with self._connect() as con:
            existing_row = con.execute(
                "SELECT canonical_user_id FROM mail_accounts WHERE account_id=?", (account,)
            ).fetchone()
            if existing_row is not None and str(existing_row["canonical_user_id"]) != canonical.canonical_user_id:
                raise ValueError("mail account belongs to a different canonical user")
            if existing_row is not None:
                con.execute(
                    """
                    UPDATE mail_accounts SET
                        canonical_user_id=?,name=?,enabled=?,host=?,port=?,security=?,verify_tls=?,username=?,
                        mailboxes_json=?,max_messages_per_run=?,not_before=?,store_eml=?,store_attachments=?,
                        target_path=?,eml_target_path=?,updated_at=?
                    WHERE account_id=?
                    """,
                    (
                        canonical.canonical_user_id, str(name or "primary").strip() or "primary",
                        int(bool(enabled)), host_value, port_value, security_value, int(bool(verify_tls)), username_value,
                        json.dumps(list(boxes), ensure_ascii=False), max(0, int(max_messages_per_run)), str(not_before or "").strip(),
                        int(bool(store_eml)), int(bool(store_attachments)), target, str(eml_target_path or "").strip(" /"),
                        now, account,
                    ),
                )
            else:
                con.execute(
                    """
                    INSERT INTO mail_accounts(
                        account_id,canonical_user_id,name,enabled,host,port,security,verify_tls,username,
                        mailboxes_json,max_messages_per_run,not_before,store_eml,store_attachments,
                        target_path,eml_target_path,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        account, canonical.canonical_user_id, str(name or "primary").strip() or "primary",
                        int(bool(enabled)), host_value, port_value, security_value, int(bool(verify_tls)), username_value,
                        json.dumps(list(boxes), ensure_ascii=False), max(0, int(max_messages_per_run)), str(not_before or "").strip(),
                        int(bool(store_eml)), int(bool(store_attachments)), target, str(eml_target_path or "").strip(" /"),
                        now, now,
                    ),
                )
        with self._connect() as con:
            now_roots = time.time()
            for raw in (target, str(eml_target_path or "").strip(" /")):
                root = str(raw or "").strip(" /")
                if root:
                    con.execute(
                        """
                        INSERT INTO mail_archive_roots(target_path,first_seen_at,last_seen_at)
                        VALUES(?,?,?)
                        ON CONFLICT(target_path) DO UPDATE SET last_seen_at=excluded.last_seen_at
                        """,
                        (root, now_roots, now_roots),
                    )

        owner = canonical_credential_owner(canonical.canonical_user_id)
        if password:
            self.set_credential(
                owner,
                "mail_imap",
                username_value,
                password,
                account_id=account,
                server=host_value,
            )
        else:
            # Configuration edits must not touch the encrypted secret itself,
            # but keep non-secret credential metadata in sync.
            with self._connect() as con:
                con.execute(
                    "UPDATE credentials SET username=?,server=?,updated_at=? WHERE rag_user_id=? AND service='mail_imap' AND account_id=?",
                    (username_value, host_value, time.time(), owner, account),
                )
        result = self.get_mail_account(account)
        assert result is not None
        return result

    def get_mail_account(self, account_id: str) -> MailAccount | None:
        account = str(account_id or "").strip()
        with self._connect() as con:
            row = con.execute("SELECT * FROM mail_accounts WHERE account_id=?", (account,)).fetchone()
        if row is None:
            return None
        secret = self.get_credential(
            canonical_credential_owner(str(row["canonical_user_id"])), "mail_imap", account_id=account
        )
        return self._mail_account_from_row(row, has_secret=secret is not None)

    def list_mail_accounts(self, canonical_user_id: str | None = None, *, enabled_only: bool = False) -> list[MailAccount]:
        clauses: list[str] = []
        params: list[object] = []
        if canonical_user_id:
            clauses.append("canonical_user_id=?")
            params.append(str(canonical_user_id).strip())
        if enabled_only:
            clauses.append("enabled=1")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM mail_accounts" + where + " ORDER BY canonical_user_id,name,account_id",
                params,
            ).fetchall()
        out: list[MailAccount] = []
        for row in rows:
            account_id = str(row["account_id"])
            owner = canonical_credential_owner(str(row["canonical_user_id"]))
            secret = self.get_credential(owner, "mail_imap", account_id=account_id)
            item = self._mail_account_from_row(row, has_secret=secret is not None)
            if item is not None:
                out.append(item)
        return out

    def get_mail_secret(self, account: MailAccount | str) -> StoredCredential | None:
        item = self.get_mail_account(account) if isinstance(account, str) else account
        if item is None:
            return None
        return self.get_credential(
            canonical_credential_owner(item.canonical_user_id), "mail_imap", account_id=item.account_id
        )

    def set_mail_secret(self, account_id: str, secret: str) -> MailAccount:
        item = self.get_mail_account(account_id)
        if item is None:
            raise ValueError("unknown mail account")
        value = str(secret or "")
        if not value:
            raise ValueError("mail password is required")
        self.set_credential(
            canonical_credential_owner(item.canonical_user_id),
            "mail_imap",
            item.username,
            value,
            account_id=item.account_id,
            server=item.host,
        )
        refreshed = self.get_mail_account(item.account_id)
        assert refreshed is not None
        return refreshed

    def delete_mail_account(self, account_id: str) -> bool:
        item = self.get_mail_account(account_id)
        if item is None:
            return False
        with self._connect() as con:
            cur = con.execute("DELETE FROM mail_accounts WHERE account_id=?", (item.account_id,))
        self.delete_credential(
            canonical_credential_owner(item.canonical_user_id), "mail_imap", account_id=item.account_id
        )
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Per-user Web-research settings
    # ------------------------------------------------------------------
    @staticmethod
    def _web_settings_from_row(row: sqlite3.Row | None) -> UserWebSettings | None:
        if row is None:
            return None
        return UserWebSettings(
            canonical_user_id=str(row["canonical_user_id"]), enabled=bool(row["enabled"]),
            archive_enabled=bool(row["archive_enabled"]), target_path=str(row["target_path"] or ""),
            created_at=float(row["created_at"]), updated_at=float(row["updated_at"]),
        )

    def set_web_settings(
        self, canonical_user_id: str, *, enabled: bool = True,
        archive_enabled: bool | None = None, target_path: str | None = None,
    ) -> UserWebSettings:
        """Store the per-user live-Web gate.

        archive_enabled/target_path are retained only for compatibility with
        pre-rc2 Webarchiv data. New Web Evidence is persisted inside the
        user's chat research dossier and no longer has a separate target.
        """
        if self.get_canonical_user(canonical_user_id) is None:
            raise ValueError("unknown canonical user")
        existing = self.get_web_settings(canonical_user_id)
        legacy_archive_enabled = (
            bool(existing.archive_enabled) if archive_enabled is None and existing is not None
            else bool(archive_enabled)
        )
        target = (
            str(existing.target_path or "").strip(" /")
            if target_path is None and existing is not None
            else str(target_path or "").strip(" /")
        )
        if archive_enabled is not None and legacy_archive_enabled and not target:
            raise ValueError("legacy web archive target_path is required when archive is enabled")
        now = time.time()
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO user_web_settings(canonical_user_id,enabled,archive_enabled,target_path,created_at,updated_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(canonical_user_id) DO UPDATE SET
                    enabled=excluded.enabled, archive_enabled=excluded.archive_enabled,
                    target_path=excluded.target_path, updated_at=excluded.updated_at
                """,
                (str(canonical_user_id).strip(), int(bool(enabled)), int(legacy_archive_enabled), target, now, now),
            )
            if legacy_archive_enabled and target:
                # Preserve every legacy archive root ever used. If an admin later moves
                # a user's Web archive, old snapshots must not silently re-enter
                # ordinary internal retrieval.
                con.execute(
                    """
                    INSERT INTO web_archive_roots(target_path,first_seen_at,last_seen_at)
                    VALUES(?,?,?)
                    ON CONFLICT(target_path) DO UPDATE SET last_seen_at=excluded.last_seen_at
                    """,
                    (target, now, now),
                )
        result = self.get_web_settings(canonical_user_id)
        assert result is not None
        return result

    def get_web_settings(self, canonical_user_id: str) -> UserWebSettings | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM user_web_settings WHERE canonical_user_id=?",
                (str(canonical_user_id or "").strip(),),
            ).fetchone()
        return self._web_settings_from_row(row)

    # ------------------------------------------------------------------
    # Per-user chat-archive settings
    # ------------------------------------------------------------------
    @staticmethod
    def normalize_chat_archive_path(value: str) -> str:
        target = str(value or "").strip().strip("/")
        if not target:
            target = "SunaQ-Chats"
        if len(target) > 240 or "\x00" in target or "\\" in target:
            raise ValueError("invalid chat archive target_path")
        parts = [part.strip() for part in target.split("/")]
        if any(
            not part
            or part in {".", ".."}
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in part)
            for part in parts
        ):
            raise ValueError("invalid chat archive target_path")
        return "/".join(parts)

    def get_chat_settings(self, canonical_user_id: str) -> UserChatSettings | None:
        user_id = str(canonical_user_id or "").strip()
        if not user_id:
            return None
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM user_chat_settings WHERE canonical_user_id=?",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        return UserChatSettings(
            canonical_user_id=user_id,
            enabled=bool(row["enabled"]),
            target_path=self.normalize_chat_archive_path(str(row["target_path"] or "")),
            updated_at=float(row["updated_at"] or 0.0),
        )

    def set_chat_settings(
        self, canonical_user_id: str, *, enabled: bool = True,
        target_path: str = "SunaQ-Chats"
    ) -> UserChatSettings:
        user_id = str(canonical_user_id or "").strip()
        if not user_id or self.get_canonical_user(user_id) is None:
            raise ValueError("unknown canonical user")
        target = self.normalize_chat_archive_path(target_path)
        now = time.time()
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO user_chat_settings(canonical_user_id,enabled,target_path,updated_at)
                VALUES(?,?,?,?)
                ON CONFLICT(canonical_user_id) DO UPDATE SET
                    enabled=excluded.enabled,
                    target_path=excluded.target_path,
                    updated_at=excluded.updated_at
                """,
                (user_id, int(bool(enabled)), target, now),
            )
            con.execute(
                """
                INSERT INTO chat_archive_roots(target_path,first_seen_at,last_seen_at)
                VALUES(?,?,?)
                ON CONFLICT(target_path) DO UPDATE SET last_seen_at=excluded.last_seen_at
                """,
                (target, now, now),
            )
        settings = self.get_chat_settings(user_id)
        assert settings is not None
        return settings

    # ------------------------------------------------------------------
    # Per-user CardDAV/contact seed settings
    # ------------------------------------------------------------------
    @staticmethod
    def _contact_settings_from_row(row: sqlite3.Row | None) -> ContactSyncSettings | None:
        if row is None:
            return None
        def _list(name: str) -> tuple[str, ...]:
            try:
                value = json.loads(str(row[name] or "[]"))
            except Exception:
                value = []
            return tuple(str(x).strip() for x in value if str(x).strip())
        return ContactSyncSettings(
            canonical_user_id=str(row["canonical_user_id"]),
            enabled=bool(row["enabled"]),
            include_addressbooks=_list("include_addressbooks_json"),
            exclude_addressbooks=_list("exclude_addressbooks_json"),
            last_sync_at=(float(row["last_sync_at"]) if row["last_sync_at"] is not None else None),
            last_status=str(row["last_status"] or "never"),
            contacts_seen=int(row["contacts_seen"] or 0),
            contacts_written=int(row["contacts_written"] or 0),
            error_count=int(row["error_count"] or 0),
            last_error=str(row["last_error"] or ""),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    def set_contact_sync_settings(
        self, canonical_user_id: str, *, enabled: bool = True,
        include_addressbooks: Iterable[str] | str = (),
        exclude_addressbooks: Iterable[str] | str = (),
    ) -> ContactSyncSettings:
        if self.get_canonical_user(canonical_user_id) is None:
            raise ValueError("unknown canonical user")

        def _normalize(value: Iterable[str] | str) -> list[str]:
            if isinstance(value, str):
                raw = value.replace("\r", "\n").split("\n")
            else:
                raw = list(value)
            out: list[str] = []
            seen: set[str] = set()
            for item in raw:
                text = str(item or "").strip()
                if text and text.casefold() not in seen:
                    out.append(text)
                    seen.add(text.casefold())
            return out

        include = _normalize(include_addressbooks)
        exclude = _normalize(exclude_addressbooks)
        now = time.time()
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO contact_sync_settings(
                    canonical_user_id,enabled,include_addressbooks_json,exclude_addressbooks_json,
                    created_at,updated_at
                ) VALUES(?,?,?,?,?,?)
                ON CONFLICT(canonical_user_id) DO UPDATE SET
                    enabled=excluded.enabled,
                    include_addressbooks_json=excluded.include_addressbooks_json,
                    exclude_addressbooks_json=excluded.exclude_addressbooks_json,
                    updated_at=excluded.updated_at
                """,
                (
                    str(canonical_user_id).strip(), int(bool(enabled)),
                    json.dumps(include, ensure_ascii=False), json.dumps(exclude, ensure_ascii=False),
                    now, now,
                ),
            )
        result = self.get_contact_sync_settings(canonical_user_id)
        assert result is not None
        return result

    def get_contact_sync_settings(self, canonical_user_id: str) -> ContactSyncSettings | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM contact_sync_settings WHERE canonical_user_id=?",
                (str(canonical_user_id or "").strip(),),
            ).fetchone()
        return self._contact_settings_from_row(row)

    def record_contact_sync_result(
        self, canonical_user_id: str, *, status: str, contacts_seen: int = 0,
        contacts_written: int = 0, error_count: int = 0, last_error: str = "",
    ) -> ContactSyncSettings:
        user_id = str(canonical_user_id or "").strip()
        if self.get_canonical_user(user_id) is None:
            raise ValueError("unknown canonical user")
        existing = self.get_contact_sync_settings(user_id)
        if existing is None:
            existing = self.set_contact_sync_settings(user_id)
        now = time.time()
        with self._connect() as con:
            con.execute(
                """
                UPDATE contact_sync_settings
                SET last_sync_at=?,last_status=?,contacts_seen=?,contacts_written=?,
                    error_count=?,last_error=?,updated_at=?
                WHERE canonical_user_id=?
                """,
                (
                    now, str(status or "unknown")[:80], max(0, int(contacts_seen)),
                    max(0, int(contacts_written)), max(0, int(error_count)),
                    str(last_error or "")[:2000], now, user_id,
                ),
            )
        result = self.get_contact_sync_settings(user_id)
        assert result is not None
        return result

    # ------------------------------------------------------------------
    # Nextcloud Login Flow state
    # ------------------------------------------------------------------
    def create_nextcloud_flow(self, rag_user_id: str, poll_endpoint: str, poll_token: str, login_url: str) -> str:
        flow_id = uuid.uuid4().hex
        user = str(rag_user_id).strip()
        stored_token = self._crypto.encrypt(str(poll_token), aad=self._flow_aad(flow_id, user))
        with self._connect() as con:
            con.execute(
                "INSERT INTO nextcloud_login_flows VALUES(?,?,?,?,?,?)",
                (flow_id, user, poll_endpoint, stored_token, login_url, time.time()),
            )
        return flow_id

    def _flow_from_row(self, row: sqlite3.Row | None, *, allow_plaintext: bool = False) -> dict[str, object] | None:
        if row is None:
            return None
        flow_id = str(row["flow_id"])
        user = str(row["rag_user_id"])
        return {
            "flow_id": flow_id,
            "rag_user_id": user,
            "poll_endpoint": str(row["poll_endpoint"]),
            "poll_token": self._crypto.decrypt(
                str(row["poll_token"]), aad=self._flow_aad(flow_id, user), allow_plaintext=allow_plaintext
            ),
            "login_url": str(row["login_url"]),
            "created_at": float(row["created_at"]),
        }

    def get_nextcloud_flow(self, flow_id: str) -> dict[str, object] | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM nextcloud_login_flows WHERE flow_id=?", (str(flow_id),)
            ).fetchone()
        return self._flow_from_row(row)

    def get_recent_nextcloud_flow(
        self,
        rag_user_id: str,
        *,
        max_age_seconds: float = 1200.0,
    ) -> dict[str, object] | None:
        user = str(rag_user_id or "").strip()
        if not user:
            return None
        cutoff = time.time() - max(0.0, float(max_age_seconds))
        with self._connect() as con:
            row = con.execute(
                """
                SELECT * FROM nextcloud_login_flows
                WHERE rag_user_id=? AND created_at>=?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (user, cutoff),
            ).fetchone()
        return self._flow_from_row(row)

    def delete_nextcloud_flows_for_user(self, rag_user_id: str) -> int:
        user = str(rag_user_id or "").strip()
        if not user:
            return 0
        with self._connect() as con:
            cur = con.execute("DELETE FROM nextcloud_login_flows WHERE rag_user_id=?", (user,))
            return int(cur.rowcount or 0)

    def delete_nextcloud_flow(self, flow_id: str) -> None:
        with self._connect() as con:
            con.execute("DELETE FROM nextcloud_login_flows WHERE flow_id=?", (str(flow_id),))
