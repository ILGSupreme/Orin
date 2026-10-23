#!/usr/bin/env python3
"""Named local tasks, shared Git storage and an interactive terminal menu."""
import argparse
import fcntl
import json
import hashlib
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import time

import run as runner


def task_dir(home, name):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", name):
        raise ValueError("task name: 1–64 lowercase letters, digits, underscores or hyphens")
    path = home / "runs" / name
    if path.is_symlink() or path.resolve().parent != (home / "runs").resolve():
        raise ValueError("task path must stay inside managed runs")
    return path


def state_for(path):
    return json.loads((path / "state.json").read_text())


def text_input(label):
    print(label + " (finish with a line containing only '.')")
    lines = []
    while True:
        line = input()
        if line == ".":
            break
        lines.append(line)
    value = "\n".join(lines).strip()
    if not value:
        raise ValueError("input cannot be empty")
    return value + "\n"


def install(home, ref, bin_dir):
    """Install without relocating or deleting any existing lab directory."""
    home.mkdir(parents=True, exist_ok=True)
    config = home / "config.json"
    source = home / "source"
    if config.exists() or source.exists():
        raise ValueError("managed home already initialized; use its installed orin-dev command")
    if ref.startswith("-"):
        raise ValueError("invalid source ref")
    launcher = bin_dir / "orin-dev"
    if launcher.exists() or launcher.is_symlink():
        raise ValueError(f"launcher already exists: {launcher}")
    runner.command(["git", "clone", "--branch", ref,
                    "https://github.com/ILGSupreme/Orin.git", str(source)], timeout=300)
    if not (source / "tools/local-agent/manage.py").is_file():
        raise ValueError("selected source ref lacks the managed interface; source retained for inspection")
    (home / "runs").mkdir()
    runner.save(config, {"repo": "ILGSupreme/Orin", "base": ref})
    bin_dir.mkdir(parents=True, exist_ok=True)
    launcher.write_text("#!/bin/sh\nexec python3 " + shlex.quote(str(source / "tools/local-agent/manage.py"))
                        + " --home " + shlex.quote(str(home)) + ' "$@"\n')
    launcher.chmod(0o755)
    print(f"Installed {launcher}. Add {bin_dir} to PATH if needed. Managed home: {home}")


def overview(home):
    print(f"Managed home: {home}")
    for path in sorted((home / "runs").glob("*/state.json")):
        state = state_for(path.parent)
        print(f"{path.parent.name:24} {state['status']:16} attempt {state['attempt']}  "
              f"{state.get('pr', '')} {('from ' + state['archived_from']) if state.get('archived_from') else ''}")


def progress(path):
    if not (path / "state.json").exists():
        return "Preparing checkout"
    state = state_for(path)
    label = f"{state['status']} | attempt {state['attempt']}"
    events = path / f"attempt-{state['attempt']}" / "specialists.jsonl"
    roles = {}
    if events.exists():
        for line in events.read_text().splitlines():
            try:
                event = json.loads(line)
                roles[event['agent_type']] = ('reported' if event['hook_event_name'] == 'SubagentStop'
                                              else 'working')
            except (ValueError, KeyError):
                continue  # A hook may still be appending its final line.
    return label + " | " + ", ".join(f"{role}: {status}" for role, status in roles.items())


def invoke(home, path, extra, minutes):
    argv = [sys.executable, str(home / "source/tools/local-agent/run.py"),
            "--run", str(path), "--minutes", str(minutes)] + extra
    proc = subprocess.Popen(argv, start_new_session=True)
    started = time.monotonic()
    last, printed = None, 0
    try:
        while proc.poll() is None:
            label = progress(path)
            elapsed = int(time.monotonic() - started)
            if label != last or elapsed - printed >= 30:
                print(f"[{elapsed}s] {label}", flush=True)
                last, printed = label, elapsed
            time.sleep(1)
    except BaseException:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGINT)
        proc.wait()  # Controller terminates its own Codex/check process groups.
        raise
    if proc.returncode:
        raise RuntimeError(f"run stopped ({proc.returncode}); inspect {path}/state.json")
    if (path / "state.json").exists():
        print(progress(path))


