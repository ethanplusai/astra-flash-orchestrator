#!/usr/bin/env python3
"""Preview/install the Astra + Flash skill without changing Codex model/provider settings."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from datetime import datetime, timezone
import uuid

if sys.version_info < (3, 11):
    raise SystemExit("Python 3.11+ is required. No packages or settings were changed.")
sys.dont_write_bytecode = True
BUNDLE = Path(__file__).resolve().parent
SKILL_SOURCE = BUNDLE / "skill" / "astra-flash-orchestrator"
sys.path.insert(0, str(SKILL_SOURCE / "scripts"))
from local_config import SetupError, default_locations, inspect, resolve_worker_selection, ROLE, SKILL, SUPPORTED_ROUTES, BACKEND_NATIVE

BEGIN = b"<!-- BEGIN astra-flash-orchestrator managed policy -->"
END = b"<!-- END astra-flash-orchestrator managed policy -->"


def digest(data: bytes | None) -> str | None:
    return hashlib.sha256(data).hexdigest() if data is not None else None


def no_symlinks(path: Path) -> None:
    for item in (path, *path.parents):
        if item.is_symlink():
            raise SetupError(f"Refusing to write through a symlink: {item}. Use the documented manual installation instead.")


def contents(path: Path) -> bytes | None:
    no_symlinks(path)
    if not path.exists():
        return None
    if not path.is_file():
        raise SetupError(f"Expected a regular file: {path}")
    return path.read_bytes()


def managed_policy(original: bytes, block: bytes) -> bytes:
    if original.count(BEGIN) != original.count(END) or original.count(BEGIN) > 1:
        raise SetupError("The managed instruction block is malformed or duplicated; reconcile it before installation.")
    newline = b"\r\n" if b"\r\n" in original else b"\n"
    block = block.replace(b"\r\n", b"\n").replace(b"\n", newline).rstrip() + newline
    if BEGIN in original:
        start, end = original.index(BEGIN), original.index(END) + len(END)
        if end < start:
            raise SetupError("The managed policy markers are reversed.")
        if original[end:end + len(newline)] == newline:
            end += len(newline)
        return original[:start] + block + original[end:]
    separator = b"" if not original else (newline if original.endswith(newline) else newline + newline)
    return original + separator + block



def remove_managed_policy(original: bytes) -> bytes:
    if BEGIN not in original and END not in original:
        return original
    if original.count(BEGIN) != 1 or original.count(END) != 1 or original.index(END) < original.index(BEGIN):
        raise SetupError("The managed instruction block is malformed or duplicated; reconcile it before installation.")
    start = original.index(BEGIN)
    end = original.index(END) + len(END)
    if original[end:end + 2] == b"\r\n":
        end += 2
    elif original[end:end + 1] == b"\n":
        end += 1
    return original[:start] + original[end:]

def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    no_symlinks(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".astra-flash-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def plan_changes(home: Path, codex_home: Path, report: dict, with_policy: bool, replace: bool) -> list[dict]:
    target = home / ".agents" / "skills" / SKILL
    role_path = codex_home / "agents" / f"{ROLE}.toml"
    no_symlinks(target)
    # Detect a second copy instead of silently creating duplicate skill activations.
    legacy = codex_home / "skills" / SKILL
    if legacy.exists():
        raise SetupError(f"An existing same-named skill is at {legacy}. Reconcile duplicate locations first.")
    requested: dict[Path, bytes] = {}
    for source in sorted(SKILL_SOURCE.rglob("*")):
        if source.is_symlink():
            raise SetupError("The bundle contains an unexpected symlink.")
        if (source.is_file() and "__pycache__" not in source.parts
                and source.suffix not in {".pyc", ".pyo", ".bak"}
                and ".before-" not in source.name and source.name != ".DS_Store"):
            requested[target / source.relative_to(SKILL_SOURCE)] = source.read_bytes()
    routing = {
        key: report[key]
        for key in ("backend", "worker_model", "worker_provider", "worker_effort", "custom_agent", "profile_inspected")
    }
    if report["backend"] == BACKEND_NATIVE:
        routing["model_catalog_source"] = report["model_catalog_source"]
    requested[target / "routing.json"] = (json.dumps(routing, indent=2) + "\n").encode()
    instructions = (BUNDLE / "WORKER-INSTRUCTIONS.md").read_text(encoding="utf-8").strip()
    # JSON basic strings are valid TOML basic strings for these generated values.
    role = (
        f'name = {json.dumps(ROLE)}\n'
        'description = "Implement an Astra-approved task bundle using the installed worker model; never orchestrate or self-approve."\n'
        f'model = {json.dumps(report["worker_model"])}\n'
    )
    if report["backend"] == BACKEND_NATIVE:
        role += 'model_provider = "openai"\n'
    if report["worker_effort"]:
        role += f'model_reasoning_effort = {json.dumps(report["worker_effort"])}\n'
    role += f'developer_instructions = {json.dumps(instructions, ensure_ascii=False)}\n\n'
    role += '# Inherit parent sandbox/approvals. Declare no nested agents for compatible clients.\n[agents]\nenabled = false\n'
    requested[role_path] = role.encode()
    policy_path = None
    if not with_policy:
        for existing_policy in (codex_home / "AGENTS.md", codex_home / "AGENTS.override.md"):
            old_policy = contents(existing_policy)
            if old_policy and BEGIN in old_policy and managed_policy(old_policy, (BUNDLE / "POLICY.md").read_bytes()) != old_policy:
                raise SetupError("--no-policy would leave an outdated managed workflow block; update it with the reviewed installation.")
    if with_policy:
        override = codex_home / "AGENTS.override.md"
        override_data = contents(override)
        policy_path = override if override_data and override_data.strip() else codex_home / "AGENTS.md"
        requested[policy_path] = managed_policy(contents(policy_path) or b"", (BUNDLE / "POLICY.md").read_bytes())
        other = codex_home / ("AGENTS.md" if policy_path == override else "AGENTS.override.md")
        other_data = contents(other)
        if other_data and (BEGIN in other_data or END in other_data):
            requested[other] = remove_managed_policy(other_data)
    changes = []
    for path, new in requested.items():
        old = contents(path)
        if old == new:
            continue
        if old is not None and path != policy_path and not replace:
            raise SetupError(f"Different content already exists at {path}. Inspect it, then explicitly use --replace to back it up and update it.")
        changes.append({"path": path, "before": old, "after": new, "mode": path.stat().st_mode & 0o777 if path.exists() else 0o600})
    return changes


def apply_changes(changes: list[dict], codex_home: Path, input_hashes: dict[str, str | None]) -> Path | None:
    if not changes:
        return None
    for name, expected in input_hashes.items():
        source = Path(name)
        if expected is None:
            changed = source.exists() or source.is_symlink()
        else:
            try:
                changed = digest(source.read_bytes()) != expected
            except OSError:
                changed = True
        if changed:
            raise SetupError("The Codex configuration changed during inspection. Rerun the installer.")
    for change in changes:
        if contents(change["path"]) != change["before"]:
            raise SetupError("An installation target changed during inspection. Rerun the installer.")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    backup_dir = codex_home / "astra-flash-install-backups" / stamp
    no_symlinks(backup_dir)
    backup_dir.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    backup_dir.mkdir(mode=0o700)
    entries = []
    for index, change in enumerate(changes):
        backup = f"before-{index}.bin" if change["before"] is not None else None
        if backup:
            atomic_write(backup_dir / backup, change["before"], 0o600)
        entries.append({"path": str(change["path"]), "before_file": backup, "before_hash": digest(change["before"]), "after_hash": digest(change["after"]), "mode": change["mode"]})
    receipt = backup_dir / "receipt.json"
    record = {"format": 1, "status": "prepared", "files": entries}
    atomic_write(receipt, (json.dumps(record, indent=2) + "\n").encode())
    completed = []
    try:
        for change in changes:
            atomic_write(change["path"], change["after"], change["mode"])
            completed.append(change)
    except BaseException:
        for change in reversed(completed):
            if change["before"] is None:
                change["path"].unlink(missing_ok=True)
            else:
                atomic_write(change["path"], change["before"], change["mode"])
        record["status"] = "rolled-back"
        atomic_write(receipt, (json.dumps(record, indent=2) + "\n").encode())
        raise
    record["status"] = "installed"
    atomic_write(receipt, (json.dumps(record, indent=2) + "\n").encode())
    return receipt


def undo(receipt: Path, home: Path, codex_home: Path, apply: bool) -> None:
    no_symlinks(receipt)
    if ".." in receipt.parts or not receipt.resolve().is_relative_to((codex_home / "astra-flash-install-backups").resolve()):
        raise SetupError("The receipt must be inside this CODEX_HOME's astra-flash-install-backups folder.")
    record = json.loads(receipt.read_text())
    if record.get("format") != 1 or record.get("status") != "installed":
        raise SetupError("This receipt does not describe an installed, undoable transaction.")
    root = home / ".agents" / "skills" / SKILL
    fixed = {codex_home / "agents" / f"{ROLE}.toml", codex_home / "AGENTS.md", codex_home / "AGENTS.override.md"}
    pending = []
    for entry in record["files"]:
        path = Path(entry["path"])
        if not path.is_absolute() or ".." in path.parts or (path not in fixed and not path.resolve().is_relative_to(root.resolve())):
            raise SetupError("The receipt contains a target outside this package's installation paths.")
        current = contents(path)
        if digest(current) != entry["after_hash"]:
            raise SetupError(f"Refusing undo: {path} changed after installation. Preserve/reconcile those edits first.")
        before = None
        if entry["before_file"]:
            name = entry["before_file"]
            if Path(name).name != name:
                raise SetupError("Invalid backup filename in receipt.")
            before = (receipt.parent / name).read_bytes()
        if digest(before) != entry["before_hash"]:
            raise SetupError("A backup no longer matches its receipt. Nothing was restored.")
        pending.append((path, before, entry["mode"]))
    for path, before, mode in pending:
        print(f"{'RESTORE' if before is not None else 'REMOVE'} {path}")
        if apply:
            if before is None:
                path.unlink()
            else:
                atomic_write(path, before, mode)
    if apply:
        record["status"] = "restored"
        atomic_write(receipt, (json.dumps(record, indent=2) + "\n").encode())
        if root.exists():
            for directory in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            try:
                root.rmdir()
            except OSError:
                pass
    else:
        print("Preview only. Add --apply to perform this undo.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the reviewed installation; otherwise preview only")
    parser.add_argument("--no-policy", action="store_true", help="install skill/role without changing personal AGENTS guidance")
    parser.add_argument("--replace", action="store_true", help="back up and replace different existing package-owned files")
    parser.add_argument("--home", help="override HOME (primarily for isolated tests)")
    parser.add_argument("--codex-home", help="override CODEX_HOME")
    parser.add_argument("--profile", help="inspect a specific existing profile; does not change profile selection")
    parser.add_argument(
        "--worker-route",
        choices=SUPPORTED_ROUTES,
        help="pin one reviewed DeepSeek V4.1 Flash provider route (default: existing binding, then direct DeepSeek API)",
    )
    parser.add_argument("--worker-model", help="pin an exact native OpenAI model from local metadata")
    parser.add_argument("--worker-effort", help="pin a supported native OpenAI reasoning effort; requires --worker-model")
    parser.add_argument("--model-catalog", help="local exported model catalog when no configured catalog or cache exists; requires --worker-model")
    parser.add_argument("--undo", type=Path, metavar="RECEIPT", help="preview restoration from an installation receipt; combine with --apply to restore")
    args = parser.parse_args()
    try:
        home, codex_home = default_locations(args.home, args.codex_home)
        if args.undo:
            undo(args.undo, home, codex_home, args.apply)
            return 0
        binding = home / ".agents" / "skills" / SKILL / "routing.json"
        selection = resolve_worker_selection(args.worker_route, args.worker_model, args.worker_effort,
                                             binding, args.model_catalog)
        report, _private_url = inspect(home, codex_home, args.profile if args.profile is not None else selection.get("profile"), selection=selection)
        changes = plan_changes(home, codex_home, report, not args.no_policy, args.replace)
        print(json.dumps(report, indent=2))
        for change in changes:
            print(f"{'UPDATE' if change['before'] is not None else 'CREATE'} {change['path']}")
        if not args.apply:
            print("Preview only. No files changed. Add --apply after reviewing these destinations.")
            return 0
        receipt = apply_changes(changes, codex_home, report["input_hashes"])
        print(f"Installed. Undo receipt: {receipt}" if receipt else "Already installed; no changes needed.")
        print("config.toml and router/authentication files were not written. No model request was made.")
        print("Fully quit/reopen the host app, then start an Astra session. Runtime model identity still needs a real delegated-task check.")
        return 0
    except (SetupError, OSError, ValueError) as exc:
        message = str(exc) if isinstance(exc, SetupError) else f"Local installation error ({type(exc).__name__}); inspect locally."
        print(f"INSTALL FAILED: {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
