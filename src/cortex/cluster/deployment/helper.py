from kubernetes import client
from kubernetes.client.exceptions import ApiException


def deployment_exists(
    apps: client.AppsV1Api,
    *,
    namespace: str,
    name: str,
) -> bool:
    try:
        apps.read_namespaced_deployment(
            name=name,
            namespace=namespace,
        )
        return True
    except ApiException as exc:
        if exc.status == 404:
            return False
        raise


def pvc_exists(
    core: client.CoreV1Api,
    *,
    namespace: str,
    name: str,
) -> bool:
    try:
        core.read_namespaced_persistent_volume_claim(
            name=name,
            namespace=namespace,
        )
        return True
    except ApiException as exc:
        if exc.status == 404:
            return False
        raise


def service_exists(
    core: client.CoreV1Api,
    *,
    namespace: str,
    name: str,
) -> bool:
    try:
        core.read_namespaced_service(
            name=name,
            namespace=namespace,
        )
        return True
    except ApiException as exc:
        if exc.status == 404:
            return False
        raise
