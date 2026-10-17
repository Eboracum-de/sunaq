import re
import sys
import builtins
from typing import Any

from pydantic import BaseModel, Field
from rag.logging_utils import get_logger
from rag.search_text import normalize_query_quotes

log = get_logger("api")

def _module_print(*args, **kwargs):
    if __name__ == "__main__":
        return builtins.print(*args, **kwargs)
    text = " ".join(str(x) for x in args)
    if text.strip():
        log.debug(text)

print = _module_print


# ------------------------------------------------------------
# Datenmodell
#
# Bleibt kompatibel mit search.py.
# ------------------------------------------------------------

class SearchPlan(BaseModel):

    search_mode: str

    semantic_query: str

    # Legacy compatibility only. New structured retrieval intent never grants
    # the planner ownership of backend query syntax.
    elastic_query: str = ""

    # Backend-neutral lexical groups compiled deterministically by search.py.
    # Each group is an OR over terms; required groups become MUST(OR(...)).
    lexical_groups: list[dict[str, Any]] = Field(
        default_factory=list
    )

    must: list[str] = Field(
        default_factory=list
    )

    should: list[str] = Field(
        default_factory=list
    )

    must_not: list[str] = Field(
        default_factory=list
    )

    phrases: list[str] = Field(
        default_factory=list
    )

    must_not_phrases: list[str] = Field(
        default_factory=list
    )

    # Entity-aware Query Preparation.  These fields are optional additions
    # and keep the historic planner contract backwards-compatible.
    entity_mentions: list[dict[str, Any]] = Field(
        default_factory=list
    )

    entity_should_phrases: list[dict[str, Any]] = Field(
        default_factory=list
    )

    # Für direkte Relationsfragen werden die Varianten derselben Entity
    # innerhalb einer Gruppe OR-verknüpft; mehrere Entity-Gruppen können
    # anschließend im Elasticsearch-Arm als verpflichtende Gruppen dienen.
    entity_groups: list[dict[str, Any]] = Field(
        default_factory=list
    )

    entity_relation_query: bool = False

    # Zweiter Retrieval-Pass bei unzureichender Evidenz: Die erkannte einzelne
    # Entity bleibt erhalten, ihre Namensbestandteile dürfen aber als schwache
    # lexikalische Recall-Anker verwendet werden.
    entity_recall_backoff: bool = False

    entity_recall_terms: list[dict[str, Any]] = Field(
        default_factory=list
    )

    date_from: str | None = None

    date_to: str | None = None

    notes: str = ""


# ------------------------------------------------------------
# Deutsche Stopwörter
#
# Diese Wörter sind für die lexikalische Elasticsearch-Suche
# normalerweise wenig hilfreich.
#
# WICHTIG:
# Die vollständige Originalfrage bleibt für die semantische
# Suche unverändert erhalten.
# ------------------------------------------------------------

