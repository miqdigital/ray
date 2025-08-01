import argparse
import base64
import json
import os
import sys
import time

import ray
import ray._private.node
import ray._private.ray_constants as ray_constants
import ray._private.utils
import ray.actor
from ray._common.ray_constants import (
    LOGGING_ROTATE_BACKUP_COUNT,
    LOGGING_ROTATE_BYTES,
)
from ray._private.async_compat import try_install_uvloop
from ray._private.parameter import RayParams
from ray._private.ray_logging import get_worker_log_file_name
from ray._private.runtime_env.setup_hook import load_and_execute_setup_hook

parser = argparse.ArgumentParser(
    description=("Parse addresses for the worker to connect to.")
)
parser.add_argument(
    "--cluster-id",
    required=True,
    type=str,
    help="the auto-generated ID of the cluster",
)
parser.add_argument(
    "--node-id",
    required=True,
    type=str,
    help="the auto-generated ID of the node",
)
parser.add_argument(
    "--node-ip-address",
    required=True,
    type=str,
    help="the ip address of the worker's node",
)
parser.add_argument(
    "--node-manager-port", required=True, type=int, help="the port of the worker's node"
)
parser.add_argument(
    "--raylet-ip-address",
    required=False,
    type=str,
    default=None,
    help="the ip address of the worker's raylet",
)
parser.add_argument(
    "--redis-address", required=True, type=str, help="the address to use for Redis"
)
parser.add_argument(
    "--gcs-address", required=True, type=str, help="the address to use for GCS"
)
parser.add_argument(
    "--redis-username",
    required=False,
    type=str,
    default=None,
    help="the username to use for Redis",
)
parser.add_argument(
    "--redis-password",
    required=False,
    type=str,
    default=None,
    help="the password to use for Redis",
)
parser.add_argument(
    "--object-store-name", required=True, type=str, help="the object store's name"
)
parser.add_argument("--raylet-name", required=False, type=str, help="the raylet's name")
parser.add_argument(
    "--logging-level",
    required=False,
    type=str,
    default=ray_constants.LOGGER_LEVEL,
    choices=ray_constants.LOGGER_LEVEL_CHOICES,
    help=ray_constants.LOGGER_LEVEL_HELP,
)
parser.add_argument(
    "--logging-format",
    required=False,
    type=str,
    default=ray_constants.LOGGER_FORMAT,
    help=ray_constants.LOGGER_FORMAT_HELP,
)
parser.add_argument(
    "--temp-dir",
    required=False,
    type=str,
    default=None,
    help="Specify the path of the temporary directory use by Ray process.",
)
parser.add_argument(
    "--load-code-from-local",
    default=False,
    action="store_true",
    help="True if code is loaded from local files, as opposed to the GCS.",
)
parser.add_argument(
    "--worker-type",
    required=False,
    type=str,
    default="WORKER",
    help="Specify the type of the worker process",
)
parser.add_argument(
    "--metrics-agent-port",
    required=True,
    type=int,
    help="the port of the node's metric agent.",
)
parser.add_argument(
    "--runtime-env-agent-port",
    required=True,
    type=int,
    default=None,
    help="The port on which the runtime env agent receives HTTP requests.",
)
parser.add_argument(
    "--object-spilling-config",
    required=False,
    type=str,
    default="",
    help="The configuration of object spilling. Only used by I/O workers.",
)
parser.add_argument(
    "--logging-rotate-bytes",
    required=False,
    type=int,
    default=LOGGING_ROTATE_BYTES,
    help="Specify the max bytes for rotating "
    "log file, default is "
    f"{LOGGING_ROTATE_BYTES} bytes.",
)
parser.add_argument(
    "--logging-rotate-backup-count",
    required=False,
    type=int,
    default=LOGGING_ROTATE_BACKUP_COUNT,
    help="Specify the backup count of rotated log file, default is "
    f"{LOGGING_ROTATE_BACKUP_COUNT}.",
)
parser.add_argument(
    "--runtime-env-hash",
    required=False,
    type=int,
    default=0,
    help="The computed hash of the runtime env for this worker.",
)
parser.add_argument(
    "--startup-token",
    required=True,
    type=int,
    help="The startup token assigned to this worker process by the raylet.",
)
parser.add_argument(
    "--ray-debugger-external",
    default=False,
    action="store_true",
    help="True if Ray debugger is made available externally.",
)
parser.add_argument("--session-name", required=False, help="The current session name")
parser.add_argument(
    "--webui",
    required=False,
    help="The address of web ui",
)
parser.add_argument(
    "--worker-launch-time-ms",
    required=True,
    type=int,
    help="The time when raylet starts to launch the worker process.",
)

parser.add_argument(
    "--worker-preload-modules",
    type=str,
    required=False,
    help=(
        "A comma-separated list of Python module names "
        "to import before accepting work."
    ),
)
parser.add_argument(
    "--enable-resource-isolation",
    type=bool,
    required=False,
    default=False,
    help=(
        "If true, core worker enables resource isolation by adding itself into appropriate cgroup."
    ),
)

