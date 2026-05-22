import argparse
import inspect
import json
import shlex
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx
from fastapi.responses import PlainTextResponse, StreamingResponse
from common.hfservice import HuggingFaceService
from common.jobs import JobManager, JobSpec
from common import primer
from common.primer import Primer
from cortex.cluster.deployment import pod_factory
from cortex.cluster.deployment import pod_naming
from cortex.cluster.deployment import pod_template
from cortex.cluster.deployment.service import DeploymentError, DeploymentService
from cortex.cluster.discovery import formatting
from cortex.cluster.discovery.client import BackendClient
from cortex.cluster.discovery.policy import BackendRoutingPolicy
from cortex.cluster.discovery.services import DiscoveryService
from typing import Awaitable, Callable, Literal


@dataclass
class ShellState:
    path: list[str] = field(default_factory=lambda: ["Root"])
    selected_backend_name: str | None = None


GLOBAL_COMMANDS = {"/help", "/commands", "/cd", "/render", "/attach"}

CommandMode = Literal["terminal", "chat"]
HandlerKind = Literal["text", "stream"]

TextHandler = Callable[[str], Awaitable[str]]
StreamHandler = Callable[[str], object]


@dataclass(slots=True)
class Command:
    command: str
    desc: str
    kind: HandlerKind
    handler: TextHandler | StreamHandler

    # Harness exposure
    modes: set[CommandMode] = field(default_factory=set)
    harness_action: str | None = None
    safe_info_action: bool = False
    suggest_only: bool = False

    async def run_text(self, args: str = "") -> str:
        if self.kind != "text":
            raise ValueError(f"Command is not text command: {self.command}")

        result = self.handler(args)

        if inspect.isawaitable(result):
            result = await result

        return str(result)


@dataclass(slots=True)
class CommandFolder:
    name: str
    desc: str
    commands: list[Command] = field(default_factory=list)
    folders: dict[str, "CommandFolder"] = field(default_factory=dict)


MODEL_ALIASES = {
    "qwen35-4b": {
        "provider": "huggingface",
        "engine": "gguf",
        "model_id": "qwen35-4b",
        "repo_id": "unsloth/Qwen3.5-4b-GGUF",
        "filename": "Qwen3.5-4B-Q4_K_M.gguf",
        "tokenizer_id": "Qwen/Qwen3.5-4B",
        "revision": "main",
    },
    "qwen35-9b": {
        "provider": "huggingface",
        "engine": "gguf",
        "model_id": "qwen35-9b",
        "repo_id": "unsloth/Qwen3.5-9B-GGUF",
        "filename": "Qwen3.5-9B-Q4_K_M.gguf",
        "tokenizer_id": "Qwen/Qwen3.5-9B",
        "revision": "main",
    },
    "qwen35-0.8b": {
        "provider": "huggingface",
        "engine": "gguf",
        "model_id": "qwen35-0.8b",
        "repo_id": "unsloth/Qwen3.5-0.8B-GGUF",
        "filename": "Qwen3.5-0.8B-Q4_K_M.gguf",
        "tokenizer_id": "Qwen/Qwen3.5-0.8B",
        "revision": "main",
    },
}


