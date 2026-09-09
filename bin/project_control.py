#!/usr/bin/env python3
"""Policy-limited project control plane over durable goals and registered repositories."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence


BIN_DIR = Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


BUS = _load("project_control_bus", "chatgpt_server_bus.py")
GOAL = _load("project_control_goal", "server_goal_driver.py")
WORKER = _load("project_control_worker", "server_goal_task_worker.py")
REGISTRY = _load("project_control_registry", "project_repo_registry.py")
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = Path.home() / ".local/state/ai-bus/bus.sqlite3"
MAX_TEXT_FILE_BYTES = 1_000_000
MAX_READ_LINES = 800
MAX_SEARCH_RESULTS = 200
MAX_DIFF_BYTES = 300_000
MAX_PROCESS_OUTPUT_BYTES = 200_000
MAX_PATCH_OPERATIONS = 50
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ProjectControlError(RuntimeError):
    pass


def _repo_id(value: str) -> str:
    try:
        return REGISTRY.normalize_repo_id(value)
    except REGISTRY.RepoRegistryError as exc:
        raise ProjectControlError(str(exc)) from exc


def load_repo_registry(items: Sequence[str] = ()) -> dict[str, Path]:
    try:
        return REGISTRY.load_registry(items)
    except REGISTRY.RepoRegistryError as exc:
        raise ProjectControlError(str(exc)) from exc


def _registered_repo(registry: Mapping[str, Path], repo_id: str) -> tuple[str, Path]:
    normalized = _repo_id(repo_id)
    if normalized not in registry:
        raise ProjectControlError(f"repository id {normalized!r} is not registered")
    root = Path(registry[normalized]).expanduser().resolve()
    if not root.is_dir():
        raise ProjectControlError(f"registered repository {normalized!r} is unavailable: {root}")
    return normalized, root


def _relative_path(value: str, *, allow_dot: bool = False) -> Path:
    text = str(value or "").strip()
    if allow_dot and text in {"", "."}:
        return Path(".")
    if not text or "\x00" in text or "\\" in text:
        raise ProjectControlError("path must be a non-empty repository-relative POSIX path")
    rel = Path(text)
    if rel.is_absolute() or any(part in {"", ".", "..", ".git"} for part in rel.parts):
        raise ProjectControlError("path must stay inside the registered repository and may not address .git")
    return rel


def _inside(root: Path, resolved: Path) -> bool:
    return resolved == root or root in resolved.parents


def _reject_symlink_components(root: Path, rel: Path) -> None:
    current = root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise ProjectControlError("path may not traverse symlinks")


def _repo_file(root: Path, value: str, *, allow_missing: bool = False) -> tuple[Path, Path]:
    rel = _relative_path(value)
    _reject_symlink_components(root, rel)
    target = root / rel
    parent = target.parent.resolve()
    if not _inside(root, parent):
        raise ProjectControlError("path escapes the registered repository")
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_file():
            raise ProjectControlError("path must address a regular non-symlink file")
        if not _inside(root, target.resolve()):
            raise ProjectControlError("path escapes the registered repository")
    elif not allow_missing:
        raise ProjectControlError(f"file does not exist: {rel.as_posix()}")
    elif not target.parent.is_dir():
        raise ProjectControlError("parent directory must already exist")
    return rel, target


def _read_utf8(target: Path) -> tuple[bytes, str]:
    raw = target.read_bytes()
    if len(raw) > MAX_TEXT_FILE_BYTES:
        raise ProjectControlError(f"file exceeds {MAX_TEXT_FILE_BYTES} byte control-plane limit")
    try:
        return raw, raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProjectControlError("file is not UTF-8 text") from exc


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _run(root: Path, argv: Sequence[str], *, timeout: int = 300, text: bool = True):
    try:
        return subprocess.run(
            list(argv), cwd=root, capture_output=True, text=text,
            timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProjectControlError(f"allowed command timed out after {timeout}s") from exc
    except OSError as exc:
        raise ProjectControlError(f"could not start allowed command: {exc}") from exc


def _require_success(proc: subprocess.CompletedProcess[Any], *, action: str) -> None:
    if proc.returncode == 0:
        return
    stderr = proc.stderr.decode("utf-8", "replace") if isinstance(proc.stderr, bytes) else (proc.stderr or "")
    stdout = proc.stdout.decode("utf-8", "replace") if isinstance(proc.stdout, bytes) else (proc.stdout or "")
    detail = (stderr or stdout).strip()[-4000:]
    raise ProjectControlError(f"{action} failed with exit {proc.returncode}: {detail}")


def _bounded_output(value: str, limit: int = MAX_PROCESS_OUTPUT_BYTES) -> tuple[str, bool]:
    encoded = value.encode("utf-8", "replace")
    if len(encoded) <= limit:
        return value, False
    tail = encoded[-limit:].decode("utf-8", "replace")
    return tail, True


def repo_status(registry: Mapping[str, Path]) -> dict[str, Any]:
    return {
        "action": "project_repos",
        "repos": [
            {"repo_id": name, "available": Path(path).is_dir(), "name": Path(path).name}
            for name, path in sorted(registry.items())
        ],
    }


def repo_read(
    registry: Mapping[str, Path], *, repo_id: str, path: str,
    start_line: int = 1, max_lines: int = 200,
) -> dict[str, Any]:
    repo_id, root = _registered_repo(registry, repo_id)
    rel, target = _repo_file(root, path)
    if start_line < 1:
        raise ProjectControlError("start_line must be >= 1")
    if max_lines < 1 or max_lines > MAX_READ_LINES:
        raise ProjectControlError(f"max_lines must be between 1 and {MAX_READ_LINES}")
    raw, text = _read_utf8(target)
    lines = text.splitlines(keepends=True)
    start = min(start_line - 1, len(lines))
    end = min(start + max_lines, len(lines))
    return {
        "action": "project_repo_read", "repo_id": repo_id, "path": rel.as_posix(),
        "sha256": _sha256(raw), "start_line": start + 1 if lines else 1,
        "end_line": end, "total_lines": len(lines), "truncated": end < len(lines),
        "text": "".join(lines[start:end]),
    }


def repo_search(
    registry: Mapping[str, Path], *, repo_id: str, query: str, path: str = ".",
    case_sensitive: bool = True, max_results: int = 50,
) -> dict[str, Any]:
    repo_id, root = _registered_repo(registry, repo_id)
    needle = str(query or "")
    if not needle or len(needle) > 500:
        raise ProjectControlError("query must be 1-500 characters")
    if max_results < 1 or max_results > MAX_SEARCH_RESULTS:
        raise ProjectControlError(f"max_results must be between 1 and {MAX_SEARCH_RESULTS}")
    scope = _relative_path(path, allow_dot=True)
    if scope != Path("."):
        scope_target = (root / scope)
        if not scope_target.exists() or scope_target.is_symlink():
            raise ProjectControlError("search path must exist and may not be a symlink")
        if not _inside(root, scope_target.resolve()):
            raise ProjectControlError("search path escapes the registered repository")
    listed = _run(root, ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], text=False)
    _require_success(listed, action="git ls-files")
    names = [item.decode("utf-8", "strict") for item in listed.stdout.split(b"\x00") if item]
    prefix = "" if scope == Path(".") else scope.as_posix().rstrip("/")
    matches: list[dict[str, Any]] = []
    scanned = 0
    comparable_needle = needle if case_sensitive else needle.casefold()
    for name in names:
        if prefix and not (name == prefix or name.startswith(prefix + "/")):
            continue
        try:
            rel, target = _repo_file(root, name)
            raw = target.read_bytes()
        except (OSError, ProjectControlError):
            continue
        if len(raw) > MAX_TEXT_FILE_BYTES or b"\x00" in raw:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        scanned += 1
        for number, line in enumerate(text.splitlines(), start=1):
            comparable = line if case_sensitive else line.casefold()
            if comparable_needle in comparable:
                matches.append({"path": rel.as_posix(), "line": number, "text": line[:2000]})
                if len(matches) >= max_results:
                    return {
                        "action": "project_repo_search", "repo_id": repo_id, "query": needle,
                        "path": scope.as_posix(), "matches": matches, "scanned_files": scanned,
                        "truncated": True,
                    }
    return {
        "action": "project_repo_search", "repo_id": repo_id, "query": needle,
        "path": scope.as_posix(), "matches": matches, "scanned_files": scanned,
        "truncated": False,
    }


def repo_patch(
    registry: Mapping[str, Path], *, repo_id: str, path: str, expected_sha256: str,
    replacements: Sequence[Mapping[str, Any]], create: bool = False,
) -> dict[str, Any]:
    repo_id, root = _registered_repo(registry, repo_id)
    rel, target = _repo_file(root, path, allow_missing=create)
    if not replacements or len(replacements) > MAX_PATCH_OPERATIONS:
        raise ProjectControlError(f"replacements must contain 1-{MAX_PATCH_OPERATIONS} operations")
    if create:
        if target.exists() or target.is_symlink():
            raise ProjectControlError("create=true requires an absent target")
        if expected_sha256 != "absent":
            raise ProjectControlError("create=true requires expected_sha256='absent'")
        if len(replacements) != 1 or str(replacements[0].get("old_text", "")) != "":
            raise ProjectControlError("create=true requires one replacement with empty old_text")
        original = ""
        original_raw = b""
    else:
        if not SHA256_RE.fullmatch(str(expected_sha256 or "").casefold()):
            raise ProjectControlError("expected_sha256 must be a lowercase SHA-256 digest")
        original_raw, original = _read_utf8(target)
        actual = _sha256(original_raw)
        if actual != str(expected_sha256).casefold():
            raise ProjectControlError(f"stale file hash: expected {expected_sha256}, actual {actual}")

    updated = original
    for index, item in enumerate(replacements, start=1):
        if not isinstance(item, Mapping) or set(item) != {"old_text", "new_text"}:
            raise ProjectControlError("each replacement must contain exactly old_text and new_text")
        old = str(item["old_text"])
        new = str(item["new_text"])
        if create:
            updated = new
            break
        if not old:
            raise ProjectControlError("old_text must not be empty for an existing file")
        count = updated.count(old)
        if count != 1:
            raise ProjectControlError(f"replacement {index} must match exactly once; found {count}")
        updated = updated.replace(old, new, 1)
    encoded = updated.encode("utf-8")
    if len(encoded) > MAX_TEXT_FILE_BYTES:
        raise ProjectControlError(f"patched file exceeds {MAX_TEXT_FILE_BYTES} byte control-plane limit")
    if not create and encoded == original_raw:
        raise ProjectControlError("patch produced no change")

    mode = 0o644 if create else (target.stat().st_mode & 0o777)
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.project-control-", dir=target.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, mode)
        os.replace(temp, target)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
    return {
        "action": "project_repo_patch", "repo_id": repo_id, "path": rel.as_posix(),
        "created": create, "old_sha256": "absent" if create else _sha256(original_raw),
        "new_sha256": _sha256(encoded), "bytes": len(encoded),
    }


def _pytest_target(root: Path, value: str) -> str:
    text = str(value or "").strip()
    if not text or text.startswith("-"):
        raise ProjectControlError("pytest targets must be repository-relative test paths")
    file_part = text.split("::", 1)[0]
    rel, target = _repo_file(root, file_part)
    if not (rel.parts and rel.parts[0] == "tests"):
        raise ProjectControlError("pytest targets must stay under tests/")
    if not target.exists():
        raise ProjectControlError(f"pytest target does not exist: {file_part}")
    return text


def repo_test(
    registry: Mapping[str, Path], *, repo_id: str, profile: str,
    targets: Sequence[str] = (), timeout: int = 300,
) -> dict[str, Any]:
    repo_id, root = _registered_repo(registry, repo_id)
    if timeout < 1 or timeout > 900:
        raise ProjectControlError("timeout must be between 1 and 900 seconds")
    profile = str(profile or "").strip().casefold()
    if profile == "fast":
        if not (root / "scripts/run_fast_gate.py").is_file():
            raise ProjectControlError("fast profile is unavailable in this repository")
        if targets:
            raise ProjectControlError("fast profile does not accept targets")
        argv = ["python3", "scripts/run_fast_gate.py"]
    elif profile == "pytest":
        checked = [_pytest_target(root, item) for item in targets] if targets else ["tests"]
        argv = ["python3", "-m", "pytest", "-q", *checked]
    elif profile in {"npm-test", "npm-lint", "npm-typecheck"}:
        if targets:
            raise ProjectControlError(f"{profile} does not accept targets")
        if not (root / "package.json").is_file():
            raise ProjectControlError(f"{profile} requires package.json")
        script = {"npm-test": "test", "npm-lint": "lint", "npm-typecheck": "typecheck"}[profile]
        argv = ["npm", "run", script]
    else:
        raise ProjectControlError("profile must be fast, pytest, npm-test, npm-lint, or npm-typecheck")
    proc = _run(root, argv, timeout=timeout)
    stdout, stdout_truncated = _bounded_output(proc.stdout or "")
    stderr, stderr_truncated = _bounded_output(proc.stderr or "")
    return {
        "action": "project_repo_test", "repo_id": repo_id, "profile": profile,
        "exit_code": proc.returncode, "ok": proc.returncode == 0,
        "stdout": stdout, "stderr": stderr,
        "output_truncated": stdout_truncated or stderr_truncated,
    }


def repo_git_status(registry: Mapping[str, Path], *, repo_id: str) -> dict[str, Any]:
    repo_id, root = _registered_repo(registry, repo_id)
    proc = _run(root, ["git", "status", "--short", "--branch"])
    _require_success(proc, action="git status")
    head = _run(root, ["git", "rev-parse", "--short", "HEAD"])
    _require_success(head, action="git rev-parse")
    return {
        "action": "project_repo_git_status", "repo_id": repo_id,
        "head": (head.stdout or "").strip(), "status": proc.stdout,
    }


def repo_diff(
    registry: Mapping[str, Path], *, repo_id: str, path: str | None = None,
    staged: bool = False,
) -> dict[str, Any]:
    repo_id, root = _registered_repo(registry, repo_id)
    argv = ["git", "diff", "--no-ext-diff", "--no-color"]
    if staged:
        argv.append("--cached")
    argv.append("--")
    normalized_path = None
    if path:
        rel = _relative_path(path)
        normalized_path = rel.as_posix()
        argv.append(normalized_path)
    proc = _run(root, argv)
    _require_success(proc, action="git diff")
    diff, truncated = _bounded_output(proc.stdout or "", MAX_DIFF_BYTES)
    return {
        "action": "project_repo_diff", "repo_id": repo_id, "path": normalized_path,
        "staged": staged, "truncated": truncated, "diff": diff,
    }


def _changed_paths(root: Path) -> set[str]:
    changed = _run(root, ["git", "diff", "--name-only", "--"])
    _require_success(changed, action="git diff --name-only")
    untracked = _run(root, ["git", "ls-files", "--others", "--exclude-standard"])
    _require_success(untracked, action="git ls-files")
    return {line.strip() for line in ((changed.stdout or "") + (untracked.stdout or "")).splitlines() if line.strip()}


def repo_commit(
    registry: Mapping[str, Path], *, repo_id: str, message: str, paths: Sequence[str],
) -> dict[str, Any]:
    repo_id, root = _registered_repo(registry, repo_id)
    message = str(message or "").strip()
    if not message or len(message) > 200 or "\n" in message or "\r" in message:
        raise ProjectControlError("commit message must be one non-empty line up to 200 characters")
    if not paths or len(paths) > 100:
        raise ProjectControlError("paths must contain 1-100 changed files")
    staged = _run(root, ["git", "diff", "--cached", "--quiet", "--"])
    if staged.returncode not in {0, 1}:
        _require_success(staged, action="git diff --cached --quiet")
    if staged.returncode == 1:
        raise ProjectControlError("refusing to commit while pre-existing staged changes are present")
    author = _run(root, ["git", "var", "GIT_AUTHOR_IDENT"])
    _require_success(author, action="git author identity preflight")
    committer = _run(root, ["git", "var", "GIT_COMMITTER_IDENT"])
    _require_success(committer, action="git committer identity preflight")
    changed = _changed_paths(root)
    normalized: list[str] = []
    seen: set[str] = set()
    for item in paths:
        rel = _relative_path(item)
        name = rel.as_posix()
        if name in seen:
            continue
        if name not in changed:
            raise ProjectControlError(f"path is not an unstaged/untracked changed file: {name}")
        _reject_symlink_components(root, rel)
        parent = (root / rel).parent.resolve()
        if not _inside(root, parent):
            raise ProjectControlError("commit path escapes the registered repository")
        normalized.append(name)
        seen.add(name)
    add = _run(root, ["git", "add", "--all", "--", *normalized])
    _require_success(add, action="git add")
    commit = _run(root, ["git", "commit", "--no-gpg-sign", "-m", message])
    _require_success(commit, action="git commit")
    head = _run(root, ["git", "rev-parse", "--short", "HEAD"])
    _require_success(head, action="git rev-parse")
    status = _run(root, ["git", "status", "--short", "--branch"])
    _require_success(status, action="git status")
    return {
        "action": "project_repo_commit", "repo_id": repo_id,
        "commit": (head.stdout or "").strip(), "message": message,
        "paths": normalized, "status": status.stdout,
    }


def create_goal(db: Path, *, goal_id: str, description: str) -> dict[str, Any]:
    return BUS.create_goal(
        db, goal_id=goal_id, owner="gemini", created_by="user", description=description,
    )


def add_task(
    db: Path, *, registry: Mapping[str, Path], goal_id: str, task_id: str,
    assignee: str, repo_id: str, description: str,
) -> dict[str, Any]:
    repo_id = _repo_id(repo_id)
    if repo_id not in registry:
        raise ProjectControlError(f"repository id {repo_id!r} is not registered")
    return BUS.add_goal_task(
        db, goal_id=goal_id, task_id=task_id, assignee=assignee, repo_id=repo_id,
        created_by="user", description=description,
    )


def tick(
    db: Path, *, registry: Mapping[str, Path], provider_lock: Path | None = None,
) -> dict[str, Any]:
    task = WORKER.run_one(
        db_path=db, repo=registry.get("automation", REPO_ROOT), repo_routes=registry,
        provider_lock=provider_lock,
    )
    manager = GOAL.advance_all(
        db_path=db, repo_ids=tuple(registry), provider_lock=provider_lock,
    )
    return {"action": "project_tick", "goal_task": task, "goal_manager": manager}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--repo-route", action="append", default=[], metavar="ID=PATH")
    parser.add_argument("--provider-lock", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("repos")
    commands.add_parser("backlog")
    status = commands.add_parser("goal-status"); status.add_argument("--goal-id", required=True)
    goal = commands.add_parser("create-goal")
    goal.add_argument("--goal-id", required=True); goal.add_argument("--description", required=True)
    task = commands.add_parser("add-task")
    task.add_argument("--goal-id", required=True); task.add_argument("--task-id", required=True)
    task.add_argument("--assignee", required=True); task.add_argument("--repo-id", required=True)
    task.add_argument("--description", required=True)
    commands.add_parser("tick")

    read = commands.add_parser("repo-read")
    read.add_argument("--repo-id", required=True); read.add_argument("--path", required=True)
    read.add_argument("--start-line", type=int, default=1); read.add_argument("--max-lines", type=int, default=200)
    search = commands.add_parser("repo-search")
    search.add_argument("--repo-id", required=True); search.add_argument("--query", required=True)
    search.add_argument("--path", default="."); search.add_argument("--ignore-case", action="store_true")
    search.add_argument("--max-results", type=int, default=50)
    patch = commands.add_parser("repo-patch")
    patch.add_argument("--repo-id", required=True); patch.add_argument("--path", required=True)
    patch.add_argument("--expected-sha256", required=True); patch.add_argument("--replacements-json", required=True)
    patch.add_argument("--create", action="store_true")
    test = commands.add_parser("repo-test")
    test.add_argument("--repo-id", required=True); test.add_argument("--profile", required=True)
    test.add_argument("--target", action="append", default=[]); test.add_argument("--timeout", type=int, default=300)
    git_status = commands.add_parser("repo-git-status"); git_status.add_argument("--repo-id", required=True)
    diff = commands.add_parser("repo-diff")
    diff.add_argument("--repo-id", required=True); diff.add_argument("--path"); diff.add_argument("--staged", action="store_true")
    commit = commands.add_parser("repo-commit")
    commit.add_argument("--repo-id", required=True); commit.add_argument("--message", required=True)
    commit.add_argument("--path", action="append", required=True)
    return parser


def main(argv: Sequence[str] | None = None, *, output=print) -> int:
    args = build_parser().parse_args(argv)
    try:
        registry = load_repo_registry(args.repo_route)
        if args.command == "repos": payload = repo_status(registry)
        elif args.command == "backlog": payload = BUS.backlog_summary(args.db)
        elif args.command == "goal-status": payload = BUS.goal_status(args.db, goal_id=args.goal_id)
        elif args.command == "create-goal": payload = create_goal(args.db, goal_id=args.goal_id, description=args.description)
        elif args.command == "add-task": payload = add_task(
            args.db, registry=registry, goal_id=args.goal_id, task_id=args.task_id,
            assignee=args.assignee, repo_id=args.repo_id, description=args.description,
        )
        elif args.command == "tick": payload = tick(args.db, registry=registry, provider_lock=args.provider_lock)
        elif args.command == "repo-read": payload = repo_read(
            registry, repo_id=args.repo_id, path=args.path, start_line=args.start_line, max_lines=args.max_lines,
        )
        elif args.command == "repo-search": payload = repo_search(
            registry, repo_id=args.repo_id, query=args.query, path=args.path,
            case_sensitive=not args.ignore_case, max_results=args.max_results,
        )
        elif args.command == "repo-patch":
            try:
                replacements = json.loads(args.replacements_json)
            except json.JSONDecodeError as exc:
                raise ProjectControlError("replacements_json must be valid JSON") from exc
            if not isinstance(replacements, list):
                raise ProjectControlError("replacements_json must be a JSON array")
            payload = repo_patch(
                registry, repo_id=args.repo_id, path=args.path, expected_sha256=args.expected_sha256,
                replacements=replacements, create=args.create,
            )
        elif args.command == "repo-test": payload = repo_test(
            registry, repo_id=args.repo_id, profile=args.profile, targets=args.target, timeout=args.timeout,
        )
        elif args.command == "repo-git-status": payload = repo_git_status(registry, repo_id=args.repo_id)
        elif args.command == "repo-diff": payload = repo_diff(
            registry, repo_id=args.repo_id, path=args.path, staged=args.staged,
        )
        else: payload = repo_commit(
            registry, repo_id=args.repo_id, message=args.message, paths=args.path,
        )
    except (ProjectControlError, BUS.BusError, GOAL.GoalDriverError) as exc:
        output(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    output(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