STOPWORDS = {

    # Artikel
    "der",
    "die",
    "das",
    "den",
    "dem",
    "des",

    "ein",
    "eine",
    "einer",
    "einem",
    "einen",
    "eines",

    # Pronomen
    "ich",
    "du",
    "er",
    "sie",
    "es",
    "wir",
    "ihr",

    "mir",
    "mich",
    "dir",
    "dich",
    "ihm",
    "ihn",
    "ihnen",

    "mein",
    "meine",
    "dein",
    "deine",
    "sein",
    "seine",
    "ihr",
    "ihre",

    "jemand",
    "jemanden",
    "jemandem",

    # Fragewörter
    "wer",
    "wie",
    "wo",
    "wann",
    "warum",
    "wieso",
    "weshalb",

    "welche",
    "welcher",
    "welches",
    "welchen",
    "welchem",

    "was",

    # Konjunktionen
    "und",
    "oder",
    "aber",
    "denn",
    "sondern",
    "sowie",

    "dass",
    "daß",
    "ob",
    "obwohl",
    "wenn",
    "falls",

    # Präpositionen
    "in",
    "im",
    "an",
    "am",
    "auf",
    "aus",
    "bei",
    "mit",
    "von",
    "vom",
    "zu",
    "zum",
    "zur",

    "für",
    "fuer",
    "über",
    "ueber",
    "unter",

    "vor",
    "hinter",
    "neben",

    "durch",
    "gegen",
    "ohne",
    "um",

    # Häufige Hilfsverben
    "ist",
    "sind",
    "war",
    "waren",

    "sein",
    "gewesen",

    "hat",
    "haben",
    "hatte",
    "hatten",

    "wird",
    "werden",
    "wurde",
    "wurden",

    "kann",
    "können",
    "koennen",

    "soll",
    "sollen",

    # Typische Frage-/Füllformulierungen
    "findest",
    "finde",
    "finden",

    "gibt",
    "gebe",

    "geht",
    "gehen",

    "darum",
    "dazu",
    "darin",
    "dabei",

    "informationen",
    "information",

    # Produkt-/Systemnamen duerfen kein globales Stopwort sein: In Enterprise-
    # Dokumenten koennen sie selbst der gesuchte Sachgegenstand sein (z. B.
    # "Bereitstellung Nextcloud"). Allgemeine Begriffe bleiben ggf. Stopwoerter.
    "daten",

    # Negation:
    #
    # Für Elasticsearch ist "nicht" meist kein guter
    # lexikalischer Anker. In der semantic_query bleibt
    # die Negation vollständig erhalten.
    "nicht",
}


# ------------------------------------------------------------
# Normalisierung
# ------------------------------------------------------------

def normalize_question(
    question: str,
) -> str:

    question = question.strip()

    question = re.sub(
        r"\s+",
        " ",
        question,
    )

    return question


# ------------------------------------------------------------
# Wörter extrahieren
# ------------------------------------------------------------

def get_words(
    question: str,
) -> list[str]:

    return re.findall(
        r"[A-Za-zÄÖÜäöüß0-9]"
        r"[A-Za-zÄÖÜäöüß0-9._/-]*",
        question,
    )


# ------------------------------------------------------------
# Explizite Suchsyntax des Benutzers
#
#   foo             -> freier Begriff / SHOULD
#   +foo            -> MUST
#   -foo            -> MUST NOT
#   "foo bar"       -> verpflichtende Phrase
#   +"foo bar"      -> verpflichtende Phrase
#   -"foo bar"      -> ausgeschlossene Phrase
#
# Diese Syntax wird deterministisch vor der Planner-Heuristik
# ausgewertet. Ein expliziter Benutzeroperator darf dadurch nicht
# durch Stopwortfilter oder andere Heuristiken verloren gehen.
# ------------------------------------------------------------

class ParsedSearchSyntax(BaseModel):

    free_words: list[str] = Field(
        default_factory=list
    )

    must: list[str] = Field(
        default_factory=list
    )

    must_not: list[str] = Field(
        default_factory=list
    )

    phrases: list[str] = Field(
        default_factory=list
    )

    must_not_phrases: list[str] = Field(
        default_factory=list
    )


_PHRASE_RE = re.compile(
    r'(?P<modifier>[+-]?)"(?P<phrase>[^"\r\n]+)"'
)

_WORD_WITH_MODIFIER_RE = re.compile(
    r'(?<![A-Za-zÄÖÜäöüß0-9._/-])'
    r'(?P<modifier>[+-]?)'
    r'(?P<word>[A-Za-zÄÖÜäöüß0-9]'
    r'[A-Za-zÄÖÜäöüß0-9._/-]*)'
)


def _append_unique(
    target: list[str],
    value: str,
) -> None:

    value = value.strip()

    if not value:
        return

    folded = value.casefold()

    if any(
        item.casefold() == folded
        for item in target
    ):
        return

    target.append(
        value
    )


