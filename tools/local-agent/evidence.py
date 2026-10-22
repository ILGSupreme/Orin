"""Capture Codex lifecycle reports and validate one attempt before publication."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROLES = {"investigator", "implementer", "verifier"}


def snapshot(cwd):
    paths = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=cwd).split(b"\0")
    digest = hashlib.sha256()
    for name in sorted(set(p for p in paths if p)):
        path = Path(cwd) / os.fsdecode(name)
        digest.update(name + b"\0")
        if path.is_symlink():
            digest.update(b"link:" + os.fsencode(os.readlink(path)))
        elif path.is_file():
            digest.update(str(path.stat().st_mode).encode() + b":" + path.read_bytes())
        elif not path.exists():
            digest.update(b"deleted")
        else:
            raise ValueError(f"unsupported checkout entry: {name!r}")
        digest.update(b"\0")
    return digest.hexdigest()


def capture(payload, destination):
    event = payload.get("hook_event_name")
    if event not in ("SubagentStart", "SubagentStop"):
        raise ValueError("unsupported lifecycle event")
    record = {key: payload.get(key) for key in
              ("session_id", "agent_id", "agent_type", "cwd", "hook_event_name")}
    if any(not isinstance(value, str) or not value for value in record.values()):
        raise ValueError("incomplete lifecycle identity")
    if event == "SubagentStop":
        record["report"] = payload.get("last_assistant_message")
        if record["agent_type"] == "verifier":
            record["snapshot"] = snapshot(record["cwd"])
    with Path(destination).open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.write(json.dumps(record) + "\n")
        stream.flush()


def validate(events_path, agent_log, cwd):
    sessions = set()
    for line in agent_log.read_text().splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue  # CLI stderr shares this log.
        if event.get("type") == "thread.started":
            sessions.add(event.get("thread_id"))
    if len(sessions) != 1 or None in sessions:
        raise ValueError("missing or ambiguous coordinator thread ID")
    session = sessions.pop()
    if not events_path.is_file():
        raise ValueError("missing specialist lifecycle evidence; review/trust Codex hooks")
    agents = {}
    for index, line in enumerate(events_path.read_text().splitlines()):
        event = json.loads(line)
        if event.get("session_id") != session:
            raise ValueError("specialist evidence belongs to a different session")
        if Path(event.get("cwd", "")).resolve() != Path(cwd).resolve():
            raise ValueError("specialist evidence belongs to a different checkout")
        agent_id, role = event.get("agent_id"), event.get("agent_type")
        if not isinstance(agent_id, str) or not agent_id or role not in ROLES:
            raise ValueError("unknown specialist identity or role")
        if event.get("hook_event_name") == "SubagentStart":
            if agent_id in agents:
                raise ValueError("duplicate specialist start")
            agents[agent_id] = {"role": role, "start": index}
        elif event.get("hook_event_name") == "SubagentStop":
            if agent_id not in agents or agents[agent_id]["role"] != role:
                raise ValueError("specialist stopped without a matching start")
            report = json.loads(event.get("report") or "null")
            if (not isinstance(report, dict) or set(report) != {"verdict", "summary", "evidence"}
                    or report["verdict"] != "pass"
                    or not isinstance(report["summary"], str) or not report["summary"].strip()
                    or not isinstance(report["evidence"], list) or not report["evidence"]
                    or any(not isinstance(e, str) or not e.strip() for e in report["evidence"])):
                raise ValueError(f"specialist {role} has no valid passing report")
            agents[agent_id].update(stop=index, report=report, snapshot=event.get("snapshot"))
        else:
            raise ValueError("unknown lifecycle event")
    if {a["role"] for a in agents.values()} != ROLES:
        raise ValueError("required specialist role missing")
    if any("stop" not in a for a in agents.values()):
        raise ValueError("specialist has not reported completion")
    worker_stop = max(a["stop"] for a in agents.values() if a["role"] != "verifier")
    verifiers = [a for a in agents.values() if a["role"] == "verifier"]
    final = max(verifiers, key=lambda a: a["stop"])
    if final["start"] <= worker_stop:
        raise ValueError("final verification did not start after worker completion")
    if final["snapshot"] != snapshot(cwd):
        raise ValueError("checkout changed after the verifier report; request a fresh review")
    return {"session_id": session, "agents": agents, "snapshot": final["snapshot"]}


if __name__ == "__main__":
    target = os.environ.get("ORIN_AGENT_EVIDENCE")
    if target:
        capture(json.load(sys.stdin), target)
    print("{}")
