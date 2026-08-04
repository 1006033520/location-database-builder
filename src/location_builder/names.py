"""Multi-language name selection: mapping, filtering, dedupe, NFC normalization.

FIX-004: the `local` fallback entry is always preferred (client queries
`language_tag = 'local' AND is_preferred = 1`).
FIX-005: zh-Hans candidates come from a *priority-ordered* source list
(zh-Hans > zh-CN > zh > wuu/yue) and traditional/dialect text is converted to
simplified via OpenCC before being eligible as a preferred name.
"""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict

from .model import AltName

_WIKIDATA_RE = re.compile(r"^Q\d+$")
_HTTP_RE = re.compile(r"^(https?://|www\.)", re.IGNORECASE)
# zh-Hans display names must contain at least one CJK ideograph (filters POJ
# romanizations like "Lo̍k-chhám-kî" that arrive via non-zh tags).
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

# FIX-005: zh-Hans source priority. Lower wins. wuu/yue are conversion-covered
# supplements only; they must never beat a genuine simplified source.
ZH_SOURCE_PRIORITY = {"zh-Hans": 0, "zh-CN": 1, "zh": 2, "wuu": 3, "yue": 4}
_DEFAULT_PRIORITY = 50

try:  # pragma: no cover - opencc is a hard dependency (pyproject)
    from opencc import OpenCC

    _T2S = OpenCC("t2s")
except Exception:  # pragma: no cover
    _T2S = None


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def norm_key(s: str) -> str:
    """FIX-009: unified normalization key for search/duplicate detection.
    NFC + trim + casefold + fullwidth-space -> ASCII space."""
    return nfc(s).strip().casefold().replace("\u3000", " ")


def to_simplified(s: str) -> str:
    """Convert Traditional Chinese text to Simplified (idempotent on
    already-simplified input)."""
    if _T2S is None:
        return s
    return _T2S.convert(s)


def build_source_to_app_map(lang_cfg) -> dict[str, str]:
    """Map a GeoNames isolanguage tag to the canonical app language tag."""
    m: dict[str, str] = {}
    for app_tag, source_tags in lang_cfg.languages.items():
        for st in source_tags:
            m[st] = app_tag
    return m


class NameSelector:
    def __init__(self, source_to_app: dict[str, str], excluded_tags: set[str], official_lang: str | None):
        self.source_to_app = source_to_app
        self.excluded_tags = excluded_tags
        self.official_lang = official_lang  # country's first official language tag (e.g. zh-CN)

    def select(self, altnames: list[AltName], default_name: str) -> dict[str, list[dict]]:
        """Group valid alt names by app language tag and pick preferred/short/aliases.

        Returns {app_tag: [{name, is_preferred, is_short}, ...]} with at most one
        preferred and one short per language.

        FIX-005: preferred name is chosen by *source priority* first (zh-Hans >
        zh-CN > zh > wuu > yue), then by the GeoNames isPreferredName flag, then
        by input order -- never by raw file traversal order alone.
        """
        by_app: dict[str, list[dict]] = {}
        by_key: dict[str, dict[str, dict]] = defaultdict(dict)  # app_tag -> norm_key -> entry

        for alt in altnames:
            if not self._valid(alt):
                continue
            app_tag = self._app_tag(alt.iso_language)
            if app_tag is None:
                continue
            name = nfc(alt.name).strip()
            if not name:
                continue
            if app_tag == "zh-Hans":
                if not _CJK_RE.search(name):
                    continue  # non-Han-script noise for Chinese names
                name = to_simplified(name)  # FIX-005: always simplified for display
            key = norm_key(name)
            existing = by_key[app_tag].get(key)
            entry = {
                "name": name,
                "is_preferred": bool(alt.is_preferred),
                "is_short": bool(alt.is_short),
                "source_tag": alt.iso_language,
                "source_priority": (
                    ZH_SOURCE_PRIORITY.get(alt.iso_language, _DEFAULT_PRIORITY)
                    if app_tag == "zh-Hans"
                    else 0
                ),
                "orig_name": None,  # pre-conversion name, kept for audit (FIX-005)
            }
            if app_tag == "zh-Hans":
                raw = nfc(alt.name).strip()
                if raw != name:
                    entry["orig_name"] = raw
            if existing is not None:
                # FIX-005: on exact-duplicate, a higher-priority source replaces
                # the entry in place (audit tag follows the best source).
                if app_tag == "zh-Hans" and entry["source_priority"] < existing["source_priority"]:
                    existing["name"] = name
                    existing["source_tag"] = alt.iso_language
                    existing["source_priority"] = entry["source_priority"]
                    existing["orig_name"] = entry["orig_name"]
                    existing["is_preferred"] = existing["is_preferred"] or entry["is_preferred"]
                    existing["is_short"] = existing["is_short"] or entry["is_short"]
                continue  # exact duplicate (case/space/unicode-insensitive)
            by_key[app_tag][key] = entry
            by_app.setdefault(app_tag, []).append(entry)

        result: dict[str, list[dict]] = {}
        for app_tag, entries in by_app.items():
            if app_tag == "zh-Hans":
                # FIX-005: priority first, then GeoNames preference, then order.
                entries.sort(key=lambda e: (e["source_priority"], not e["is_preferred"]))
            else:
                entries.sort(key=lambda e: not e["is_preferred"])
            pref = entries[0]
            pref["is_preferred"] = True
            sh = next((e for e in entries if e["is_short"] and e is not pref), None)
            picked: list[dict] = [pref]
            if sh is not None:
                sh["is_preferred"] = False
                picked.append(sh)
            for e in entries:
                if len(picked) >= 5:  # preferred + short + up to 3 aliases
                    break
                if e is pref or e is sh:
                    continue
                e["is_preferred"] = False  # exactly one preferred per language
                picked.append(e)
            result[app_tag] = picked
        return result

    def _valid(self, alt: AltName) -> bool:
        if alt.is_historic or alt.is_colloquial:
            return False
        tag = alt.iso_language
        if not tag or tag in self.excluded_tags:
            return False
        if tag.startswith("link"):
            return False
        if _HTTP_RE.match(alt.name):
            return False
        if tag == "wk" and _WIKIDATA_RE.match(alt.name):
            return False
        return True

    def _app_tag(self, source_tag: str) -> str | None:
        if source_tag in self.source_to_app:
            return self.source_to_app[source_tag]
        return None

    def local_entry(self, selected: dict[str, list[dict]], default_name: str) -> dict | None:
        """FIX-004: the 'local' fallback entry is ALWAYS the preferred display
        fallback: the country's official-language name if available, else the
        GeoNames default name -- in both cases marked preferred."""
        if self.official_lang and self.official_lang in self.source_to_app:
            app = self.source_to_app[self.official_lang]
            entries = selected.get(app)
            if entries:
                return {"name": entries[0]["name"], "is_preferred": True, "is_short": False}
        return {"name": nfc(default_name).strip(), "is_preferred": True, "is_short": False}
