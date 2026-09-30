"""Reporting: turn a run's ams-report.json into Markdown (CI job summary) and a
self-contained HTML report (no external assets, light + dark).

Everything renders from the JSON alone, so `ams report <out-dir>` can rebuild the
reports for any past run, e.g. one downloaded from a CI artifact.
"""

from __future__ import annotations

import json
from html import escape
from pathlib import Path

PHASES = {
    "1_parse": "Parse", "2_map": "Map", "3_generate": "Generate", "4_validate": "Validate", "security": "Security",
}


def failures(data: dict) -> list[str]:
    """Human-readable reasons a run failed (empty when it passed)."""
    out = []
    if not data["heal"]["passed"]:
        last = data["heal"]["rounds"][-1] if data["heal"]["rounds"] else {"diagnostics": []}
        out.append(f"Generated code does not compile: {'; '.join(last['diagnostics'][:3]) or 'see heal rounds'}")
    e2e = data["e2e"]
    if not e2e["passed"]:
        bad = [c for c in e2e["checks"] if not c["ok"]]
        if bad:
            out += [f"End-to-end: `{c['consumer_field']}` expected {c['expected']!r} from `{c['provider_json']}`, "
                    f"got {c['actual']!r} ({c['method']})" for c in bad]
        else:
            out.append(f"End-to-end check did not run: {e2e.get('error') or 'unknown error'}")
    for s in data["security"]:
        if s["status"] == "findings":
            out.append(f"{s['tool']}: {s['findings']} finding(s): {', '.join(s['items'][:5])}")
        elif s["status"] == "error":
            out.append(f"{s['tool']} errored, so the code is not proven clean: {s['detail']}")
    unmapped = data["mapping"]["unmapped_consumer"]
    if unmapped:
        out.append(f"Consumer fields with no provider source: {', '.join(unmapped)}")
    return out


def _field_types(data: dict) -> tuple[dict[str, str], dict[str, str]]:
    """(consumer field -> type, provider field -> type) for the mapped entity pair."""
    m, schemas = data["mapping"], data.get("schemas", {})

    def types(service: str, entity: str) -> dict[str, str]:
        ents = schemas.get(service, {}).get("entities", [])
        ent = next((e for e in ents if e["name"] == entity), {"fields": []})
        return {f["name"]: f.get("source_type") or f["type"] for f in ent["fields"]}

    return types(m["consumer_service"], m["consumer_entity"]), types(m["provider_service"], m["provider_entity"])


# ------------------------------------------------------------------ markdown


def markdown(data: dict) -> str:
    m, e2e = data["mapping"], data["e2e"]
    lines = [f"# AMS report: {data['consumer']} -> {data['provider']}", "",
             f"**Result:** {'PASS' if data['passed'] else 'FAIL'}  ",
             f"**Mapping engine:** {m['engine']}  ", f"**Generated:** {data.get('generated_at', '')}", ""]
    why = failures(data)
    if why:
        lines += ["## Why it failed", "", *[f"- {w}" for w in why], ""]
    lines += [f"## Field mapping ({m['provider_entity']} -> {m['consumer_entity']})", "",
              "| Consumer field | Provider field (wire) | Confidence | Source |", "|---|---|---|---|"]
    lines += [f"| `{p['consumer_field']}` | `{p['provider_json']}` | {p['confidence']:.2f} | {p['source']} |"
              for p in m["pairs"]]
    lines += [f"| `{f}` | _unmapped_ | | |" for f in m["unmapped_consumer"]]
    heal = data["heal"]
    lines += ["", "## Validation", "",
              f"- Compile: {'pass' if heal['passed'] else 'fail'} ({len(heal['rounds'])} heal round(s))"]
    lines += [f"  - round {h['round']}: {h['action']} ({len(h['diagnostics'])} diagnostic(s))" for h in heal["rounds"]]
    checks = f"{sum(c['ok'] for c in e2e['checks'])}/{len(e2e['checks'])} field checks"
    lines += [f"- End-to-end: {'pass' if e2e['passed'] else 'fail'}, {checks}, requests: {', '.join(e2e['requests'])}"]
    if data["security"]:
        lines += ["", "## Security (delta-scoped, parallel)", "", "| Scanner | Status | Findings | Detail |",
                  "|---|---|---|---|"]
        lines += [f"| {s['tool']} | {s['status']} | {s['findings'] if s['status'] != 'skipped' else '-'} | "
                  f"{s['detail']} |" for s in data["security"]]
        items = [f"- `{s['tool']}`: {item}" for s in data["security"] for item in s["items"]]
        if items:
            lines += ["", *items]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------- html

