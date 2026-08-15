"""Build a grouped endpoint catalog for the active profile, served at `GET /__mock__/catalog`.

Two sources:

  1. --spec <openapi.yaml>   (preferred)  group operations by their first OpenAPI tag, giving one
     group per API area (Accounts, Orders, Notifications, …). Needs PyYAML.

  2. --from-recent           (fallback)  derive a rough catalog from traffic the proxy has already
     seen (GET /__mock__/recent), grouping by the leading path segments. Needs the proxy running.

Output shape:
  [ { "domain": "Orders", "operations": [ {"method","path","operationId","summary"} ] }, ... ]

The catalog is a derived copy of your API's structure, so it is written to the tool's cache
directory rather than into the profile — keep it out of screenshots and bug reports.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from collections import defaultdict

import click

import config


def _write(catalog: list[dict]) -> None:
    config.CATALOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.CATALOG_FILE.write_text(json.dumps(catalog, indent=2), encoding="utf-8")
    operations = sum(len(group["operations"]) for group in catalog)
    click.echo(f"wrote {config.CATALOG_FILE} — {len(catalog)} domains, {operations} operations")


def from_spec(spec_path: str) -> list[dict]:
    try:
        import yaml  # type: ignore
    except ImportError:
        click.echo("PyYAML required for --spec: .venv/bin/pip install pyyaml", err=True)
        raise SystemExit(2) from None

    with open(spec_path, encoding="utf-8") as handle:
        spec = yaml.safe_load(handle)

    groups: dict[str, list[dict]] = defaultdict(list)
    for path, methods in (spec.get("paths") or {}).items():
        for method, operation in methods.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                continue
            tags = operation.get("tags") or ["Untagged"]
            groups[_titleize(tags[0])].append({
                "method": method.upper(),
                "path": path,
                "operationId": operation.get("operationId") or f"{method}:{path}",
                "summary": operation.get("summary", ""),
            })
    return _sorted_catalog(groups)


def from_recent() -> list[dict]:
    url = f"{config.CONTROL_ORIGIN}/__mock__/recent"
    request = urllib.request.Request(url, headers={"Host": config.CONTROL_HOST_HEADER})
    try:
        recent = json.load(urllib.request.urlopen(request, timeout=3))
    except Exception as error:  # noqa: BLE001
        click.echo(f"could not read {url}: {error} (is the proxy running?)", err=True)
        raise SystemExit(2) from None

    groups: dict[str, list[dict]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    for entry in recent:
        method, path = entry.get("method", "GET"), entry.get("path", "/")
        if (method, path) in seen:
            continue
        seen.add((method, path))
        groups[_domain_from_path(path)].append({
            "method": method,
            "path": path,
            "operationId": f"{method}:{path}",
            "summary": "",
        })
    return _sorted_catalog(groups)


def _domain_from_path(path: str) -> str:
    """Drop `vN` segments, then group by the second remaining segment if there is one.

    Matches `v1`/`v2` exactly rather than any segment starting with `v`, which also swallowed
    real path segments such as `/vouchers/` and `/validate/`.
    """
    segments = [
        segment for segment in path.strip("/").split("/")
        if segment and not re.fullmatch(r"v\d+", segment)
    ]
    if not segments:
        return "Other"
    return _titleize(segments[1] if len(segments) > 1 else segments[0])


def _titleize(value: str) -> str:
    return value.replace("-", " ").replace("_", " ").strip().title() or "Other"


def _sorted_catalog(groups: dict[str, list[dict]]) -> list[dict]:
    return [
        {"domain": domain, "operations": sorted(operations, key=lambda o: (o["path"], o["method"]))}
        for domain, operations in sorted(groups.items())
    ]


@click.command()
@click.option("--spec", "spec_path", type=click.Path(exists=True), help="Path to a merged openapi.yaml")
@click.option("--from-recent", "recent", is_flag=True, help="Derive from proxy /recent traffic")
def main(spec_path: str | None, recent: bool) -> None:
    if spec_path:
        _write(from_spec(spec_path))
    elif recent:
        _write(from_recent())
    else:
        click.echo("give --spec <openapi.yaml> or --from-recent", err=True)
        sys.exit(2)


if __name__ == "__main__":
    main()
