"""Phase 3: generate the integration layer for a Python consumer of a provider service.

Produces, from the normalized schemas + mapping plan (never from raw source text):
  * provider wire DTOs (Pydantic, with the provider's JSON names as aliases)
  * a mapper from the provider entity into the consumer's own model
  * an HTTP client per provider endpoint (pooled connections, timeouts, retries)
  * a Docker Compose file wiring both services on one network with static DNS names
  * a pinned requirements file for the generated code (scanned by OSV / Trivy)
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from ams.mapping import MappingPlan
from ams.schema import Endpoint, Entity, ServiceSchema

PY_TYPES = {
    "string": "str", "integer": "int", "decimal": "Decimal", "boolean": "bool",
    "datetime": "datetime", "date": "date", "uuid": "str", "object": "Any",
}


@dataclass
class Generated:
    package_dir: Path
    client_file: Path
    compose_file: Path
    requirements_file: Path
    module: str  # importable module name of the client, e.g. ams_generated.order_service_client
    client_class: str
    mapper: str
    mapped_methods: list[str]

    @property
    def files(self) -> list[Path]:
        return [self.client_file, self.package_dir / "__init__.py", self.compose_file, self.requirements_file]


def snake(name: str) -> str:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return re.sub(r"[^a-zA-Z0-9]+", "_", s).strip("_").lower()


def pascal(name: str) -> str:
    return "".join(p[:1].upper() + p[1:] for p in re.split(r"[^a-zA-Z0-9]+", name) if p)


def py_type(ctype: str, provider: ServiceSchema) -> str:
    if ctype.startswith("list<"):
        return f"list[{py_type(ctype[5:-1], provider)}]"
    if ctype.startswith("ref:"):
        target = provider.entity(ctype[4:])
        if target is None:
            return "Any"
        return "str" if target.kind == "enum" else f"{target.name}DTO"
    return PY_TYPES.get(ctype, "Any")


def _dto_order(provider: ServiceSchema, root: Entity) -> list[Entity]:
    """Root entity plus every entity it references, dependencies first."""
    ordered: list[Entity] = []

    def visit(ent: Entity) -> None:
        if ent in ordered or ent.kind == "enum":
            return
        for f in ent.fields:
            for ref in re.findall(r"ref:(\w+)", f.type):
                dep = provider.entity(ref)
                if dep is not None and dep is not ent:
                    visit(dep)
        ordered.append(ent)

    visit(root)
    return ordered


def _dto(ent: Entity, provider: ServiceSchema) -> str:
    lines = [f"class {ent.name}DTO(BaseModel):",
             f'    """Wire model of {provider.name} `{ent.name}` ({ent.file}:{ent.line})."""',
             "", '    model_config = ConfigDict(populate_by_name=True, extra="ignore")', ""]
    for f in ent.fields:
        annotation = f"{py_type(f.type, provider)} | None"
        lines.append(f'    {snake(f.name)}: {annotation} = Field(default=None, alias="{f.json_name}")')
    return "\n".join(lines)


def _method(ep: Endpoint, provider: ServiceSchema) -> tuple[str, str, str, list[str]] | None:
    """(method source, python name, return kind, argument list) for one provider endpoint."""
    ret = ep.returns or ""
    is_list = ret.startswith("list<")
    ent_name = ret[5:-1] if is_list else ret
    ent = provider.entity(ent_name)
    if ent is None or ent.kind == "enum":
        return None
    dto = f"{ent.name}DTO"
    args, path_fmt, query, body = ["self"], ep.path, [], None
    for p in ep.params:
        pname = snake(p.name)
        ptype = py_type(p.type, provider)
        if p.location == "path":
            args.append(f"{pname}: {ptype}")
            path_fmt = path_fmt.replace("{" + p.name + "}", "{" + pname + "}")
        elif p.location == "query":
            args.append(f"{pname}: {ptype} | None = None")
            query.append((p.name, pname))
        else:
            args.append(f"{pname}: {ptype}")
            body = pname
    name = snake(ep.handler)
    ret_type = f"list[{dto}]" if is_list else dto
    call = [f'"{ep.method}"', f'f"{path_fmt}"' if "{" in path_fmt else f'"{path_fmt}"']
    if query:
        pairs = ", ".join(f'"{wire}": {py}' for wire, py in query)
        call.append(f"params={{k: v for k, v in {{{pairs}}}.items() if v is not None}}")
    if body:
        call.append(f'json={body}.model_dump(by_alias=True, mode="json", exclude_none=True)')
    parse = (f"[{dto}.model_validate(item) for item in data]" if is_list else f"{dto}.model_validate(data)")
    src = "\n".join([
        f"    def {name}({', '.join(args)}) -> {ret_type}:",
        f'        """{ep.method} {ep.path} ({ep.file}:{ep.line})."""',
        f"        data = self._request({', '.join(call)})",
        f"        return {parse}",
    ])
    return src, name, ("list" if is_list else "one") + ":" + ent.name, args[1:]


def generate(provider: ServiceSchema, consumer: ServiceSchema, plan: MappingPlan, out_dir: Path) -> Generated:
    p_ent = provider.entity(plan.provider_entity)
    c_ent = consumer.entity(plan.consumer_entity)
    assert p_ent is not None and c_ent is not None

    pkg = out_dir / "ams_generated"
    pkg.mkdir(parents=True, exist_ok=True)
    module_name = f"{snake(provider.name)}_client"
    client_class = f"{pascal(provider.name)}Client"
    mapper = f"to_{snake(c_ent.name)}"
    consumer_module = c_ent.file.removesuffix(".py").replace("/", ".")
    env_var = f"{snake(provider.name).upper()}_URL"

    dtos = "\n\n\n".join(_dto(e, provider) for e in _dto_order(provider, p_ent))

    mapped = {pair.consumer_field: pair for pair in plan.pairs}
    assigns = []
    for f in c_ent.fields:
        pair = mapped.get(f.name)
        if pair:
            assigns.append(f"        {f.name}=src.{snake(pair.provider_field)},"
                           f"  # <- {pair.provider_json} ({pair.source}, {pair.confidence:.2f})")
        else:
            assigns.append(f"        # {f.name}: no provider field carries this value (left to its default)")
    mapper_src = "\n".join([
        f"def {mapper}(src: {p_ent.name}DTO) -> {c_ent.name}:",
        f'    """{provider.name}.{p_ent.name} -> {consumer.name}.{c_ent.name}, per the AMS mapping plan."""',
        f"    return {c_ent.name}(",
        *assigns,
        "    )",
    ])

    methods, mapped_methods = [], []
    for ep in provider.endpoints:
        built = _method(ep, provider)
        if built is None:
            continue
        src, name, kind, params = built
        methods.append(src)
        shape, ent_name = kind.split(":")
        if ent_name == p_ent.name and ep.method == "GET":
            wrapper = f"{name}_as_{snake(c_ent.name)}"
            names = ", ".join(f"{p.split(':')[0]}={p.split(':')[0]}" for p in params)
            ret = f"list[{c_ent.name}]" if shape == "list" else c_ent.name
            body = (f"[{mapper}(item) for item in self.{name}({names})]" if shape == "list"
                    else f"{mapper}(self.{name}({names}))")
            methods.append("\n".join([
                f"    def {wrapper}({', '.join(['self', *params])}) -> {ret}:",
                f"        return {body}",
            ]))
            mapped_methods.append(wrapper)

    client_src = f'''"""Integration layer: {consumer.name} -> {provider.name}.

