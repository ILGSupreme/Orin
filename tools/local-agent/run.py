#!/usr/bin/env python3
"""Local Codex coordinator; Linux, Python 3.10+, git and optional gh required."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import uuid

HERE = Path(__file__).resolve().parent
FIELDS = ("status", "title", "body")
SCHEMA = {
    "type": "object", "additionalProperties": False, "required": list(FIELDS),
    "properties": {"status": {"type": "string", "enum": ["ready", "blocked"]},
                   "title": {"type": "string"}, "body": {"type": "string"}},
}


def command(argv, cwd=None, timeout=120):
    return subprocess.run(argv, cwd=cwd, text=True, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout).stdout.strip()


def execute(argv, cwd, log, timeout, stdin=None):
    """Terminate the whole local process group on timeout or interruption."""
    with log.open("w") as output:
        proc = subprocess.Popen(argv, cwd=cwd, text=True, start_new_session=True,
                                stdin=subprocess.PIPE if stdin is not None else None,
                                stdout=output, stderr=subprocess.STDOUT)
        try:
            proc.communicate(stdin, timeout=timeout)
        except BaseException:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            raise
        if proc.returncode:
            raise RuntimeError(f"{argv[0]} failed ({proc.returncode}); see {log}")


def save(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def checks_from(path):
    checks = json.loads(path.read_text())
    if not isinstance(checks, list) or not checks:
        raise ValueError("checks must be a non-empty JSON array of argument arrays")
    for check in checks:
        if not isinstance(check, list) or not check or any(
            not isinstance(arg, str) or not arg for arg in check
        ):
            raise ValueError("each check must contain non-empty string arguments")
    return checks


def result_from(path):
    result = json.loads(path.read_text())
    if (not isinstance(result, dict) or set(result) != set(FIELDS)
            or any(not isinstance(result[k], str) or not result[k].strip() for k in FIELDS)
            or result["status"] not in ("ready", "blocked")):
        raise ValueError("invalid coordinator result")
    if "\n" in result["title"] or len(result["title"]) > 200:
        raise ValueError("title must be a single line, at most 200 characters")
    return result


def run(args):
    run_dir = args.run.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / "lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state_path = run_dir / "state.json"
        if state_path.exists():
            state = json.loads(state_path.read_text())
            if not args.feedback:
                raise ValueError("existing run: supply --feedback with instructions to continue")
            if state["repo"] != args.repo:
                raise ValueError("repository differs from original run")
        else:
            if not args.task or not args.checks:
                raise ValueError("new runs require --task and --checks")
            state = {"repo": args.repo, "base": args.base,
                     "branch": "agent/" + uuid.uuid4().hex[:12],
                     "task": args.task.read_text(), "checks": checks_from(args.checks),
                     "attempt": 0, "status": "created"}
            save(state_path, state)
        work = run_dir / "checkout"
        try:
            if not work.exists():
                command(["git", "clone", "--branch", state["base"], "--single-branch",
                         f"https://github.com/{state['repo']}.git", str(work)], timeout=300)
                command(["git", "switch", "-c", state["branch"]], work)
                state["base_sha"] = command(["git", "rev-parse", "HEAD"], work)
                save(state_path, state)
            if "base_sha" not in state:
                raise RuntimeError("incomplete checkout initialization; use a new run directory")
            if command(["git", "branch", "--show-current"], work) != state["branch"]:
                raise RuntimeError("unexpected checkout branch")
            if args.publish:
                command(["gh", "auth", "status"])
                prs = json.loads(command(["gh", "pr", "list", "--repo", state["repo"],
                    "--head", state["branch"], "--state", "all", "--json", "url,state,isDraft"]))
                if any(pr["state"] != "OPEN" for pr in prs):
                    raise RuntimeError("PR is closed or merged; start a new task")
            state["attempt"] += 1
            attempt = run_dir / f"attempt-{state['attempt']}"
            attempt.mkdir()
            schema = attempt / "schema.json"
            save(schema, SCHEMA)
            result_path = attempt / "result.json"
            prompt = (HERE / "coordinator.md").read_text()
            prompt += "\nApproved task:\n" + state["task"]
            prompt += "\nRequired check argument arrays:\n" + json.dumps(state["checks"])
            if args.feedback:
                prompt += "\nHuman follow-up instructions:\n" + args.feedback.read_text()
            (attempt / "prompt.txt").write_text(prompt)
            state["status"] = "running"
            save(state_path, state)
            execute(["codex", "exec", "--sandbox", "workspace-write", "--json",
                     "--output-schema", str(schema), "-o", str(result_path), "-"],
                    work, attempt / "agent.jsonl", args.minutes * 60, prompt)
            result = result_from(result_path)
            if result["status"] != "ready":
                state["status"] = "blocked"
                save(state_path, state)
                print(f"Blocked: see {result_path}")
                return 2
            # Checks are captured before the agent starts, not read from edited code.
            for index, check in enumerate(state["checks"]):
                execute(check, work, attempt / f"check-{index}.log", args.check_minutes * 60)
            command(["git", "diff", "--check"], work)
            command(["git", "add", "--all"], work)
            if command(["git", "diff", "--cached", "--name-only"], work):
                command(["git", "commit", "-m", result["title"]], work)
            head = command(["git", "rev-parse", "HEAD"], work)
            if not command(["git", "diff", "--stat", state["base_sha"], head], work):
                state["status"] = "no_changes"
                save(state_path, state)
                print("No changes; no PR created. See the local result.")
                return 0
            body = (result["body"] + f"\n\nCandidate commit: `{head}`\n"
                    "\nLocal controller checks passed:\n" +
                    "\n".join("- `" + json.dumps(c) + "`" for c in state["checks"]) +
                    "\n\nCI and human lab acceptance are pending. No image has been built "
                    "by this controller. Record the tested commit and image digest before acceptance.\n")
            body_path = attempt / "pr-body.md"
            body_path.write_text(body)
            state.update(status="local_ready", candidate=head)
            save(state_path, state)
            if args.publish:
                command(["git", "push", "origin", f"HEAD:refs/heads/{state['branch']}"], work)
                if prs:
                    state["pr"] = prs[0]["url"]
                    if not prs[0]["isDraft"]:
                        command(["gh", "pr", "ready", state["pr"], "--undo"], work)
                    command(["gh", "pr", "edit", state["pr"], "--title", result["title"],
                             "--body-file", str(body_path)], work)
                else:
                    state["pr"] = command(["gh", "pr", "create", "--repo", state["repo"],
                        "--base", state["base"], "--head", state["branch"], "--draft",
                        "--title", result["title"], "--body-file", str(body_path)], work)
                state["status"] = "awaiting_human"
                save(state_path, state)
            print(state.get("pr", str(body_path)))
            return 0
        except BaseException as exc:
            state.update(status="blocked", error=f"{type(exc).__name__}: {exc}")
            save(state_path, state)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="ILGSupreme/Orin")
    parser.add_argument("--base", default="main")
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--task", type=Path)
    parser.add_argument("--feedback", type=Path)
    parser.add_argument("--checks", type=Path)
    parser.add_argument("--publish", action="store_true", help="push and create/update a draft PR")
    parser.add_argument("--minutes", type=int, default=45)
    parser.add_argument("--check-minutes", type=int, default=15)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo):
        parser.error("--repo must be owner/name")
    if args.base.startswith("-") or args.minutes < 1 or args.check_minutes < 1:
        parser.error("invalid base or time limit")
    try:
        return run(args)
    except (Exception, KeyboardInterrupt) as exc:
        print(f"Stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
