from sentry_sdk._types import TYPE_CHECKING
from sentry_sdk.consts import SPANDATA
from sentry_sdk.integrations.redis.consts import (
    _COMMANDS_INCLUDING_SENSITIVE_DATA,
    _MAX_NUM_ARGS,
    _MAX_NUM_COMMANDS,
    _MULTI_KEY_COMMANDS,
    _SINGLE_KEY_COMMANDS,
)
from sentry_sdk.scope import should_send_default_pii
from sentry_sdk.utils import SENSITIVE_DATA_SUBSTITUTE, capture_internal_exceptions


if TYPE_CHECKING:
    from typing import Any, Sequence
    from sentry_sdk.tracing import Span


def _get_safe_command(name, args):
    # type: (str, Sequence[Any]) -> str
    command_parts = [name]

    for i, arg in enumerate(args):
        if i > _MAX_NUM_ARGS:
            break

        name_low = name.lower()

        if name_low in _COMMANDS_INCLUDING_SENSITIVE_DATA:
            command_parts.append(SENSITIVE_DATA_SUBSTITUTE)
            continue

        arg_is_the_key = i == 0
        if arg_is_the_key:
            command_parts.append(repr(arg))

        else:
            if should_send_default_pii():
                command_parts.append(repr(arg))
            else:
                command_parts.append(SENSITIVE_DATA_SUBSTITUTE)

    command = " ".join(command_parts)
    return command


def _parse_rediscluster_command(command):
    # type: (Any) -> Sequence[Any]
    return command.args


def _get_span_description(name, *args):
    # type: (str, *Any) -> str
    description = name

    with capture_internal_exceptions():
        description = _get_safe_command(name, args)

    return description


def _set_pipeline_data(
    span, is_cluster, get_command_args_fn, is_transaction, command_stack
):
    # type: (Span, bool, Any, bool, Sequence[Any]) -> None
    span.set_tag("redis.is_cluster", is_cluster)
    span.set_tag("redis.transaction", is_transaction)

    commands = []
    for i, arg in enumerate(command_stack):
        if i >= _MAX_NUM_COMMANDS:
            break

        command = get_command_args_fn(arg)
        commands.append(_get_safe_command(command[0], command[1:]))

    span.set_data(
        "redis.commands",
        {
            "count": len(command_stack),
            "first_ten": commands,
        },
    )


def _set_client_data(span, is_cluster, name, *args):
    # type: (Span, bool, str, *Any) -> None
    span.set_tag("redis.is_cluster", is_cluster)
    if name:
        span.set_tag("redis.command", name)
        span.set_tag(SPANDATA.DB_OPERATION, name)

    if name and args:
        name_low = name.lower()
        if (name_low in _SINGLE_KEY_COMMANDS) or (
            name_low in _MULTI_KEY_COMMANDS and len(args) == 1
        ):
            span.set_tag("redis.key", args[0])