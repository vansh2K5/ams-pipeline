"""Normalized, language-agnostic service schema.

Every parser (Phase 1) emits these structures, so the mapping, generation and
validation phases never see language-specific syntax trees.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Field:
    name: str  # name as written in source
    json_name: str  # name on the wire (after @JsonProperty / alias)
    type: str  # canonical type: string, integer, decimal, boolean, datetime, date, uuid, list<T>, ref:Entity
    source_type: str  # type as written in source, e.g. "BigDecimal"
    optional: bool = False


@dataclass
class Entity:
    name: str
    fields: list[Field]
    file: str
    line: int
    kind: str = "class"  # class | record | enum
    values: list[str] = field(default_factory=list)  # enum constants

    def field(self, name: str) -> Field | None:
        return next((f for f in self.fields if f.name == name), None)


@dataclass
class Param:
    name: str
    location: str  # path | query | body
    type: str


@dataclass
class Endpoint:
    method: str  # GET, POST, ...
    path: str  # /orders/{id}
    handler: str
    returns: str | None  # entity name or canonical type; "list<Order>" for collections
    params: list[Param]
    file: str
    line: int


@dataclass
class ServiceSchema:
    name: str
    language: str
    framework: str
    root: str
    port: int
    entities: list[Entity]
    endpoints: list[Endpoint]

    def entity(self, name: str) -> Entity | None:
        return next((e for e in self.entities if e.name == name), None)

    def to_dict(self) -> dict:
        return asdict(self)
