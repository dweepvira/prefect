from __future__ import annotations

import asyncio
import base64
import gzip
import multiprocessing
import multiprocessing.context
import os
import subprocess
import sys
from typing import Any, TypedDict

import cloudpickle

from prefect.client.schemas.objects import FlowRun
from prefect.context import SettingsContext, get_settings_context, serialize_context
from prefect.engine import handle_engine_signals
from prefect.flow_engine import run_flow
from prefect.flows import Flow
from prefect.settings.context import get_current_settings
from prefect.settings.models.root import Settings
from prefect.utilities.slugify import slugify

try:
    import uv

    uv_path = uv.find_uv_bin()
except (ImportError, ModuleNotFoundError):
    uv_path = "uv"


class SerializedBundle(TypedDict):
    """
    A serialized bundle is a serialized function, context, and flow run that can be
    easily transported for later execution.
    """

    function: str
    context: str
    flow_run: dict[str, Any]
    dependencies: str


def _serialize_bundle_object(obj: Any) -> str:
    """
    Serializes an object to a string.
    """
    return base64.b64encode(gzip.compress(cloudpickle.dumps(obj))).decode()


def _deserialize_bundle_object(serialized_obj: str) -> Any:
    """
    Deserializes an object from a string.
    """
    return cloudpickle.loads(gzip.decompress(base64.b64decode(serialized_obj)))


def create_bundle_for_flow_run(
    flow: Flow[Any, Any],
    flow_run: FlowRun,
    context: dict[str, Any] | None = None,
) -> SerializedBundle:
    """
    Creates a bundle for a flow run.

    Args:
        flow: The flow to bundle.
        flow_run: The flow run to bundle.
        context: The context to use when running the flow.

    Returns:
        A serialized bundle.
    """
    context = context or serialize_context()

    return {
        "function": _serialize_bundle_object(flow),
        "context": _serialize_bundle_object(context),
        "flow_run": flow_run.model_dump(mode="json"),
        "dependencies": subprocess.check_output(
            [
                uv_path,
                "pip",
                "freeze",
                # Exclude editable installs because we won't be able to install them in the execution environment
                "--exclude-editable",
            ]
        )
        .decode()
        .strip(),
    }


def extract_flow_from_bundle(bundle: SerializedBundle) -> Flow[Any, Any]:
    """
    Extracts a flow from a bundle.
    """
    return _deserialize_bundle_object(bundle["function"])


def _extract_and_run_flow(
    bundle: SerializedBundle, env: dict[str, Any] | None = None
) -> None:
    """
    Extracts a flow from a bundle and runs it.

    Designed to be run in a subprocess.

    Args:
        bundle: The bundle to extract and run.
        env: The environment to use when running the flow.
    """

    os.environ.update(env or {})
    # TODO: make this a thing we can pass directly to the engine
    os.environ["PREFECT__ENABLE_CANCELLATION_AND_CRASHED_HOOKS"] = "false"
    settings_context = get_settings_context()

    flow = _deserialize_bundle_object(bundle["function"])
    context = _deserialize_bundle_object(bundle["context"])
    flow_run = FlowRun.model_validate(bundle["flow_run"])

    with SettingsContext(
        profile=settings_context.profile,
        settings=Settings(),
    ):
        with handle_engine_signals(flow_run.id):
            maybe_coro = run_flow(
                flow=flow,
                flow_run=flow_run,
                context=context,
            )
            if asyncio.iscoroutine(maybe_coro):
                # This is running in a brand new process, so there won't be an existing
                # event loop.
                asyncio.run(maybe_coro)


def execute_bundle_in_subprocess(
    bundle: SerializedBundle,
) -> multiprocessing.context.SpawnProcess:
    """
    Executes a bundle in a subprocess.

    Args:
        bundle: The bundle to execute.

    Returns:
        A multiprocessing.context.SpawnProcess.
    """

    ctx = multiprocessing.get_context("spawn")

    # Install dependencies if necessary
    if dependencies := bundle.get("dependencies"):
        subprocess.check_call(
            [uv_path, "pip", "install", *dependencies.split("\n")],
            # Copy the current environment to ensure we install into the correct venv
            env=os.environ,
        )

    process = ctx.Process(
        target=_extract_and_run_flow,
        kwargs={
            "bundle": bundle,
            "env": get_current_settings().to_environment_variables(exclude_unset=True)
            | os.environ,
        },
    )

    process.start()

    return process


def convert_step_to_command(
    step: dict[str, Any], key: str, quiet: bool = False
) -> list[str]:
    """
    Converts a bundle upload or execution step to a command.

    Args:
        step: The step to convert.
        key: The key to use for the remote file when downloading or uploading.
        quiet: Whether to suppress `uv` output from the command.

    Returns:
        A list of strings representing the command to run the step.
    """
    # Start with uv run
    command = ["uv", "run"]

    if quiet:
        command.append("--quiet")

    step_keys = list(step.keys())

    if len(step_keys) != 1:
        raise ValueError("Expected exactly one function in step")

    function_fqn = step_keys[0]
    function_args = step[function_fqn]

    # Add the `--with` argument to handle dependencies for running the step
    requires: list[str] | str = function_args.get("requires", [])
    if isinstance(requires, str):
        requires = [requires]
    if requires:
        command.extend(["--with", ",".join(requires)])

    # Add the `--python` argument to handle the Python version for running the step
    python_version = sys.version_info
    command.extend(["--python", f"{python_version.major}.{python_version.minor}"])

    # Add the `-m` argument to defined the function to run
    command.extend(["-m", function_fqn])

    # Add any arguments with values defined in the step
    for arg_name, arg_value in function_args.items():
        if arg_name == "requires":
            continue

        command.extend([f"--{slugify(arg_name)}", arg_value])

    # Add the `--key` argument to specify the remote file name
    command.extend(["--key", key])

    return command