def create(home, name, base, checks, minutes):
    path = task_dir(home, name)
    if path.exists():
        raise ValueError("task already exists; select it or choose another name")
    config = json.loads((home / "config.json").read_text())
    check_path = checks or home / "source/tools/local-agent/checks.example.json"
    frozen = runner.checks_from(check_path)
    print("Required local checks:")
    for check in frozen:
        print("  " + shlex.join(check))
    task = text_input("Describe the task, scope and acceptance criteria")
    if input("Create this task with these checks? [y/N] ").lower() != "y":
        return
    path.mkdir(parents=True)
    (path / "task.md").write_text(task)
    runner.save(path / "checks.json", frozen)
    invoke(home, path, ["--repo", config["repo"], "--base", base or config["base"],
                       "--task", str(path / "task.md"), "--checks", str(path / "checks.json"),
                       "--worktree-source", str(home / "source"), "--prepare"], minutes)
    print("Next: select Trust hooks, review /hooks in Codex, exit, then select Run.")


def trust(path):
    if state_for(path)["status"] == "archived":
        raise ValueError("archived task has no working checkout")
    print("Review project trust and both lifecycle hooks with /hooks; exit Codex when done.")
    subprocess.run(["codex"], cwd=path / "checkout", check=True)


def continue_task(home, path, feedback, publish, minutes):
    state = state_for(path)
    if state["status"] == "archived":
        raise ValueError("archived task: create a new task")
    extra = ["--repo", state["repo"]]
    if feedback or state["status"] != "prepared":
        value = text_input("Feedback for a fresh agent attempt")
        # Unique input files avoid overwriting feedback during concurrent invocation.
        import uuid
        feedback_path = path / f"feedback-{uuid.uuid4().hex}.md"
        feedback_path.write_text(value)
        extra += ["--feedback", str(feedback_path)]
    if publish:
        print("This starts a NEW attempt, reruns evidence/checks, then creates or updates a draft PR.")
        if input("Authorize controller publication? [y/N] ").lower() != "y":
            return
        extra.append("--publish")
    invoke(home, path, extra, minutes)


def show(path):
    state = state_for(path)
    print(json.dumps(state, indent=2))
    body = path / f"attempt-{state['attempt']}" / "pr-body.md"
    if body.exists():
        print(body.read_text())
    if state["status"] != "archived" and (path / "checkout").exists():
        print(runner.command(["git", "diff", "--stat", state["base_sha"]], path / "checkout"))
    print(f"Evidence and check logs: {path}")