if __name__ == "__main__":
    # NOTE(sang): For some reason, if we move the code below
    # to a separate function, tensorflow will capture that method
    # as a step function. For more details, check out
    # https://github.com/ray-project/ray/pull/12225#issue-525059663.
    args = parser.parse_args()
    ray._private.ray_logging.setup_logger(args.logging_level, args.logging_format)
    worker_launched_time_ms = time.time_ns() // 1e6
    if args.worker_type == "WORKER":
        mode = ray.WORKER_MODE
    elif args.worker_type == "SPILL_WORKER":
        mode = ray.SPILL_WORKER_MODE
    elif args.worker_type == "RESTORE_WORKER":
        mode = ray.RESTORE_WORKER_MODE
    else:
        raise ValueError("Unknown worker type: " + args.worker_type)

    # Try installing uvloop as default event-loop implementation
    # for asyncio
    try_install_uvloop()

    raylet_ip_address = args.raylet_ip_address
    if raylet_ip_address is None:
        raylet_ip_address = args.node_ip_address
    ray_params = RayParams(
        node_ip_address=args.node_ip_address,
        raylet_ip_address=raylet_ip_address,
        node_manager_port=args.node_manager_port,
        redis_address=args.redis_address,
        redis_username=args.redis_username,
        redis_password=args.redis_password,
        plasma_store_socket_name=args.object_store_name,
        raylet_socket_name=args.raylet_name,
        temp_dir=args.temp_dir,
        metrics_agent_port=args.metrics_agent_port,
        runtime_env_agent_port=args.runtime_env_agent_port,
        gcs_address=args.gcs_address,
        session_name=args.session_name,
        webui=args.webui,
        cluster_id=args.cluster_id,
        node_id=args.node_id,
    )
    node = ray._private.node.Node(
        ray_params,
        head=False,
        shutdown_at_exit=False,
        spawn_reaper=False,
        connect_only=True,
        default_worker=True,
    )

    # NOTE(suquark): We must initialize the external storage before we
    # connect to raylet. Otherwise we may receive requests before the
    # external storage is initialized.
    if mode == ray.RESTORE_WORKER_MODE or mode == ray.SPILL_WORKER_MODE:
        from ray._private import external_storage

        if args.object_spilling_config:
            object_spilling_config = base64.b64decode(args.object_spilling_config)
            object_spilling_config = json.loads(object_spilling_config)
        else:
            object_spilling_config = {}
        external_storage.setup_external_storage(
            object_spilling_config, node.node_id, node.session_name
        )

    ray._private.worker._global_node = node
    ray._private.worker.connect(
        node,
        node.session_name,
        mode=mode,
        runtime_env_hash=args.runtime_env_hash,
        startup_token=args.startup_token,
        ray_debugger_external=args.ray_debugger_external,
        worker_launch_time_ms=args.worker_launch_time_ms,
        worker_launched_time_ms=worker_launched_time_ms,
        enable_resource_isolation=args.enable_resource_isolation,
    )

    worker = ray._private.worker.global_worker

    stdout_fileno = sys.stdout.fileno()
    stderr_fileno = sys.stderr.fileno()
    # We also manually set sys.stdout and sys.stderr because that seems to
    # have an effect on the output buffering. Without doing this, stdout
    # and stderr are heavily buffered resulting in seemingly lost logging
    # statements. We never want to close the stdout file descriptor, dup2 will
    # close it when necessary and we don't want python's GC to close it.
    sys.stdout = ray._private.utils.open_log(
        stdout_fileno, unbuffered=True, closefd=False
    )
    sys.stderr = ray._private.utils.open_log(
        stderr_fileno, unbuffered=True, closefd=False
    )

    # Setup log file.
    out_filepath, err_filepath = node.get_log_file_names(
        get_worker_log_file_name(args.worker_type),
        unique=False,  # C++ core worker process already creates the file, should use a deterministic function to get the same file path.
        create_out=True,
        create_err=True,
    )
    worker.set_out_file(out_filepath)
    worker.set_err_file(err_filepath)

    rotation_max_bytes = os.getenv("RAY_ROTATION_MAX_BYTES", None)

    # Log rotation is disabled on windows platform.
    if sys.platform != "win32" and rotation_max_bytes and int(rotation_max_bytes) > 0:
        worker.set_file_rotation_enabled(True)

    if mode == ray.WORKER_MODE and args.worker_preload_modules:
        module_names_to_import = args.worker_preload_modules.split(",")
        ray._private.utils.try_import_each_module(module_names_to_import)

    # If the worker setup function is configured, run it.
    worker_process_setup_hook_key = os.getenv(
        ray_constants.WORKER_PROCESS_SETUP_HOOK_ENV_VAR
    )
    if worker_process_setup_hook_key:
        error = load_and_execute_setup_hook(worker_process_setup_hook_key)
        if error is not None:
            worker.core_worker.drain_and_exit_worker("system", error)

    if mode == ray.WORKER_MODE:
        worker.main_loop()
    elif mode in [ray.RESTORE_WORKER_MODE, ray.SPILL_WORKER_MODE]:
        # It is handled by another thread in the C++ core worker.
        # We just need to keep the worker alive.
        while True:
            time.sleep(100000)
    else:
        raise ValueError(f"Unexcepted worker mode: {mode}")
