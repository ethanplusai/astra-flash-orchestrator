#!/usr/bin/env python3
"""Read-only inspection of the selected worker backend; no model requests."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

if sys.version_info < (3, 11):
    raise SystemExit("Python 3.11+ is required. No packages or settings were changed.")
import tomllib

ROUTE = "deepseek/deepseek-v4.1-flash"
SUPPORTED_ROUTES = {
    ROUTE: "DeepSeek API",
    "openrouter/deepseek-v4.1-flash": "OpenRouter",
    "opencode-go/deepseek-v4.1-flash": "opencode Go",
    "commandcode/deepseek-v4.1-flash": "Command Code",
    "nousresearch/deepseek-v4.1-flash": "Nous Research",
    "ollama-cloud/deepseek-v4.1-flash": "Ollama Cloud",
}
ROLE = "astra_flash_builder"
SKILL = "astra-flash-orchestrator"
BACKEND_ROUTER = "router"
BACKEND_NATIVE = "native_openai"
ENDPOINT_OVERRIDES = ("OPENAI_BASE_URL", "CODEX_OPENAI_BASE_URL", "CODEX_CHATGPT_BASE_URL")

# Keys Codex reads as scalar settings directly under [agents]. Every other key
# there is read as an agent NAME whose value must be a role table, so a scalar
# under an unrecognized name makes Codex reject the entire config with
# "invalid type: ..., expected struct AgentRoleToml in `agents`" -- which takes
# down the host app and the CLI together, not just subagent routing.
AGENT_SCALAR_SETTINGS = frozenset({
    "enabled",
    "default_subagent_model",
    "default_subagent_reasoning_effort",
    "interrupt_message",
    "max_concurrent_threads_per_session",
    "max_threads",
    "max_depth",
    "job_max_runtime_seconds",
})


class SetupError(ValueError):
    """An actionable configuration problem, without credential-bearing details."""


def read_toml(path: Path) -> dict:
    try:
        with path.open("rb") as stream:
            return tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        # TOML errors can embed source text. Never echo them or a full config.
        raise SetupError(f"Cannot read valid TOML from {path.name} ({type(exc).__name__}).") from None


def merge_tables(base: dict, overlay: dict) -> dict:
    result = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_tables(result[key], value)
        else:
            result[key] = value
    return result


def resolve_path(value: str, home: Path, codex_home: Path) -> Path:
    value = value.replace("${CODEX_HOME}", str(codex_home)).replace("$CODEX_HOME", str(codex_home))
    value = value.replace("${HOME}", str(home)).replace("$HOME", str(home))
    if value == "~":
        value = str(home)
    elif value.startswith("~/"):
        value = str(home / value[2:])
    path = Path(value)
    return path if path.is_absolute() else codex_home / path


def model_entries(payload: object) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("models", "data"):
            if isinstance(payload.get(key), list):
                return model_entries(payload[key])
    raise SetupError("Unrecognized model catalog structure; inspect it locally before installing.")


def model_id(entry: dict) -> str | None:
    return entry.get("slug") or entry.get("id")


def resolve_worker_route(requested: str | None = None, binding: Path | None = None) -> str:
    """Validate an explicit route or reuse this package's existing routing binding."""
    if requested is not None:
        route = requested
    elif binding is None:
        route = ROUTE
    else:
        if any(item.is_symlink() for item in (binding, *binding.parents)):
            raise SetupError("Refusing to read a worker route through a symlinked routing binding.")
        if not binding.exists():
            return ROUTE
        try:
            if binding.stat().st_size > 64_000:
                raise SetupError("The existing routing binding is unexpectedly large; inspect it locally.")
            payload = json.loads(binding.read_text(encoding="utf-8"))
            route = payload.get("worker_model") if isinstance(payload, dict) else None
        except (OSError, json.JSONDecodeError, UnicodeError) as exc:
            raise SetupError(f"Cannot read the existing routing binding ({type(exc).__name__}).") from None
        if not isinstance(route, str):
            raise SetupError("The existing routing binding does not name a worker model.")
    if route not in SUPPORTED_ROUTES:
        raise SetupError(
            "Unsupported worker route. Choose a reviewed DeepSeek V4.1 Flash route: "
            + ", ".join(SUPPORTED_ROUTES)
        )
    return route


