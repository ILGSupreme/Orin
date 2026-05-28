from typing import Any


def as_dict(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}

    if isinstance(obj, dict):
        return obj
    
    if hasattr(obj, "to_dict"):
        return obj.to_dict()

    if hasattr(obj, "model_dump"):
        return obj.model_dump()

    raise TypeError(f"Unsupported job object: {type(obj)}")


def extract_from_content(content: Any, visibility: str, content_type: str) -> str:
    if not isinstance(content, list):
        return ""

    chunks: list[str] = []

    for message in content:
        if hasattr(message, "model_dump"):
            message = message.model_dump(mode="json")

        if not isinstance(message, dict):
            continue

        metadata = message.get("metadata") or {}
        if metadata.get("visibility", visibility) != visibility:
            continue

        for part in message.get("parts") or []:
            if hasattr(part, "model_dump"):
                part = part.model_dump(mode="json")

            if not isinstance(part, dict):
                continue

            if part.get("type") == content_type and part.get("data"):
                chunks.append(str(part["data"]))

    return "\n\n".join(chunks).strip()


def extract_job_result_text(job: Any) -> str:
    data = as_dict(obj=job)
    result = data.get("result") or {}

    if not isinstance(result, dict):
        return ""

    # Pipeline final shape:
    # job.result.latest_result.content
    latest_result = result.get("latest_result") or {}
    if isinstance(latest_result, dict):
        text = extract_from_content(
            content=latest_result.get("content"), visibility="user", content_type="text"
        )
        if text:
            return text

    # Single job / WorkResult shape:
    # job.result.content
    text = extract_from_content(
        content=result.get("content"), visibility="user", content_type="text"
    )
    if text:
        return text

    return ""


def format_session_job_status(jobs: list[Any]) -> str:
    if not jobs:
        return "There are no background jobs for this session."

    job_dicts = [as_dict(job) for job in jobs]
    job_dicts.sort(
        key=lambda j: j.get("updated_at") or j.get("created_at") or "", reverse=True
    )

    lines: list[str] = ["Background jobs for this session:"]

    for job in job_dicts:
        job_id = job.get("job_id", "unknown")
        status = job.get("status", "unknown")

        progress = job.get("progress") or {}
        current_stage = progress.get("current_stage")
        stage_index = progress.get("stage_index")
        stage_count = progress.get("stage_count")

        lines.append("")
        lines.append(f"- Job ID: `{job_id}`")
        lines.append(f"  Status: {status}")

        if current_stage:
            lines.append(f"  Stage: {current_stage} {stage_index}/{stage_count}")

        if status == "completed":
            if extract_job_result_text(job):
                lines.append("  Result: ready")
            else:
                lines.append(
                    "  Result: completed, but no user-visible result was stored"
                )

        elif status in ("accepted", "running"):
            lines.append("  Result: not ready yet")

        elif status == "failed":
            lines.append(f"  Error: {job.get('error') or 'unknown error'}")

    return "\n".join(lines)


def format_latest_job_result(jobs: list[Any]) -> str:
    if not jobs:
        return "There are no background jobs for this session."

    job_dicts = [as_dict(job) for job in jobs]
    job_dicts.sort(
        key=lambda j: j.get("updated_at") or j.get("created_at") or "", reverse=True
    )

    # Prefer newest completed job with visible result.
    for job in job_dicts:
        if job.get("status") != "completed":
            continue

        text = extract_job_result_text(job)
        if text:
            return text

    latest = job_dicts[0]
    job_id = latest.get("job_id", "unknown")
    status = latest.get("status", "unknown")

    if status in ("accepted", "running"):
        progress = latest.get("progress") or {}
        current_stage = progress.get("current_stage")
        stage_index = progress.get("stage_index")
        stage_count = progress.get("stage_count")

        if current_stage:
            return f"Job `{job_id}` is still running. Current stage: {current_stage} {stage_index}/{stage_count}."

        return f"Job `{job_id}` is still running."

    if status == "failed":
        return f"Job `{job_id}` failed: {latest.get('error') or 'unknown error'}"

    if status == "completed":
        return f"Job `{job_id}` completed, but no user-visible result was stored."

    return f"Job `{job_id}` is {status}."
