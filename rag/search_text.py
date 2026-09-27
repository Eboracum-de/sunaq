"""Small deterministic normalizations for user-entered search syntax."""

from __future__ import annotations

import re


_DOUBLE_QUOTE_TRANSLATION = str.maketrans({
    # English / generic smart quotes.
    "\u201c": '"',  # LEFT DOUBLE QUOTATION MARK
    "\u201d": '"',  # RIGHT DOUBLE QUOTATION MARK
    "\u201e": '"',  # DOUBLE LOW-9 QUOTATION MARK (German opening quote)
    "\u201f": '"',  # DOUBLE HIGH-REVERSED-9 QUOTATION MARK
    # Guillemets are frequently produced by localized editors / copy-paste.
    "\u00ab": '"',
    "\u00bb": '"',
    "\u2039": '"',
    "\u203a": '"',
})


def normalize_query_quotes(text: str) -> str:
    """Normalize typographic double quotes to the ASCII search delimiter.

    Search syntax treats double quotes as phrase delimiters. Keyboard layout,
    browser/editor typography and copy/paste should not change retrieval
    semantics, therefore the common typographic variants are equivalent here.
    The function deliberately leaves apostrophes/single quotes untouched.
    """

    return str(text or "").translate(_DOUBLE_QUOTE_TRANSLATION)


_QUOTED_TOKEN_RE = re.compile(r'(?<![\w+-])([+-]?)"((?:\\.|[^"])*)"')
_RAW_QUERY_TOKEN_RE = re.compile(r'[([{]*[+-]?"(?:\\.|[^"])*"[)\]}]*|\S+')
_GROUPING_CHARS = "()[]{}"


def _comparison_token(token: str) -> tuple[str, str]:
    value = str(token or "").strip().strip(_GROUPING_CHARS).strip()
    sign = value[:1] if value[:1] in {"+", "-"} else ""
    if sign:
        value = value[1:].strip()
    value = value.strip(_GROUPING_CHARS).strip()
    if value.startswith('"') and value.endswith('"') and len(value) >= 2:
        value = value[1:-1].replace(r'\"', '"')
    return sign, value.strip(" \t\r\n.,;:!?()[]{}").casefold()


def preserve_explicit_quoted_phrases(original_query: str, rewritten_query: str) -> str:
    """Preserve explicit user-entered phrase syntax across LLM query rewriting.

    A quoted multi-word phrase is a deliberate lexical constraint. The query
    rewriter may add other anchors, but it must not weaken the phrase into its
    individual words. Generated standalone copies of phrase components are
    removed unless the user also wrote that component outside the phrase.
    Finally the original quoted token is restored with its + / - / neutral
    occurrence marker.
    """

    original = normalize_query_quotes(original_query)
    rewritten = normalize_query_quotes(rewritten_query)

    matches = [
        match
        for match in _QUOTED_TOKEN_RE.finditer(original)
        if len(re.findall(r"\S+", match.group(2))) >= 2
    ]
    if not matches:
        return rewritten

    outside = list(original)
    for match in matches:
        for pos in range(match.start(), match.end()):
            outside[pos] = " "
    outside_words = {
        value
        for token in _RAW_QUERY_TOKEN_RE.findall("".join(outside))
        for _, value in [_comparison_token(token)]
        if value
    }

    protected_components: set[str] = set()
    canonical_phrases: list[str] = []
    protected_phrase_values: set[str] = set()
    for match in matches:
        sign = match.group(1) or ""
        phrase = match.group(2)
        phrase_value = phrase.replace(r'\"', '"')
        protected_phrase_values.add(phrase_value.casefold())
        canonical_phrases.append(f'{sign}"{phrase}"')
        for component in re.findall(r"\S+", phrase_value):
            folded = component.strip(".,;:!?()[]{}").casefold()
            if folded and folded not in outside_words:
                protected_components.add(folded)

    kept: list[str] = []
    for raw in _RAW_QUERY_TOKEN_RE.findall(rewritten):
        token = raw.strip()
        _, value = _comparison_token(token)
        if '"' in token and value:
            if value in protected_phrase_values:
                # Re-add below using exactly the user's occurrence marker.
                continue
            kept.append(token)
            continue

        if value in protected_components:
            continue
        kept.append(token)

    kept.extend(canonical_phrases)
    return re.sub(r"\s+", " ", " ".join(kept)).strip()
