#!/usr/bin/env python3
"""Nextcloud CardDAV -> Neo4j identity seed.

The importer intentionally uses CardDAV records as provenance objects and keeps
Person/Organization identities separate. It discovers address books below the
user's CardDAV home, fetches vCards with an addressbook-query REPORT and upserts
only identity/contact metadata.

No photos and no arbitrary vCard payload are written to Neo4j.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urljoin, urlparse
from xml.etree import ElementTree as ET

import httpx

from rag.nextcloud_tls import nextcloud_verify_value
from rag.secret_env import secret_env
from rag.credential_store import (
    CanonicalUser,
    ContactSyncSettings,
    CredentialStore,
    StoredCredential,
)
from rag.graph import (
    BASE_DIR,
    GraphStore,
    cfg_get,
    generated_person_aliases,
    generated_organization_aliases,
    load_config,
    normalize_name,
    stable_contact_id,
)

DAV = "DAV:"
CARD = "urn:ietf:params:xml:ns:carddav"
NS = {"d": DAV, "card": CARD}


def _env_or_cfg(cfg: dict[str, Any], direct_path: str, env_path: str, default: str = "") -> str:
    env_name = str(cfg_get(cfg, env_path, default="") or "").strip()
    if env_name:
        value = secret_env(env_name, "")
        if value:
            return value
    return str(cfg_get(cfg, direct_path, default=default) or default)


@dataclass
class CardDAVSettings:
    dav_url: str
    username: str
    password: str
    user_id: str
    cloud_id: str
    verify_tls: bool | str
    timeout: float
    include_addressbooks: list[str]
    exclude_addressbooks: list[str]

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "CardDAVSettings":
        username = _env_or_cfg(cfg, "carddav.username", "carddav.username_env")
        password = _env_or_cfg(cfg, "carddav.password", "carddav.password_env")
        if not username or not password:
            raise RuntimeError(
                "Legacy-CardDAV-Zugangsdaten fehlen. Für normale Installationen "
                "verwende den benutzergebundenen Sync (--user <Nextcloud-Login>), "
                "der das bereits gespeicherte Nextcloud-Credential nutzt."
            )

        dav_url = str(cfg_get(cfg, "carddav.url", default="") or "").strip().rstrip("/")
        if not dav_url:
            base = str(cfg_get(cfg, "nextcloud.base_url", default="") or "").strip().rstrip("/")
            if not base:
                raise RuntimeError("carddav.url oder nextcloud.base_url fehlt")
            dav_url = base + "/remote.php/dav"

        user_id = str(cfg_get(cfg, "carddav.user_id", default=username) or username).strip()
        explicit_cloud_id = str(
            cfg_get(cfg, "carddav.cloud_id", "nextcloud.cloud_id", default="") or ""
        ).strip()
        parsed_dav = urlparse(dav_url)
        derived_cloud_id = (
            f"{parsed_dav.scheme}://{parsed_dav.netloc}"
            if parsed_dav.scheme and parsed_dav.netloc else dav_url
        )
        cloud_id = explicit_cloud_id or derived_cloud_id
        include = [str(x).strip() for x in (cfg_get(cfg, "carddav.include_addressbooks", default=[]) or []) if str(x).strip()]
        exclude = [str(x).strip() for x in (cfg_get(cfg, "carddav.exclude_addressbooks", default=[]) or []) if str(x).strip()]
        # The auto-generated recent-contact book is volatile and a bad identity seed.
        if "z-app-generated--contactsinteraction--recent" not in {x.casefold() for x in exclude}:
            exclude.append("z-app-generated--contactsinteraction--recent")

        return cls(
            dav_url=dav_url,
            username=username,
            password=password,
            user_id=user_id,
            cloud_id=cloud_id,
            verify_tls=nextcloud_verify_value(cfg, "carddav", "acl"),
            timeout=float(cfg_get(cfg, "carddav.timeout", default=120)),
            include_addressbooks=include,
            exclude_addressbooks=exclude,
        )

    @classmethod
    def for_canonical_user(
        cls,
        cfg: dict[str, Any],
        user: CanonicalUser,
        credential: StoredCredential,
        contact_settings: ContactSyncSettings | None = None,
    ) -> "CardDAVSettings":
        """Build CardDAV settings from the already authenticated Nextcloud user.

        The canonical UUID is deliberately not CardDAV provenance. The external
        source identity remains Nextcloud server/login/addressbook/UID; the UUID
        is only the internal settings/credential join key.
        """
        dav_url = str(cfg_get(cfg, "carddav.url", default="") or "").strip().rstrip("/")
        if not dav_url:
            dav_url = user.nextcloud_server.rstrip("/") + "/remote.php/dav"

        explicit_cloud_id = str(
            cfg_get(cfg, "carddav.cloud_id", "nextcloud.cloud_id", default="") or ""
        ).strip()
        cloud_id = explicit_cloud_id or user.nextcloud_server
        if contact_settings is not None:
            include = list(contact_settings.include_addressbooks)
            exclude = list(contact_settings.exclude_addressbooks)
        else:
            include = [
                str(x).strip() for x in (cfg_get(cfg, "carddav.include_addressbooks", default=[]) or [])
                if str(x).strip()
            ]
            exclude = [
                str(x).strip() for x in (cfg_get(cfg, "carddav.exclude_addressbooks", default=[]) or [])
                if str(x).strip()
            ]
        if "z-app-generated--contactsinteraction--recent" not in {x.casefold() for x in exclude}:
            exclude.append("z-app-generated--contactsinteraction--recent")

        return cls(
            dav_url=dav_url,
            username=credential.username,
            password=credential.secret,
            user_id=user.nextcloud_login,
            cloud_id=cloud_id,
            verify_tls=nextcloud_verify_value(cfg, "carddav", "acl"),
            timeout=float(cfg_get(cfg, "carddav.timeout", default=120)),
            include_addressbooks=include,
            exclude_addressbooks=exclude,
        )


class CardDAVClient:
    def __init__(self, settings: CardDAVSettings):
        self.settings = settings
        self.client = httpx.Client(
            auth=(settings.username, settings.password),
            verify=settings.verify_tls,
            timeout=settings.timeout,
            follow_redirects=True,
            headers={"User-Agent": "nextcloud-rag-carddav/0.1"},
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "CardDAVClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @property
    def addressbook_home(self) -> str:
        return (
            self.settings.dav_url.rstrip("/")
            + "/addressbooks/users/"
            + quote(self.settings.user_id, safe="")
            + "/"
        )

    def _request(self, method: str, url: str, *, body: str, depth: str | None = None) -> httpx.Response:
        headers = {"Content-Type": "application/xml; charset=utf-8"}
        if depth is not None:
            headers["Depth"] = depth
        response = self.client.request(method, url, content=body.encode("utf-8"), headers=headers)
        response.raise_for_status()
        return response

    def discover_addressbooks(self) -> list[dict[str, str]]:
        body = """<?xml version="1.0" encoding="utf-8" ?>