def resolve_worker_selection(requested_route: str | None = None, requested_model: str | None = None,
                             requested_effort: str | None = None, binding: Path | None = None,
                             requested_catalog: str | None = None) -> dict:
    """Resolve explicit selection or retain an installed binding; legacy means Router."""
    if requested_route is not None and requested_model is not None:
        raise SetupError("--worker-route and --worker-model conflict; choose one backend.")
    if requested_effort is not None and requested_model is None:
        raise SetupError("--worker-effort requires --worker-model; an installed effort is retained automatically.")
    if requested_catalog is not None and requested_model is None:
        raise SetupError("--model-catalog requires --worker-model; an installed catalog is retained automatically.")
    if requested_route is not None:
        return {"backend": BACKEND_ROUTER, "model": resolve_worker_route(requested_route)}
    if requested_model is not None:
        if not requested_model.strip() or requested_model != requested_model.strip():
            raise SetupError("--worker-model requires a nonempty exact model identifier.")
        return {"backend": BACKEND_NATIVE, "model": requested_model,
                "effort": requested_effort,
                "catalog": str(Path(requested_catalog).expanduser().absolute()) if requested_catalog else None,
                "catalog_explicit": requested_catalog is not None}
    if binding is None:
        return {"backend": BACKEND_ROUTER, "model": ROUTE}
    if any(item.is_symlink() for item in (binding, *binding.parents)):
        raise SetupError("Refusing to read a worker selection through a symlinked routing binding.")
    if not binding.exists():
        return {"backend": BACKEND_ROUTER, "model": ROUTE}
    try:
        if binding.stat().st_size > 64_000:
            raise SetupError("The existing routing binding is unexpectedly large; inspect it locally.")
        payload = json.loads(binding.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        raise SetupError(f"Cannot read the existing routing binding ({type(exc).__name__}).") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("worker_model"), str):
        raise SetupError("The existing routing binding does not name a worker model.")
    backend = payload.get("backend", BACKEND_ROUTER)
    if backend == BACKEND_ROUTER:
        return {"backend": backend, "model": resolve_worker_route(payload["worker_model"]),
                "profile": payload.get("profile_inspected")}
    if backend != BACKEND_NATIVE:
        raise SetupError("The existing routing binding names an unsupported backend.")
    if payload.get("worker_provider") != "OpenAI" or not payload["worker_model"].strip():
        raise SetupError("The existing native routing binding is incomplete.")
    effort, catalog = payload.get("worker_effort"), payload.get("model_catalog_source")
    if effort is not None and not isinstance(effort, str):
        raise SetupError("The existing native worker effort is invalid.")
    if catalog is not None and not isinstance(catalog, str):
        raise SetupError("The existing native catalog source is invalid.")
    return {"backend": backend, "model": payload["worker_model"], "effort": effort,
            "catalog": catalog, "catalog_explicit": False, "profile": payload.get("profile_inspected")}


