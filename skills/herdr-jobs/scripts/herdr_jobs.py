#!/usr/bin/env python3
"""Launch and manage grouped Codex agents in background Herdr tabs."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from typing import Any, Iterable


AGENT_RE = re.compile(r"^hj-([0-9a-f]{4})-([a-z0-9][a-z0-9-]*)$")
TAB_RE = re.compile(r"^([0-9a-f]{4}) · (.+)$")
WORKSPACE_RE = re.compile(r"^hj-([0-9a-f]{4}) · (.+)$")
BOOTSTRAP_TAB_RE = re.compile(r"^hj-bootstrap-([0-9a-f]{4})$")
ACTIVE_STATES = {"working", "blocked", "unknown"}
READY_STATES = {"done", "idle"}


class JobError(RuntimeError):
    """A structured, user-correctable workflow error."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


class HerdrCommandError(RuntimeError):
    """A failed Herdr subprocess."""

    def __init__(
        self, args: list[str], returncode: int, stdout: str, stderr: str
    ) -> None:
        message = stderr.strip() or stdout.strip() or f"exit status {returncode}"
        super().__init__(f"herdr {' '.join(args)} failed: {message}")
        self.args_run = args
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@dataclass
class Workspace:
    workspace_id: str
    label: str | None = None
    cwd: str | None = None
    owner_group: str | None = None


@dataclass
class Job:
    name: str
    group: str
    label: str
    state: str = "unknown"
    workspace_id: str | None = None
    tab_id: str | None = None
    pane_id: str | None = None
    terminal_id: str | None = None
    tab_label: str | None = None


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def walk_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