def parse_search_syntax(
    question: str,
) -> ParsedSearchSyntax:

    question = normalize_query_quotes(question)
    parsed = ParsedSearchSyntax()

    # --------------------------------------------------------
    # Zuerst Phrasen extrahieren und ihre Bereiche maskieren,
    # damit deren Einzelwörter später nicht nochmals als freie
    # Begriffe erscheinen.
    # --------------------------------------------------------

    masked = list(
        question
    )

    for match in _PHRASE_RE.finditer(
        question
    ):

        modifier = match.group(
            "modifier"
        )

        phrase = re.sub(
            r"\s+",
            " ",
            match.group("phrase").strip(),
        )

        if phrase:

            if modifier == "-":

                _append_unique(
                    parsed.must_not_phrases,
                    phrase,
                )

            else:

                # Sowohl "foo bar" als auch +"foo bar" sind
                # eine verpflichtende Phrase.
                _append_unique(
                    parsed.phrases,
                    phrase,
                )

        for index in range(
            match.start(),
            match.end(),
        ):
            masked[index] = " "

    remainder = "".join(
        masked
    )

    # --------------------------------------------------------
    # Danach einzelne Wörter inklusive + / - auswerten.
    # --------------------------------------------------------

    for match in _WORD_WITH_MODIFIER_RE.finditer(
        remainder
    ):

        modifier = match.group(
            "modifier"
        )

        word = match.group(
            "word"
        )

        if modifier == "+":

            _append_unique(
                parsed.must,
                word,
            )

        elif modifier == "-":

            _append_unique(
                parsed.must_not,
                word,
            )

        else:

            _append_unique(
                parsed.free_words,
                word,
            )

    return parsed


# ------------------------------------------------------------
# Hilfreiche lexikalische Begriffe extrahieren
# ------------------------------------------------------------

def get_search_terms(
    question: str,
) -> list[str]:

    parsed = parse_search_syntax(
        question
    )

    terms = []
    seen = set()


    for word in parsed.free_words:

        lower = word.casefold()


        # Stopwort?
        if lower in STOPWORDS:

            continue


        # Einzelbuchstaben usw. ignorieren.
        if len(word) < 2:

            continue


        # Doppelte Begriffe vermeiden,
        # aber ursprüngliche Schreibweise erhalten.
        if lower in seen:

            continue


        seen.add(
            lower
        )

        terms.append(
            word
        )


    return terms


# ------------------------------------------------------------
# Kurze Anfrage:
#
# 1 bis 3 Wörter
#
# Deterministischer Planner-Fast-Path. Er spart weiterhin den aufwendigeren
# Planungsweg, unterdrückt aber keinen Retrieval-Arm: Elasticsearch erhält
# lexikalische Terme und Qdrant die bereinigte Originalfrage.
# ------------------------------------------------------------

def create_lexical_plan(
    question: str,
) -> SearchPlan:

    parsed = parse_search_syntax(
        question
    )

    should = []


    # --------------------------------------------------------
    # Nur unmarkierte Einzelbegriffe werden als SHOULD geführt.
    # Explizite + / - / "..."-Angaben bleiben ausschließlich
    # in ihren dafür vorgesehenen Feldern.
    # --------------------------------------------------------

    for word in parsed.free_words:

        _append_unique(
            should,
            word,
        )


    # --------------------------------------------------------
    # Historisches Boosting für reine, unmarkierte Kurzsuchen
    # beibehalten: "Max Mustermann" erhält zusätzlich den
    # gemeinsamen AND-multi_match "Max Mustermann". Sobald der
    # Benutzer explizite Operatoren verwendet, wird nichts
    # künstlich kombiniert.
    # --------------------------------------------------------

    has_explicit_syntax = bool(
        parsed.must
        or parsed.must_not
        or parsed.phrases
        or parsed.must_not_phrases
    )

    if (
        len(parsed.free_words) > 1
        and not has_explicit_syntax
    ):

        combined = " ".join(
            parsed.free_words
        )

        _append_unique(
            should,
            combined,
        )


    words = get_words(
        question
    )


    return SearchPlan(

        search_mode="hybrid",

        # Der Fast-Path ist nur ein Planner-Shortcut. Die Auswahl der tatsächlich
        # ausgeführten Arme erfolgt separat über retrieval_arms (/files, /vector).
        semantic_query=question,

        must=parsed.must,

        should=should,

        must_not=parsed.must_not,

        phrases=parsed.phrases,

        must_not_phrases=(
            parsed.must_not_phrases
        ),

        date_from=None,

        date_to=None,

        notes=(
            "Deterministischer Kurzquery-Fast-Path: "
            f"{len(words)} Wörter; lexikalisch + semantisch vorbereitet"
        ),
    )


