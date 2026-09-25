#!/usr/bin/env python3
"""Check installed worker configuration; optionally query only the loopback Router /models endpoint."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
import tomllib
import urllib.error
import urllib.request
sys.dont_write_bytecode = True
from local_config import SetupError, default_locations, inspect, model_entries, model_id, resolve_worker_selection, SUPPORTED_ROUTES, BACKEND_NATIVE, ROLE


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SetupError("Local catalog redirected. Refusing to forward the private caller URL.")


def check_local_catalog(url: str, worker_route: str) -> None:
    # Disable ambient HTTP proxies and redirects: the capability must stay local.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(url.rstrip("/") + "/models", headers={"Accept": "application/json"})
    try:
        with opener.open(request, timeout=5) as response:
            body = response.read(10_000_001)
        if len(body) > 10_000_000:
            raise SetupError("Local model response exceeded its size limit.")
        entries = model_entries(json.loads(body))
        if not any(model_id(entry) == worker_route for entry in entries):
            raise SetupError("The live local catalog does not advertise the requested Flash route.")
    except (OSError, urllib.error.URLError, json.JSONDecodeError, UnicodeError) as exc:
        raise SetupError(f"Local catalog check failed ({type(exc).__name__}); private URL withheld.") from None


def check_installed_role(path: Path, report: dict) -> None:
    if path.is_symlink() or not path.is_file():
        raise SetupError("The installed worker role is missing or symlinked; inspect the installation.")
    try:
        with path.open("rb") as stream:
            role = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError):
        raise SetupError("The installed worker role is unreadable or invalid TOML.") from None
    if role.get("name") != ROLE:
        raise SetupError("The installed worker role name differs from its routing binding.")
    if role.get("model") != report["worker_model"] or role.get("model_reasoning_effort") != report["worker_effort"]:
        raise SetupError("The installed worker role model/effort differs from routing.json; reinstall with --replace.")
    if report["backend"] == BACKEND_NATIVE:
        if role.get("model_provider") != "openai":
            raise SetupError("The installed native worker role does not declare the intended OpenAI provider.")
    elif role.get("model_provider") is not None:
        raise SetupError("The installed Router worker role unexpectedly overrides its provider.")
    if not isinstance(role.get("agents"), dict) or role["agents"].get("enabled") is not False:
        raise SetupError("The installed worker role does not disable nested agents.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home")
    parser.add_argument("--codex-home")
    parser.add_argument("--profile")
    parser.add_argument(
        "--worker-route",
        choices=SUPPORTED_ROUTES,
        help="check a reviewed route (default: installed routing binding, then direct DeepSeek API)",
    )
    parser.add_argument("--worker-model", help="inspect an exact native OpenAI model")
    parser.add_argument("--worker-effort", help="supported native effort; requires --worker-model")
    parser.add_argument("--model-catalog", help="exported local metadata when no configured catalog/cache exists")
    parser.add_argument(
        "--check-local-router",
        action="store_true",
        help="GET the loopback /models endpoint; never send an inference request",
    )
    args = parser.parse_args()
    try:
        home, codex_home = default_locations(args.home, args.codex_home)
        binding = Path(__file__).resolve().parents[1] / "routing.json"
        selection = resolve_worker_selection(args.worker_route, args.worker_model, args.worker_effort,
                                             binding, args.model_catalog)
        report, url = inspect(home, codex_home, args.profile if args.profile is not None else selection.get("profile"), selection=selection)
        if binding.exists() and args.worker_route is None and args.worker_model is None:
            check_installed_role(codex_home / "agents" / f"{ROLE}.toml", report)
        if args.check_local_router:
            if report["backend"] == BACKEND_NATIVE:
                raise SetupError("--check-local-router applies only to Router installations.")
            check_local_catalog(url, report["worker_model"])
            report["status"] = "local-catalog-ready"
            report["local_catalog_checked"] = True
        print(json.dumps(report, indent=2))
        return 0
    except SetupError as exc:
        print(f"CHECK FAILED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
