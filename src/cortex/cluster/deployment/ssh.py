import shutil
import subprocess
from pathlib import Path

from cortex.cluster.deployment.types import Node


class SSHError(RuntimeError):
    pass


def check_ssh_requirements(node: Node) -> None:
    if shutil.which("ssh") is None:
        raise SSHError("'ssh' is not installed in the current runtime.")

    if shutil.which("scp") is None:
        raise SSHError("'scp' is not installed in the current runtime.")

    if node.ssh_config.auth_method == "password" and shutil.which("sshpass") is None:
        raise SSHError(
            "Password auth requires 'sshpass' to be installed in the current runtime."
        )


def _expand_key_path(path: str | None) -> str | None:
    if path is None:
        return None

    return str(Path(path).expanduser())


def _target_host(node: Node, *, prefer_ip: bool = False) -> str:
    machine = node.machine_settings

    if prefer_ip:
        return machine.ip

    return machine.host


def _base_ssh_cmd(
    node: Node,
    *,
    password: str | None = None,
    prefer_ip: bool = False,
    connect_timeout_seconds: int = 10,
) -> list[str]:
    check_ssh_requirements(node)

    ssh = node.ssh_config
    target = _target_host(node, prefer_ip=prefer_ip)
    user_at_host = f"{ssh.default_user}@{target}"

    common_options = [
        "-p",
        str(ssh.port),
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"ConnectTimeout={connect_timeout_seconds}",
    ]

    if ssh.auth_method == "ssh_key":
        key = _expand_key_path(ssh.default_key)

        if key is None:
            raise SSHError(f"No SSH key configured for node {node.alias}")

        return [
            "ssh",
            "-i",
            key,
            "-o",
            "BatchMode=yes",
            *common_options,
            user_at_host,
        ]

    if ssh.auth_method == "password":
        if not password:
            raise SSHError(f"No SSH password provided for node {node.alias}")

        return [
            "sshpass",
            "-p",
            password,
            "ssh",
            *common_options,
            user_at_host,
        ]

    raise SSHError(f"Unsupported SSH auth method: {ssh.auth_method}")


def run_ssh(
    node: Node,
    command: str,
    *,
    check: bool = True,
    password: str | None = None,
    prefer_ip: bool = False,
    timeout_seconds: int | None = None,
    connect_timeout_seconds: int = 10,
) -> subprocess.CompletedProcess[str]:
    cmd = _base_ssh_cmd(
        node,
        password=password,
        prefer_ip=prefer_ip,
        connect_timeout_seconds=connect_timeout_seconds,
    ) + [command]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )

    if check and result.returncode != 0:
        raise SSHError(
            f"SSH command failed on node {node.alias}: {command}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )

    return result


def scp_from(
    node: Node,
    *,
    remote_path: str,
    local_path: str,
    password: str | None = None,
    prefer_ip: bool = False,
    connect_timeout_seconds: int = 10,
) -> None:
    check_ssh_requirements(node)

    ssh = node.ssh_config
    target = _target_host(node, prefer_ip=prefer_ip)
    user_at_host = f"{ssh.default_user}@{target}"

    common_options = [
        "-P",
        str(ssh.port),
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"ConnectTimeout={connect_timeout_seconds}",
    ]

    if ssh.auth_method == "ssh_key":
        key = _expand_key_path(ssh.default_key)

        if key is None:
            raise SSHError(f"No SSH key configured for node {node.alias}")

        cmd = [
            "scp",
            "-i",
            key,
            *common_options,
            f"{user_at_host}:{remote_path}",
            local_path,
        ]

    elif ssh.auth_method == "password":
        if not password:
            raise SSHError(f"No SSH password provided for node {node.alias}")

        cmd = [
            "sshpass",
            "-p",
            password,
            "scp",
            *common_options,
            f"{user_at_host}:{remote_path}",
            local_path,
        ]

    else:
        raise SSHError(f"Unsupported SSH auth method: {ssh.auth_method}")

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        raise SSHError(
            f"SCP from node {node.alias} failed.\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )


def scp_to(
    node: Node,
    *,
    local_path: str,
    remote_path: str,
    password: str | None = None,
    prefer_ip: bool = False,
    connect_timeout_seconds: int = 10,
) -> None:
    check_ssh_requirements(node)

    ssh = node.ssh_config
    target = _target_host(node, prefer_ip=prefer_ip)
    user_at_host = f"{ssh.default_user}@{target}"

    common_options = [
        "-P",
        str(ssh.port),
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"ConnectTimeout={connect_timeout_seconds}",
    ]

    if ssh.auth_method == "ssh_key":
        key = _expand_key_path(ssh.default_key)

        if key is None:
            raise SSHError(f"No SSH key configured for node {node.alias}")

        cmd = [
            "scp",
            "-i",
            key,
            *common_options,
            local_path,
            f"{user_at_host}:{remote_path}",
        ]

    elif ssh.auth_method == "password":
        if not password:
            raise SSHError(f"No SSH password provided for node {node.alias}")

        cmd = [
            "sshpass",
            "-p",
            password,
            "scp",
            *common_options,
            local_path,
            f"{user_at_host}:{remote_path}",
        ]

    else:
        raise SSHError(f"Unsupported SSH auth method: {ssh.auth_method}")

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        raise SSHError(
            f"SCP to node {node.alias} failed.\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