def first_text(mapping: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def run_herdr(args: list[str], *, check: bool = True) -> Any:
    completed = subprocess.run(
        ["herdr", *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if check and completed.returncode != 0:
        raise HerdrCommandError(
            args, completed.returncode, completed.stdout, completed.stderr
        )
    output = completed.stdout.strip()
    if not output:
        return {
            "returncode": completed.returncode,
            "stderr": completed.stderr.strip(),
        }
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return {
            "raw": output,
            "returncode": completed.returncode,
            "stderr": completed.stderr.strip(),
        }


def ensure_runtime() -> None:
    if os.environ.get("HERDR_ENV") != "1":
        raise JobError(
            "not_in_herdr",
            "This command must run from a Herdr-managed pane with HERDR_ENV=1.",
        )
    missing = [name for name in ("herdr", "codex") if shutil.which(name) is None]
    if missing:
        raise JobError(
            "missing_executable",
            f"Required executable(s) not found: {', '.join(missing)}.",
            missing=missing,
        )


def workspace_from_mapping(mapping: dict[str, Any]) -> Workspace | None:
    workspace_id = first_text(mapping, "workspace_id")
    if not workspace_id:
        possible_id = first_text(mapping, "id")
        if possible_id and re.fullmatch(r"w[0-9A-Za-z]+", possible_id):
            workspace_id = possible_id
    if not workspace_id:
        return None
    label = first_text(mapping, "label", "name", "title")
    owner_match = WORKSPACE_RE.fullmatch(label) if label else None
    return Workspace(
        workspace_id=workspace_id,
        label=label,
        cwd=first_text(mapping, "cwd", "working_directory", "path"),
        owner_group=owner_match.group(1) if owner_match else None,
    )


def merge_workspace(existing: Workspace, newer: Workspace) -> Workspace:
    return Workspace(
        workspace_id=existing.workspace_id,
        label=newer.label or existing.label,
        cwd=newer.cwd or existing.cwd,
        owner_group=newer.owner_group or existing.owner_group,
    )


def list_workspaces() -> list[Workspace]:
    response = run_herdr(["workspace", "list"])
    by_id: dict[str, Workspace] = {}
    for mapping in walk_dicts(response):
        workspace = workspace_from_mapping(mapping)
        if not workspace:
            continue
        current = by_id.get(workspace.workspace_id)
        by_id[workspace.workspace_id] = (
            merge_workspace(current, workspace) if current else workspace
        )
    return list(by_id.values())


def get_workspace(workspace_id: str) -> Workspace:
    response = run_herdr(["workspace", "get", workspace_id])
    found = [
        workspace_from_mapping(mapping) for mapping in walk_dicts(response)
    ]
    workspaces = [
        workspace
        for workspace in found
        if workspace and workspace.workspace_id == workspace_id
    ]
    if not workspaces:
        raise JobError(
            "invalid_workspace_response",
            f"Herdr did not return metadata for workspace {workspace_id}.",
        )
    result = workspaces[0]
    for workspace in workspaces[1:]:
        result = merge_workspace(result, workspace)
    return result


def resolve_workspace(selector: str | None) -> Workspace:
    selector = selector or os.environ.get("HERDR_WORKSPACE_ID")
    if not selector:
        raise JobError(
            "workspace_required",
            "Specify a workspace because HERDR_WORKSPACE_ID is unavailable.",
        )

    workspaces = list_workspaces()
    matches = [
        workspace
        for workspace in workspaces
        if workspace.workspace_id == selector or workspace.label == selector
    ]
    if not matches:
        raise JobError(
            "workspace_not_found",
            f"No Herdr workspace matches {selector!r}.",
            available=[
                {"id": workspace.workspace_id, "label": workspace.label}
                for workspace in workspaces
            ],
        )
    unique_ids = {workspace.workspace_id for workspace in matches}
    if len(unique_ids) > 1:
        raise JobError(
            "ambiguous_workspace",
            f"More than one workspace is labeled {selector!r}.",
            candidates=[
                {"id": workspace.workspace_id, "label": workspace.label}
                for workspace in matches
            ],
        )

    workspace = get_workspace(matches[0].workspace_id)
    if not workspace.cwd:
        workspace.cwd = matches[0].cwd
    if not workspace.cwd:
        raise JobError(
            "workspace_cwd_missing",
            f"Workspace {workspace.workspace_id} has no reported working directory.",
        )
    return workspace


def slugify(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", label.strip().lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)[:32].rstrip("-")
    if not slug:
        raise JobError("invalid_label", f"Task label {label!r} has no usable characters.")
    return slug


def normalize_tasks(raw_tasks: list[list[str]]) -> list[tuple[str, str]]:
    tasks: list[tuple[str, str]] = []
    seen: set[str] = set()
    for label, prompt in raw_tasks:
        slug = slugify(label)
        prompt = prompt.strip()
        if not prompt:
            raise JobError("invalid_prompt", f"Task {label!r} has an empty prompt.")
        if slug in seen:
            raise JobError(
                "duplicate_label",
                f"Task labels must be unique after normalization: {slug!r}.",
            )
        seen.add(slug)
        tasks.append((slug, prompt))
    return tasks


def state_from_mapping(mapping: dict[str, Any]) -> str:
    for key in ("agent_status", "agent_state", "state", "status"):
        value = mapping.get(key)
        if isinstance(value, str) and value in {
            "idle",
            "working",
            "blocked",
            "done",
            "unknown",
        }:
            return value
    return "unknown"


def job_from_mapping(mapping: dict[str, Any]) -> Job | None:
    candidates = [
        first_text(mapping, "name"),
        first_text(mapping, "agent_name"),
        first_text(mapping, "agent"),
        first_text(mapping, "target_name"),
        first_text(mapping, "custom_name"),
        first_text(mapping, "display_name"),
        first_text(mapping, "label"),
    ]
    match = None
    name = None
    for candidate in candidates:
        if candidate and (candidate_match := AGENT_RE.fullmatch(candidate)):
            name = candidate
            match = candidate_match
            break
    if not name or not match:
        return None
    return Job(
        name=name,
        group=match.group(1),
        label=match.group(2),
        state=state_from_mapping(mapping),
        workspace_id=first_text(mapping, "workspace_id"),
        tab_id=first_text(mapping, "tab_id"),
        pane_id=first_text(mapping, "pane_id"),
        terminal_id=first_text(mapping, "terminal_id"),
        tab_label=first_text(mapping, "tab_label"),
    )


def merge_job(existing: Job, newer: Job) -> Job:
    preferred_state = (
        newer.state if newer.state != "unknown" else existing.state
    )
    return Job(
        name=existing.name,
        group=existing.group,
        label=existing.label,
        state=preferred_state,
        workspace_id=newer.workspace_id or existing.workspace_id,
        tab_id=newer.tab_id or existing.tab_id,
        pane_id=newer.pane_id or existing.pane_id,
        terminal_id=newer.terminal_id or existing.terminal_id,
        tab_label=newer.tab_label or existing.tab_label,
    )


def discover_jobs(*, include_tab_fallback: bool = True) -> list[Job]:
    response = run_herdr(["agent", "list"])
    jobs: dict[str, Job] = {}
    for mapping in walk_dicts(response):
        job = job_from_mapping(mapping)
        if not job:
            continue
        jobs[job.name] = merge_job(jobs[job.name], job) if job.name in jobs else job

    if not include_tab_fallback:
        return sorted(jobs.values(), key=lambda job: (job.group, job.label))

    # Preserve discoverability when a generated agent name is renamed but its
    # generated tab label remains. This fallback is intentionally read-only.
    for workspace in list_workspaces():
        tabs_response = run_herdr(["tab", "list", "--workspace", workspace.workspace_id])
        matching_tabs: list[tuple[str, str, str]] = []
        for mapping in walk_dicts(tabs_response):
            tab_id = first_text(mapping, "tab_id")
            if not tab_id:
                possible_id = first_text(mapping, "id")
                if possible_id and re.fullmatch(
                    r"w[0-9A-Za-z]+:t[0-9A-Za-z]+", possible_id
                ):
                    tab_id = possible_id
            tab_label = first_text(mapping, "label", "name", "title")
            if not tab_id or not tab_label:
                continue
            match = TAB_RE.fullmatch(tab_label)
            if match:
                try:
                    label = slugify(match.group(2))
                except JobError:
                    continue
                matching_tabs.append((tab_id, match.group(1), label))
        if not matching_tabs:
            continue

        panes_response = run_herdr(
            ["pane", "list", "--workspace", workspace.workspace_id]
        )
        panes_by_tab: dict[str, list[dict[str, Any]]] = {}
        for mapping in walk_dicts(panes_response):
            tab_id = first_text(mapping, "tab_id")
            pane_id = first_text(mapping, "pane_id")
            if tab_id and pane_id:
                panes_by_tab.setdefault(tab_id, []).append(mapping)

        for tab_id, group, label in matching_tabs:
            name = f"hj-{group}-{label}"
            tab_job = Job(
                name=name,
                group=group,
                label=label,
                workspace_id=workspace.workspace_id,
                tab_id=tab_id,
                tab_label=f"{group} · {label}",
            )
            panes = panes_by_tab.get(tab_id, [])
            if panes:
                pane = panes[0]
                tab_job.pane_id = first_text(pane, "pane_id")
                tab_job.terminal_id = first_text(pane, "terminal_id")
                tab_job.state = state_from_mapping(pane)
            jobs[name] = merge_job(jobs[name], tab_job) if name in jobs else tab_job
    return sorted(jobs.values(), key=lambda job: (job.group, job.label))


def refresh_job(job: Job) -> Job:
    try:
        response = run_herdr(["agent", "get", job.name])
    except HerdrCommandError:
        if not job.pane_id:
            return job
        response = run_herdr(["pane", "get", job.pane_id])
    refreshed = job
    for mapping in walk_dicts(response):
        candidate = job_from_mapping(mapping)
        if candidate and candidate.name == job.name:
            refreshed = merge_job(refreshed, candidate)
            continue
        state = state_from_mapping(mapping)
        if state != "unknown":
            refreshed.state = state
        refreshed.workspace_id = (
            first_text(mapping, "workspace_id") or refreshed.workspace_id
        )
        refreshed.tab_id = first_text(mapping, "tab_id") or refreshed.tab_id
        refreshed.pane_id = first_text(mapping, "pane_id") or refreshed.pane_id
        refreshed.terminal_id = (
            first_text(mapping, "terminal_id") or refreshed.terminal_id
        )
    return refreshed


def normalize_group(group: str) -> str:
    value = group.strip().lower()
    if value.startswith("hj-"):
        value = value[3:]
    if not re.fullmatch(r"[0-9a-f]{4}", value):
        raise JobError("invalid_group", f"Invalid Herdr job group token: {group!r}.")
    return value


def select_group(jobs: list[Job], requested: str | None) -> tuple[str, list[Job]]:
    groups = sorted({job.group for job in jobs})
    if requested:
        group = normalize_group(requested)
        selected = [job for job in jobs if job.group == group]
        if not selected:
            raise JobError(
                "group_not_found",
                f"No live Herdr job group matches {group}.",
                candidates=groups,
            )
        return group, selected
    if not groups:
        raise JobError("no_jobs", "No live $herdr-jobs groups were found.")
    if len(groups) > 1:
        raise JobError(
            "ambiguous_group",
            "More than one live Herdr job group exists.",
            candidates=[
                {
                    "group": group,
                    "jobs": [job.label for job in jobs if job.group == group],
                }
                for group in groups
            ],
        )
    return groups[0], jobs


def choose_group_token(
    existing_jobs: list[Job], existing_workspaces: list[Workspace]
) -> str:
    existing = {job.group for job in existing_jobs}
    existing.update(
        workspace.owner_group
        for workspace in existing_workspaces
        if workspace.owner_group
    )
    for _ in range(256):
        token = secrets.token_hex(2)
        if token not in existing:
            return token
    raise JobError("group_token_exhausted", "Could not allocate a unique group token.")


def normalize_new_workspace_cwd(raw_cwd: str | None) -> str:
    cwd = os.path.abspath(os.path.expanduser(raw_cwd or os.getcwd()))
    if not os.path.isdir(cwd):
        raise JobError(
            "workspace_cwd_not_found",
            f"New workspace directory does not exist: {cwd}",
        )
    return cwd


def normalize_workspace_label(raw_label: str | None, tasks: list[tuple[str, str]]) -> str:
    if raw_label is None:
        return tasks[0][0] if len(tasks) == 1 else f"{len(tasks)}-jobs"
    label = " ".join(raw_label.split())
    if not label:
        raise JobError("invalid_workspace_label", "Workspace label cannot be empty.")
    return label


def create_owned_workspace(
    group: str, cwd: str, display_label: str
) -> tuple[Workspace, dict[str, str | None], list[dict[str, str]]]:
    full_label = f"hj-{group} · {display_label}"
    response = run_herdr(
        [
            "workspace",
            "create",
            "--cwd",
            cwd,
            "--label",
            full_label,
            "--no-focus",
        ]
    )
    ids = extract_ids(response)
    if not ids["workspace_id"]:
        raise JobError(
            "workspace_id_missing",
            "Herdr did not report the newly created workspace ID.",
        )
    workspace = Workspace(
        workspace_id=ids["workspace_id"],
        label=full_label,
        cwd=cwd,
        owner_group=group,
    )
    warnings: list[dict[str, str]] = []
    if ids["tab_id"]:
        try:
            run_herdr(
                ["tab", "rename", ids["tab_id"], f"hj-bootstrap-{group}"]
            )
        except HerdrCommandError as error:
            warnings.append(
                {
                    "operation": "bootstrap_tab_rename",
                    "error": str(error),
                }
            )
    return workspace, ids, warnings


def extract_ids(response: Any) -> dict[str, str | None]:
    found: dict[str, str | None] = {
        "workspace_id": None,
        "tab_id": None,
        "pane_id": None,
        "terminal_id": None,
    }
    for mapping in walk_dicts(response):
        for key in found:
            if not found[key]:
                found[key] = first_text(mapping, key)
    return found


def wait_for_working(name: str, timeout_ms: int) -> dict[str, Any]:
    try:
        run_herdr(
            ["agent", "wait", name, "--status", "working", "--timeout", str(timeout_ms)]
        )
        return {"name": name, "confirmed": True, "state": "working"}
    except HerdrCommandError as error:
        state = "unknown"
        try:
            probe = run_herdr(["agent", "get", name])
            for mapping in walk_dicts(probe):
                candidate = state_from_mapping(mapping)
                if candidate != "unknown":
                    state = candidate
        except HerdrCommandError:
            pass
        return {
            "name": name,
            "confirmed": False,
            "state": state,
            "error": str(error),
        }


def start_command(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    tasks = normalize_tasks(args.task)
    if not args.new_workspace and (args.workspace_label or args.cwd):
        raise JobError(
            "new_workspace_option_required",
            "--workspace-label and --cwd require --new-workspace.",
        )

    existing_workspaces = list_workspaces()
    group = choose_group_token(
        discover_jobs(include_tab_fallback=False), existing_workspaces
    )
    workspace_created = bool(args.new_workspace)
    bootstrap_ids: dict[str, str | None] = {
        "workspace_id": None,
        "tab_id": None,
        "pane_id": None,
        "terminal_id": None,
    }
    warnings: list[dict[str, str]] = []
    if workspace_created:
        cwd = normalize_new_workspace_cwd(args.cwd)
        display_label = normalize_workspace_label(args.workspace_label, tasks)
        workspace, bootstrap_ids, create_warnings = create_owned_workspace(
            group, cwd, display_label
        )
        warnings.extend(create_warnings)
    else:
        workspace = resolve_workspace(args.workspace)

    launched: list[Job] = []
    failures: list[dict[str, str]] = []

    for label, prompt in tasks:
        name = f"hj-{group}-{label}"
        try:
            response = run_herdr(
                [
                    "agent",
                    "start",
                    name,
                    "--workspace",
                    workspace.workspace_id,
                    "--cwd",
                    workspace.cwd,
                    "--no-focus",
                    "--",
                    "codex",
                    prompt,
                ]
            )
            ids = extract_ids(response)
            job = Job(
                name=name,
                group=group,
                label=label,
                state="unknown",
                workspace_id=ids["workspace_id"] or workspace.workspace_id,
                tab_id=ids["tab_id"],
                pane_id=ids["pane_id"],
                terminal_id=ids["terminal_id"],
                tab_label=f"{group} · {label}",
            )
            if not job.tab_id or not job.pane_id:
                try:
                    job = refresh_job(job)
                except HerdrCommandError:
                    pass
            launched.append(job)
            if job.tab_id:
                try:
                    run_herdr(["tab", "rename", job.tab_id, job.tab_label])
                except HerdrCommandError as error:
                    warnings.append(
                        {"label": label, "operation": "tab_rename", "error": str(error)}
                    )
        except (HerdrCommandError, JobError) as error:
            failures.append({"label": label, "error": str(error)})

    if workspace_created:
        launched_tab_ids = {job.tab_id for job in launched if job.tab_id}
        launched_pane_ids = {job.pane_id for job in launched if job.pane_id}
        if launched and bootstrap_ids["tab_id"] in launched_tab_ids:
            if (
                bootstrap_ids["pane_id"]
                and bootstrap_ids["pane_id"] not in launched_pane_ids
            ):
                try:
                    run_herdr(["pane", "close", bootstrap_ids["pane_id"]])
                except HerdrCommandError as error:
                    warnings.append(
                        {
                            "operation": "bootstrap_pane_close",
                            "error": str(error),
                        }
                    )
            else:
                warnings.append(
                    {
                        "operation": "bootstrap_pane_close",
                        "error": "Herdr did not report a distinct bootstrap pane ID.",
                    }
                )
        elif launched and bootstrap_ids["tab_id"]:
            try:
                run_herdr(["tab", "close", bootstrap_ids["tab_id"]])
            except HerdrCommandError as error:
                warnings.append(
                    {
                        "operation": "bootstrap_tab_close",
                        "error": str(error),
                    }
                )
        elif launched:
            warnings.append(
                {
                    "operation": "bootstrap_tab_close",
                    "error": "Herdr did not report the bootstrap tab ID.",
                }
            )
        elif not launched:
            try:
                run_herdr(["workspace", "close", workspace.workspace_id])
            except HerdrCommandError as error:
                warnings.append(
                    {
                        "operation": "failed_launch_workspace_rollback",
                        "error": str(error),
                    }
                )

    confirmations: list[dict[str, Any]] = []
    if args.confirm and launched:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(8, len(launched))
        ) as executor:
            confirmations = list(
                executor.map(
                    lambda job: wait_for_working(job.name, args.timeout),
                    launched,
                )
            )

    payload = {
        "ok": not failures,
        "operation": "start",
        "group": group,
        "workspace": {
            **asdict(workspace),
            "created_by_skill": workspace_created,
        },
        "launched": [asdict(job) for job in launched],
        "failed": failures,
        "warnings": warnings,
        "confirmed": bool(args.confirm),
        "confirmations": confirmations,
    }
    return payload, 0 if not failures else 2


def selected_live_jobs(group: str | None) -> tuple[str, list[Job]]:
    selected_group, jobs = select_group(discover_jobs(), group)
    return selected_group, [refresh_job(job) for job in jobs]


def status_command(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    group, jobs = selected_live_jobs(args.group)
    return {
        "ok": True,
        "operation": "status",
        "group": group,
        "jobs": [asdict(job) for job in jobs],
    }, 0


def resolve_job(selector: str) -> Job:
    jobs = discover_jobs()
    exact = [job for job in jobs if job.name == selector]
    if exact:
        return refresh_job(exact[0])
    slug = slugify(selector)
    matches = [job for job in jobs if job.label == slug]
    if not matches:
        raise JobError("job_not_found", f"No live Herdr job matches {selector!r}.")
    if len(matches) > 1:
        raise JobError(
            "ambiguous_job",
            f"More than one live job is labeled {slug!r}.",
            candidates=[job.name for job in matches],
        )
    return refresh_job(matches[0])


def respond_command(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    job = resolve_job(args.job)
    if not job.pane_id:
        raise JobError(
            "pane_id_missing",
            f"Herdr did not report a pane ID for {job.name}.",
        )
    response = run_herdr(["pane", "run", job.pane_id, args.prompt])
    return {
        "ok": True,
        "operation": "respond",
        "job": asdict(job),
        "response_ids": extract_ids(response),
    }, 0


def extract_read_text(response: Any) -> str:
    if isinstance(response, dict) and isinstance(response.get("raw"), str):
        return response["raw"]
    preferred = ("text", "content", "output", "transcript", "data")
    for mapping in walk_dicts(response):
        for key in preferred:
            value = mapping.get(key)
            if isinstance(value, str):
                return value
    return json.dumps(response, ensure_ascii=False)


def read_job(job: Job, lines: int) -> str:
    if not job.pane_id:
        raise JobError("pane_id_missing", f"No pane ID is available for {job.name}.")
    response = run_herdr(
        [
            "pane",
            "read",
            job.pane_id,
            "--source",
            "recent-unwrapped",
            "--lines",
            str(lines),
        ]
    )
    return extract_read_text(response)


def collect_command(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    group, jobs = selected_live_jobs(args.group)
    collected: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for job in jobs:
        if job.state in READY_STATES:
            try:
                collected.append({**asdict(job), "transcript": read_job(job, args.lines)})
            except (HerdrCommandError, JobError) as error:
                collected.append({**asdict(job), "read_error": str(error)})
        else:
            pending.append(asdict(job))
    return {
        "ok": True,
        "operation": "collect",
        "group": group,
        "collected": collected,
        "pending": pending,
        "waited": False,
    }, 0


def list_workspace_tabs(workspace_id: str) -> dict[str, dict[str, Any]]:
    response = run_herdr(["tab", "list", "--workspace", workspace_id])
    tabs: dict[str, dict[str, Any]] = {}
    for mapping in walk_dicts(response):
        tab_id = first_text(mapping, "tab_id")
        if not tab_id:
            possible_id = first_text(mapping, "id")
            if possible_id and re.fullmatch(
                r"w[0-9A-Za-z]+:t[0-9A-Za-z]+", possible_id
            ):
                tab_id = possible_id
        if not tab_id:
            continue
        tabs[tab_id] = {
            "tab_id": tab_id,
            "label": first_text(mapping, "label", "name", "title"),
        }
    return tabs


def list_workspace_panes(workspace_id: str) -> dict[str, list[str]]:
    response = run_herdr(["pane", "list", "--workspace", workspace_id])
    panes_by_tab: dict[str, list[str]] = {}
    seen: set[tuple[str, str]] = set()
    for mapping in walk_dicts(response):
        tab_id = first_text(mapping, "tab_id")
        pane_id = first_text(mapping, "pane_id")
        if not tab_id or not pane_id or (tab_id, pane_id) in seen:
            continue
        seen.add((tab_id, pane_id))
        panes_by_tab.setdefault(tab_id, []).append(pane_id)
    return panes_by_tab


def inspect_workspace_ownership(
    workspace: Workspace, jobs: list[Job], group: str
) -> dict[str, Any]:
    tabs = list_workspace_tabs(workspace.workspace_id)
    panes_by_tab = list_workspace_panes(workspace.workspace_id)
    jobs_by_tab = {
        job.tab_id: job
        for job in jobs
        if job.workspace_id == workspace.workspace_id and job.tab_id
    }
    safe_job_tabs: set[str] = set()
    contaminated_job_tabs: dict[str, str] = {}
    owned_bootstrap_tabs: set[str] = set()
    unrelated_tabs: list[dict[str, Any]] = []

    for tab_id, tab in tabs.items():
        pane_ids = panes_by_tab.get(tab_id, [])
        job = jobs_by_tab.get(tab_id)
        if job:
            if job.pane_id and pane_ids == [job.pane_id]:
                safe_job_tabs.add(tab_id)
            else:
                contaminated_job_tabs[tab_id] = "job_tab_has_unowned_or_missing_panes"
            continue

        bootstrap_match = (
            BOOTSTRAP_TAB_RE.fullmatch(tab["label"]) if tab["label"] else None
        )
        if bootstrap_match and bootstrap_match.group(1) == group and len(pane_ids) == 1:
            owned_bootstrap_tabs.add(tab_id)
            continue
        unrelated_tabs.append(
            {"tab_id": tab_id, "label": tab["label"], "pane_ids": pane_ids}
        )

    known_tab_ids = set(tabs)
    missing_job_tabs = sorted(
        tab_id for tab_id in jobs_by_tab if tab_id not in known_tab_ids
    )
    exclusive = bool(tabs) and workspace.owner_group == group and not (
        contaminated_job_tabs or unrelated_tabs or missing_job_tabs
    )
    reasons: list[str] = []
    if workspace.owner_group != group:
        reasons.append("not_skill_owned")
    if not tabs:
        reasons.append("workspace_has_no_reported_tabs")
    if contaminated_job_tabs:
        reasons.append("job_tab_contains_unowned_or_missing_panes")
    if unrelated_tabs:
        reasons.append("workspace_contains_unrelated_tabs")
    if missing_job_tabs:
        reasons.append("job_tab_missing_from_workspace_inventory")

    return {
        "exclusive": exclusive,
        "safe_job_tabs": safe_job_tabs,
        "contaminated_job_tabs": contaminated_job_tabs,
        "owned_bootstrap_tabs": owned_bootstrap_tabs,
        "unrelated_tabs": unrelated_tabs,
        "missing_job_tabs": missing_job_tabs,
        "reasons": reasons,
    }


def cleanup_command(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    group, jobs = selected_live_jobs(args.group)
    closed: list[dict[str, Any]] = []
    preserved: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    closed_workspaces: list[dict[str, Any]] = []
    preserved_workspaces: list[dict[str, Any]] = []
    closed_tabs: set[str] = set()
    handled_jobs: set[str] = set()

    workspaces = {workspace.workspace_id: workspace for workspace in list_workspaces()}
    workspace_ids = sorted(
        {
            job.workspace_id
            for job in jobs
            if job.workspace_id
        }
        | {
            workspace.workspace_id
            for workspace in workspaces.values()
            if workspace.owner_group == group
        }
    )

    for workspace_id in workspace_ids:
        workspace = workspaces.get(workspace_id)
        workspace_jobs = [job for job in jobs if job.workspace_id == workspace_id]
        if not workspace:
            for job in workspace_jobs:
                failed.append({**asdict(job), "error": "workspace_not_found"})
                handled_jobs.add(job.name)
            continue

        try:
            inspection = inspect_workspace_ownership(workspace, workspace_jobs, group)
        except HerdrCommandError as error:
            preserved_workspaces.append(
                {**asdict(workspace), "reasons": ["workspace_inspection_failed"]}
            )
            for job in workspace_jobs:
                failed.append({**asdict(job), "error": str(error)})
                handled_jobs.add(job.name)
            continue

        active_jobs = [job for job in workspace_jobs if job.state in ACTIVE_STATES]
        if inspection["exclusive"] and (args.force or not active_jobs):
            try:
                run_herdr(["workspace", "close", workspace_id])
                closed_workspaces.append(asdict(workspace))
                for job in workspace_jobs:
                    closed.append(asdict(job))
                    handled_jobs.add(job.name)
                continue
            except HerdrCommandError as error:
                preserved_workspaces.append(
                    {**asdict(workspace), "reasons": ["workspace_close_failed"]}
                )
                for job in workspace_jobs:
                    failed.append({**asdict(job), "error": str(error)})
                    handled_jobs.add(job.name)
                continue

        workspace_reasons = list(inspection["reasons"])
        if active_jobs and not args.force:
            workspace_reasons.append("workspace_contains_active_jobs")
        preserved_workspaces.append(
            {**asdict(workspace), "reasons": sorted(set(workspace_reasons))}
        )

        safe_job_tabs = inspection["safe_job_tabs"]
        for job in workspace_jobs:
            handled_jobs.add(job.name)
            if not args.force and job.state in ACTIVE_STATES:
                preserved.append({**asdict(job), "reason": "active_job"})
                continue
            if not job.tab_id:
                failed.append({**asdict(job), "error": "tab_id_missing"})
                continue
            if job.tab_id not in safe_job_tabs:
                preserved.append(
                    {**asdict(job), "reason": "job_tab_not_exclusively_owned"}
                )
                continue
            if job.tab_id in closed_tabs:
                continue
            try:
                run_herdr(["tab", "close", job.tab_id])
                closed_tabs.add(job.tab_id)
                closed.append(asdict(job))
            except HerdrCommandError as error:
                failed.append({**asdict(job), "error": str(error)})

    for job in jobs:
        if job.name in handled_jobs:
            continue
        if not job.workspace_id:
            failed.append({**asdict(job), "error": "workspace_id_missing"})
        else:
            failed.append({**asdict(job), "error": "workspace_not_inspected"})

    return {
        "ok": not failed,
        "operation": "cleanup",
        "group": group,
        "force": bool(args.force),
        "closed": closed,
        "preserved": preserved,
        "closed_workspaces": closed_workspaces,
        "preserved_workspaces": preserved_workspaces,
        "failed": failed,
    }, 0 if not failed else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Launch and manage background Codex agents in Herdr."
    )
    subparsers = parser.add_subparsers(dest="operation", required=True)

    start = subparsers.add_parser("start")
    workspace_target = start.add_mutually_exclusive_group()
    workspace_target.add_argument("--workspace")
    workspace_target.add_argument("--new-workspace", action="store_true")
    start.add_argument("--workspace-label")
    start.add_argument("--cwd")
    start.add_argument(
        "--task",
        action="append",
        nargs=2,
        metavar=("LABEL", "PROMPT"),
        required=True,
    )
    start.add_argument("--confirm", action="store_true")
    start.add_argument("--timeout", type=int, default=30_000)
    start.set_defaults(handler=start_command)

    status = subparsers.add_parser("status")
    status.add_argument("--group")
    status.set_defaults(handler=status_command)

    respond = subparsers.add_parser("respond")
    respond.add_argument("--job", required=True)
    respond.add_argument("--prompt", required=True)
    respond.set_defaults(handler=respond_command)

    collect = subparsers.add_parser("collect")
    collect.add_argument("--group")
    collect.add_argument("--lines", type=int, default=200)
    collect.set_defaults(handler=collect_command)

    cleanup = subparsers.add_parser("cleanup")
    cleanup.add_argument("--group")
    cleanup.add_argument("--force", action="store_true")
    cleanup.set_defaults(handler=cleanup_command)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        ensure_runtime()
        payload, returncode = args.handler(args)
        emit(payload)
        return returncode
    except JobError as error:
        emit(
            {
                "ok": False,
                "error": {
                    "code": error.code,
                    "message": str(error),
                    **error.details,
                },
            }
        )
        return 2
    except HerdrCommandError as error:
        emit(
            {
                "ok": False,
                "error": {
                    "code": "herdr_command_failed",
                    "message": str(error),
                    "returncode": error.returncode,
                },
            }
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