def inspect(
    home: Path,
    codex_home: Path,
    profile: str | None = None,
    worker_route: str = ROUTE,
    selection: dict | None = None,
) -> tuple[dict, str]:
    """Return a redacted static report and a PRIVATE local URL. Do not print URL."""
    selection = selection or {"backend": BACKEND_ROUTER, "model": resolve_worker_route(worker_route)}
    if selection["backend"] == BACKEND_ROUTER:
        worker_route = resolve_worker_route(selection["model"])
    config_path = codex_home / "config.toml"
    if selection["backend"] == BACKEND_NATIVE and not config_path.exists():
        config = {}
        input_hashes = {str(config_path): None}
    else:
        config = read_toml(config_path)
        input_hashes = {str(config_path): hashlib.sha256(config_path.read_bytes()).hexdigest()}
    selected = profile if profile is not None else config.get("profile")
    warnings: list[str] = []
    if input_hashes.get(str(config_path)) is None:
        warnings.append("No config.toml was found; the root model and effective session profile must be verified in the client.")
    if selected:
        if not isinstance(selected, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", selected):
            raise SetupError("Unsupported profile name; inspect the active profile manually.")
        standalone = codex_home / f"{selected}.config.toml"
        legacy = config.get("profiles", {}).get(selected)
        if standalone.exists() and legacy is not None:
            raise SetupError("Both standalone and legacy profile definitions exist; resolve that ambiguity first.")
        if standalone.exists():
            config = merge_tables(config, read_toml(standalone))
            input_hashes[str(standalone)] = hashlib.sha256(standalone.read_bytes()).hexdigest()
        elif isinstance(legacy, dict):
            config = merge_tables(config, legacy)
            warnings.append("A legacy inline profile was inspected; confirm your client still applies it.")
        else:
            raise SetupError("The selected profile is not available as a readable configuration file.")

    agents = config.get("agents", {})
    if not isinstance(agents, dict):
        raise SetupError("The existing [agents] setting is not a TOML table.")
    # Checking shape rather than a list of known top-level names catches any
    # absorbed key, not just the handful an installer happens to anticipate.
    misplaced = sorted(
        key for key, value in agents.items()
        if key not in AGENT_SCALAR_SETTINGS and not isinstance(value, dict)
    )
    if misplaced:
        raise SetupError(
            "Setting(s) that do not belong under [agents] were found there: "
            + ", ".join(misplaced)
            + ". In TOML, a table header remains active until the next table header, so a "
            "top-level key written after [agents] is absorbed into it; Codex then reads that "
            "key as an agent name and refuses to load the whole config. Move those keys above "
            "the first table header, or under the agent role they belong to, before installing. "
            "If your Codex build documents one of them as a genuine [agents] setting, it is newer "
            "than this check; verify with `codex doctor` rather than editing around this error."
        )
    if agents.get("enabled") is False:
        raise SetupError("Subagents are disabled in the inspected config. This installer will not enable them silently.")
    if "default_subagent_model" in agents:
        warnings.append(
            "The global default_subagent_model is not used or changed; the installed named role pins its own worker model."
        )
    if ROLE in agents:
        raise SetupError("An inline [agents.astra_flash_builder] role would compete with the standalone role. Reconcile it first.")
    if config.get("model") in SUPPORTED_ROUTES:
        raise SetupError("The root model is Flash. Select Astra as root before installing this workflow.")
    if config.get("features", {}).get("multi_agent") is False:
        warnings.append("A legacy features.multi_agent=false flag exists; check whether your client honors it.")
    if selection["backend"] == BACKEND_NATIVE:
        return inspect_native(home, codex_home, config, input_hashes, selected, warnings, selection)
    catalog_value = config.get("model_catalog_json")
    if not isinstance(catalog_value, str) or not catalog_value:
        raise SetupError("No model_catalog_json was found. Confirm the existing Codex Router configuration.")
    catalog_path = resolve_path(catalog_value, home, codex_home)
    try:
        if catalog_path.stat().st_size > 20_000_000:
            raise SetupError("The model catalog is unexpectedly large; inspect it manually.")
        payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        raise SetupError(f"Cannot read the configured model catalog ({type(exc).__name__}).") from None
    matches = [entry for entry in model_entries(payload) if model_id(entry) == worker_route]
    if len(matches) != 1:
        raise SetupError(
            f"The selected Flash V4.1 route ({worker_route}) is missing or duplicated in the local catalog. "
            "Configure that exact route with the Router's own local setup, then rerun this installer. "
            "No provider was substituted."
        )
    entry = matches[0]
    if entry.get("multi_agent_version") != "v2":
        raise SetupError(
            f"The selected Flash route ({worker_route}) exists in the catalog but is not "
            "advertised for native subagents "
            "(multi_agent_version must be v2). Select this exact route using your "
            "Router's documented subagent settings, republish the catalog, and fully "
            "quit/reopen the host app. Selection is not runtime verification. "
            "Do not run subagents certify, test-model --live, a smoke test, or another "
            "paid probe as part of this package's installation."
        )
    levels = entry.get("supported_reasoning_levels", [])
    supported = [x.get("effort") if isinstance(x, dict) else x for x in levels] if isinstance(levels, list) else []
    # The named role owns both worker settings. Do not couple installation to,
    # inherit, or encourage mutation of global defaults used by unrelated agents.
    effort = entry.get("default_reasoning_level")
    if effort is not None and (not isinstance(effort, str) or not re.fullmatch(r"[a-z_]+", effort)):
        raise SetupError("The worker reasoning effort is not a recognized string value.")
    if effort is not None and supported and effort not in supported:
        raise SetupError("The worker effort does not match its catalog's supported efforts. Reconcile it locally first.")
    if effort is None:
        warnings.append("No worker effort was pinned; use explicit model selection without an effort at spawn, then inspect the actual thread.")

    provider = config.get("model_provider", "openai")
    if provider == "openai":
        url = config.get("openai_base_url", "")
    else:
        url = config.get("model_providers", {}).get(provider, {}).get("base_url", "")
    try:
        parsed = urlsplit(url)
        loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        # Codex Router supports both native authenticated /v1 and capability paths.
        # Keep exact path shapes; never accept arbitrary loopback API paths.
        route_path = parsed.path.rstrip("/")
        recognized_path = route_path == "/v1" or bool(re.fullmatch(r"/_codex-router/[A-Za-z0-9_-]+/v1", route_path))
        valid = parsed.scheme in {"http", "https"} and loopback and recognized_path
        valid = valid and not parsed.query and not parsed.fragment
        valid = valid and not parsed.username and not parsed.password
        _ = parsed.port
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise SetupError("The inspected provider does not point at a recognized loopback Codex Router URL. URL withheld.")
    if not config.get("model"):
        warnings.append("No root model is set in this config; select GPT-6 Astra in the new session UI.")
    warnings.append("Project, CLI, UI and managed-policy overrides are not resolved by this static inspection.")
    report = {
        "status": "static-ready",
        "runtime_verified": False,
        "inference_request_made": False,
        "root_model_observed": config.get("model"),
        "root_effort_observed": config.get("model_reasoning_effort"),
        "backend": BACKEND_ROUTER,
        "worker_model": worker_route,
        "worker_provider": SUPPORTED_ROUTES[worker_route],
        "worker_effort": effort,
        "custom_agent": ROLE,
        "profile_inspected": selected,
        "catalog_contains_worker": True,
        "catalog_advertises_subagent": True,
        "loopback_router_configured": True,
        "input_hashes": input_hashes,
        "warnings": warnings,
    }
    return report, url



def inspect_native(home: Path, codex_home: Path, config: dict, input_hashes: dict,
                   selected: str | None, warnings: list[str], selection: dict) -> tuple[dict, str]:
    """Check only local metadata and effective native-provider compatibility."""
    if config.get("model_provider", "openai") != "openai":
        raise SetupError("Native OpenAI requires an already active openai parent provider/profile; this client inherits the parent provider in subagents.")
    if config.get("openai_base_url"):
        raise SetupError("Native OpenAI requires the built-in openai endpoint; remove the custom openai_base_url in a compatible profile/session.")
    if config.get("chatgpt_base_url") not in (None, "https://chatgpt.com/backend-api/"):
        raise SetupError("Native OpenAI requires the built-in ChatGPT endpoint; choose a compatible profile/session.")
    if isinstance(config.get("model_providers"), dict) and "openai" in config["model_providers"]:
        raise SetupError("Native OpenAI cannot verify a redefined openai provider; choose a compatible profile/session.")
    if any(os.environ.get(key) for key in ENDPOINT_OVERRIDES):
        raise SetupError("Native OpenAI cannot verify provider identity while an endpoint override environment variable is set.")
    model = selection["model"]
    if "/" in model:
        raise SetupError("A provider-qualified route is not a native OpenAI model; use --worker-route for reviewed DeepSeek routes.")
    if not config.get("model"):
        warnings.append("No root model is set in inspected config; verify the selected root in the active session.")
    configured = config.get("model_catalog_json")
    cache = codex_home / "models_cache.json"
    supplied = selection.get("catalog")
    if configured is not None and (not isinstance(configured, str) or not configured):
        raise SetupError("The configured model_catalog_json is invalid; cannot verify native capabilities locally.")
    if selection.get("catalog_explicit") and configured and Path(supplied) != resolve_path(configured, home, codex_home):
        raise SetupError("--model-catalog cannot override the configured model_catalog_json.")
    if selection.get("catalog_explicit") and cache.exists() and not configured and Path(supplied) != cache:
        raise SetupError("--model-catalog cannot override the local models_cache.json.")
    source = resolve_path(configured, home, codex_home) if configured else (
        Path(supplied) if supplied and not selection.get("catalog_explicit") else
        cache if cache.exists() else Path(supplied) if supplied else None)
    if source is None:
        raise SetupError("Cannot verify native model capabilities locally; provide a local catalog with --model-catalog.")
    try:
        if source.stat().st_size > 20_000_000:
            raise SetupError("The model catalog is unexpectedly large; inspect it manually.")
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        raise SetupError(f"Cannot read native model metadata locally ({type(exc).__name__}).") from None
    matches = [entry for entry in model_entries(payload) if model_id(entry) == model]
    if len(matches) != 1:
        raise SetupError("The selected native model is missing or duplicated in local metadata; no model was substituted.")
    entry = matches[0]
    advertised_provider = entry.get("model_provider", entry.get("provider"))
    if advertised_provider is not None and advertised_provider != "openai":
        raise SetupError("The selected catalog entry declares a non-OpenAI provider; choose a native OpenAI model.")
    version = entry.get("multi_agent_version")
    if version is None:
        raise SetupError("Cannot verify native subagent capability locally: metadata omits multi_agent_version.")
    if version != "v2":
        raise SetupError("The selected native model is advertised as incompatible with v2 subagents.")
    levels = entry.get("supported_reasoning_levels")
    if not isinstance(levels, list) or not levels:
        raise SetupError("Cannot verify supported native reasoning efforts locally: metadata is missing.")
    supported = [x.get("effort") if isinstance(x, dict) else x for x in levels]
    supported = [x for x in supported if isinstance(x, str) and x]
    if not supported:
        raise SetupError("Cannot verify supported native reasoning efforts locally: metadata is invalid.")
    effort = selection.get("effort")
    if effort is None:
        effort = entry.get("default_reasoning_level")
        if not isinstance(effort, str) or not effort:
            raise SetupError("Cannot verify a default native reasoning effort locally; pass --worker-effort.")
    if effort not in supported:
        raise SetupError("The selected native reasoning effort is not supported by this model's local metadata.")
    if source == cache:
        warnings.append("The local models_cache.json is a capability snapshot; this check does not validate its age, account identity or current service access.")
    else:
        warnings.append("The local model catalog is capability metadata, not proof of account access or serving identity.")
    warnings.append("The installed role pins an intended model/provider, but this client inherits the parent's provider; check active profile, project, UI and managed overrides.")
    warnings.append("Runtime identity requires client-recorded child metadata and service-reported model/provider evidence.")
    report = {
        "status": "static-ready", "runtime_verified": False, "inference_request_made": False,
        "backend": BACKEND_NATIVE,
        "root_model_observed": config.get("model"),
        "root_effort_observed": config.get("model_reasoning_effort"),
        "worker_model": model, "worker_provider": "OpenAI", "worker_effort": effort,
        "custom_agent": ROLE, "profile_inspected": selected,
        "model_catalog_source": str(source),
        "catalog_contains_worker": True, "catalog_advertises_subagent": True,
        "loopback_router_configured": False,
        "input_hashes": {**input_hashes, str(source): hashlib.sha256(source.read_bytes()).hexdigest()},
        "warnings": warnings,
    }
    return report, ""


def default_locations(home_arg: str | None = None, codex_home_arg: str | None = None) -> tuple[Path, Path]:
    home = Path(home_arg).expanduser().resolve() if home_arg else Path.home().resolve()
    codex_home = Path(codex_home_arg or os.environ.get("CODEX_HOME", str(home / ".codex"))).expanduser().absolute()
    return home, codex_home
