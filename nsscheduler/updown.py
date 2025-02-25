import logging
import time
import re
from enum import Enum

import kubernetes
from kubernetes.utils.quantity import parse_quantity

from nsscheduler.data_models.internal import NamespaceState


class NamespaceAction(Enum):
    UP = "up"
    DOWN = "down"


protected_namespaces = ("kube-system",)
updown_annotation = "ns.scheduler/replicas"
scale_up_counters: dict[str, int] = {}
ns_state_cache = {}
ns_state_cache_update_time = {}


def kube_init(args):
    # initialize kubernetes client
    if args.incluster:
        kubernetes.config.load_incluster_config()
    else:
        kubernetes.config.load_kube_config(context=args.context)


def wait_on_batch_full(ns, batch_size, batch_interval):
    global scale_up_counters
    scale_up_counters[ns] = scale_up_counters.get(ns, 0) + 1
    if scale_up_counters[ns] > batch_size and batch_interval > 0:
        logging.info(f"Waiting {batch_interval} seconds before scaling up next workload in namespace {ns}")
        time.sleep(batch_interval)
        scale_up_counters[ns] = 1


async def up(namespaces: list, batch_size: int = 0, batch_timeout: int = 0) -> None:
    """
    Start up resources from the namespaces listed in batches with timeout between batches
    If batch_size or batch_timeout are 0 then no batching applied and all the resources are
    started at once. Namespaces will be processed in the order of the list.

    :param namespaces: list of namespace names possibly specified with regexps
    :param batch_size: number of resources to scale up simultaneously
    :param batch_timeout: delay in seconds between batches
    """
    logging.debug(f"Starting up namespaces: {namespaces}")

    app_v1 = kubernetes.client.AppsV1Api()

    for ns in resolve_namespaces(namespaces):
        deployments = app_v1.list_namespaced_deployment(ns, watch=False)
        stateful_sets = app_v1.list_namespaced_stateful_set(ns, watch=False)

        logging.info(f"Starting up namespace '{ns}'")

        for ss in stateful_sets.items:
            wait_on_batch_full(ns, batch_size, batch_timeout)
            modify_workload(NamespaceAction.UP, ss, "StatefulSet", app_v1.patch_namespaced_stateful_set)
        for d in deployments.items:
            wait_on_batch_full(ns, batch_size, batch_timeout)
            modify_workload(NamespaceAction.UP, d, "Deployment", app_v1.patch_namespaced_deployment)


async def down(namespaces: list) -> None:
    """
    Shut down resources from the namespaces listed. Namespaces will be processed in
    reverse order.

    :param namespaces: list of namespace names possibly specified with regexps
    """
    logging.debug(f"Shutting down namespaces: {namespaces}")

    app_v1 = kubernetes.client.AppsV1Api()

    for ns in reversed(resolve_namespaces(namespaces)):
        logging.info(f"Shut down namespace '{ns}'")

        deployments = app_v1.list_namespaced_deployment(ns, watch=False)
        stateful_sets = app_v1.list_namespaced_stateful_set(ns, watch=False)

        for d in deployments.items:
            modify_workload(NamespaceAction.DOWN, d, "Deployment", app_v1.patch_namespaced_deployment)
        for ss in stateful_sets.items:
            modify_workload(NamespaceAction.DOWN, ss, "StatefulSet", app_v1.patch_namespaced_stateful_set)


def resolve_namespaces(namespaces: list) -> list:
    """
    Resolve Namespaces

    This method takes a list of namespace patterns and returns a list of resolved namespaces that match the patterns.

    :param namespaces: A list of namespace patterns to resolve.
    :return: A list of resolved namespaces.

    Example Usage:

    ```python
    namespaces = ['default', 'test-*']
    resolved = resolve_namespaces(namespaces)
    print(resolved)
    ```
    Expected Output:

    ```python
    ['default', 'test-1', 'test-2']
    ```
    """
    v1 = kubernetes.client.CoreV1Api()
    ns_list = v1.list_namespace()
    all_namespaces = [ns.metadata.name for ns in ns_list.items]

    resolved_namespaces = []
    for pattern in namespaces:
        compiled_pattern = re.compile(f"^{pattern}$")
        matching_namespaces = [n for n in all_namespaces if compiled_pattern.match(n)]
        resolved_namespaces.extend(matching_namespaces)
    return resolved_namespaces