def archive(home, path, confirmed=False):
    """Retire an idle task without discarding its checkout or changing its verdict."""
    with (path / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = state_for(path)
        source = home / "source"
        if state.get("worktree_source") != str(source.resolve()):
            raise ValueError("not a worktree owned by this managed home")
        if state["status"] not in ("prepared", "blocked", "local_ready", "awaiting_human", "no_changes"):
            raise ValueError("only idle, non-archived tasks can be archived")
        work = path / "checkout"
        saved = path / "archive"
        destination = saved / "checkout"
        manifest_path = saved / "manifest.json"
        if work.is_symlink() or saved.is_symlink() or destination.is_symlink():
            raise ValueError("refusing a redirected checkout or archive")
        if not confirmed and input(
            f"Archive {path.name} ({state['status']}), retaining ALL files and logs "
            "without freeing disk space? [y/N] "
        ).lower() != "y":
            return
        with (source / ".git/orin-worktrees.lock").open("a") as shared_lock:
            fcntl.flock(shared_lock, fcntl.LOCK_EX)
            # A manifest is written before moving anything. Retrying after an
            # interrupted move/state update finishes the same transaction.
            current = work if work.exists() else destination
            common = runner.command(["git", "rev-parse", "--git-common-dir"], current)
            if (current / common).resolve() != (source / ".git").resolve():
                raise ValueError("checkout belongs to a different Git repository")
            if runner.command(["git", "rev-parse", "--show-toplevel"], current) != str(current.resolve()):
                raise ValueError("checkout is not a worktree root")
            if runner.command(["git", "branch", "--show-current"], current) != state["branch"]:
                raise ValueError("unexpected branch")
            head = runner.command(["git", "rev-parse", "HEAD"], current)
            if manifest_path.exists():
                manifest = json.loads(manifest_path.read_text())
                if (manifest["original_state"] != state or manifest["head"] != head
                        or manifest["original_checkout"] != str(work)):
                    raise ValueError("archive transaction no longer matches task; retained for inspection")
                bundle = saved / "history.bundle"
                if hashlib.sha256(bundle.read_bytes()).hexdigest() != manifest["bundle_sha256"]:
                    raise ValueError("archive history checksum mismatch")
                runner.command(["git", "bundle", "verify", str(bundle)], source)
            else:
                if saved.exists():
                    raise ValueError("incomplete archive directory; inspect it before retrying")
                saved.mkdir(mode=0o700)
                bundle = saved / "history.bundle"
                # Include HEAD and the named branch, with their full ancestry.
                runner.command(["git", "bundle", "create", str(bundle), "HEAD",
                                f"refs/heads/{state['branch']}"], work)
                runner.command(["git", "bundle", "verify", str(bundle)], source)
                manifest = {
                    "version": 1, "original_state": state.copy(), "head": head,
                    "original_checkout": str(work), "archived_checkout": str(destination),
                    "bundle_sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
                    "created_at": time.time(), "merge_verified": False,
                }
                runner.save(manifest_path, manifest)
            if work.exists():
                # Git moves the full directory and preserves its index. No clean,
                # reset, force removal, stash or remote operation is involved.
                runner.command(["git", "worktree", "move", str(work), str(destination)], source)
            state.update(status="archived", archived_from=state["status"], archived_head=head,
                         archived_checkout=str(destination), archive_manifest=str(manifest_path))
            runner.save(path / "state.json", state)
        print(f"Task archived as {state['archived_from']}. All checkout files retained at {destination}.")


def menu(home, minutes):
    while True:
        overview(home)
        choice = input("\n[n] New task  [s] Select task  [q] Quit: ").lower()
        if choice == "q":
            return
        try:
            if choice == "n":
                create(home, input("Task name: "), None, None, minutes)
            elif choice == "s":
                path = task_dir(home, input("Task name: "))
                action = input("[t] Trust hooks  [r] Run  [f] Feedback  [p] Publish  [v] Review  [a] Archive: ")
                if action == "t":
                    trust(path)
                elif action in ("r", "f", "p"):
                    continue_task(home, path, action == "f", action == "p", minutes)
                elif action == "v":
                    show(path)
                elif action == "a":
                    archive(home, path)
        except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
            print(f"Stopped: {exc}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path(os.environ.get("ORIN_DEV_HOME", "/data/orin-dev")))
    parser.add_argument("--minutes", type=int, default=45)
    subs = parser.add_subparsers(dest="action")
    setup = subs.add_parser("install")
    setup.add_argument("--source-ref", default="main")
    setup.add_argument("--bin-dir", type=Path, default=Path.home() / ".local/bin")
    subs.add_parser("list")
    new = subs.add_parser("new")
    new.add_argument("name")
    new.add_argument("--base")
    new.add_argument("--checks", type=Path)
    for action in ("trust", "run", "feedback", "publish", "review", "archive"):
        subs.add_parser(action).add_argument("name")
    args = parser.parse_args()
    home = args.home.expanduser().resolve()
    if args.minutes < 1:
        parser.error("minutes must be positive")
    try:
        if args.action == "install":
            install(home, args.source_ref, args.bin_dir.expanduser().resolve())
        elif not (home / "config.json").is_file():
            raise ValueError("home not initialized; run manage.py --home PATH install first")
        elif args.action is None:
            menu(home, args.minutes)
        elif args.action == "list":
            overview(home)
        elif args.action == "new":
            create(home, args.name, args.base, args.checks, args.minutes)
        else:
            path = task_dir(home, args.name)
            if args.action == "trust":
                trust(path)
            elif args.action == "review":
                show(path)
            elif args.action == "archive":
                archive(home, path)
            else:
                continue_task(home, path, args.action == "feedback", args.action == "publish", args.minutes)
        return 0
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError, KeyboardInterrupt, EOFError) as exc:
        print(f"Stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
