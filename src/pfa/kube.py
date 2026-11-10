"""Kubernetes API access. The only module that loads kubeconfig."""

from __future__ import annotations

from dataclasses import dataclass

from kubernetes import client, config
from kubernetes.config.config_exception import ConfigException


class KubeConfigError(RuntimeError):
    pass


@dataclass
class KubeClients:
    core: client.CoreV1Api
    apps: client.AppsV1Api
    timeout: float


def load_clients(context: str | None = None, timeout: float = 10.0) -> KubeClients:
    """Load kubeconfig (optionally a named context), falling back to in-cluster config."""
    try:
        config.load_kube_config(context=context)
    except ConfigException as exc:
        if context:
            raise KubeConfigError(f"cannot load kubeconfig context {context!r}: {exc}") from exc
        try:
            config.load_incluster_config()
        except ConfigException:
            raise KubeConfigError(f"no usable kubeconfig or in-cluster config: {exc}") from exc
    except FileNotFoundError as exc:
        raise KubeConfigError(f"kubeconfig not found: {exc}") from exc
    return KubeClients(core=client.CoreV1Api(), apps=client.AppsV1Api(), timeout=timeout)
