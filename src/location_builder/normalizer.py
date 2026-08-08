"""Level normalization engine: feature codes -> product levels, hierarchy
construction, municipality virtual nodes, missing-level self fallback, stable IDs."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from .config import CountryConfig
from .model import CountryInfo, Unit

VIRTUAL_BIT = 1 << 62

# Feature-code weight for sort_priority (PPLC first, then capitals, then population).
_FEATURE_WEIGHT = {
    "PPLC": 10**12,
    "PPLA": 10**11,
    "PPLA2": 10**10,
    "PPL": 10**9,
    "ADM1": 10**8,
    "ADM2": 10**7,
    "ADM3": 10**6,
    "PPLX": 10**5,
    "ADM4": 10**4,
    "ADM5": 10**3,
}

# Administrative-entity priority used when deduplicating level-2 duplicates:
# a real ADM2 must win over a PPLA2/PPL twin of the same city (e.g. JP
# "Atsugi Shi" ADM2 vs "Atsugi" PPLA2), and a PPLA2 must win over a PPL.
_ADMIN_WEIGHT = {
    "ADM1": 6,
    "ADM2": 5,
    "ADM3": 4,
    "PPLA": 3,
    "PPLA2": 3,
    "PPLC": 3,
    "PPL": 2,
    "PPLX": 1,
    "ADM4": 1,
    "ADM5": 1,
}

# Admin suffixes stripped (only with a separator, so "Akashi" is never mangled)
# when matching duplicate names in "admin2_name" dedupe mode (US).
_SUFFIX_RE = re.compile(r"[-_ ](city|town|village|cdp|borough|county|municipality|shi|ku|gun|machi|cho|mura|son)$", re.IGNORECASE)


def _strip_admin_suffix(name: str) -> str:
    return _SUFFIX_RE.sub("", name.strip()).strip().casefold()


def virtual_id(country_code: str, level: int, parent_id: int, geoname_id: int) -> int:
    """Deterministic stable ID for virtual nodes; high bit set so it can never
    collide with real GeoNames IDs (< 2^31)."""
    raw = f"{country_code}|{level}|{parent_id}|{geoname_id}".encode("utf-8")
    digest = hashlib.sha256(raw).digest()
    n = int.from_bytes(digest[:8], "big") % VIRTUAL_BIT
    return VIRTUAL_BIT | n


@dataclass
class Node:
    id: int
    geoname_id: int
    parent_id: int
    level: int
    country_code: str
    admin_code: str
    source_feature_code: str
    source_admin_level: int | None
    default_name: str
    latitude: float | None
    longitude: float | None
    population: int
    is_virtual: int
    sort_priority: int
    source_unit: Unit | None = None


@dataclass
class NormalizeResult:
    nodes: list[Node]
    level0_id: int
    stats: dict = field(default_factory=dict)


class Normalizer:
    def __init__(self, cfg: CountryConfig, country_info: CountryInfo):
        self.cfg = cfg
        self.country_info = country_info

    # ------------------------------------------------------------------
    def run(self, units: list[Unit], hierarchy: dict[int, set[int]]) -> NormalizeResult:
        cfg = self.cfg
        # 1. level assignment
        by_geoname: dict[int, Node] = {}
        source_admin_level = {
            "ADM1": 1, "ADM2": 2, "ADM3": 3, "ADM4": 4, "ADM5": 5,
            "PPL": None, "PPLA": None, "PPLA2": None, "PPLC": None, "PPLX": None,
        }
        for u in units:
            if u.feature_code not in cfg.candidate_codes:
                continue
            level = self._assign_level(u)
            if level is None:
                continue
            node = Node(
                id=u.geoname_id,
                geoname_id=u.geoname_id,
                parent_id=0,
                level=level,
                country_code=u.country_code,
                admin_code=self._admin_code(u),
                source_feature_code=u.feature_code,
                source_admin_level=source_admin_level.get(u.feature_code),
                default_name=u.name.strip(),
                latitude=u.latitude,
                longitude=u.longitude,
                population=u.population,
                is_virtual=0,
                sort_priority=0,
                source_unit=u,
            )
            old = by_geoname.get(u.geoname_id)
            if old is None or u.population > old.population:
                by_geoname[u.geoname_id] = node
        nodes = list(by_geoname.values())

        # 2. JP/US-style dedupe of duplicated level-2 entities (ADM2 vs PPLA2,
        #    PPLA2 vs PPL)
        if cfg.dedupe_level2_by_name:
            nodes = self._dedupe_level2(nodes)
        by_geoname = {n.geoname_id: n for n in nodes}

        # 2b. CN-style cross-level twins (prefecture as ADM3 duplicating ADM2)
        if cfg.dedupe_cross_level:
            nodes = self._drop_cross_level_dups(nodes)
            by_geoname = {n.geoname_id: n for n in nodes}

        # 3. country root (level 0)
        root = self._make_root(units)
        nodes.append(root)

        # 4. municipality virtual twins (e.g. CN: 北京市 ADM1 -> virtual level2)
        muni_twins: dict[str, Node] = {}
        if cfg.municipalities:
            for n in list(nodes):
                if n.level == 1 and n.source_unit and n.source_unit.admin1 in cfg.municipalities:
                    twin = Node(
                        id=virtual_id(n.country_code, 2, n.id, n.geoname_id),
                        geoname_id=n.geoname_id,
                        parent_id=n.id,
                        level=2,
                        country_code=n.country_code,
                        admin_code=n.admin_code,
                        source_feature_code=n.source_feature_code,
                        source_admin_level=n.source_admin_level,
                        default_name=n.default_name,
                        latitude=n.latitude,
                        longitude=n.longitude,
                        population=n.population,
                        is_virtual=1,
                        sort_priority=n.sort_priority,
                        source_unit=n.source_unit,
                    )
                    nodes.append(twin)
                    muni_twins[n.source_unit.admin1] = twin

        # 5. parent resolution
        self._resolve_parents(nodes, hierarchy, muni_twins)

        # 6. missing-level self fallback
        self._missing_level_fallback(nodes)

        # 7. sort priority + id collision check
        ids = set()
        dupes: list[Node] = []
        for n in nodes:
            n.sort_priority = self._sort_priority(n)
            if n.id in ids:
                dupes.append(n)
            ids.add(n.id)
        if dupes:
            for n in dupes:
                twin = next((m for m in nodes if m.id == n.id and m is not n), None)
                print(f"DUPE id={n.id} name={n.default_name!r} lvl={n.level} parent={n.parent_id} gid={n.geoname_id} virt={n.is_virtual} fcode={n.source_feature_code}")
                if twin:
                    print(f"  twin: name={twin.default_name!r} lvl={twin.level} parent={twin.parent_id} gid={twin.geoname_id} virt={twin.is_virtual} fcode={twin.source_feature_code}")
            raise RuntimeError(f"ID collision at {dupes[0].id} ({dupes[0].country_code})")

        stats = {
            "candidates": len(units),
            "nodes": len(nodes),
            "by_level": {lv: sum(1 for n in nodes if n.level == lv) for lv in range(5)},
            "virtual": sum(1 for n in nodes if n.is_virtual),
            "reparented_to_root": sum(1 for n in nodes if n.level > 0 and n.parent_id == root.id),
        }
        return NormalizeResult(nodes=nodes, level0_id=root.id, stats=stats)

    # ------------------------------------------------------------------
    def _assign_level(self, u: Unit) -> int | None:
        cfg = self.cfg
        if u.feature_code in cfg.province:
            return 1
        if u.feature_code in cfg.city:
            if cfg.municipalities and u.admin1 in cfg.municipalities and u.feature_code == "ADM2":
                # 直辖市整体 ADM2（如北京市 11876380）与省级节点重复，直接剔除；
                # 其下 ADM3 区县仍会通过 muni_twins 挂到虚拟市级节点。
                return None
            return 2
        if u.feature_code in cfg.district:
            return 3
        return None

    def _dedupe_level2(self, nodes: list[Node]) -> list[Node]:
        """Level-2 duplicate removal.

        - mode "admin2" (JP): same (admin1, admin2) is the same city; keep the
          entity with the highest administrative weight (ADM2 > PPLA2), so the
          "Atsugi Shi" ADM2 twin wins over the "Atsugi" PPLA2 twin.
        - mode "admin2_name" (US): same (admin1, admin2, suffix-stripped name)
          within a county is the same town; keep the PPLA2 over the PPL.
        """
        mode = getattr(self.cfg, "dedupe_level2_key", "admin2_name")
        best: dict[tuple, Node] = {}
        out: list[Node] = []
        for n in nodes:
            if n.level != 2 or n.is_virtual:
                out.append(n)
                continue
            su = n.source_unit
            a1 = su.admin1 if su else ""
            a2 = su.admin2 if su else ""
            if mode == "admin2":
                key = (a1, a2)
            else:
                key = (a1, a2, _strip_admin_suffix(n.default_name))
            prev = best.get(key)
            if prev is None:
                best[key] = n
            else:
                w_prev = _ADMIN_WEIGHT.get(prev.source_feature_code, 0)
                w_new = _ADMIN_WEIGHT.get(n.source_feature_code, 0)
                if w_new > w_prev or (w_new == w_prev and n.population > prev.population):
                    best[key] = n
        best_ids = {n.id for n in best.values()}
        out.extend(n for n in nodes if n.id in best_ids)
        return out

    def _drop_cross_level_dups(self, nodes: list[Node]) -> list[Node]:
        """CN: GeoNames sometimes stores a prefecture-level city twice — once as
        ADM2 (kept at level 2) and once as ADM3 with a broken/self admin2 that
        cannot attach to any level-2 unit (Yiyang Shi, Changde Shi, Lingshui
        County, ...). Those level-3 twins would otherwise hang directly off the
        province and duplicate the level-2 entry. Drop them.
        """
        l2_names: dict[str, set[str]] = {}
        l2_keys: set[tuple] = set()
        for n in nodes:
            su = n.source_unit
            if n.level == 2 and su is not None:
                l2_names.setdefault(su.admin1, set()).add(_strip_admin_suffix(n.default_name))
                l2_keys.add((su.admin1, su.admin2))
        out: list[Node] = []
        for n in nodes:
            su = n.source_unit
            if n.level == 3 and su is not None and su.feature_code == "ADM3":
                if (su.admin1, su.admin2) not in l2_keys and _strip_admin_suffix(n.default_name) in l2_names.get(su.admin1, set()):
                    continue
            out.append(n)
        return out

    def _make_root(self, units: list[Unit]) -> Node:
        ci = self.country_info
        existing = next((u for u in units if u.geoname_id == ci.geoname_id), None)
        name = existing.name.strip() if existing else ci.name
        return Node(
            id=ci.geoname_id or -1,
            geoname_id=ci.geoname_id,
            parent_id=None,  # no parent: root of the tree
            level=0,
            country_code=ci.iso,
            admin_code="",
            source_feature_code="PCLI",
            source_admin_level=None,
            default_name=name,
            latitude=existing.latitude if existing else None,
            longitude=existing.longitude if existing else None,
            population=ci.population,
            is_virtual=0,
            sort_priority=10**13,
            source_unit=existing,
        )

    def _resolve_parents(self, nodes: list[Node], hierarchy: dict[int, set[int]], muni_twins: dict[str, Node]) -> None:
        root = next(n for n in nodes if n.level == 0)
        by_geoname = {n.geoname_id: n for n in nodes if n.geoname_id}

        l1_by_a1: dict[str, list[Node]] = {}
        l2_by_a12: dict[tuple, list[Node]] = {}
        for n in nodes:
            su = n.source_unit
            if su is None or n.is_virtual:
                continue
            if n.level == 1:
                l1_by_a1.setdefault(su.admin1, []).append(n)
            elif n.level == 2:
                l2_by_a12.setdefault((su.admin1, su.admin2), []).append(n)

        def pick(cands: list[Node]) -> Node | None:
            if not cands:
                return None
            return max(cands, key=lambda c: (c.population, c.geoname_id))

        def admin_parent(n: Node) -> Node | None:
            """Admin-code based parent lookup (states for level 2, city/state for
            level 3). Used as the hierarchy fallback when the hierarchy edge
            points to a same-or-higher level node (e.g. US boroughs whose
            hierarchy parent is the city itself)."""
            su = n.source_unit
            if su is None:
                return None
            if n.level == 1:
                return root
            if n.level == 2:
                return pick(l1_by_a1.get(su.admin1, []))
            if n.level == 3:
                p = pick(l2_by_a12.get((su.admin1, su.admin2), []))
                if p is None and su.admin1 in muni_twins:
                    p = muni_twins[su.admin1]
                if p is None:
                    p = pick(l1_by_a1.get(su.admin1, []))
                return p
            return None

        for n in nodes:
            if n.level == 0 or n.is_virtual:
                # root keeps no parent; virtual nodes keep the parent set at creation time
                n.parent_id = n.parent_id if n.is_virtual else None
                continue
            su = n.source_unit
            parent = None
            # (a) hierarchy edges first
            if su and n.geoname_id in hierarchy:
                for p_gid in hierarchy[n.geoname_id]:
                    pn = by_geoname.get(p_gid)
                    if pn is not None and pn.level < n.level:
                        parent = pn
                        break
            # (b) admin-code matching
            if parent is None and su:
                parent = admin_parent(n)
            # (c) final guard: hierarchy edge to a same/higher level node falls
            # back to admin-code matching instead of the root.
            if parent is not None and parent.level >= n.level:
                parent = admin_parent(n)
            if parent is None:
                parent = root
            if parent.level >= n.level:
                parent = root
            n.parent_id = parent.id

    def _missing_level_fallback(self, nodes: list[Node]) -> None:
        """FIX-002/003: per-target-level self fallback.

        - level1 -> level2: controlled by `self_level_fallback.city`.
        - level2 -> level3: controlled by `self_level_fallback.district` AND
          `allow_missing_district`. `allow_missing_district=true` now means a
          single city may simply *end* at city level (no same-name virtual
          district is created); it is not a country-wide toggle.
        """
        cfg = self.cfg

        # children index: parent_id -> list of child nodes (built once, O(n))
        children: dict[int, list[Node]] = {}
        for n in nodes:
            if n.parent_id is not None:
                children.setdefault(n.parent_id, []).append(n)

        def has_child(parent_id: int, level: int) -> bool:
            return any(c.level == level for c in children.get(parent_id, []))

        # level1 -> level2 fallback (省即市)
        if cfg.fallback_for("city"):
            for l1 in [n for n in nodes if n.level == 1]:
                if not has_child(l1.id, 2):
                    nodes.append(self._self_virtual(nodes, l1, 2))
        # level2 -> level3 fallback (市即区县). FIX-003: when
        # allow_missing_district=true the city keeps no district children at all.
        if cfg.fallback_for("district") and not cfg.allow_missing_district:
            for l2 in [n for n in nodes if n.level == 2]:
                if not has_child(l2.id, 3):
                    nodes.append(self._self_virtual(nodes, l2, 3))

    def _self_virtual(self, nodes: list[Node], parent: Node, level: int) -> Node:
        return Node(
            id=virtual_id(parent.country_code, level, parent.id, parent.geoname_id),
            geoname_id=parent.geoname_id,
            parent_id=parent.id,
            level=level,
            country_code=parent.country_code,
            admin_code=parent.admin_code,
            source_feature_code=parent.source_feature_code,
            source_admin_level=parent.source_admin_level,
            default_name=parent.default_name,
            latitude=parent.latitude,
            longitude=parent.longitude,
            population=parent.population,
            is_virtual=1,
            sort_priority=parent.sort_priority,
            source_unit=parent.source_unit,
        )

    @staticmethod
    def _admin_code(u: Unit) -> str:
        parts = [u.admin1, u.admin2, u.admin3]
        parts = [p for p in parts if p]
        return ".".join(parts)

    @staticmethod
    def _sort_priority(n: Node) -> int:
        w = _FEATURE_WEIGHT.get(n.source_feature_code, 0)
        return w + n.population
