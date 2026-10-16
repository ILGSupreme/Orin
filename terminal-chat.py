# terminal_chat.py

import os
import threading
from uuid import uuid4

import requests

BASE_URL = os.getenv("ORIN_URL", "http://orin-gw:30080")
ENDPOINT = os.getenv("ORIN_ENDPOINT", "/terminal_chat")
USER_ID = os.getenv("ORIN_USER_ID", "terminal-user")
USE_STREAMING = os.getenv("ORIN_STREAM", "true").lower() in {"1", "true", "yes"}
CHAT_ONLY = os.getenv("ORIN_CHAT_ONLY", "false").lower() in {"1", "true", "yes"}

URL = f"{BASE_URL.rstrip('/')}/{ENDPOINT.lstrip('/')}"

session_id = os.getenv("ORIN_SESSION_ID")
history = []

command_buffer = ""
chat_buffer: list[tuple[str, str]] = []
terminal_lock = threading.RLock()
sending = threading.Event()
stop_notices = threading.Event()


def text_msg(role: str, text: str) -> dict:
    return {
        "role": role,
        "parts": [
            {
                "type": "text",
                "data": text,
                "encoding": "plain",
                "mime_type": "text/plain",
                "metadata": {},
            }
        ],
        "metadata": {"visibility": "user"},
    }


def extract_visible_text(messages: list[dict]) -> str:
    texts = []
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        if msg.get("metadata", {}).get("visibility") == "internal":
            continue

        for part in msg.get("parts", []):
            if part.get("type") == "text":
                texts.append(part.get("data", ""))

    return "\n".join(texts).strip()


def send_non_streaming(payload: dict) -> tuple[str | None, list[dict], str, str | None]:
    r = requests.post(URL, json=payload, timeout=600)
    r.raise_for_status()

    data = r.json()
    new_session_id = data.get("session_id")
    messages = data.get("content", [])
    text = extract_visible_text(messages) or str(data)
    metadata = data.get("metadata", {})
    is_command = None
    if metadata:
        is_command = metadata.get("kind", None)

    return new_session_id, messages, text, is_command


def clear_screen():
    os.system("clear")


def _render_terminal():
    clear_screen()

    if not CHAT_ONLY:
        print("####")
        print(command_buffer.rstrip())
        print("#####")

    print("Chat Terminal:")

    for role, text in chat_buffer:
        if role == "user":
            print(f"User> {text}")
        elif role == "assistant":
            print(f"Ai> {text}")

    print()


def render_terminal():
    with terminal_lock:
        _render_terminal()


def collect_goal_notices(current_session, seen):
    response = requests.get(
        f"{BASE_URL.rstrip('/')}/goals",
        params={
            "user_id": USER_ID,
            "session_id": current_session,
        },
        timeout=10,
    )
    response.raise_for_status()
    notices = []
    for goal in response.json().get("goals", []):
        values = goal.get("notices", [])
        previous = seen.get(goal["id"])
        if previous is None:
            values = values[-1:]  # On reconnect, show the latest result per goal.
        else:
            ids = [notice["id"] for notice in values]
            values = (
                values[ids.index(previous) + 1 :] if previous in ids else values[-1:]
            )
        for notice in values:
            notices.append((goal["id"], notice["id"], notice["text"]))
    return notices


def watch_goal_notices():
    seen = {}
    while not stop_notices.wait(2):
        current = session_id
        if not current or sending.is_set():
            continue
        try:
            notices = collect_goal_notices(current, seen)
        except (requests.RequestException, ValueError, KeyError, TypeError):
            continue
        with terminal_lock:
            if current != session_id or sending.is_set() or not notices:
                continue
            for goal_id, notice_id, text in notices:
                chat_buffer.append(("assistant", text))
                seen[goal_id] = notice_id
            _render_terminal()
            try:
                import readline

                print("User> " + readline.get_line_buffer(), end="", flush=True)
            except ImportError:
                print("User> ", end="", flush=True)


def send_streaming(payload: dict) -> tuple[str | None, list[dict], str, str | None]:
    collected_text = ""

    try:
        with requests.post(URL, json=payload, timeout=600, stream=True) as r:
            r.raise_for_status()

            r_session_id = r.headers.get("Session-Id")
            response_type = r.headers.get("Terminal-output")

            if response_type != "terminal":
                print("Ai> ", end="", flush=True)

            try:
                for chunk in r.iter_content(chunk_size=1, decode_unicode=True):
                    if not chunk:
                        continue

                    print(chunk, end="", flush=True)
                    collected_text += chunk

            except KeyboardInterrupt:
                print("\n\ndetached.\n")
                return r_session_id, [], collected_text, response_type

            print()

        return r_session_id, [], collected_text, response_type

    except KeyboardInterrupt:
        print("\n\ndetached.\n")
        return None, [], collected_text, "terminal"


def send_text(text: str):
    global session_id, command_buffer

    payload = {
        "user_id": USER_ID,
        "channel": "terminal",
        "stream": USE_STREAMING,
        "content": [text_msg("user", text)],
        "metadata": {"turn_id": uuid4().hex},
    }
    if os.getenv("ORIN_PRIVACY"):
        payload["metadata"]["privacy"] = os.environ["ORIN_PRIVACY"]

    if session_id:
        payload["session_id"] = session_id

    if USE_STREAMING:
        new_session_id, _, output, response_type = send_streaming(payload)
    else:
        new_session_id, _, output, response_type = send_non_streaming(payload)

    session_id = new_session_id or session_id

    if not CHAT_ONLY and response_type == "terminal":
        command_buffer = output
    else:
        chat_buffer.append(("user", text))
        chat_buffer.append(("assistant", output))

    render_terminal()


def main():
    global command_buffer
    print("AI terminal chat. Type /exit to quit.\n")
    if not CHAT_ONLY:
        send_text("/render")
    else:
        render_terminal()
    watcher = threading.Thread(target=watch_goal_notices, daemon=True)
    watcher.start()
    try:
        while True:
            try:
                user_input = input("User> ").strip()
                if user_input in {"/exit", "/quit"}:
                    break
                if not user_input:
                    continue
                is_command = user_input.startswith("/")
                sending.set()
                try:
                    send_text(user_input)
                except requests.HTTPError as exc:
                    response = exc.response
                    error_text = f"error> {response.status_code}: {response.text}"
                    if is_command:
                        command_buffer = error_text
                    else:
                        chat_buffer.append(("assistant", error_text))
                    render_terminal()
                except Exception as exc:
                    error_text = f"error> {exc}"
                    if is_command:
                        command_buffer = error_text
                    else:
                        chat_buffer.append(("assistant", error_text))
                    render_terminal()
                finally:
                    sending.clear()
            except KeyboardInterrupt:
                print()
                render_terminal()
    finally:
        stop_notices.set()
        watcher.join(timeout=1)


if __name__ == "__main__":
    main()