# ------------------------------------------------------------
# Längere Anfrage:
#
# Echte Hybrid-Suche:
#
# 1. Originalfrage unverändert -> nomic/Qdrant
# 2. Inhaltliche Einzelbegriffe -> Elasticsearch
# ------------------------------------------------------------

def create_hybrid_plan(
    question: str,
) -> SearchPlan:

    parsed = parse_search_syntax(
        question
    )

    search_terms = get_search_terms(
        question
    )


    return SearchPlan(

        search_mode="hybrid",

        # ----------------------------------------------------
        # Die komplette ursprüngliche Frage geht unverändert
        # an nomic-embed-text. Die explizite Suchsyntax steuert
        # in diesem Patch den Elasticsearch-Arm; Qdrant bleibt
        # semantisch frei.
        # ----------------------------------------------------

        semantic_query=question,

        must=parsed.must,

        # ----------------------------------------------------
        # Elasticsearch erhält nur die unmarkierten,
        # inhaltstragenden Wörter als SHOULD-Klauseln.
        # ----------------------------------------------------

        should=search_terms,

        must_not=parsed.must_not,

        phrases=parsed.phrases,

        must_not_phrases=(
            parsed.must_not_phrases
        ),

        date_from=None,

        date_to=None,

        notes=(
            "Hybrid-Direktpfad: "
            "Originalfrage semantisch + "
            f"{len(search_terms)} freie ES-Begriffe; "
            "explizite Suchsyntax deterministisch"
        ),
    )




# ------------------------------------------------------------
# Generische Relationsformulierungen
#
# Sie beschreiben die Absicht der Frage, sind aber für die lexikalische
# Dokumentensuche selbst normalerweise kein brauchbarer Inhaltsanker.
# Bedeutungsvolle Prädikate wie "Geschäftsführer", "Darlehen" usw.
# werden ausdrücklich NICHT entfernt.
# ------------------------------------------------------------

_RELATION_QUERY_HINT_RE = re.compile(
    r"\b(?:verbindung|zusammenhang|beziehung|verhältnis|verhaeltnis|relation)\b",
    re.IGNORECASE,
)

_RELATION_FILLER_TERMS = {
    "verbindung",
    "besteht",
    "bestehen",
    "zwischen",
    "zusammenhang",
    "beziehung",
    "verhältnis",
    "verhaeltnis",
    "relation",
}


def _is_direct_relation_query(question: str, entities: list[dict[str, Any]]) -> bool:
    usable = [
        entity
        for entity in entities
        if str(entity.get("status") or "")
        in {"resolved", "ambiguous", "fuzzy_candidate"}
        and str(entity.get("mention") or "").strip()
    ]
    return len(usable) >= 2 and bool(_RELATION_QUERY_HINT_RE.search(question))


# ------------------------------------------------------------
# Entity-aware Ergänzung
#
# Die eigentliche Entity-Erkennung läuft absichtlich vor dem Planner.
# Hier werden nur die bereits erkannten Mentions in den lexikalischen
# Suchplan übersetzt.  semantic_query bleibt unverändert.
# ------------------------------------------------------------