class CommandRouter:
    def __init__(
        self,
        primer: Primer,
        deployment_service: DeploymentService,
        backend_service: DiscoveryService,
        routing_policy: BackendRoutingPolicy,
        backend_client: BackendClient,
        job_manager: JobManager,
        log_stream=None,
    ):
        self.hf_service = HuggingFaceService()
        self.primer = primer
        self.deployment_service = deployment_service
        self.backend_service = backend_service
        self.routing_policy = routing_policy
        self.backend_client = backend_client
        self.job_manager = job_manager
        self.log_stream = log_stream
        self.shell_state = ShellState()

        self.root_folder = self._build_folders()

    def _cmd(
        self,
        command: str,
        desc: str,
        handler,
        *,
        kind: HandlerKind = "text",
        modes: set[CommandMode] | None = None,
        harness_action: str | None = None,
        safe_info_action: bool = False,
        suggest_only: bool = False,
    ) -> Command:
        return Command(
            command=command,
            desc=desc,
            kind=kind,
            handler=handler,
            modes=modes or set(),
            harness_action=harness_action,
            safe_info_action=safe_info_action,
            suggest_only=suggest_only,
        )

    def _build_folders(self) -> CommandFolder:
        return CommandFolder(
            name="Root",
            desc="Root commands",
            commands=[
                self._cmd(
                    "/help",
                    "Show help information",
                    self.handle_help,
                    modes={"terminal"},
                    harness_action="help",
                    safe_info_action=True,
                ),
                self._cmd(
                    "/commands",
                    "Show available commands",
                    self.handle_get_commands,
                    modes={"terminal"},
                    harness_action="list_commands",
                    safe_info_action=True,
                ),
                self._cmd(
                    "/cd",
                    "Traverse folder",
                    self.handle_cd,
                    modes={"terminal"},
                    harness_action="change_folder",
                    suggest_only=True,
                ),
                self._cmd(
                    "/attach",
                    "Attach to logs for current context",
                    self.handle_attach,
                    kind="stream",
                    modes={"terminal"},
                    harness_action="attach_logs",
                    suggest_only=True,
                ),
                self._cmd(
                    "/render",
                    "Render current folder",
                    self.handle_render,
                    modes={"terminal"},
                    harness_action="render_current_folder",
                    safe_info_action=True,
                ),
            ],
            folders={
                "Deployment": CommandFolder(
                    name="Deployment",
                    desc="Deploy and remove pods/services",
                    commands=[
                        self._cmd(
                            "/list_pods",
                            "List deployed pods",
                            self.handle_list_pods,
                            modes={"terminal"},
                            harness_action="list_pods",
                            safe_info_action=True,
                        ),
                        self._cmd(
                            "/list_nodes",
                            "Show inventory nodes",
                            self.handle_list_nodes,
                            modes={"terminal"},
                            harness_action="list_nodes",
                            safe_info_action=True,
                        ),
                        self._cmd(
                            "/deploy_pod",
                            "Deploy a pod/service",
                            self.handle_deploy_pod,
                            modes={"terminal"},
                            harness_action="deploy_pod",
                            suggest_only=True,
                        ),
                        self._cmd(
                            "/delete_pod",
                            "Delete a pod/deployment",
                            self.handle_delete_pod,
                            modes={"terminal"},
                            harness_action="delete_pod",
                            suggest_only=True,
                        ),
                        self._cmd(
                            "/install_k3s",
                            "Install k3s on a host machine",
                            self.handle_install_k3s,
                            modes={"terminal"},
                            harness_action="install_k3s",
                            suggest_only=True,
                        ),
                        self._cmd(
                            "/add_node",
                            "Add host to inventory",
                            self.handle_add_node,
                            modes={"terminal"},
                            harness_action="add_node",
                            suggest_only=True,
                        ),
                        self._cmd(
                            "/remove_node",
                            "Remove host from inventory",
                            self.handle_remove_node,
                            modes={"terminal"},
                            harness_action="remove_node",
                            suggest_only=True,
                        ),
                        self._cmd(
                            "/label_node",
                            "Add priority label to node",
                            self.handle_label_node,
                            modes={"terminal"},
                            harness_action="label_node",
                            suggest_only=True,
                        ),
                        self._cmd(
                            "/update_discovery_meta",
                            "Add/remove/update discovery metadata",
                            self.handle_update_discovery_meta,
                            modes={"terminal"},
                            harness_action="update_discovery_meta",
                            suggest_only=True,
                        ),
                    ],
                ),
                "Cluster": CommandFolder(
                    name="Cluster",
                    desc="Cluster configuration and discovery controls",
                    commands=[
                        self._cmd(
                            "/show",
                            "Show discovered cluster backends",
                            self.handle_show,
                            modes={"terminal"},
                            harness_action="show_cluster",
                            safe_info_action=True,
                        ),
                    ],
                ),
                "Configuration": CommandFolder(
                    name="Configuration",
                    desc="Node and model configuration controls",
                    commands=[
                        self._cmd(
                            "/models",
                            "List available model aliases",
                            self.handle_models,
                            modes={"terminal"},
                            harness_action="model_status",
                            safe_info_action=True,
                        ),
                        self._cmd(
                            "/job_status",
                            "Show job status",
                            self.handle_job_status,
                            modes={"terminal", "chat"},
                            harness_action="job_status",
                            safe_info_action=True,
                        ),
                        self._cmd(
                            "/load_model",
                            "Load model to backend engine",
                            self.handle_load_model,
                            modes={"terminal"},
                            harness_action="load_model",
                            suggest_only=True,
                        ),
                        self._cmd(
                            "/unload_model",
                            "Unload model from backend engine",
                            self.handle_unload_model,
                            modes={"terminal"},
                            harness_action="unload_model",
                            suggest_only=True,
                        ),
                        self._cmd(
                            "/engine",
                            "Load backend engine, gguf or vllm",
                            self.handle_engine,
                            modes={"terminal"},
                            harness_action="engine_status",
                            suggest_only=True,
                        ),
                    ],
                    folders={
                        "ModelDownloader": CommandFolder(
                            name="ModelDownloader",
                            desc="Download models from a provider",
                            commands=[
                                self._cmd(
                                    "/huggingface",
                                    "Models provided by Hugging Face",
                                    self.handle_huggingface_commands,
                                    modes={"terminal"},
                                    harness_action="huggingface",
                                    suggest_only=True,
                                ),
                            ],
                        )
                    },
                ),
            },
        )

    def get_commands(self, mode: CommandMode) -> list[dict[str, str | bool | None]]:
        commands: list[dict[str, str | bool | None]] = []

        for folder_path, command in self._walk_commands(self.root_folder):
            if mode not in command.modes:
                continue

            commands.append(
                {
                    "action": command.harness_action,
                    "command": command.command,
                    "description": command.desc,
                    "folder": folder_path,
                    "safe_info_action": command.safe_info_action,
                    "suggest_only": command.suggest_only,
                }
            )

        return commands

    def _walk_commands(
        self,
        folder: CommandFolder,
        path: str = "Root",
    ):
        for command in folder.commands:
            yield path, command

        for child_name, child in folder.folders.items():
            yield from self._walk_commands(child, f"{path}/{child_name}")

    def _parse(self, text: str):
        parts = text.strip().split(maxsplit=1)
        command = parts[0]
        args = parts[1] if len(parts) > 1 else ""
        return command.lower(), args

    def _parse_load_model_args(self, args: str):
        parser = argparse.ArgumentParser(prog="/load_model", add_help=False)
        parser.add_argument("model_alias", nargs="?")
        parser.add_argument("--force-reload", action="store_true")

        try:
            parsed = parser.parse_args(shlex.split(args))
        except SystemExit:
            return "Invalid usage.\n\nUsage:\n  /load_model <alias> [--force-reload]\n"

        if not parsed.model_alias:
            return "Missing model alias.\n\nUsage:\n  /load_model <alias>\n"

        return parsed

    def output_generator(self, output_string: str):
        chunk_size = 64  # smaller = nicer streaming feel
        for i in range(0, len(output_string), chunk_size):
            yield output_string[i : i + chunk_size]

    async def handle_command(self, text: str):
        command_name, args = self._parse(text)

        try:
            result = await self.dispatch_command(
                command=command_name,
                args=args,
            )

            if inspect.isasyncgen(result):
                return self._stream_async_output(result)

            return self._text_output(str(result))

        except Exception as e:
            return self._text_output(f"error: {e}\n")

    async def handle_command_text(self, text: str):
        command_name, args = self._parse(text)

        try:
            result = await self.dispatch_command(
                command=command_name,
                args=args,
            )

            if inspect.isasyncgen(result):
                return (
                    "Command opens a stream and cannot be used in non-stream mode.\n"
                    "Use streaming terminal mode for /attach.\n"
                )

            return str(result)

        except Exception as e:
            return f"error: {e}"

    def _stream_output(self, output: str):
        return StreamingResponse(
            self.output_generator(output),
            media_type="text/plain",
            headers={"Cache-Control": "no-cache", "Terminal-output": "terminal"},
        )

    def _text_output(self, output: str):
        return PlainTextResponse(
            output,
            media_type="text/plain",
            headers={
                "Cache-Control": "no-cache",
                "Terminal-output": "terminal",
            },
        )

    def _stream_async_output(self, gen: AsyncIterator[str]):
        return StreamingResponse(
            gen,
            media_type="text/plain",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Terminal-output": "terminal",
            },
        )

    def _split_csv_items(self, value: str) -> list[str]:
        return [item.strip() for item in value.split(",") if item.strip()]

    def _merge_repeated_list_args(
        self,
        *,
        repeated: list[str] | None = None,
        csv_values: list[str] | None = None,
    ) -> list[str] | None:
        values: list[str] = []

        for item in repeated or []:
            item = item.strip()
            if item:
                values.append(item)

        for csv in csv_values or []:
            values.extend(self._split_csv_items(csv))

        if not values:
            return None

        # preserve order while deduping
        seen: set[str] = set()
        deduped: list[str] = []

        for value in values:
            if value in seen:
                continue
            seen.add(value)
            deduped.append(value)

        return deduped

    def _service_name_from_target(
        self, target: str, *, exact_service: bool = False
    ) -> str:
        target = target.strip()

        if not target:
            raise SystemExit("Missing service/app name.")

        if exact_service:
            return target

        if target.endswith("-service"):
            return target

        return f"{target}-service"

    def _parse_extra_annotations(
        self, values: list[str] | None
    ) -> dict[str, str] | None:
        if not values:
            return None

        annotations: dict[str, str] = {}

        for raw in values:
            if "=" not in raw:
                raise SystemExit(f"Invalid annotation '{raw}'. Expected key=value.")

            key, value = raw.split("=", 1)
            key = key.strip()
            value = value.strip()

            if not key:
                raise SystemExit(
                    f"Invalid annotation '{raw}'. Annotation key is empty."
                )

            annotations[key] = value

        return annotations

    def _list_discovered_backends(self) -> list:
        return self.backend_service.get_registry().list_backends()

    def _find_backend_by_name(self, name: str | None):
        backends = self._list_discovered_backends()

        for backend in backends:
            if backend.name == name:
                return backend

        for backend in backends:
            if backend.service_name == name:
                return backend

        return None

    def _is_in_cluster_folder(self) -> bool:
        return self.shell_state.path == ["Root", "Cluster"]

    def _is_in_backend_folder(self) -> bool:
        return (
            len(self.shell_state.path) == 3
            and self.shell_state.path[0] == "Root"
            and self.shell_state.path[1] == "Cluster"
            and self.shell_state.selected_backend_name is not None
            and self.shell_state.path[2] == self.shell_state.selected_backend_name
        )

    def _cd_up(self) -> str:
        if len(self.shell_state.path) <= 1:
            return self._render_current_folder()

        self.shell_state.path.pop()

        if not self._is_in_backend_folder():
            self.shell_state.selected_backend_name = None

        return self._render_current_folder()

    def _try_cd_static_folder(self, target: str) -> str | None:
        current = self._get_static_folder(self.shell_state.path)

        if current is None:
            return None

        folders = current.folders

        if target not in folders:
            return None

        self.shell_state.path.append(target)
        self.shell_state.selected_backend_name = None

        return self._render_current_folder()

    def _get_static_folder(self, path: list[str]) -> CommandFolder | None:
        if not path:
            return self.root_folder

        if path[0] != "Root":
            return None

        folder = self.root_folder

        for part in path[1:]:
            # Dynamic backend folder, not part of static tree.
            if (
                folder.name == "Cluster"
                and part == self.shell_state.selected_backend_name
            ):
                break

            next_folder = folder.folders.get(part)
            if next_folder is None:
                return None

            folder = next_folder

        return folder

    def _render_static_folder(self, folder: CommandFolder) -> str:
        title = " / ".join(self.shell_state.path)

        lines: list[str] = [
            title,
            "=" * len(title),
        ]

        if folder.desc:
            lines.extend(["", folder.desc])

        if folder.commands:
            lines.extend(["", "Commands:"])

            for command in folder.commands:
                if command.desc:
                    lines.append(f"  {command.command:<24} {command.desc}")
                else:
                    lines.append(f"  {command.command}")

        if folder.folders:
            lines.extend(["", "Folders:"])

            for folder_name, child in folder.folders.items():
                if child.desc:
                    lines.append(f"  {folder_name:<24} {child.desc}")
                else:
                    lines.append(f"  {folder_name}")

        if not folder.commands and not folder.folders:
            lines.extend(["", "No commands or folders."])

        lines.extend(
            [
                "",
                "Navigation:",
                "  /cd <folder>",
                "  /cd ..",
                "  /cd /",
            ]
        )

        return "\n".join(lines)

    def _render_current_folder(self) -> str:
        if self._is_in_backend_folder():
            backend = self._find_backend_by_name(self.shell_state.selected_backend_name)
            if backend is None:
                return "Selected backend is no longer discovered."
            return self._render_backend_folder(backend)

        if self._is_in_cluster_folder():
            return self._render_cluster_folder()

        folder = self._get_static_folder(self.shell_state.path)

        if folder is None:
            return "Invalid folder."

        return self._render_static_folder(folder)

    def _render_cluster_folder(self) -> str:
        backends = self._list_discovered_backends()
        folder = self._get_static_folder(["Root", "Cluster"])

        lines = [
            "Cluster",
            "=======",
            "",
        ]

        if folder and folder.desc:
            lines.append(folder.desc)
            lines.append("")

        lines.append("Commands:")

        if folder and folder.commands:
            for command in folder.commands:
                lines.append(f"  {command.command:<24} {command.desc}")
        else:
            lines.append("  -")

        lines.extend(["", "Backends:"])

        if not backends:
            lines.append("  -")
            return "\n".join(lines)

        for backend in sorted(backends, key=lambda b: b.name):
            role = getattr(backend, "role", "-")
            health = getattr(getattr(backend, "health", None), "status", "unknown")
            model = getattr(backend, "model", None) or "-"

            lines.append(f"  {backend.name}  role={role} health={health} model={model}")

        lines.extend(
            [
                "",
                "Navigation:",
                "  /cd <backend-name>",
                "  /cd ..",
                "  /cd /",
            ]
        )

        return "\n".join(lines)

    def _backend_commands(self, backend) -> list[str]:
        commands = [
            "/show",
            "/health",
            "/attach",
            "/update_discovery_meta",
        ]

        if getattr(backend, "models_path", None):
            commands.append("/models")

        if getattr(backend, "role", None) in {"llm", "cortex"}:
            commands.extend(
                [
                    "/engine",
                    "/load_model",
                    "/job_status",
                    "/unload_model",
                ]
            )

        return commands

    def _render_backend_folder(self, backend) -> str:
        health = getattr(backend, "health", None)
        runtime = getattr(backend, "runtime", None)

        status = getattr(health, "status", "unknown") if health else "unknown"
        ready = getattr(health, "ready_endpoints", "-") if health else "-"
        effective_n_ctx = getattr(runtime, "effective_n_ctx", None) if runtime else None

        lines = [
            f"Cluster / {backend.name}",
            "=" * (10 + len(backend.name)),
            "",
            "Backend:",
            f"  service: {backend.namespace}/{backend.service_name}",
            f"  role: {backend.role}",
            f"  kind: {backend.kind}",
            f"  model: {backend.model or '-'}",
            f"  health: {status}",
            f"  ready endpoints: {ready}",
        ]

        if effective_n_ctx:
            lines.append(f"  effective context: {effective_n_ctx}")

        lines.extend(
            [
                "",
                "Commands:",
            ]
        )

        for command in self._backend_commands(backend):
            lines.append(f"  {command}")

        return "\n".join(lines)

    def _description_for_visible_command(self, command_name: str) -> str:
        if command_name == "/render":
            return "Render current folder"
        if command_name == "/help":
            return "Show help information"
        if command_name == "/commands":
            return "Show available commands"
        if command_name == "/cd":
            return "Traverse folder"

        if self._is_in_backend_folder():
            backend_descs = {
                "/show": "Show selected backend",
                "/health": "Read selected backend health",
                "/attach": "Attach to selected backend logs",
                "/models": "Read selected backend models",
                "/engine": "Load backend engine on selected backend",
                "/load_model": "Load model on selected backend",
                "/job_status": "Show selected backend job status",
                "/unload_model": "Unload model on selected backend",
                "/update_discovery_meta": "Update selected backend discovery metadata",
            }
            return backend_descs.get(command_name, "")

        folder = self._get_static_folder(self.shell_state.path)
        if folder is None:
            return ""

        for command in folder.commands:
            if command.command == command_name:
                return command.desc

        return ""

    async def _attach_to_cortex(self, args: str):
        if self.log_stream is None:
            yield "Cortex log attach is not configured.\n"
            return

        async for line in self.log_stream.stream():
            yield line

    async def _attach_to_backend(self, backend, args: str):
        url = f"{backend.url.rstrip('/')}/attach"

        if args.strip():
            url = f"{url}?{args.strip()}"

        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()

                    async for chunk in response.aiter_text():
                        if chunk:
                            yield chunk

        except Exception as exc:
            yield f"Failed to attach to backend {backend.name}: {exc}\n"

    async def handle_attach(self, args: str):
        if self._is_in_backend_folder():
            backend = self._find_backend_by_name(self.shell_state.selected_backend_name)

            if backend is None:
                yield "Selected backend is no longer discovered.\n"
                return

            async for chunk in self._attach_to_backend(backend, args):
                yield chunk

            return

        async for chunk in self._attach_to_cortex(args):
            yield chunk

    async def dispatch_command(self, command: str, args: str):
        if command in GLOBAL_COMMANDS:
            return await self.dispatch_static_command(
                command,
                args,
                prefer_stream=(command == "/attach"),
            )

        if self._is_in_backend_folder():
            backend_result = await self.dispatch_backend_command(command, args)
            if backend_result is not None:
                return backend_result

            return (
                f"Unknown command in backend folder: {command}\n\n"
                "Use /commands or /help to see available commands."
            )

        return await self.dispatch_static_command(command, args)

    async def dispatch_backend_command(self, command: str, args: str):
        backend = self._find_backend_by_name(self.shell_state.selected_backend_name)

        if backend is None:
            return "Selected backend is no longer discovered."

        if command == "/show":
            return formatting.format_backend_list([backend], verbose=True)

        if command == "/health":
            return await self.handle_backend_health(backend, args)

        if command == "/models":
            return await self.handle_backend_models(backend, args)

        if command == "/update_discovery_meta":
            return await self.handle_backend_update_discovery_meta(backend, args)

        if command == "/load_model":
            return await self.handle_backend_load_model(backend, args)

        if command == "/job_status":
            return await self.handle_backend_job_status(backend, args)

        if command == "/unload_model":
            return await self.handle_backend_unload_model(backend, args)

        if command == "/engine":
            return await self.handle_backend_engine(backend, args)

        return None

    def _find_visible_static_command(self, command: str) -> Command | None:
        for cmd in self._get_visible_static_commands():
            if cmd.command == command:
                return cmd

        return None

    def _get_visible_static_commands(self) -> list[Command]:
        commands: list[Command] = []

        root_commands = self.root_folder.commands
        commands.extend(cmd for cmd in root_commands if cmd.command in GLOBAL_COMMANDS)

        folder = self._get_static_folder(self.shell_state.path)

        if folder is None:
            return commands

        for cmd in folder.commands:
            if cmd.command not in GLOBAL_COMMANDS:
                commands.append(cmd)

        return commands

    def _get_visible_command_names(self) -> set[str]:
        commands: set[str] = {
            cmd.command for cmd in self._get_visible_static_commands()
        }

        if self._is_in_backend_folder():
            backend = self._find_backend_by_name(self.shell_state.selected_backend_name)

            if backend is not None:
                commands.update(self._backend_commands(backend))

        return commands

    async def dispatch_static_command(
        self,
        command: str,
        args: str,
        *,
        prefer_stream: bool = False,
    ):
        cmd = self._find_visible_static_command(command)

        if cmd is None:
            visible_commands = self._get_visible_command_names()

            if command in visible_commands:
                return f"Command {command} is visible but has no registered handler."

            return (
                f"Unknown command in current folder: {command}\n\n"
                "Use /commands or /help to see available commands."
            )

        result = cmd.handler(args)

        if inspect.isawaitable(result):
            result = await result

        if inspect.isasyncgen(result) and not prefer_stream:
            chunks: list[str] = []
            async for chunk in result:
                chunks.append(str(chunk))
            return "".join(chunks)

        return result

    # ---------------------------------------------------------------------------
    # Handle Functions
    # ---------------------------------------------------------------------------

    def handle_render(self, args: str = "") -> str:
        return self._render_current_folder() + "\n"

    async def handle_cd(self, args: str) -> str:
        target = args.strip()

        if not target:
            return self._render_current_folder()

        if target in {"..", "../"}:
            return self._cd_up()

        if target == "/":
            self.shell_state.path = ["Root"]
            self.shell_state.selected_backend_name = None
            return self._render_current_folder()

        # static folder traversal first
        static_result = self._try_cd_static_folder(target)
        if static_result is not None:
            return static_result

        # dynamic backend folder traversal
        if self._is_in_cluster_folder():
            backend = self._find_backend_by_name(target)

            if backend is None:
                return f"No discovered backend named: {target}"

            self.shell_state.path = ["Root", "Cluster", backend.name]
            self.shell_state.selected_backend_name = backend.name
            return self._render_backend_folder(backend)

        return f"No folder named: {target}"

    def handle_get_commands(self, args: str = "") -> str:
        lines = [
            "Available commands",
            "==================",
            "",
        ]

        for command in sorted(self._get_visible_command_names()):
            desc = self._description_for_visible_command(command)
            if desc:
                lines.append(f"  {command:<24} {desc}")
            else:
                lines.append(f"  {command}")

        return "\n".join(lines) + "\n"

    def handle_models(self, args: str = "") -> str:
        lines = ["Available model aliases:\n"]

        for alias, model in MODEL_ALIASES.items():
            lines.append(f"{alias}")
            lines.append(f"  engine: {model['engine']}")
            lines.append(f"  repo: {model.get('repo_id', model['model_id'])}")
            lines.append("")

        return "\n".join(lines)

    async def handle_huggingface_commands(self, args: str) -> str:
        parser = argparse.ArgumentParser(prog="/huggingface", add_help=False)
        sub = parser.add_subparsers(dest="cmd")

        # search
        p_search = sub.add_parser("search", add_help=False)
        p_search.add_argument("query")
        p_search.add_argument("--limit", type=int, default=10)

        # info
        p_info = sub.add_parser("info", add_help=False)
        p_info.add_argument("repo_id")

        # list gguf files
        p_gguf = sub.add_parser("gguf", add_help=False)
        p_gguf.add_argument("repo_id")

        # alias add
        p_alias = sub.add_parser("alias", add_help=False)
        p_alias_sub = p_alias.add_subparsers(dest="alias_cmd")

        p_alias_add = p_alias_sub.add_parser("add", add_help=False)
        p_alias_add.add_argument("alias")
        p_alias_add.add_argument("repo_id")
        p_alias_add.add_argument("--file", dest="filename")
        p_alias_add.add_argument("--tokenizer", dest="tokenizer_id")
        p_alias_add.add_argument("--revision", default="main")

        try:
            parsed = parser.parse_args(shlex.split(args))
        except SystemExit:
            return (
                "Invalid usage.\n\n"
                "Examples:\n"
                "  /huggingface search qwen\n"
                "  /huggingface info unsloth/Qwen3.5-9B-GGUF\n"
                "  /huggingface gguf unsloth/Qwen3.5-9B-GGUF\n"
                "  /huggingface alias add qwen35-9b unsloth/Qwen3.5-9B-GGUF\n"
            )

        if parsed.cmd == "search":
            results = self.hf_service.search_models(
                query=parsed.query,
                limit=parsed.limit,
            )

            lines = ["Results:\n"]
            for r in results:
                lines.append(f"{r.repo_id} (downloads: {r.downloads})")

            return "\n".join(lines)

        if parsed.cmd == "info":
            info = self.hf_service.get_model_info(parsed.repo_id)

            lines = [
                f"Repo: {info.repo_id}",
                f"Downloads: {info.downloads}",
                f"Likes: {info.likes}",
                "",
                "Files:",
            ]
            lines.extend(info.siblings[:20])

            return "\n".join(lines)

        if parsed.cmd == "gguf":
            files = self.hf_service.find_gguf_files(parsed.repo_id)

            if not files:
                return "No GGUF files found."

            return "\n".join(files)

        if parsed.cmd == "alias" and parsed.alias_cmd == "add":
            alias = parsed.alias
            repo_id = parsed.repo_id

            filename = parsed.filename
            tokenizer_id = parsed.tokenizer_id

            # auto-pick GGUF if not provided
            if not filename:
                filename = self.hf_service.choose_gguf_file(repo_id)

                if not filename:
                    return "Could not find a GGUF file. Use --file."

            MODEL_ALIASES[alias] = {
                "provider": "huggingface",
                "engine": "gguf",
                "model_id": alias,
                "repo_id": repo_id,
                "filename": filename,
                "tokenizer_id": tokenizer_id,
                "revision": parsed.revision,
            }

            return f"Alias added: {alias}\nRepo: {repo_id}\nFile: {filename}"

        return (
            "Invalid usage.\n\n"
            "Examples:\n"
            "  /huggingface search qwen\n"
            "  /huggingface info unsloth/Qwen3.5-9B-GGUF\n"
            "  /huggingface gguf unsloth/Qwen3.5-9B-GGUF\n"
            "  /huggingface alias add qwen35-9b unsloth/Qwen3.5-9B-GGUF\n"
        )

    async def handle_engine(self, args: str) -> str:
        parser = argparse.ArgumentParser(prog="/engine", add_help=False)
        parser.add_argument("engine", nargs="?")

        try:
            parsed = parser.parse_args(shlex.split(args))
        except SystemExit:
            return "Invalid usage.\n\nExamples:\n  /engine gguf\n  /engine vllm\n"

        if not parsed.engine:
            return "Missing engine.\n\nUsage:\n  /engine <engine_name>\n"

        try:
            await self.primer.load_engine(parsed.engine)
            status = self.primer.status()
            return f"engine loading success\n{status}"

        except Exception as e:
            return f"error: {e}"

    async def handle_load_model(self, args: str = "") -> str:
        parser = argparse.ArgumentParser(prog="/load_model", add_help=False)
        parser.add_argument("model_alias", nargs="?")
        parser.add_argument("--force-reload", action="store_true")

        try:
            parsed = parser.parse_args(shlex.split(args))
        except SystemExit:
            return "Invalid usage.\n\nUsage:\n  /load_model <alias> [--force-reload]\n"

        if not parsed.model_alias:
            return "Missing model alias.\n\nUsage:\n  /load_model <alias>\n"

        alias = parsed.model_alias
        model = MODEL_ALIASES.get(alias)

        if not model:
            return (
                f"Unknown model alias: {alias}\n\n"
                "Use /models to list available aliases.\n"
            )

        spec = JobSpec(
            kind="load_model",
            payload={
                "model_id": model["model_id"],
                "provider": model["provider"],
                "engine": model["engine"],
                "repo_id": model.get("repo_id"),
                "filename": model.get("filename"),
                "revision": model.get("revision", "main"),
                "tokenizer_id": model.get("tokenizer_id"),
                "force_reload": parsed.force_reload,
            },
        )

        def update_cortex_discovery(job):
            metadata = job.result.get("metadata", {})
            model_id = metadata.get("model_id") or model["model_id"]

            service_name = self._service_name_from_target("cortex")

            self.deployment_service.update_service_discovery_metadata(
                service_name=service_name,
                model=model_id,
            )

        job = self.job_manager.start(
            spec=spec,
            runner=lambda job: primer.execute_load_model(job, self.primer),
            on_completed=update_cortex_discovery,
        )

        return (
            "model load accepted\n"
            f"job_id: {job.job_id}\n"
            f"alias: {alias}\n"
            f"model: {model['model_id']}\n"
            f"engine: {model['engine']}\n\n"
            f"Use /job_status {job.job_id} to check status.\n"
            f"Use /attach to follow Cortex logs.\n"
        )

    async def handle_job_status(self, args: str = "") -> str:
        job_id = args.strip()

        if not job_id:
            return "Missing job id.\n\nUsage:\n  /job_status <job_id>\n"

        job = self.job_manager.get(job_id)

        if job is None:
            return f"Job not found: {job_id}\n"

        return json.dumps(job.to_dict(), indent=2, ensure_ascii=False) + "\n"

    async def handle_unload_model(self, args: str) -> str:
        await self.primer.stop()

        discovery_update_text = ""

        try:
            service_name = self._service_name_from_target("cortex")

            updated = self.deployment_service.update_service_discovery_metadata(
                service_name=service_name,
                model="",
            )

            patched = "\n".join(f"  {key}={value}" for key, value in updated.items())

            discovery_update_text = (
                "\n\n"
                f"discovery metadata updated: "
                f"{self.deployment_service.namespace}/{service_name}\n"
                f"{patched}"
            )

        except Exception as exc:
            discovery_update_text = (
                "\n\n"
                f"warning: model unloaded, but failed to update discovery metadata: {exc}"
            )

        return f"{self.primer.status()}{discovery_update_text}"

    async def handle_deploy_pod(self, args: str) -> str:
        parser = argparse.ArgumentParser(prog="/deploy_pod", add_help=False)
        parser.add_argument("role", nargs="?", default=None)
        parser.add_argument("size", nargs="?", default=None)
        parser.add_argument("--alias", dest="alias", default=None)
        parser.add_argument(
            "--platform", choices=["amd64", "arm64", "jetson"], default=None
        )
        parser.add_argument("--node", dest="node", default=None)

        try:
            parsed = parser.parse_args(shlex.split(args))
        except SystemExit:
            return (
                "Invalid usage.\n\n"
                "Usage:\n"
                "  /deploy_pod <role> [size] [--alias name] [--node node_alias]\n\n"
                "Examples:\n"
                "  /deploy_pod cortex light\n"
                "  /deploy_pod llm light\n"
                "  /deploy_pod llm medium\n"
                "  /deploy_pod llm high\n"
                "  /deploy_pod llm light --alias my-llm\n"
                "  /deploy_pod llm light --node orin-node-1\n"
                "  /deploy_pod tool\n"
                "  /deploy_pod memory\n"
            )

        if parsed.role is None:
            return (
                "Missing role.\n\n"
                "Usage:\n"
                "  /deploy_pod <role> [size] [--alias name] [--node node_alias]\n\n"
                "Valid roles:\n"
                "  cortex\n"
                "  llm\n"
                "  tool\n"
                "  memory\n"
            )

        role = parsed.role.lower()

        if role not in {"cortex", "llm", "tool", "memory"}:
            return (
                f"Invalid role: {parsed.role}\n\n"
                "Valid roles:\n"
                "  cortex\n"
                "  llm\n"
                "  tool\n"
                "  memory\n"
            )

        storage_profile = parsed.size.lower() if parsed.size else None

        if role in {"llm", "cortex"} and storage_profile is None:
            return (
                f"Missing {role} size.\n\n"
                "Usage:\n"
                f"  /deploy_pod {role} <light|medium|high> [--alias name] [--node node_alias]\n"
            )

        if role in {"llm", "cortex"} and storage_profile not in {
            "light",
            "medium",
            "high",
        }:
            return (
                f"Invalid {role} size: {parsed.size}\n\n"
                "Valid sizes:\n"
                "  light\n"
                "  medium\n"
                "  high\n"
            )

        if role in {"tool", "memory"} and storage_profile is not None:
            return (
                f"{role} does not use size.\n\n"
                "Usage:\n"
                f"  /deploy_pod {role} [--alias name] [--node node_alias]\n"
            )

        try:
            namespace = self.deployment_service.namespace

            if parsed.alias:
                app_name = pod_naming.normalize_name_part(parsed.alias)

                pod_naming.assert_pod_app_name_available(
                    self.deployment_service.core,
                    self.deployment_service.apps,
                    namespace=namespace,
                    app_name=app_name,
                )
            else:
                app_name = pod_naming.next_available_pod_app_name(
                    self.deployment_service.core,
                    self.deployment_service.apps,
                    namespace=namespace,
                    role=role,
                    size=storage_profile,
                )

            placement = self.deployment_service.resolve_pod_placement(
                role=role,
                platform=parsed.platform,
                explicit_node_alias=parsed.node,
            )

            deployment, service = pod_factory.create_pod_app_specs(
                role=role,
                app_name=app_name,
                platform=placement.platform,
                defaults=self.deployment_service.pod_factory_defaults,
                node_selector=placement.node_selector,
                affinity=placement.affinity,
                pvc_size=pod_template.pvc_size_for_profile(storage_profile),
            )

            self.deployment_service.deploy_pod_app(
                deployment=deployment,
                service=service,
            )

            return (
                "success: deployed pod app\n"
                f"role: {role}\n"
                f"size: {storage_profile or '-'}\n"
                f"deployment: {deployment.name}\n"
                f"service: {service.name}\n"
                f"namespace: {deployment.namespace}\n"
                f"platform: {placement.platform}\n"
                f"node_selector: {placement.node_selector or '{}'}\n"
            )

        except Exception as e:
            return f"error: {e}\n"

    async def handle_delete_pod(self, args: str) -> str:
        parser = argparse.ArgumentParser(prog="/delete_pod", add_help=False)
        parser.add_argument("name", nargs="?", default=None)
        parser.add_argument("--keep-pvc", action="store_true")
        parser.add_argument("--namespace", default=None)

        try:
            parsed = parser.parse_args(shlex.split(args))
        except SystemExit:
            return (
                "Invalid usage.\n\n"
                "Usage:\n"
                "  /delete_pod <name> [--keep-pvc] [--namespace namespace]\n\n"
                "Examples:\n"
                "  /delete_pod llm-light-1\n"
                "  /delete_pod cortex\n"
                "  /delete_pod memory\n"
                "  /delete_pod tool-search-1 --keep-pvc\n"
            )

        if parsed.name is None:
            return (
                "Missing pod app name.\n\n"
                "Usage:\n"
                "  /delete_pod <name> [--keep-pvc] [--namespace namespace]\n"
            )

        try:
            app_name = pod_naming.normalize_name_part(parsed.name)
            namespace = parsed.namespace or self.deployment_service.namespace

            result = self.deployment_service.delete_pod_app(
                app_name=app_name,
                namespace=namespace,
                delete_pvc=not parsed.keep_pvc,
            )

            lines = [
                "success: delete requested",
                f"app: {app_name}",
                f"namespace: {namespace}",
            ]

            for item in result:
                lines.append(f"{item['kind']}: {item['name']} -> {item['status']}")

            return "\n".join(lines) + "\n"

        except Exception as e:
            return f"error: {e}\n"

    async def handle_list_pods(self, args: str) -> str:
        parser = argparse.ArgumentParser(prog="/list_pods", add_help=False)
        parser.add_argument("--namespace", default=None)
        parser.add_argument("--verbose", action="store_true")

        try:
            parsed = parser.parse_args(shlex.split(args))
        except SystemExit:
            return (
                "Invalid usage.\n\n"
                "Usage:\n"
                "  /list_pods [--namespace namespace] [--verbose]\n\n"
                "Examples:\n"
                "  /list_pods\n"
                "  /list_pods --verbose\n"
                "  /list_pods --namespace orin\n"
            )

        try:
            namespace = parsed.namespace or self.deployment_service.namespace

            apps = self.deployment_service.list_pod_apps(
                namespace=namespace,
            )

            if not apps:
                return f"No pod apps found in namespace: {namespace}\n"

            lines: list[str] = [
                f"Pod apps in namespace: {namespace}",
                "",
                "NAME                 KIND      ROLE      IMAGE                         PODS     SERVICE              PVC",
                "----                 ----      ----      -----                         ----     -------              ---",
            ]

            for app in apps:
                pods_text = f"{app['ready_pods']}/{app['total_pods']}"

                lines.append(
                    f"{app['name']:<20} "
                    f"{app['kind']:<9} "
                    f"{app['role']:<9} "
                    f"{app['image']:<29} "
                    f"{pods_text:<8} "
                    f"{app['service']:<20} "
                    f"{app['pvc']}"
                )

                if parsed.verbose:
                    for pod in app["pods"]:
                        lines.append(
                            f"  - {pod['name']} | phase={pod['phase']} | ready={pod['ready']} | node={pod['node']}"
                        )

            return "\n".join(lines) + "\n"

        except Exception as e:
            return f"error: {e}\n"

    async def handle_install_k3s(self, args: str) -> str:
        parser = argparse.ArgumentParser(prog="/install_k3s", add_help=False)
        parser.add_argument("alias", nargs="?", default=None)
        parser.add_argument("--mode", choices=["server", "agent"], default="agent")

        try:
            parsed = parser.parse_args(shlex.split(args))
        except SystemExit:
            return (
                "Invalid usage.\n\n"
                "Usage:\n"
                "  /install_k3s <node_alias> [--mode agent|server]\n\n"
                "Examples:\n"
                "  /install_k3s orin-node-1\n"
                "  /install_k3s orin-node-2 --mode agent\n"
            )

        if parsed.alias is None:
            return (
                "Missing node alias.\n\n"
                "Usage:\n"
                "  /install_k3s <node_alias> [--mode agent|server]\n"
            )

        try:
            result = self.deployment_service.install_kubernetes(
                parsed.alias,
                mode=parsed.mode,
            )

            return (
                "success: k3s install completed\n"
                f"alias: {result.alias}\n"
                f"node_name: {result.node_name}\n"
                f"mode: {result.mode}\n"
                f"server_url: {result.server_url or '-'}\n"
            )

        except Exception as e:
            return f"error: {e}\n"

    async def handle_add_node(self, args: str) -> str:
        parser = argparse.ArgumentParser(prog="/add_node", add_help=False)
        parser.add_argument("alias", nargs="?", default=None)

        parser.add_argument("--host", default=None)
        parser.add_argument("--ip", default=None)
        parser.add_argument(
            "--platform", choices=["amd64", "arm64", "jetson"], default=None
        )

        parser.add_argument("--user", default="ubuntu")
        parser.add_argument("--ssh-key", default="/data/ssh/id_ed25519")
        parser.add_argument("--port", type=int, default=22)

        parser.add_argument("--gpu", action="store_true")
        parser.add_argument("--no-gpu", action="store_true")

        parser.add_argument("--k3s-node-name", default=None)

        try:
            parsed = parser.parse_args(shlex.split(args))
        except SystemExit:
            return (
                "Invalid usage.\n\n"
                "Usage:\n"
                "  /add_node <alias> --host <host> --ip <ip> --platform <amd64|arm64|jetson> [options]\n\n"
                "Examples:\n"
                "  /add_node orin-node-1 --host orin-node-1 --ip 192.168.50.11 --platform jetson --gpu\n"
                "  /add_node worker-x86 --host worker-x86 --ip 192.168.50.20 --platform amd64\n"
                "  /add_node orin-node-2 --host orin-node-2 --ip 192.168.50.12 --platform jetson --user jetson --ssh-key ~/.ssh/id_ed25519\n"
            )

        if parsed.alias is None:
            return (
                "Missing alias.\n\n"
                "Usage:\n"
                "  /add_node <alias> --host <host> --ip <ip> --platform <amd64|arm64|jetson>\n"
            )

        if parsed.host is None:
            return (
                "Missing host.\n\n"
                "Usage:\n"
                "  /add_node <alias> --host <host> --ip <ip> --platform <amd64|arm64|jetson>\n"
            )

        if parsed.ip is None:
            return (
                "Missing ip.\n\n"
                "Usage:\n"
                "  /add_node <alias> --host <host> --ip <ip> --platform <amd64|arm64|jetson>\n"
            )

        if parsed.platform is None:
            return "Missing platform.\n\nValid platforms:\n  amd64\n  arm64\n  jetson\n"

        if parsed.gpu and parsed.no_gpu:
            return "Invalid usage: use either --gpu or --no-gpu, not both.\n"

        try:
            alias = pod_naming.normalize_name_part(parsed.alias)

            k3s_node_name = (
                pod_naming.normalize_name_part(parsed.k3s_node_name)
                if parsed.k3s_node_name
                else alias
            )

            nvidia_gpu = parsed.gpu

            if parsed.platform == "jetson" and not parsed.no_gpu:
                nvidia_gpu = True

            node = self.deployment_service.add_node(
                alias=alias,
                host=parsed.host,
                ip=parsed.ip,
                platform=parsed.platform,
                ssh_user=parsed.user,
                ssh_key=parsed.ssh_key,
                ssh_port=parsed.port,
                nvidia_gpu=nvidia_gpu,
                k3s_node_name=k3s_node_name,
            )

            return (
                "success: node added to inventory\n"
                f"alias: {node.alias}\n"
                f"host: {node.machine_settings.host}\n"
                f"ip: {node.machine_settings.ip}\n"
                f"platform: {node.machine_settings.platform}\n"
                f"nvidia_gpu: {node.machine_settings.nvidia_gpu}\n"
                f"k3s_node_name: {node.kubernetes_settings.k3s_node_name if node.kubernetes_settings else '-'}\n"
                f"ssh_user: {node.ssh_config.default_user}\n"
                f"ssh_port: {node.ssh_config.port}\n"
            )

        except Exception as e:
            return f"error: {e}\n"

    async def handle_remove_node(self, args: str) -> str:
        parser = argparse.ArgumentParser(prog="/remove_node", add_help=False)
        parser.add_argument("alias", nargs="?", default=None)

        try:
            parsed = parser.parse_args(shlex.split(args))
        except SystemExit:
            return (
                "Invalid usage.\n\n"
                "Usage:\n"
                "  /remove_node <alias>\n\n"
                "Examples:\n"
                "  /remove_node orin-node-1\n"
                "  /remove_node worker-x86\n"
            )

        if parsed.alias is None:
            return "Missing alias.\n\nUsage:\n  /remove_node <alias>\n"

        try:
            alias = pod_naming.normalize_name_part(parsed.alias)

            existing = self.deployment_service.get_node(alias)

            if existing is None:
                return f"Node not found in inventory: {alias}\n"

            removed = self.deployment_service.remove_node(alias)

            if not removed:
                return f"Node not removed: {alias}\n"

            return (
                "success: node removed from inventory\n"
                f"alias: {alias}\n"
                "note: this did not uninstall k3s from the machine\n"
            )

        except Exception as e:
            return f"error: {e}\n"

    async def handle_label_node(self, args: str) -> str:
        parser = argparse.ArgumentParser(prog="/label_node", add_help=False)
        parser.add_argument("alias", nargs="?", default=None)
        parser.add_argument(
            "--role",
            dest="roles",
            action="append",
            choices=["cortex", "llm", "tool", "memory"],
            default=[],
        )

        try:
            parsed = parser.parse_args(shlex.split(args))
        except SystemExit:
            return (
                "Invalid usage.\n\n"
                "Usage:\n"
                "  /label_node <alias> [--role cortex|llm|tool|memory]\n\n"
                "Examples:\n"
                "  /label_node master-node --role cortex --role memory\n"
                "  /label_node orin-1 --role llm\n"
                "  /label_node orin-2 --role tool\n"
            )

        if parsed.alias is None:
            return (
                "Missing node alias.\n\n"
                "Usage:\n"
                "  /label_node <alias> [--role cortex|llm|tool|memory]\n"
            )

        try:
            labels = self.deployment_service.label_node(
                alias=parsed.alias,
                priority_roles=parsed.roles,
            )

            lines = [
                "success: node labels synced",
                f"alias: {parsed.alias}",
                "labels:",
            ]

            for key in sorted(labels):
                lines.append(f"  {key}={labels[key]}")

            return "\n".join(lines) + "\n"

        except Exception as e:
            return f"error: {e}\n"

    async def handle_update_discovery_meta(self, args: str) -> str:
        parser = argparse.ArgumentParser(
            prog="/update_discovery_meta",
            add_help=False,
        )

        parser.add_argument(
            "target",
            nargs="?",
            help="App name or service name. App names are converted to <app>-service.",
        )

        parser.add_argument(
            "--namespace",
            "-n",
            default=None,
            help="Kubernetes namespace. Defaults to DeploymentService namespace.",
        )

        parser.add_argument(
            "--service",
            action="store_true",
            help="Treat target as exact service name; do not append -service.",
        )

        parser.add_argument(
            "--model",
            default=None,
            help="Model id/name to write to orin.ai/model.",
        )

        parser.add_argument(
            "--capability",
            action="append",
            default=[],
            help="Capability to add. Can be repeated.",
        )

        parser.add_argument(
            "--capabilities",
            action="append",
            default=[],
            help="Comma-separated capabilities, for example text-generation,json.",
        )

        parser.add_argument(
            "--modality",
            action="append",
            default=[],
            help="Modality to add. Can be repeated.",
        )

        parser.add_argument(
            "--modalities",
            action="append",
            default=[],
            help="Comma-separated modalities, for example text,image.",
        )

        parser.add_argument(
            "--annotation",
            "-a",
            action="append",
            default=[],
            help="Extra annotation as key=value. Can be repeated.",
        )

        parser.add_argument(
            "--help",
            action="store_true",
            help="Show usage.",
        )

        try:
            ns = parser.parse_args(shlex.split(args))
        except SystemExit as exc:
            return (
                f"Usage:\n"
                f"  /update_discovery_meta <app-or-service> "
                f"[--model MODEL] "
                f"[--capability CAP] "
                f"[--capabilities a,b] "
                f"[--modality MOD] "
                f"[--modalities text,image] "
                f"[-a key=value] "
                f"[--namespace NAMESPACE] "
                f"[--service]\n\n"
                f"Error: {exc}"
            )

        if ns.help or not ns.target:
            return (
                "Usage:\n"
                "  /update_discovery_meta <app-or-service> [options]\n\n"
                "Options:\n"
                "  --model MODEL                  Set orin.ai/model\n"
                "  --capability CAP               Add one capability; repeatable\n"
                "  --capabilities a,b             Add comma-separated capabilities\n"
                "  --modality MOD                 Add one modality; repeatable\n"
                "  --modalities text,image        Add comma-separated modalities\n"
                "  -a, --annotation key=value     Add extra annotation; repeatable\n"
                "  -n, --namespace NAMESPACE      Override namespace\n"
                "  --service                      Treat target as exact service name\n\n"
                "Examples:\n"
                "  /update_discovery_meta llm-light-1 --model qwen35-4b "
                "--capabilities chat,json --modalities text\n"
                "  /update_discovery_meta llm-light-1-service --service "
                "--model qwen35-4b\n"
                "  /update_discovery_meta llm-light-1 "
                "-a orin.ai/context_window=4096 "
                "-a orin.ai/max_output_tokens=1024"
            )

        try:
            service_name = self._service_name_from_target(
                ns.target,
                exact_service=ns.service,
            )

            capabilities = self._merge_repeated_list_args(
                repeated=ns.capability,
                csv_values=ns.capabilities,
            )

            modalities = self._merge_repeated_list_args(
                repeated=ns.modality,
                csv_values=ns.modalities,
            )

            extra_annotations = self._parse_extra_annotations(ns.annotation)

            updated = self.deployment_service.update_service_discovery_metadata(
                service_name=service_name,
                namespace=ns.namespace,
                model=ns.model,
                capabilities=capabilities,
                modalities=modalities,
                extra_annotations=extra_annotations,
            )

        except (SystemExit, ValueError, DeploymentError) as exc:
            return f"Failed to update discovery metadata: {exc}"

        namespace = ns.namespace or self.deployment_service.namespace

        lines = [
            f"Updated discovery metadata for service {namespace}/{service_name}.",
            "",
            "Patched annotations:",
        ]

        for key, value in updated.items():
            lines.append(f"  {key}={value}")

        return "\n".join(lines)

    async def handle_list_nodes(self, args: str) -> str:
        parser = argparse.ArgumentParser(prog="/list_nodes", add_help=False)
        parser.add_argument("--verbose", action="store_true")

        try:
            parsed = parser.parse_args(shlex.split(args))
        except SystemExit:
            return (
                "Invalid usage.\n\n"
                "Usage:\n"
                "  /list_nodes [--verbose]\n\n"
                "Examples:\n"
                "  /list_nodes\n"
                "  /list_nodes --verbose\n"
            )

        try:
            nodes = self.deployment_service.list_inventory_nodes(
                verbose=parsed.verbose,
            )

            if not nodes:
                return "No nodes in inventory.\n"

            lines: list[str] = [
                "Inventory nodes",
                "",
                "ALIAS              HOST               IP              PLATFORM  GPU    K3S_NODE           K8S",
                "-----              ----               --              --------  ---    --------           ---",
            ]

            for node in nodes:
                lines.append(
                    f"{node['alias']:<18} "
                    f"{node['host']:<18} "
                    f"{node['ip']:<15} "
                    f"{node['platform']:<9} "
                    f"{node['nvidia_gpu']:<6} "
                    f"{node['k3s_node_name']:<18} "
                    f"{node['k8s_status']}"
                )

                if parsed.verbose:
                    labels = node.get("labels") or {}

                    if labels:
                        lines.append("  labels:")
                        for key in sorted(labels):
                            if key.startswith("orin."):
                                lines.append(f"    {key}={labels[key]}")

            return "\n".join(lines) + "\n"

        except Exception as e:
            return f"error: {e}\n"

    async def handle_show(self, args: str) -> str:
        parser = argparse.ArgumentParser(prog="/show", add_help=False)
        parser.add_argument("role", nargs="?", default=None)
        parser.add_argument("--raw", action="store_true")
        parser.add_argument("--verbose", "-v", action="store_true")
        parser.add_argument("--help", action="store_true")

        try:
            parsed = parser.parse_args(shlex.split(args))
        except SystemExit:
            return (
                "Usage:\n"
                "  /show [role] [--verbose] [--raw]\n\n"
                "Examples:\n"
                "  /show\n"
                "  /show llm\n"
                "  /show cortex --verbose\n"
                "  /show --raw\n"
            )

        if parsed.help:
            return (
                "Usage:\n"
                "  /show [role] [--verbose] [--raw]\n\n"
                "Examples:\n"
                "  /show\n"
                "  /show llm\n"
                "  /show cortex --verbose\n"
                "  /show --raw\n"
            )

        if parsed.role:
            backends = self.routing_policy.get_all_candidates(role=parsed.role)
        else:
            backends = self._list_discovered_backends()

        if not backends:
            if parsed.role:
                return f"No discovered backends for role: {parsed.role}"
            return "No discovered backends."

        if parsed.raw:
            return "\n".join(str(backend.to_dict()) for backend in backends)

        return formatting.format_backend_list(
            backends=backends, role_filter=parsed.role, verbose=parsed.verbose
        )

    async def handle_backend_update_discovery_meta(self, backend, args: str) -> str:
        args = f"{backend.service_name} --service {args}".strip()
        return await self.handle_update_discovery_meta(args)

    async def handle_backend_models(self, backend, args: str) -> str:
        models_path = getattr(backend, "models_path", None)

        if not models_path:
            return f"Backend {backend.name} does not expose a models endpoint."

        try:
            data = await self.backend_client.get_json(
                url=f"{backend.url}{models_path}",
            )
        except Exception as exc:
            return f"Failed to read models from {backend.name}: {exc}"

        return json.dumps(data, indent=2, ensure_ascii=False)

    async def handle_backend_load_model(self, backend, args: str) -> str:
        if backend.role not in {"llm", "cortex"}:
            return f"Backend {backend.name} does not support model loading."

        alias = args.strip()

        if not alias:
            return "Missing model alias.\n\nUsage:\n  /load_model <alias>\n"

        model = MODEL_ALIASES.get(alias)

        if not model:
            return f"Unknown model alias: {alias}"

        try:
            await self.backend_client.post_json(
                url=f"{backend.url}/engine",
                payload={"engine": model["engine"]},
            )

            if model["engine"] == "gguf":
                payload = {
                    "provider": model["provider"],
                    "model_id": model["model_id"],
                    "repo_id": model["repo_id"],
                    "filename": model["filename"],
                    "revision": model.get("revision", "main"),
                    "tokenizer_id": model.get("tokenizer_id"),
                    "force_reload": False,
                }
            else:
                payload = {
                    "provider": model["provider"],
                    "model_id": model["model_id"],
                    "force_reload": False,
                }

            rsp = await self.backend_client.post_json(
                url=f"{backend.url}/load",
                payload=payload,
            )

        except Exception as exc:
            return f"Failed to start model load on {backend.name}: {exc}"

        return (
            f"Model load accepted on backend: {backend.name}\n"
            f"model: {model['model_id']}\n"
            f"job_id: {rsp.get('job_id')}\n"
            f"status: {rsp.get('status')}\n\n"
            f"Use /job_status {rsp.get('job_id')} to check status.\n"
            f"Use /attach to stream logs from this backend.\n"
        )

    async def handle_backend_job_status(self, backend, args: str) -> str:
        job_id = args.strip()

        if not job_id:
            return "Missing job id.\n\nUsage:\n  /load_status <job_id>\n"

        try:
            data = await self.backend_client.get_json(
                url=f"{backend.url}/jobs/{job_id}",
            )
        except Exception as exc:
            return f"Failed to read job status from {backend.name}: {exc}"

        lines = [
            f"Job on backend: {backend.name}",
            json.dumps(data, indent=2, ensure_ascii=False),
        ]

        if data.get("status") == "completed":
            result = data.get("result") or {}
            model_id = result.get("model_id") or data.get("payload", {}).get("model_id")

            if model_id:
                try:
                    updated = self.deployment_service.update_service_discovery_metadata(
                        service_name=backend.service_name,
                        namespace=backend.namespace,
                        model=model_id,
                    )

                    lines.append("")
                    lines.append("Updated discovery metadata:")
                    for key, value in updated.items():
                        lines.append(f"{key}={value}")

                except Exception as exc:
                    lines.append("")
                    lines.append(f"warning: failed to update discovery metadata: {exc}")

        return "\n".join(lines) + "\n"

    async def handle_backend_health(self, backend, args: str) -> str:
        health_path = getattr(backend, "health_path", None) or "/health"

        try:
            data = await self.backend_client.get_json(
                url=f"{backend.url}{health_path}",
            )
        except Exception as exc:
            return f"Failed to read health from {backend.name}: {exc}"

        return json.dumps(data, indent=2, ensure_ascii=False)

    async def handle_backend_unload_model(self, backend, args: str) -> str:
        if backend.role not in {"llm", "cortex"}:
            return f"Backend {backend.name} does not support model unloading."

        try:
            data = await self.backend_client.post_json(
                url=f"{backend.url}/unload",
                payload={},
            )

            updated = self.deployment_service.update_service_discovery_metadata(
                service_name=backend.service_name,
                namespace=backend.namespace,
                model="",
            )

        except Exception as exc:
            return f"Failed to unload model on {backend.name}: {exc}"

        lines = [
            f"Unloaded model on backend: {backend.name}",
            "",
            "Backend response:",
            json.dumps(data, indent=2, ensure_ascii=False),
            "",
            "Updated discovery metadata:",
        ]

        for key, value in updated.items():
            lines.append(f"  {key}={value}")

        return "\n".join(lines)

    async def handle_backend_engine(self, backend, args: str) -> str:
        if backend.role not in {"llm", "cortex"}:
            return f"Backend {backend.name} does not support backend engine loading."

        parser = argparse.ArgumentParser(prog="/backend", add_help=False)
        parser.add_argument("backend", nargs="?")

        try:
            parsed = parser.parse_args(shlex.split(args))
        except SystemExit:
            return "Invalid usage.\n\nExamples:\n  /backend gguf\n  /backend vllm\n"

        if not parsed.backend:
            return "Missing backend.\n\nUsage:\n  /backend <backend_name>\n"

        try:
            data = await self.backend_client.post_json(
                url=f"{backend.url}/backend",
                payload={"backend": parsed.backend},
            )
        except Exception as exc:
            return f"Failed to load backend engine on {backend.name}: {exc}"

        return (
            f"Backend engine loaded on {backend.name}: {parsed.backend}\n\n"
            f"{json.dumps(data, indent=2, ensure_ascii=False)}"
        )

    def handle_help(self, args: str) -> str:
        return (
            "Orin Command Interface\n\n"
            "Use /commands to list all commands.\n"
            "Use /<command> [args] to execute.\n\n"
            "Example:\n"
            "  /deploy_pod llm --node orin-node-1\n"
        )
    
    # ---------------------------------------------------------------------------
    # Execute Functions
    # ---------------------------------------------------------------------------

    async def execute_harness_action(
        self,
        action: str,
        *,
        mode: CommandMode,
        args: str = "",
    ) -> str:
        command = self._find_harness_action(
            action=action,
            mode=mode,
        )

        if command is None:
            raise ValueError(f"No available harness action: {action}")

        if not command.safe_info_action:
            raise ValueError(f"Harness action is not safe to execute: {action}")

        if command.suggest_only:
            raise ValueError(f"Harness action is suggest-only: {action}")

        result = command.handler(args)

        if inspect.isawaitable(result):
            result = await result

        if inspect.isasyncgen(result):
            chunks: list[str] = []
            async for chunk in result:
                chunks.append(str(chunk))
            return "".join(chunks)

        return str(result)
    
    def _find_harness_action(
        self,
        *,
        action: str,
        mode: CommandMode,
    ) -> Command | None:
        action = action.strip()

        if not action:
            return None

        for _, command in self._walk_commands(self.root_folder):
            if mode not in command.modes:
                continue

            if command.harness_action == action:
                return command

            # Optional convenience: allow "/list_pods" as well as "list_pods".
            if command.command == action:
                return command

        return None