<d:propfind xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">
  <d:prop>
    <d:displayname/>
    <d:resourcetype/>
  </d:prop>
</d:propfind>"""
        response = self._request("PROPFIND", self.addressbook_home, body=body, depth="1")
        root = ET.fromstring(response.content)
        out: list[dict[str, str]] = []
        for resp in root.findall("d:response", NS):
            href = (resp.findtext("d:href", default="", namespaces=NS) or "").strip()
            if not href:
                continue
            prop = None
            for propstat in resp.findall("d:propstat", NS):
                status = propstat.findtext("d:status", default="", namespaces=NS)
                if " 200 " in status:
                    prop = propstat.find("d:prop", NS)
                    break
            if prop is None:
                continue
            rt = prop.find("d:resourcetype", NS)
            if rt is None or rt.find("card:addressbook", NS) is None:
                continue
            displayname = (prop.findtext("d:displayname", default="", namespaces=NS) or "").strip()
            full_url = urljoin(self.settings.dav_url.rstrip("/") + "/", href)
            slug = urlparse(full_url).path.rstrip("/").rsplit("/", 1)[-1]
            out.append({"href": full_url, "displayname": displayname or slug, "slug": slug})
        return out

    def selected_addressbooks(self) -> list[dict[str, str]]:
        books = self.discover_addressbooks()
        includes = {x.casefold() for x in self.settings.include_addressbooks}
        excludes = {x.casefold() for x in self.settings.exclude_addressbooks}

        def selected(book: dict[str, str]) -> bool:
            keys = {book["displayname"].casefold(), book["slug"].casefold()}
            if includes and not (keys & includes):
                return False
            if keys & excludes:
                return False
            return True

        return [book for book in books if selected(book)]

    def contacts(self, addressbook_href: str) -> list[dict[str, str]]:
        body = """<?xml version="1.0" encoding="utf-8" ?>