def _entity_norm(value: str) -> str:
    value = str(value or "").casefold()
    value = re.sub(r"[^a-z0-9äöüßà-ž]+", " ", value, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", value).strip()


def _apply_entity_context(
    plan: SearchPlan,
    entity_context: dict[str, Any] | None,
    *,
    entity_recall: bool = False,
) -> SearchPlan:
    if not entity_context:
        return plan

    entities = list(entity_context.get("entities") or [])
    expansions = list(entity_context.get("elastic_phrase_expansion") or [])

    if not entities and not expansions:
        return plan

    plan.entity_mentions = entities

    protected_terms: set[str] = set()
    protected_mentions: set[str] = set()
    phrase_map: dict[str, dict[str, Any]] = {}

    def add_phrase(value: str, boost: float, **meta: Any) -> None:
        value = str(value or "").strip()
        normalized = _entity_norm(value)
        if len(normalized) < 2:
            return
        item = {
            "value": value,
            "boost": round(float(boost), 3),
            **meta,
        }
        old = phrase_map.get(normalized)
        if old is None or float(item["boost"]) > float(old.get("boost", 0.0)):
            phrase_map[normalized] = item

    # Original-Mentions schützen und als Phrase erhalten.
    for entity in entities:
        status = str(entity.get("status") or "")
        if status not in {"resolved", "ambiguous", "fuzzy_candidate"}:
            continue

        mention = str(entity.get("mention") or "").strip()
        mention_norm = _entity_norm(mention)
        if mention_norm:
            protected_mentions.add(mention_norm)
            protected_terms.update(mention_norm.split())

            original_boost = 4.2 if status != "fuzzy_candidate" else 3.4
            add_phrase(
                mention,
                original_boost,
                source="original_mention",
                status=status,
                entity_id=entity.get("entity_id"),
                entity_type=entity.get("entity_type"),
                resolution=entity.get("resolution_method"),
                original_mention=mention,
            )

    # Bekannte kanonische/alternative Formen ergänzen.
    for expansion in expansions:
        value = str(expansion.get("value") or "").strip()
        weight = float(expansion.get("weight", 0.5) or 0.5)
        resolution = str(expansion.get("resolution") or "")
        base = 2.8 if resolution == "fuzzy_candidate" else 4.4
        add_phrase(
            value,
            max(0.5, base * weight),
            source=expansion.get("source") or "entity_expansion",
            kind=expansion.get("kind"),
            status="expanded",
            entity_id=expansion.get("entity_id"),
            entity_type=expansion.get("entity_type"),
            resolution=resolution,
            similarity=expansion.get("similarity"),
            original_mention=expansion.get("original_mention"),
        )

    filtered_should: list[str] = []
    for term in plan.should:
        normalized = _entity_norm(term)
        if not normalized:
            continue
        if normalized in protected_mentions:
            continue
        if normalized in protected_terms:
            continue
        filtered_should.append(term)

    relation_query = _is_direct_relation_query(plan.semantic_query or "", entities)
    if relation_query:
        filtered_should = [
            term for term in filtered_should
            if _entity_norm(term) not in _RELATION_FILLER_TERMS
        ]

    plan.should = filtered_should
    plan.entity_should_phrases = sorted(
        phrase_map.values(),
        key=lambda item: float(item.get("boost", 0.0)),
        reverse=True,
    )
    plan.entity_relation_query = relation_query

    # --------------------------------------------------------
    # Entity-Gruppen bilden.
    #
    # Variante A1 OR A2 OR A3 bildet eine Entity-Gruppe. Bei einer direkten
    # Relationsfrage müssen anschließend ALLE erkannten Entity-Gruppen im
    # Dokument vorkommen. So wird aus "A und B" nicht "viel B reicht auch".
    # --------------------------------------------------------
    groups: list[dict[str, Any]] = []

    for entity in entities:
        status = str(entity.get("status") or "")
        mention = str(entity.get("mention") or "").strip()
        if status not in {"resolved", "ambiguous", "fuzzy_candidate"} or not mention:
            continue

        entity_id = entity.get("entity_id")
        inferred_type = entity.get("entity_type")
        if not inferred_type:
            candidate_types = {
                str(candidate.get("entity_type") or "")
                for candidate in (entity.get("candidates") or [])
                if str(candidate.get("entity_type") or "")
            }
            if len(candidate_types) == 1:
                inferred_type = next(iter(candidate_types))

        group_phrase_map: dict[str, dict[str, Any]] = {}

        def add_group_phrase(item: dict[str, Any]) -> None:
            value = str(item.get("value") or "").strip()
            norm = _entity_norm(value)
            if not norm:
                return
            old = group_phrase_map.get(norm)
            if old is None or float(item.get("boost", 0.0)) > float(old.get("boost", 0.0)):
                group_phrase_map[norm] = dict(item)

        mention_norm = _entity_norm(mention)
        if mention_norm in phrase_map:
            add_group_phrase(phrase_map[mention_norm])

        for phrase in phrase_map.values():
            phrase_entity_id = phrase.get("entity_id")
            phrase_original_mention = str(phrase.get("original_mention") or "")

            if entity_id and phrase_entity_id == entity_id:
                add_group_phrase(phrase)
                continue

            # Fuzzy-Expansionen können eine Candidate-ID tragen, obwohl die
            # Mention selbst bewusst noch unresolved bleibt. In diesem Fall
            # ordnen wir nur anhand der explizit mitgeführten Original-Mention
            # zu, nicht anhand der Candidate-ID.
            if phrase_original_mention and _entity_norm(phrase_original_mention) == mention_norm:
                add_group_phrase(phrase)

        if not group_phrase_map:
            continue

        groups.append({
            "mention": mention,
            "status": status,
            "entity_id": entity_id,
            "entity_type": inferred_type,
            "resolution": entity.get("resolution_method"),
            "required": bool(relation_query),
            "phrases": sorted(
                group_phrase_map.values(),
                key=lambda item: float(item.get("boost", 0.0)),
                reverse=True,
            ),
        })

    # Erst ab zwei brauchbaren Gruppen macht die strikte Relationslogik Sinn.
    if relation_query and len(groups) < 2:
        relation_query = False
        plan.entity_relation_query = False
        for group in groups:
            group["required"] = False

    plan.entity_groups = groups

    # --------------------------------------------------------
    # Recall-Backoff für genau eine Entity.
    #
    # Dieser Modus wird erst in einem zweiten Retrieval-Pass aktiviert, wenn
    # der Evidence Controller die präzise Suche als unzureichend beurteilt.
    # Er leitet KEINE historischen Namen her. Stattdessen werden lediglich die
    # Tokens der Original-Mention als schwache Suchanker freigegeben.
    # Dadurch kann z.B. ein Dokument unter einem früheren Firmennamen gefunden
    # werden, sofern wenigstens ein Namensbestandteil erhalten geblieben ist.
    # --------------------------------------------------------
    plan.entity_recall_backoff = False
    plan.entity_recall_terms = []

    usable_entities = [
        entity for entity in entities
        if str(entity.get("status") or "")
        in {"resolved", "ambiguous", "fuzzy_candidate"}
        and str(entity.get("mention") or "").strip()
    ]

    if entity_recall and not relation_query and len(usable_entities) == 1:
        mention = str(usable_entities[0].get("mention") or "").strip()
        tokens = _entity_norm(mention).split()
        seen_recall: set[str] = set()
        recall_terms: list[dict[str, Any]] = []

        for token in tokens:
            if not token or token in seen_recall:
                continue
            seen_recall.add(token)

            # Numerische Bestandteile sind als Identitätsanker deutlich
            # schwächer als Wörter, dürfen beim Backoff aber mithelfen.
            numeric = token.isdigit()
            if not numeric and len(token) < 3:
                continue

            recall_terms.append({
                "value": token,
                "boost": 0.35 if numeric else 0.75,
                "source": "entity_recall_token",
                "numeric": numeric,
            })

        if recall_terms:
            plan.entity_recall_backoff = True
            plan.entity_recall_terms = recall_terms

    if entities:
        print(
            "[Planner] ES-Begriffe nach Entity: "
            + (" | ".join(plan.should) if plan.should else "(keine freien Einzelbegriffe)"),
            flush=True,
        )
        if plan.entity_should_phrases:
            print(
                "[Planner] Entity-Phrasen: "
                + " | ".join(
                    f'{item["value"]}^{item["boost"]:.2f}'
                    for item in plan.entity_should_phrases
                ),
                flush=True,
            )
        if plan.entity_relation_query:
            print(
                "[Planner] Relationsfrage: "
                + " AND ".join(
                    "(" + " OR ".join(p["value"] for p in group["phrases"]) + ")"
                    for group in plan.entity_groups
                    if group.get("required")
                ),
                flush=True,
            )

    # Diagnose-Text auf den finalen Zustand korrigieren.
    plan.notes = re.sub(
        r"\d+ freie ES-Begriffe",
        f"{len(plan.should)} freie ES-Begriffe",
        plan.notes,
        count=1,
    )

    if plan.entity_should_phrases:
        plan.notes = (
            plan.notes
            + f"; entity-aware: {len(entities)} Mention(s), "
            + f"{len(plan.entity_should_phrases)} ES-Phrase-Boost(s)"
        )
        if plan.entity_relation_query:
            plan.notes += f"; relation-groups: {len(plan.entity_groups)} verpflichtend"

    if plan.entity_recall_backoff:
        plan.notes += (
            "; entity-recall-backoff: "
            + " | ".join(
                f'{item["value"]}^{item["boost"]:.2f}'
                for item in plan.entity_recall_terms
            )
        )
        print(
            "[Planner] Entity-Recall-Backoff: "
            + " | ".join(
                f'{item["value"]}^{item["boost"]:.2f}'
                for item in plan.entity_recall_terms
            ),
            flush=True,
        )

    return plan


# ------------------------------------------------------------
# Strukturierter SearchSpec -> SearchPlan
# ------------------------------------------------------------

def plan_from_search_spec(
    spec: dict[str, Any],
    entity_context: dict[str, Any] | None = None,
    *,
    original_query: str = "",
    entity_recall: bool = False,
) -> SearchPlan:
    """Compile structured retrieval intent into the mature backend plan.

    The planner describes intent; it never owns Elasticsearch syntax. Entities,
    concept alternative groups and safe literal constraints become deterministic
    lexical groups. elastic_query is retained only as a legacy fallback for
    callers that do not yet provide structured intent.
    """
    value = dict(spec or {})
    lexical_groups: list[dict[str, Any]] = []
    seen_groups: set[tuple[bool, tuple[str, ...]]] = set()

    def add_group(*, required: bool, terms: list[str], kind: str) -> None:
        cleaned: list[str] = []
        seen_terms: set[str] = set()
        for raw in terms:
            text = str(raw or "").strip()
            folded = text.casefold()
            if text and folded not in seen_terms:
                seen_terms.add(folded)
                cleaned.append(text)
        if not cleaned:
            return
        key = (bool(required), tuple(term.casefold() for term in cleaned))
        if key in seen_groups:
            return
        seen_groups.add(key)
        lexical_groups.append({
            "required": bool(required),
            "kind": str(kind or "concept"),
            "terms": cleaned,
        })

    for entity in value.get("entities") or []:
        text = str(entity or "").strip()
        if text:
            add_group(required=True, terms=[text], kind="entity")

    for group in value.get("concept_groups") or []:
        if not isinstance(group, dict):
            continue
        add_group(
            required=bool(group.get("required", False)),
            terms=[str(term) for term in (group.get("terms") or [])],
            kind="concept",
        )

    if original_query:
        # Import lazily to keep retrieval_planner -> planner imports acyclic.
        from rag.retrieval_planner import extract_safe_hard_constraints

        safe = extract_safe_hard_constraints(original_query)
        for constraint_kind in ("filenames", "identifiers", "dates", "years"):
            for constraint in safe.get(constraint_kind) or []:
                add_group(
                    required=True,
                    terms=[str(constraint)],
                    kind=f"constraint:{constraint_kind}",
                )

    structured_intent = bool(lexical_groups)
    plan = SearchPlan(
        search_mode="search_spec",
        semantic_query=str(value.get("semantic_query") or "").strip(),
        elastic_query=(
            ""
            if structured_intent
            else str(value.get("elastic_query") or "").strip()
        ),
        lexical_groups=lexical_groups,
        must=[str(x).strip() for x in (value.get("must") or []) if str(x).strip()],
        should=[str(x).strip() for x in (value.get("should") or []) if str(x).strip()],
        must_not=[str(x).strip() for x in (value.get("must_not") or []) if str(x).strip()],
        phrases=[str(x).strip() for x in (value.get("phrases") or []) if str(x).strip()],
        must_not_phrases=[str(x).strip() for x in (value.get("must_not_phrases") or []) if str(x).strip()],
        notes="structured retrieval intent",
    )
    return _apply_entity_context(
        plan,
        entity_context,
        entity_recall=entity_recall,
    )


# ------------------------------------------------------------
# Öffentliche Planner-Funktion
# ------------------------------------------------------------

def create_plan(
    question: str,
    entity_context: dict[str, Any] | None = None,
    *,
    entity_recall: bool = False,
) -> SearchPlan:

    question = normalize_question(
        question
    )


    words = get_words(
        question
    )


    # --------------------------------------------------------
    # Bis einschließlich drei Wörter:
    # deterministischer Planner-Fast-Path. Lexikalische und semantische Signale
    # werden beide vorbereitet; der Executor entscheidet über die aktiven Arme.
    # --------------------------------------------------------

    if len(words) <= 3:

        print(
            "[Planner] SHORT HYBRID: "
            f"{question}",
            flush=True,
        )

        return _apply_entity_context(
            create_lexical_plan(question),
            entity_context,
            entity_recall=entity_recall,
        )


    # --------------------------------------------------------
    # Ab vier Wörtern:
    # Hybrid.
    # --------------------------------------------------------

    search_terms = get_search_terms(
        question
    )

    parsed = parse_search_syntax(
        question
    )


    print(
        "[Planner] HYBRID DIRECT: "
        f"{question}",
        flush=True,
    )

    print(
        "[Planner] ES-Begriffe: "
        + (
            " | ".join(search_terms)
            if search_terms
            else "(keine)"
        ),
        flush=True,
    )

    if parsed.must:
        print(
            "[Planner] MUST: "
            + " | ".join(parsed.must),
            flush=True,
        )

    if parsed.phrases:
        print(
            "[Planner] PHRASES: "
            + " | ".join(parsed.phrases),
            flush=True,
        )

    if parsed.must_not:
        print(
            "[Planner] MUST NOT: "
            + " | ".join(parsed.must_not),
            flush=True,
        )

    if parsed.must_not_phrases:
        print(
            "[Planner] MUST NOT PHRASES: "
            + " | ".join(parsed.must_not_phrases),
            flush=True,
        )


    return _apply_entity_context(
        create_hybrid_plan(question),
        entity_context,
        entity_recall=entity_recall,
    )


# ------------------------------------------------------------
# CLI-Test
# ------------------------------------------------------------

def main():

    if len(sys.argv) < 2:

        print(
            "Bitte eine Suchanfrage angeben."
        )

        sys.exit(1)


    question = " ".join(
        sys.argv[1:]
    )


    question = normalize_question(
        question
    )


    words = get_words(
        question
    )


    print()
    print("Benutzeranfrage:")
    print(question)

    print()
    print(
        f"Wörter: {len(words)}"
    )

    print(
        "Erkannt: "
        + " | ".join(words)
    )


    if len(words) <= 3:

        print()
        print(
            "Planner-Pfad: "
            "LEXICAL / Elasticsearch"
        )

    else:

        search_terms = get_search_terms(
            question
        )

        print()
        print(
            "Planner-Pfad: "
            "HYBRID DIRECT"
        )

        print()
        print(
            "Elasticsearch-Begriffe:"
        )

        print(
            " | ".join(search_terms)
            if search_terms
            else "(keine)"
        )

        print()
        print(
            "Semantic Query:"
        )

        print(
            question
        )


    plan = create_plan(
        question
    )


    print()
    print("-" * 70)
    print()

    print(
        "Technischer Suchplan:"
    )

    print()


    print(
        plan.model_dump_json(
            indent=2,
        )
    )


if __name__ == "__main__":

    main()
