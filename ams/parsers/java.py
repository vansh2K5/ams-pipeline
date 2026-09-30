"""Phase 1 parser for Java / Spring Boot, built on the tree-sitter Java grammar.

Extracts DTO/entity classes, records and enums (fields + wire names) and
@RestController endpoints into the normalized schema. Purely deterministic.
"""

from __future__ import annotations

import re
from pathlib import Path

import tree_sitter_java
from tree_sitter import Language, Node, Parser

from ams.schema import Endpoint, Entity, Field, Param, ServiceSchema

JAVA = Language(tree_sitter_java.language())

SCALARS = {
    "String": "string", "char": "string", "Character": "string",
    "int": "integer", "Integer": "integer", "long": "integer", "Long": "integer",
    "short": "integer", "Short": "integer", "byte": "integer", "BigInteger": "integer",
    "double": "decimal", "Double": "decimal", "float": "decimal", "Float": "decimal", "BigDecimal": "decimal",
    "boolean": "boolean", "Boolean": "boolean",
    "LocalDateTime": "datetime", "OffsetDateTime": "datetime", "ZonedDateTime": "datetime", "Instant": "datetime",
    "LocalDate": "date", "UUID": "uuid", "Object": "object", "Map": "object",
}
COLLECTIONS = {"List", "Set", "Collection", "Iterable", "ArrayList"}
HTTP = {
    "GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT",
    "PatchMapping": "PATCH", "DeleteMapping": "DELETE",
}
PARAM_LOCATION = {"PathVariable": "path", "RequestParam": "query", "RequestBody": "body"}


def text(node: Node | None) -> str:
    return node.text.decode() if node is not None and node.text else ""


def annotations(node: Node) -> dict[str, Node]:
    """Annotations on a declaration, keyed by simple name."""
    found: dict[str, Node] = {}
    for child in node.children:
        if child.type == "modifiers":
            for mod in child.children:
                if mod.type in ("annotation", "marker_annotation"):
                    found[text(mod.child_by_field_name("name")).split(".")[-1]] = mod
    return found


def annotation_value(ann: Node, key: str = "value") -> str | None:
    """String value of an annotation: @X("v"), @X(value = "v") or @X(path = "v")."""
    args = ann.child_by_field_name("arguments")
    if args is None:
        return None
    for arg in args.named_children:
        if arg.type == "string_literal" and key == "value":
            return text(arg).strip('"')
        if arg.type == "element_value_pair":
            k = text(arg.child_by_field_name("key"))
            if k == key or (key == "value" and k == "path"):
                return text(arg.child_by_field_name("value")).strip('"')
    return None


def canonical(java_type: str, known: set[str]) -> str:
    java_type = java_type.strip()
    generic = re.match(r"^([\w.]+)\s*<(.+)>$", java_type)
    if generic:
        outer, inner = generic.group(1).split(".")[-1], generic.group(2)
        if outer in COLLECTIONS:
            return f"list<{canonical(inner, known)}>"
        if outer == "Optional":
            return canonical(inner, known)
        return "object"
    if java_type.endswith("[]"):
        return f"list<{canonical(java_type[:-2], known)}>"
    simple = java_type.split(".")[-1]
    if simple in SCALARS:
        return SCALARS[simple]
    return f"ref:{simple}" if simple in known else "object"


def _type_names(root: Node) -> set[str]:
    names = set()
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in ("class_declaration", "record_declaration", "enum_declaration"):
            names.add(text(node.child_by_field_name("name")))
        stack.extend(node.children)
    return names