<card:addressbook-query xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">
  <d:prop>
    <d:getetag/>
    <card:address-data/>
  </d:prop>
</card:addressbook-query>"""
        response = self._request("REPORT", addressbook_href, body=body, depth="1")
        root = ET.fromstring(response.content)
        out: list[dict[str, str]] = []
        for resp in root.findall("d:response", NS):
            href = (resp.findtext("d:href", default="", namespaces=NS) or "").strip()
            if not href:
                continue
            etag = ""
            vcard_text = ""
            for propstat in resp.findall("d:propstat", NS):
                status = propstat.findtext("d:status", default="", namespaces=NS)
                if " 200 " not in status:
                    continue
                prop = propstat.find("d:prop", NS)
                if prop is None:
                    continue
                etag = (prop.findtext("d:getetag", default="", namespaces=NS) or "").strip()
                vcard_text = (prop.findtext("card:address-data", default="", namespaces=NS) or "").strip()
            if vcard_text:
                out.append({
                    "href": urljoin(self.settings.dav_url.rstrip("/") + "/", href),
                    "etag": etag,
                    "vcard": vcard_text,
                })
        return out


def _contents(card, name: str):
    return list(card.contents.get(name.lower(), []) or [])


def _single_text(card, name: str) -> str:
    values = _contents(card, name)
    if not values:
        return ""
    return str(values[0].value or "").strip()


def _org_parts(card) -> tuple[str, list[str]]:
    """Return (organization, organizational_units) from vCard ORG.

    vCard 3.0 encodes ORG as structured components separated by semicolons:
    the first component is the organization name, subsequent components are
    organizational units.  They are *not* aliases or separate organizations.
    Example: ORG:Amtsgericht Musterstadt;Insolvenzgericht
    """
    values = _contents(card, "org")
    if not values:
        return "", []
    value = values[0].value
    if isinstance(value, (list, tuple)):
        parts = [str(x).strip() for x in value if str(x).strip()]
    else:
        # vobject normally returns a list for structured ORG values, but keep
        # a conservative fallback for imported/non-standard cards.
        raw = str(value or "").strip()
        parts = [x.strip() for x in raw.split(";") if x.strip()] if raw else []
    if not parts:
        return "", []
    return parts[0], parts[1:]


def _address_text(adr) -> str:
    value = adr.value
    fields = []
    for attr in ("box", "extended", "street", "city", "region", "code", "country"):
        part = getattr(value, attr, "")
        if isinstance(part, (list, tuple)):
            part = " ".join(str(x).strip() for x in part if str(x).strip())
        part = str(part or "").strip()
        if part:
            fields.append(part)
    return ", ".join(fields)


def _sanitize_vcard_for_retry(vcard_text: str) -> tuple[str, bool]:
    """Apply only conservative repairs for malformed CardDAV vCards.

    Some legacy address books contain standalone literal ``\\n``/``\\r``
    marker lines.  They are not valid vCard content lines and vobject rejects the
    whole card.  Blank physical lines and these standalone markers carry no
    contact data, so they can safely be discarded for a single retry.
    """
    raw = str(vcard_text or "").replace("\ufeff", "").replace("\x00", "")
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cleaned: list[str] = []
    changed = raw != str(vcard_text or "")
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped in {r"\n", r"\r", r"\r\n"}:
            changed = True
            continue
        # Some legacy exports contain a physical line beginning with a literal
        # escaped newline marker, e.g. ``\nGiselastraße 6``.  This is not a
        # valid vCard content line; it is almost certainly the continuation of
        # the preceding property value.  Re-attach it verbatim so the ``\n``
        # remains an escaped newline inside that property's value.
        if (stripped.startswith(r"\n") or stripped.startswith(r"\r")) and cleaned:
            cleaned[-1] = cleaned[-1] + stripped
            changed = True
            continue
        cleaned.append(line)
    normalized = "\r\n".join(cleaned)
    if normalized and not normalized.endswith("\r\n"):
        normalized += "\r\n"
    return normalized, changed


def parse_vcard(vcard_text: str) -> dict[str, Any]:
    import vobject
    repaired = False
    try:
        card = vobject.readOne(vcard_text)
    except Exception as first_exc:
        cleaned, changed = _sanitize_vcard_for_retry(vcard_text)
        if not changed or cleaned == vcard_text:
            raise
        try:
            card = vobject.readOne(cleaned)
            repaired = True
        except Exception:
            raise first_exc
    uid = _single_text(card, "uid")
    fn = _single_text(card, "fn")
    org, organization_units = _org_parts(card)
    kind = _single_text(card, "kind").casefold()

    given = family = additional = prefix = suffix = ""
    n_values = _contents(card, "n")
    if n_values:
        n = n_values[0].value
        given = str(getattr(n, "given", "") or "").strip()
        family = str(getattr(n, "family", "") or "").strip()
        additional_raw = getattr(n, "additional", "") or ""
        if isinstance(additional_raw, (list, tuple)):
            additional = " ".join(str(x).strip() for x in additional_raw if str(x).strip())
        else:
            additional = str(additional_raw).strip()
        prefix = str(getattr(n, "prefix", "") or "").strip()
        suffix = str(getattr(n, "suffix", "") or "").strip()

    # vCard KIND is authoritative when present.  In practice Nextcloud and
    # imported address books often omit KIND and may even put an institution
    # name into the family-name slot of N (e.g. N:Amtsgericht Musterstadt;;;;).
    # Therefore a *single* populated N component is not enough to call a
    # contact a person.
    normalized_fn = normalize_name(fn)
    normalized_org = normalize_name(org)
    personal_prefixes = {"herr", "frau", "mr", "mrs", "ms", "miss"}
    prefix_tokens = set(normalize_name(prefix).split()) | set(normalized_fn.split()[:1])
    explicit_person_prefix = bool(prefix_tokens & personal_prefixes)

    # If FN is the organization itself or its leading department-less name,
    # treat the card as an organization.  This covers e.g.
    #   FN:Amtsgericht Musterstadt
    #   ORG:Amtsgericht Musterstadt;Insolvenzgericht
    org_name_matches_fn = False
    if normalized_fn and normalized_org:
        org_name_matches_fn = (
            normalized_fn == normalized_org
            or normalized_org.startswith(normalized_fn + " ")
            or normalized_fn.startswith(normalized_org + " ")
        )

    organization_markers = (
        "amtsgericht", "landgericht", "oberlandesgericht", "bundesgericht",
        "verwaltungsgericht", "arbeitsgericht", "sozialgericht",
        "finanzgericht", "staatsanwaltschaft", "finanzamt", "ministerium",
        "behörde", "behoerde", "stadtverwaltung", "gemeinde", "universität",
        "universitaet", "hochschule", "institut", "stiftung", "verein",
        "kanzlei", "rechtsanwälte", "rechtsanwaelte", "sparkasse", "bank",
        "versicherung", "krankenhaus", "klinik", "schule",
        " gmbh", " ag", " ug", " kg", " ohg", " gbr", " e v",
    )
    looks_institutional = any(
        marker.strip() == normalized_fn
        or normalized_fn.startswith(marker.strip() + " ")
        or (marker.startswith(" ") and marker in " " + normalized_fn)
        for marker in organization_markers
    )

    if kind in {"org", "organization", "group"}:
        entity_type = "Organization"
    elif kind in {"individual", "person"}:
        entity_type = "Person"
    elif explicit_person_prefix:
        entity_type = "Person"
    elif looks_institutional:
        # Explicit institutional wording in FN beats a malformed/overloaded N.
        entity_type = "Organization"
    elif given and family:
        # Two genuinely populated structured name components are a much
        # stronger personal signal than a lone family field.
        entity_type = "Person"
    elif org_name_matches_fn:
        entity_type = "Organization"
    else:
        entity_type = "Person"

    structured = " ".join(x for x in (given, additional, family) if x).strip()
    if entity_type == "Person":
        # Prefer the salutation-free structured name as canonical display name
        # when available; FN remains a recorded real name form below.
        display_name = structured or fn or org
        if not structured and fn:
            display_name = re.sub(
                r"^(?:Herr|Frau|Mr\.?|Mrs\.?|Ms\.?|Miss)\s+",
                "",
                fn,
                flags=re.IGNORECASE,
            ).strip() or fn
    else:
        # For organization contacts the structured ORG value is the actual
        # organization identity.  FN is only the formatted/display label chosen
        # by the address book UI and may omit the legal form.
        display_name = org or fn or structured

    if not display_name:
        display_name = "Unbenannter Kontakt"

    names: list[dict[str, Any]] = []
    if entity_type == "Person" and structured:
        names.append({"value": structured, "kind": "structured", "preferred": True})
        if fn and normalize_name(fn) != normalize_name(structured):
            names.append({"value": fn, "kind": "formatted", "preferred": False})
    elif entity_type == "Organization":
        if org:
            names.append({"value": org, "kind": "organization", "preferred": True})
        if fn and normalize_name(fn) != normalize_name(org):
            names.append({"value": fn, "kind": "formatted", "preferred": False})
        elif not org and fn:
            names.append({"value": fn, "kind": "formatted", "preferred": True})
        elif not org and structured:
            names.append({"value": structured, "kind": "structured", "preferred": True})
    elif fn:
        names.append({"value": fn, "kind": "formatted", "preferred": True})
    elif structured:
        names.append({"value": structured, "kind": "structured", "preferred": True})

    # Nicknames/artist names are real recorded name forms, not generated aliases.
    for nickname_prop in _contents(card, "nickname"):
        raw = nickname_prop.value
        parts = raw if isinstance(raw, (list, tuple)) else re.split(r"\s*,\s*", str(raw or ""))
        for part in parts:
            part = str(part or "").strip()
            if part:
                names.append({"value": part, "kind": "nickname", "preferred": False})

    # Common non-standard maiden-name properties are preserved if present.
    for key in ("x-maidenname", "x-maiden-name", "x-previous-name"):
        for prop in _contents(card, key):
            value = str(prop.value or "").strip()
            if value:
                names.append({"value": value, "kind": "former", "preferred": False})

    # Remove duplicate name spellings.
    dedup_names: dict[str, dict[str, Any]] = {}
    for item in names:
        norm = normalize_name(item["value"])
        if norm and norm not in dedup_names:
            dedup_names[norm] = item
    names = list(dedup_names.values())

    aliases = (
        generated_person_aliases(given, family, additional)
        if entity_type == "Person"
        else generated_organization_aliases(display_name)
    )

    emails = [str(prop.value or "").strip() for prop in _contents(card, "email")]
    phones = [str(prop.value or "").strip() for prop in _contents(card, "tel")]
    addresses = [_address_text(prop) for prop in _contents(card, "adr")]

    return {
        "_repaired": repaired,
        "uid": uid,
        "entity_type": entity_type,
        "display_name": display_name,
        "names": names,
        "aliases": aliases,
        "emails": [x for x in emails if x],
        "phones": [x for x in phones if x],
        "addresses": [x for x in addresses if x],
        "organization_name": org if entity_type == "Person" else "",
        "organization_units": organization_units,
        "structured_name": {
            "given": given,
            "family": family,
            "additional": additional,
            "prefix": prefix,
            "suffix": suffix,
        },
    }


def _new_import_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"carddav:{stamp}:{uuid.uuid4().hex[:10]}"


def sync(
    cfg: dict[str, Any], *, dry_run: bool = False, limit: int = 0,
    settings: CardDAVSettings | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    settings = settings or CardDAVSettings.from_config(cfg)
    run_id = "" if dry_run else _new_import_run_id()
    summary = {
        "import_run_id": run_id or None,
        "cloud_id": settings.cloud_id,
        "source_user_id": settings.user_id,
        "addressbooks": 0,
        "contacts_total": 0,
        "contacts_seen": 0,
        "contacts_written": 0,
        "contacts_repaired": 0,
        "contacts_deleted": 0,
        "errors": [],
        "books": [],
    }

    with CardDAVClient(settings) as dav:
        books = dav.selected_addressbooks()
        summary["addressbooks"] = len(books)
        if progress:
            progress({**summary, "phase": "discovering", "current_addressbook": ""})

        book_contacts: list[tuple[dict[str, str], list[dict[str, str]]]] = []
        for book in books:
            contacts = dav.contacts(book["href"])
            book_contacts.append((book, contacts))
            summary["contacts_total"] += len(contacts)
            if progress:
                progress({
                    **summary,
                    "phase": "discovering",
                    "current_addressbook": book["displayname"],
                })
        if limit:
            summary["contacts_total"] = min(int(summary["contacts_total"]), max(0, int(limit)))

        graph = None if dry_run else GraphStore.from_config(cfg)
        run_started = False
        fatal_error: Exception | None = None
        try:
            if graph:
                graph.verify_connectivity()
                graph.ensure_schema()
                graph.start_contact_import(
                    run_id,
                    cloud_id=settings.cloud_id,
                    source_user_id=settings.user_id,
                    addressbooks=books,
                )
                run_started = True

            stop = False
            for book, contacts in book_contacts:
                if progress:
                    progress({
                        **summary,
                        "phase": "importing",
                        "current_addressbook": book["displayname"],
                    })
                book_info = {
                    "displayname": book["displayname"],
                    "slug": book["slug"],
                    "contacts": len(contacts),
                }
                summary["books"].append(book_info)

                for raw in contacts:
                    if limit and summary["contacts_seen"] >= limit:
                        stop = True
                        break
                    summary["contacts_seen"] += 1
                    try:
                        parsed = parse_vcard(raw["vcard"])
                        if parsed.pop("_repaired", False):
                            summary["contacts_repaired"] += 1
                            print(f"REPARIERT {raw.get('href')}: konservative vCard-Bereinigung angewendet")
                        # Keep the historic contact_id algorithm so an update does
                        # not duplicate existing seeds. Explicit provenance is a
                        # separate property set; GraphStore refuses a provenance
                        # collision once those fields are populated.
                        contact_id = stable_contact_id(book["href"], parsed["uid"], raw["href"])
                        if dry_run:
                            print(json.dumps({
                                "contact_id": contact_id,
                                "cloud_id": settings.cloud_id,
                                "source_user_id": settings.user_id,
                                "book": book["displayname"],
                                "book_slug": book["slug"],
                                "href": raw["href"],
                                **parsed,
                            }, ensure_ascii=False))
                            continue

                        result = graph.upsert_contact(
                            contact_id=contact_id,
                            vcard_uid=parsed["uid"],
                            href=raw["href"],
                            etag=raw["etag"],
                            addressbook_href=book["href"],
                            addressbook_name=book["displayname"],
                            addressbook_slug=book["slug"],
                            cloud_id=settings.cloud_id,
                            source_user_id=settings.user_id,
                            import_run_id=run_id,
                            entity_type=parsed["entity_type"],
                            display_name=parsed["display_name"],
                            names=parsed["names"],
                            aliases=parsed["aliases"],
                            emails=parsed["emails"],
                            phones=parsed["phones"],
                            addresses=parsed["addresses"],
                            organization_name=parsed["organization_name"],
                            organization_units=parsed["organization_units"],
                        )
                        summary["contacts_written"] += 1
                        print(
                            f"[{summary['contacts_seen']}] {parsed['display_name']} -> "
                            f"{result['entity_id']} ({result['resolved_by']})"
                        )
                    except Exception as exc:
                        summary["errors"].append({"href": raw.get("href"), "error": str(exc)})
                        print(f"FEHLER {raw.get('href')}: {exc}")
                    finally:
                        if progress:
                            progress({
                                **summary,
                                "phase": "importing",
                                "current_addressbook": book["displayname"],
                            })
                if stop:
                    break

            # Only a complete, non-limited sync is authoritative for source
            # deletions. CardDAV hrefs were collected before vCard parsing, so
            # malformed cards that still exist at the server remain protected.
            if graph and not limit:
                for book, contacts in book_contacts:
                    result = graph.reconcile_contact_addressbook(
                        cloud_id=settings.cloud_id,
                        source_user_id=settings.user_id,
                        addressbook_href=book["href"],
                        current_hrefs=[str(raw.get("href") or "") for raw in contacts],
                    )
                    deleted = int(result.get("removed_contact_records") or 0)
                    summary["contacts_deleted"] += deleted
                    if deleted:
                        print(f"GELÖSCHT {book['displayname']}: {deleted} nicht mehr vorhandene ContactRecord(s)")
                    if progress:
                        progress({**summary, "phase": "reconciling", "current_addressbook": book["displayname"]})
        except Exception as exc:
            fatal_error = exc
            raise
        finally:
            if graph:
                if run_started:
                    status = "failed" if fatal_error is not None else ("completed_with_errors" if summary["errors"] else "completed")
                    try:
                        graph.finish_contact_import(
                            run_id,
                            status=status,
                            contacts_seen=int(summary["contacts_seen"]),
                            contacts_written=int(summary["contacts_written"]),
                            error_count=len(summary["errors"]),
                            contacts_deleted=int(summary.get("contacts_deleted") or 0),
                        )
                    except Exception as finish_exc:
                        summary["errors"].append({"href": "_import_run", "error": str(finish_exc)})
                graph.close()

    if progress:
        progress({**summary, "phase": "completed", "current_addressbook": ""})
    return summary


def _store_from_config(cfg: dict[str, Any], explicit_path: str = "") -> CredentialStore:
    path = str(
        explicit_path
        or cfg_get(cfg, "auth.credential_store", default=cfg_get(cfg, "acl.credential_store", default="runtime/users.sqlite"))
        or "runtime/users.sqlite"
    )
    return CredentialStore(path)


def resolve_nextcloud_user(
    store: CredentialStore, login: str, *, server: str = ""
) -> CanonicalUser | None:
    matches = store.find_canonical_users(login, server=server)
    if not matches:
        return None
    if len(matches) > 1:
        choices = ", ".join(user.nextcloud_server for user in matches)
        raise ValueError(
            f"Nextcloud-Login {login!r} ist auf mehreren Instanzen vorhanden ({choices}); "
            "bitte --server angeben"
        )
    return matches[0]


def sync_for_canonical_user(
    cfg: dict[str, Any],
    store: CredentialStore,
    user: CanonicalUser,
    *,
    dry_run: bool = False,
    limit: int = 0,
    force: bool = False,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Sync one Nextcloud user's CardDAV source using the stored login-flow credential.

    Missing/disabled user state and a missing credential are operational no-ops,
    not stack failures. This is the path used by the admin UI and dockerized CLI.
    """
    if not user.enabled:
        return {
            "status": "skipped", "reason": "user_disabled",
            "source_user_id": user.nextcloud_login, "cloud_id": user.nextcloud_server,
            "addressbooks": 0, "contacts_seen": 0, "contacts_written": 0, "contacts_deleted": 0, "errors": [], "books": [],
        }

    contact_settings = store.get_contact_sync_settings(user.canonical_user_id)
    if contact_settings is not None and not contact_settings.enabled and not force:
        return {
            "status": "skipped", "reason": "contact_sync_disabled",
            "source_user_id": user.nextcloud_login, "cloud_id": user.nextcloud_server,
            "addressbooks": 0, "contacts_seen": 0, "contacts_written": 0, "contacts_deleted": 0, "errors": [], "books": [],
        }

    credential = store.get_nextcloud_credential_for_canonical_user(user.canonical_user_id)
    if credential is None:
        return {
            "status": "skipped", "reason": "nextcloud_credential_missing",
            "source_user_id": user.nextcloud_login, "cloud_id": user.nextcloud_server,
            "addressbooks": 0, "contacts_seen": 0, "contacts_written": 0, "contacts_deleted": 0, "errors": [], "books": [],
        }

    settings = CardDAVSettings.for_canonical_user(cfg, user, credential, contact_settings)
    try:
        summary = sync(
            cfg, dry_run=dry_run, limit=max(0, int(limit)), settings=settings, progress=progress
        )
    except Exception as exc:
        if not dry_run:
            store.record_contact_sync_result(
                user.canonical_user_id, status="failed", error_count=1, last_error=f"{type(exc).__name__}: {exc}"
            )
        raise

    summary["status"] = "completed_with_errors" if summary.get("errors") else "completed"
    summary["nextcloud_server"] = user.nextcloud_server
    summary["nextcloud_login"] = user.nextcloud_login
    if not dry_run:
        errors = list(summary.get("errors") or [])
        store.record_contact_sync_result(
            user.canonical_user_id,
            status=str(summary["status"]),
            contacts_seen=int(summary.get("contacts_seen") or 0),
            contacts_written=int(summary.get("contacts_written") or 0),
            error_count=len(errors),
            last_error=(str(errors[0].get("error") or "") if errors and isinstance(errors[0], dict) else ""),
        )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync Nextcloud CardDAV contacts into Neo4j")
    parser.add_argument("--config", default=str(BASE_DIR / "config.yaml"))
    parser.add_argument("--user", default="", help="Nextcloud login; preferred per-user mode")
    parser.add_argument("--server", default="", help="Nextcloud base URL when the login is ambiguous")
    parser.add_argument("--store", default="", help="CredentialStore path (normally from config)")
    parser.add_argument("--dry-run", action="store_true", help="Read/parse CardDAV, write nothing to Neo4j")
    parser.add_argument("--limit", type=int, default=0, help="Maximum number of contacts (0=all)")
    parser.add_argument("--list-books", action="store_true", help="Only list discovered/selected address books")
    parser.add_argument("--force", action="store_true", help="Run an explicitly disabled user's contact sync")
    args = parser.parse_args()

    cfg = load_config(args.config)

    if args.user:
        store = _store_from_config(cfg, args.store)
        try:
            user = resolve_nextcloud_user(store, args.user, server=args.server)
        except ValueError as exc:
            parser.error(str(exc))
        if user is None:
            print(json.dumps({
                "status": "skipped", "reason": "unknown_nextcloud_user",
                "nextcloud_login": args.user, "contacts_seen": 0, "contacts_written": 0, "errors": [],
            }, ensure_ascii=False, indent=2))
            return 0
        credential = store.get_nextcloud_credential_for_canonical_user(user.canonical_user_id)
        if credential is None:
            print(json.dumps({
                "status": "skipped", "reason": "nextcloud_credential_missing",
                "nextcloud_login": user.nextcloud_login, "nextcloud_server": user.nextcloud_server,
                "contacts_seen": 0, "contacts_written": 0, "errors": [],
            }, ensure_ascii=False, indent=2))
            return 0
        settings = CardDAVSettings.for_canonical_user(
            cfg, user, credential, store.get_contact_sync_settings(user.canonical_user_id)
        )
        if args.list_books:
            with CardDAVClient(settings) as dav:
                print(json.dumps(dav.selected_addressbooks(), ensure_ascii=False, indent=2))
            return 0
        summary = sync_for_canonical_user(
            cfg, store, user, dry_run=args.dry_run, limit=max(0, args.limit), force=args.force
        )
    else:
        # Legacy global credential mode retained for existing scripts only.
        settings = CardDAVSettings.from_config(cfg)
        if args.list_books:
            with CardDAVClient(settings) as dav:
                print(json.dumps(dav.selected_addressbooks(), ensure_ascii=False, indent=2))
            return 0
        summary = sync(cfg, dry_run=args.dry_run, limit=max(0, args.limit), settings=settings)

    print("\n--- Zusammenfassung ---")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not summary.get("errors") else 2


if __name__ == "__main__":
    raise SystemExit(main())