CSS = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#1b1b1a;--muted:#6b6b66;--line:#e3e2dd;--ok:#1f7a45;--okbg:#e5f4ea;
--bad:#b42318;--badbg:#fdecea;--warn:#8a5a00;--warnbg:#fdf3dc;--bar:#1b1b1a;--code:#f0efeb}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1d1d1b;--ink:#ecebe6;--muted:#9a9990;--line:#33332f;
--ok:#5fd08a;--okbg:#17301f;--bad:#ff8a7f;--badbg:#3a1a17;--warn:#f0c060;--warnbg:#352a12;--bar:#ecebe6;--code:#262623}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:1100px;margin:0 auto;padding:32px 16px 64px}
h1{font-size:26px;margin:0 0 4px}h2{font-size:17px;margin:32px 0 12px}
.sub{color:var(--muted)}.head{display:flex;gap:16px;align-items:center;justify-content:space-between;flex-wrap:wrap}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;font-weight:600;font-size:12px;letter-spacing:.02em}
.pass{background:var(--okbg);color:var(--ok)}.fail{background:var(--badbg);color:var(--bad)}
.skip{background:var(--code);color:var(--muted)}.warn{background:var(--warnbg);color:var(--warn)}
.verdict{font-size:18px;padding:6px 18px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-top:24px}
.tile,.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.tile b{display:block;font-size:24px;font-variant-numeric:tabular-nums}.tile span{color:var(--muted);font-size:12px}
.why{background:var(--badbg);border:1px solid var(--bad);border-radius:10px;padding:12px 16px;margin-top:20px}
.why h2{margin:0 0 6px;color:var(--bad)}.why li{margin:4px 0}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;
overflow:hidden}th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;color:var(--muted);font-weight:600;background:var(--code)}tr:last-child td{border-bottom:0}
code{background:var(--code);padding:1px 5px;border-radius:4px;font:12.5px ui-monospace,Consolas,monospace}
.t{color:var(--muted);font-size:12px}.num{font-variant-numeric:tabular-nums}
.conf{display:flex;align-items:center;gap:8px}.meter{width:80px;height:6px;background:var(--line);border-radius:3px}
.meter i{display:block;height:100%;background:var(--bar);border-radius:3px}
.phases{display:flex;gap:3px;height:14px;margin-top:8px}
.phases div{background:var(--bar);border-radius:3px;min-width:6px}
.legend{display:flex;flex-wrap:wrap;gap:6px 18px;margin-top:10px;font-size:12px;color:var(--muted)}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;background:var(--bar);margin-right:6px;
vertical-align:-1px}.legend b{color:var(--ink);font-weight:600}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}@media (max-width:720px){.grid2{grid-template-columns:1fr}}
ul{margin:6px 0;padding-left:20px}.scroll{overflow-x:auto}
"""


def _badge(status: str) -> str:
    cls = {"pass": "pass", "clean": "pass", "ok": "pass", "fail": "fail", "findings": "fail", "error": "fail",
           "skipped": "skip", "heuristic": "skip", "llm": "warn"}.get(status, "skip")
    return f'<span class="badge {cls}">{escape(status)}</span>'


def _table(headers: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th>{escape(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _code(value: str, note: str = "") -> str:
    return f"<code>{escape(value)}</code>" + (f" <span class='t'>{escape(note)}</span>" if note else "")


def _list(items: list[str], empty: str) -> str:
    return "<ul>" + ("".join(f"<li>{i}</li>" for i in items) or f"<li class='t'>{escape(empty)}</li>") + "</ul>"


def html(data: dict) -> str:
    m, e2e, heal, scans = data["mapping"], data["e2e"], data["heal"], data["security"]
    c_types, p_types = _field_types(data)
    ok_checks = sum(c["ok"] for c in e2e["checks"])
    clean = sum(s["status"] == "clean" for s in scans)
    ran = sum(s["status"] != "skipped" for s in scans)
    skipped = len(scans) - ran
    if not scans:
        scan_tile = ("off", "scanners not run")
    elif ran:
        scan_tile = (f"{clean}/{ran}", "scanners clean" + (f" ({skipped} skipped)" if skipped else ""))
    else:
        scan_tile = ("-", f"scanners: all {skipped} skipped")
    timings = data.get("timings_s", {})

    tiles = [
        (f"{len(m['pairs'])}/{len(m['pairs']) + len(m['unmapped_consumer'])}", f"fields mapped ({m['coverage']:.0%})"),
        (f"{ok_checks}/{len(e2e['checks'])}", "end-to-end field checks"),
        (str(len(heal["rounds"])), "self-heal rounds"),
        scan_tile,
        (f"{sum(timings.values()):.1f}s", "total pipeline time"),
    ]
    tiles_html = "".join(f'<div class="tile"><b>{escape(v)}</b><span>{escape(k)}</span></div>' for v, k in tiles)

    why = failures(data)
    why_html = ""
    if why:
        why_html = f"<section class='why'><h2>Why it failed</h2>{_list([escape(w) for w in why], '')}</section>"

    total = sum(timings.values()) or 1.0
    phases = "".join(f'<div style="flex:{max(t / total, 0.01)};opacity:{1 - 0.15 * i}" '
                     f'title="{escape(PHASES.get(k, k))}: {t:.3f}s"></div>' for i, (k, t) in enumerate(timings.items()))
    legend = "".join(f"<span><i style='opacity:{1 - 0.15 * i}'></i>{escape(PHASES.get(k, k))} "
                     f"<b class='num'>{t:.2f}s</b></span>" for i, (k, t) in enumerate(timings.items()))

    def conf(value: float) -> str:
        return (f'<div class="conf"><div class="meter"><i style="width:{value * 100:.0f}%"></i></div>'
                f'<span class="num">{value:.2f}</span></div>')

    map_rows = [[_code(p["consumer_field"], c_types.get(p["consumer_field"], "")),
                 _code(p["provider_json"], p_types.get(p["provider_field"], "")),
                 conf(p["confidence"]), _badge(p["source"]), f"<span class='t'>{escape(p.get('reason', ''))}</span>"]
                for p in m["pairs"]]
    map_rows += [[_code(f), "<span class='t'>no provider field</span>", "", _badge("fail"), ""]
                 for f in m["unmapped_consumer"]]
    extras = ""
    if m["unused_provider"]:
        unused = escape(", ".join(m["unused_provider"]))
        extras += f"<p class='t'>Provider fields not needed by the consumer: {unused}</p>"
    if m.get("notes"):
        extras += _list([f"<span class='t'>{escape(n)}</span>" for n in m["notes"]], "")

    heal_items = [f"Round {h['round']}: {escape(h['action'])}{_list([_code(d) for d in h['diagnostics']], '')}"
                  for h in heal["rounds"]]
    check_rows = [[_code(c["method"]), _code(c["consumer_field"]), _code(c["provider_json"]),
                   _code(repr(c["expected"])), _code(repr(c["actual"])), _badge("ok" if c["ok"] else "fail")]
                  for c in e2e["checks"]]

    sec_html = "<p class='t'>Scanners were not run (<code>--no-scan</code>).</p>"
    if scans:
        sec_rows = [[f"<b>{escape(s['tool'])}</b>", _badge(s["status"]),
                     f"<span class='num'>{s['findings'] if s['status'] != 'skipped' else '-'}</span>",
                     escape(s["detail"]) + (_list([_code(i) for i in s["items"]], "") if s["items"] else "")]
                    for s in scans]
        sec_html = _table(["Scanner", "Status", "Findings", "Detail"], sec_rows)

    services = ""
    for name, svc in data.get("schemas", {}).items():
        ents = [_code(e["name"], f"{e.get('kind', 'class')}, {len(e['fields'])} fields") for e in svc["entities"]]
        eps = [_code(f"{ep['method']} {ep['path']}", f"-> {ep['returns']}") for ep in svc["endpoints"]]
        services += (f"<div class='card'><h3 style='margin:0'>{escape(name)}</h3><p class='t'>{escape(svc['language'])}"
                     f" / {escape(svc['framework'])}, port {svc['port']}</p><b>Entities</b>{_list(ents, 'none')}"
                     f"<b>Endpoints</b>{_list(eps, 'none')}</div>")

    requests = _list([_code(r) for r in e2e["requests"]], "none")
    error = f"<p class='t'>{escape(e2e['error'])}</p>" if e2e.get("error") else ""
    verdict = "PASS" if data["passed"] else "FAIL"
    title = f"{escape(data['consumer'])} &rarr; {escape(data['provider'])}"

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AMS report: {escape(data['consumer'])} to {escape(data['provider'])}</title><style>{CSS}</style></head>
<body><main>
<div class="head"><div><h1>{title}</h1>
<div class="sub">AMS integration report &middot; {escape(m['provider_entity'])} &rarr; {escape(m['consumer_entity'])}
&middot; mapping engine <code>{escape(m['engine'])}</code> &middot; {escape(data.get('generated_at', ''))}</div></div>
<span class="badge verdict {'pass' if data['passed'] else 'fail'}">{verdict}</span></div>
{why_html}
<div class="tiles">{tiles_html}</div>
<h2>Pipeline</h2><div class="phases">{phases}</div><div class="legend">{legend}</div>
<h2>Field mapping</h2>{_table(["Consumer field", "Provider field (wire)", "Confidence", "Source", "Why"], map_rows)}
{extras}
<h2>Validation</h2><div class="grid2">
<div class="card"><b>Self-healing compile loop</b> {_badge("pass" if heal["passed"] else "fail")}
{_list(heal_items, "Compiled cleanly on the first pass.")}</div>
<div class="card"><b>Requests to the mock provider</b>{requests}{error}</div></div>
<h2>End-to-end field checks</h2>{_table(["Method", "Consumer field", "Provider field", "Expected", "Actual", ""],
                                        check_rows)}
<h2>Security gate</h2>{sec_html}
<h2>Services</h2><div class="grid2">{services}</div>
<h2>Generated files</h2><div class="card">{_list([_code(f) for f in data["generated"]], "none")}</div>
</main></body></html>
"""


def write_reports(out_dir: Path, data: dict) -> tuple[Path, Path]:
    md_path, html_path = out_dir / "ams-report.md", out_dir / "ams-report.html"
    md_path.write_text(markdown(data), encoding="utf8")
    html_path.write_text(html(data), encoding="utf8")
    return md_path, html_path


def rerender(out_dir: Path) -> tuple[Path, Path]:
    """Rebuild the Markdown + HTML reports from an existing ams-report.json."""
    data = json.loads((out_dir / "ams-report.json").read_text(encoding="utf8"))
    return write_reports(out_dir, data)
