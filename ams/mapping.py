"""Phase 2: semantic mapping between a provider entity and a consumer entity.

The LLM is scoped to one narrow job, deciding which differently named fields mean
the same thing, over a compact JSON view of the two schemas. Its answer is validated
against the parsed schema (no invented fields, no type clashes, one-to-one), and a
deterministic token/synonym matcher covers anything it leaves out or when no LLM is set.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field

from ams.llm import LLM
from ams.schema import Entity, ServiceSchema

SYNONYMS = [
    {"id", "ref", "reference", "key", "number", "no", "identifier"},
    {"customer", "client", "user", "account", "buyer", "purchaser", "member"},
    {"amount", "total", "sum", "price", "cost", "due", "value", "charge"},
    {"created", "placed", "issued", "submitted", "ordered", "opened", "made"},
    {"updated", "modified", "changed", "edited"},
    {"status", "state", "stage"},
    {"city", "town", "locality"},
    {"qty", "quantity", "count"},
    {"email", "mail"},
    {"phone", "mobile", "tel", "telephone"},
    {"name", "title", "label"},
]
NOISE = {"at", "on", "code", "the", "info", "data"}
COMPATIBLE = {
    frozenset({"integer", "decimal"}), frozenset({"date", "datetime"}), frozenset({"string", "uuid"}),
    frozenset({"string", "datetime"}), frozenset({"string", "date"}),
}
THRESHOLD = 0.5


@dataclass
class Pair:
    consumer_field: str
    provider_field: str
    provider_json: str
    confidence: float
    source: str  # llm | heuristic
    reason: str = ""


@dataclass
class MappingPlan:
    provider_service: str
    consumer_service: str
    provider_entity: str
    consumer_entity: str
    pairs: list[Pair]
    unmapped_consumer: list[str]
    unused_provider: list[str]
    engine: str
    notes: list[str] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        total = len(self.pairs) + len(self.unmapped_consumer)
        return len(self.pairs) / total if total else 0.0

    def to_dict(self) -> dict:
        return asdict(self) | {"coverage": round(self.coverage, 3)}


def tokens(name: str) -> list[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return [t.lower() for t in re.split(r"[\s_\-]+", spaced) if t]


def _token_sim(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if any(a in group and b in group for group in SYNONYMS):
        return 0.8
    if len(a) > 3 and len(b) > 3 and (a.startswith(b) or b.startswith(a)):
        return 0.7
    return 0.0


def name_similarity(a: str, b: str, strip: set[str] = frozenset()) -> float:
    """Dice-style similarity over name tokens, ignoring noise and the entities' own names."""
    ta = [t for t in tokens(a) if t not in NOISE and t not in strip] or tokens(a)
    tb = [t for t in tokens(b) if t not in NOISE and t not in strip] or tokens(b)
    matched = sum(max(_token_sim(x, y) for y in tb) for x in ta)
    matched_rev = sum(max(_token_sim(y, x) for x in ta) for y in tb)
    return (matched + matched_rev) / (len(ta) + len(tb))


def _base(t: str) -> str:
    if t.startswith("ref:"):
        return "enum-or-ref"
    return "list" if t.startswith("list<") else t


def type_compat(provider: ServiceSchema, ptype: str, ctype: str) -> float:
    pb, cb = _base(ptype), _base(ctype)
    if pb == "enum-or-ref":
        target = provider.entity(ptype[4:])
        pb = "string" if target is not None and target.kind == "enum" else "object"
    if pb == cb:
        return 1.0
    if frozenset({pb, cb}) in COMPATIBLE:
        return 0.9
    return 0.2


def heuristic_pairs(provider: ServiceSchema, p_ent: Entity, c_ent: Entity) -> list[Pair]:
    strip = set(tokens(p_ent.name)) | set(tokens(c_ent.name))
    scored = []
    for cf in c_ent.fields:
        for pf in p_ent.fields:
            sim = max(name_similarity(cf.name, pf.name, strip), name_similarity(cf.name, pf.json_name, strip))
            score = sim * type_compat(provider, pf.type, cf.type)
            if score >= THRESHOLD:
                scored.append((score, cf, pf))
    pairs, used_c, used_p = [], set(), set()
    for score, cf, pf in sorted(scored, key=lambda s: -s[0]):
        if cf.name in used_c or pf.name in used_p:
            continue
        used_c.add(cf.name)
        used_p.add(pf.name)
        pairs.append(Pair(cf.name, pf.name, pf.json_name, round(score, 3), "heuristic",
                          f"tokens {tokens(pf.name)} ~ {tokens(cf.name)}"))
    return pairs