async def get_state(namespaces: list) -> dict[str, NamespaceState]:
    """
    Returns current state of the namespaces.

    :param namespaces: list of namespace names possibly specified with regexps
    """
    logging.debug(f"Getting state of namespaces: {namespaces}")

    def sum(workloads):
        replicas = 0
        cpu = 0
        memory = 0
        for d in workloads:
            replicas += d.spec.replicas
            for c in d.spec.template.spec.containers:
                if c.resources.requests:
                    memory += parse_quantity(c.resources.requests.get("memory", 0)) * d.spec.replicas
                    cpu += parse_quantity(c.resources.requests.get("cpu", 0)) * d.spec.replicas

        return replicas, cpu, memory

    app_v1 = kubernetes.client.AppsV1Api()

    state = {}

    for ns in resolve_namespaces(namespaces):
        if ns in ns_state_cache:
            if ns_state_cache_update_time[ns] + 3 > time.time():
                logging.debug(f"Getting cached state of namespace '{ns}'")
                state[ns] = ns_state_cache[ns]
                continue

        deployments = app_v1.list_namespaced_deployment(ns, watch=False)
        stateful_sets = app_v1.list_namespaced_stateful_set(ns, watch=False)

        d_replicas, d_cpu, d_memory = sum(deployments.items)
        s_replicas, s_cpu, s_memory = sum(stateful_sets.items)

        state[ns] = NamespaceState(pods=d_replicas + s_replicas, cpu=d_cpu + s_cpu, memory=d_memory + s_memory)
        ns_state_cache[ns] = state[ns]
        ns_state_cache_update_time[ns] = time.time()

    logging.info(f"State: '{state}'")
    return state


def modify_workload(action: NamespaceAction, workload, kind: str, updater):
    current_replicas = workload.spec.replicas
    before_down_replicas = int(workload.metadata.annotations.get(updown_annotation, 1))
    desired_replicas = current_replicas
    if action == NamespaceAction.DOWN:
        desired_replicas = 0
    elif action == NamespaceAction.UP and current_replicas == 0:
        desired_replicas = before_down_replicas
    patch = {}

    if action == NamespaceAction.DOWN:
        if current_replicas > 0 or updown_annotation not in workload.metadata.annotations:
            patch["metadata"] = {"annotations": {updown_annotation: str(current_replicas)}}

    if current_replicas != desired_replicas:
        patch["spec"] = {"replicas": desired_replicas}

    if patch:
        try:
            updater(name=workload.metadata.name, namespace=workload.metadata.namespace, body=patch, pretty="true")
            logging.info(
                f"{kind} '{workload.metadata.namespace}/{workload.metadata.name}' was"
                f" scaled to {desired_replicas} replicas"
            )
        except Exception as e:
            logging.error(
                f"Failed to update {kind} " f"'{workload.metadata.namespace}/{workload.metadata.name}': {str(e)}"
            )
    else:
        logging.info(
            f"{kind} '{workload.metadata.namespace}/{workload.metadata.name}' was left intact"
            f" ({current_replicas} replicas)."
        )


# def select_namespaces(namespaces: list) -> list:
#
#     available_namespaces = [ns.metadata.name for ns in kubernetes.client.CoreV1Api().list_namespace().items]
#     selected_namespaces = []
#
#     for ns in namespaces:
#         ns_re = re.compile(ns)
#         matched = [n for n in available_namespaces if ns_re.fullmatch(n)]
#
#         for n in matched:
#             if n in protected_namespaces:
#                 logging.error(f"Namespace '{n}' selected by expression '{ns}' is not allowed for up/down")
#             else:
#                 selected_namespaces.append(n)
#
#         logging.debug(f"Namespace spec '{ns}' does no match any available namespace")
#
#     return selected_namespaces