Generated by AMS (Automated Microservice Synthesis) from the parsed schemas of both
services; regenerate instead of editing by hand. Mapping engine: {plan.engine}.
"""

from __future__ import annotations

import os
import time
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from {consumer_module} import {c_ent.name}

__all__ = ["{client_class}", "{p_ent.name}DTO", "{mapper}"]

DEFAULT_BASE_URL = os.getenv("{env_var}", "http://{provider.name}:{provider.port}")
RETRY_STATUSES = {{502, 503, 504}}


{dtos}


{mapper_src}


class {client_class}:
    """Pooled HTTP client for {provider.name} with timeouts and retry on transient failures."""

    def __init__(self, base_url: str | None = None, timeout: float = 5.0, retries: int = 2,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.retries = retries
        self._http = httpx.Client(
            base_url=base_url or DEFAULT_BASE_URL,
            timeout=httpx.Timeout(timeout, connect=2.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            transport=transport,
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        for attempt in range(self.retries + 1):
            try:
                response = self._http.request(method, path, **kwargs)
                if response.status_code in RETRY_STATUSES and attempt < self.retries:
                    time.sleep(0.2 * 2**attempt)
                    continue
                response.raise_for_status()
                return response.json()
            except httpx.TransportError:
                if attempt == self.retries:
                    raise
                time.sleep(0.2 * 2**attempt)
        raise RuntimeError("unreachable")
{chr(10).join(chr(10) + m for m in methods)}

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> {client_class}:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
'''
    client_file = pkg / f"{module_name}.py"
    client_file.write_text(client_src, encoding="utf8")
    (pkg / "__init__.py").write_text('"""Generated by AMS."""\n', encoding="utf8")

    compose_file = out_dir / "docker-compose.ams.yml"
    compose_file.write_text(_compose(provider, consumer, env_var, out_dir), encoding="utf8")

    requirements_file = out_dir / "requirements.txt"  # standard name so OSV-Scanner and Trivy detect it
    requirements_file.write_text("".join(f"{pkg_name}=={_version(pkg_name)}\n" for pkg_name in ("httpx", "pydantic")),
                                 encoding="utf8")
    return Generated(pkg, client_file, compose_file, requirements_file,
                     f"ams_generated.{module_name}", client_class, mapper, mapped_methods)


def _version(pkg: str) -> str:
    try:
        return version(pkg)
    except PackageNotFoundError:
        return "0"


def _compose(provider: ServiceSchema, consumer: ServiceSchema, env_var: str, out_dir: Path) -> str:
    def ctx(svc: ServiceSchema) -> str:
        root = Path(svc.root).resolve()
        try:
            return Path(os.path.relpath(root, out_dir.resolve())).as_posix()
        except ValueError:  # different drive on Windows: no relative path exists
            return root.as_posix()

    return f"""# Generated by AMS: both services on one private network, reachable by service name.
services:
  {provider.name}:
    build: {ctx(provider)}
    expose:
      - "{provider.port}"
    networks: [ams]
    read_only: true
    security_opt: ["no-new-privileges:true"]
    healthcheck:
      test: ["CMD-SHELL", "wget -qO- http://localhost:{provider.port}/ >/dev/null 2>&1 || exit 1"]
      interval: 10s
      retries: 5

  {consumer.name}:
    build: {ctx(consumer)}
    ports:
      - "127.0.0.1:{consumer.port}:{consumer.port}"
    environment:
      {env_var}: http://{provider.name}:{provider.port}
    depends_on:
      {provider.name}:
        condition: service_healthy
    networks: [ams]
    read_only: true
    security_opt: ["no-new-privileges:true"]

networks:
  ams:
    driver: bridge
"""