SYSTEM = (
    "You map fields between two backend services' data models. You receive a provider entity "
    "and a consumer entity as JSON (name, wire name, canonical type). Match consumer fields to the "
    "provider field carrying the same real-world value, even when names differ (customerId ~ client_id). "
    "Only use field names that appear in the input. Leave a consumer field out if nothing fits."
)


def llm_pairs(llm: LLM, provider: ServiceSchema, p_ent: Entity, c_ent: Entity) -> tuple[list[Pair], list[str]]:
    payload = {
        "provider": {"service": provider.name, "entity": p_ent.name,
                     "fields": [{"name": f.name, "json": f.json_name, "type": f.type} for f in p_ent.fields]},
        "consumer": {"entity": c_ent.name, "fields": [{"name": f.name, "type": f.type} for f in c_ent.fields]},
        "answer_format": {"pairs": [{"consumer_field": "str", "provider_field": "str",
                                     "confidence": "0..1", "reason": "short"}]},
    }
    answer = llm.complete_json(SYSTEM, json.dumps(payload))
    notes: list[str] = []
    if not answer or not isinstance(answer.get("pairs"), list):
        reason = getattr(llm, "last_error", "") or "no usable answer"
        return [], [f"LLM ({llm.name}) gave no usable mapping ({reason}); using heuristic matcher"]
    pairs, used_c, used_p = [], set(), set()
    for item in answer["pairs"]:
        cf, pf = c_ent.field(str(item.get("consumer_field"))), p_ent.field(str(item.get("provider_field")))
        if cf is None or pf is None:
            notes.append(f"rejected LLM pair with unknown field: {item}")
            continue
        if cf.name in used_c or pf.name in used_p:
            notes.append(f"rejected duplicate LLM pair: {cf.name} <- {pf.name}")
            continue
        if type_compat(provider, pf.type, cf.type) < 0.5:
            notes.append(f"rejected type clash: {cf.name} ({cf.type}) <- {pf.name} ({pf.type})")
            continue
        used_c.add(cf.name)
        used_p.add(pf.name)
        conf = float(item.get("confidence", 0.8))
        pairs.append(Pair(cf.name, pf.name, pf.json_name, round(conf, 3), "llm", str(item.get("reason", ""))))
    return pairs, notes


def build_plan(provider: ServiceSchema, consumer: ServiceSchema, p_ent: Entity, c_ent: Entity,
               llm: LLM | None) -> MappingPlan:
    notes: list[str] = []
    pairs: list[Pair] = []
    if llm is not None:
        pairs, notes = llm_pairs(llm, provider, p_ent, c_ent)
    # heuristic fills whatever the LLM did not cover (or everything, without an LLM)
    taken_c, taken_p = {p.consumer_field for p in pairs}, {p.provider_field for p in pairs}
    for p in heuristic_pairs(provider, p_ent, c_ent):
        if p.consumer_field not in taken_c and p.provider_field not in taken_p:
            pairs.append(p)
            taken_c.add(p.consumer_field)
            taken_p.add(p.provider_field)
    order = {f.name: i for i, f in enumerate(c_ent.fields)}
    pairs.sort(key=lambda p: order[p.consumer_field])
    return MappingPlan(
        provider.name, consumer.name, p_ent.name, c_ent.name, pairs,
        [f.name for f in c_ent.fields if f.name not in taken_c],
        [f.name for f in p_ent.fields if f.name not in taken_p],
        _engine(llm, pairs), notes,
    )


def _engine(llm: LLM | None, pairs: list[Pair]) -> str:
    """Credit the engine that actually produced the pairs."""
    if llm is None:
        return "heuristic"
    if any(p.source == "llm" for p in pairs):
        return llm.name if all(p.source == "llm" for p in pairs) else f"{llm.name} + heuristic"
    return f"heuristic (fallback from {llm.name})"


def choose_pair(provider: ServiceSchema, consumer: ServiceSchema) -> tuple[Entity, Entity]:
    """Pick the provider/consumer entity pair that maps best (coverage, then entity-name likeness)."""
    best = None
    for p_ent in (e for e in provider.entities if e.kind != "enum"):
        for c_ent in (e for e in consumer.entities if e.kind != "enum"):
            pairs = heuristic_pairs(provider, p_ent, c_ent)
            coverage = len(pairs) / max(len(c_ent.fields), 1)
            score = coverage + 0.25 * name_similarity(p_ent.name, c_ent.name)
            if best is None or score > best[0]:
                best = (score, p_ent, c_ent)
    if best is None:
        raise ValueError("no entities to map between the two services")
    return best[1], best[2]
