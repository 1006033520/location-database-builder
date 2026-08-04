"""Multi-language name selection: mapping, filtering, dedupe, NFC normalization."""
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


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def norm_key(s: str) -> str:
    """Case/space/unicode-insensitive key for duplicate detection."""
    return nfc(s).strip().casefold().replace("\u3000", " ")


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
        """
        by_app: dict[str, list[dict]] = {}
        seen: dict[str, set[str]] = defaultdict(set)  # app_tag -> normalized keys
        preferred: dict[str, dict] = {}
        short: dict[str, dict] = {}
        aliases: dict[str, list[dict]] = defaultdict(list)

        for alt in altnames:
            if not self._valid(alt):
                continue
            app_tag = self._app_tag(alt.iso_language)
            if app_tag is None:
                continue
            name = nfc(alt.name).strip()
            if not name:
                continue
            if app_tag == "zh-Hans" and not _CJK_RE.search(name):
                continue  # non-Han-script noise for Chinese names
            key = norm_key(name)
            if key in seen[app_tag]:
                continue  # exact duplicate (case/space/unicode-insensitive)
            seen[app_tag].add(key)
            entry = {"name": name, "is_preferred": False, "is_short": False}
            if alt.is_preferred and app_tag not in preferred:
                entry["is_preferred"] = True
                preferred[app_tag] = entry
            if alt.is_short and app_tag not in short:
                entry["is_short"] = True
                short[app_tag] = entry
            by_app.setdefault(app_tag, []).append(entry)

        result: dict[str, list[dict]] = {}
        for app_tag, entries in by_app.items():
            picked: list[dict] = []
            pref = preferred.get(app_tag)
            sh = short.get(app_tag)
            if pref is None and entries:
                # GeoNames often omits isPreferredName (e.g. Chinese names tagged
                # wuu/yue). The builder designates the first name of the language
                # as the preferred display name so coverage is meaningful and the
                # client fallback query (is_preferred = 1) always has a hit.
                pref = entries[0]
                pref["is_preferred"] = True
                preferred[app_tag] = pref
            if pref is not None:
                picked.append(pref)
            if sh is not None and sh is not pref:
                picked.append(sh)
            for e in entries:
                if len(picked) >= 5:  # preferred + short + up to 3 aliases
                    break
                if e is pref or e is sh:
                    continue
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
        """The 'local' fallback entry: preferred name in the country's official
        language if available, else the GeoNames default name."""
        if self.official_lang and self.official_lang in self.source_to_app:
            app = self.source_to_app[self.official_lang]
            entries = selected.get(app)
            if entries:
                return entries[0]
        return {"name": nfc(default_name).strip(), "is_preferred": False, "is_short": False}