def _entity(node: Node, rel: str, known: set[str]) -> Entity | None:
    name = text(node.child_by_field_name("name"))
    line = node.start_point[0] + 1
    if node.type == "enum_declaration":
        body = node.child_by_field_name("body")
        values = [text(c.child_by_field_name("name")) for c in body.named_children if c.type == "enum_constant"]
        return Entity(name, [], rel, line, kind="enum", values=values)

    fields: list[Field] = []
    if node.type == "record_declaration":
        for p in node.child_by_field_name("parameters").named_children:
            if p.type == "formal_parameter":
                pname, ptype = text(p.child_by_field_name("name")), text(p.child_by_field_name("type"))
                anns = annotations(p)
                json_name = annotation_value(anns["JsonProperty"]) if "JsonProperty" in anns else None
                fields.append(Field(pname, json_name or pname, canonical(ptype, known), ptype))
        return Entity(name, fields, rel, line, kind="record")

    body = node.child_by_field_name("body")
    for member in body.named_children:
        if member.type != "field_declaration":
            continue
        mods = text(next((c for c in member.children if c.type == "modifiers"), None))
        if "static" in mods.split():
            continue
        ftype = text(member.child_by_field_name("type"))
        anns = annotations(member)
        if "JsonIgnore" in anns:
            continue
        for decl in member.children_by_field_name("declarator"):
            fname = text(decl.child_by_field_name("name"))
            json_name = annotation_value(anns["JsonProperty"]) if "JsonProperty" in anns else None
            fields.append(Field(fname, json_name or fname, canonical(ftype, known), ftype,
                                optional=ftype.startswith("Optional")))
    return Entity(name, fields, rel, line) if fields else None


def _endpoints(cls: Node, rel: str, known: set[str]) -> list[Endpoint]:
    cls_anns = annotations(cls)
    if "RestController" not in cls_anns and "Controller" not in cls_anns:
        return []
    prefix = annotation_value(cls_anns["RequestMapping"]) if "RequestMapping" in cls_anns else ""
    out = []
    for member in cls.child_by_field_name("body").named_children:
        if member.type != "method_declaration":
            continue
        anns = annotations(member)
        verb = next((HTTP[a] for a in anns if a in HTTP), None)
        if verb is None:
            continue
        mapping = next(a for a in anns if a in HTTP)
        sub = annotation_value(anns[mapping]) or ""
        path = "/" + "/".join(p.strip("/") for p in (prefix or "", sub) if p.strip("/"))
        ret = canonical(text(member.child_by_field_name("type")), known)
        params = []
        for p in member.child_by_field_name("parameters").named_children:
            if p.type != "formal_parameter":
                continue
            loc = next((PARAM_LOCATION[a] for a in annotations(p) if a in PARAM_LOCATION), None)
            if loc:
                params.append(Param(text(p.child_by_field_name("name")), loc,
                                    canonical(text(p.child_by_field_name("type")), known)))
        out.append(Endpoint(verb, path, text(member.child_by_field_name("name")),
                            ret.removeprefix("ref:").replace("list<ref:", "list<"), params, rel,
                            member.start_point[0] + 1))
    return out


def _port(root: Path) -> int:
    for props in root.rglob("application.properties"):
        m = re.search(r"^server\.port\s*=\s*(\d+)", props.read_text(encoding="utf8"), re.M)
        if m:
            return int(m.group(1))
    for yml in list(root.rglob("application.yml")) + list(root.rglob("application.yaml")):
        m = re.search(r"server:\s*\n\s+port:\s*(\d+)", yml.read_text(encoding="utf8"))
        if m:
            return int(m.group(1))
    return 8080


def parse(root: Path, name: str | None = None) -> ServiceSchema:
    parser = Parser(JAVA)
    files = [p for p in sorted(root.rglob("*.java")) if "test" not in p.relative_to(root).parts]
    trees = [(p, parser.parse(p.read_bytes()).root_node) for p in files]
    known = set().union(*(_type_names(t) for _, t in trees)) if trees else set()

    entities, endpoints = [], []
    for path, tree in trees:
        rel = path.relative_to(root).as_posix()
        stack = [tree]
        while stack:
            node = stack.pop()
            if node.type in ("class_declaration", "record_declaration", "enum_declaration"):
                eps = _endpoints(node, rel, known) if node.type == "class_declaration" else []
                if eps:
                    endpoints += eps
                else:
                    ent = _entity(node, rel, known)
                    if ent:
                        entities.append(ent)
            stack.extend(reversed(node.children))
    return ServiceSchema(name or root.name, "java", "spring-boot", root.as_posix(), _port(root), entities, endpoints)
