# terminal_chat.py

import os

import requests

BASE_URL = os.getenv("ORIN_URL", "http://orin-gw:30080")
ENDPOINT = os.getenv("ORIN_ENDPOINT", "/terminal_chat")
USER_ID = os.getenv("ORIN_USER_ID", "terminal-user")
USE_STREAMING = os.getenv("ORIN_STREAM", "true").lower() in {"1", "true", "yes"}
CHAT_ONLY = os.getenv("ORIN_CHAT_ONLY", "false").lower() in {"1", "true", "yes"}

URL = f"{BASE_URL.rstrip('/')}/{ENDPOINT.lstrip('/')}"

session_id = None
history = []

command_buffer = ""
chat_buffer: list[tuple[str, str]] = []


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


def render_terminal():
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
        "metadata": {},
    }

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


print("AI terminal chat. Type /exit to quit.\n")

if not CHAT_ONLY:
    send_text("/render")
else:
    render_terminal()

while True:
    try:
        user_input = input("User> ").strip()

        if user_input in {"/exit", "/quit"}:
            break

        if not user_input:
            continue

        is_command = user_input.startswith("/")

        try:
            send_text(user_input)

        except requests.HTTPError as e:
            response = e.response
            error_text = f"error> {response.status_code}: {response.text}"

            if is_command:
                command_buffer = error_text
            else:
                chat_buffer.append(("assistant", error_text))

            render_terminal()

        except Exception as e:
            error_text = f"error> {e}"

            if is_command:
                command_buffer = error_text
            else:
                chat_buffer.append(("assistant", error_text))

            render_terminal()

    except KeyboardInterrupt:
        print()
        render_terminal()
        continue
