"""
This module contains a collection of functions that are used to validate the
values of fields in Pydantic models. These functions are used as validators in
Pydantic models to ensure that the values of fields conform to the expected
format.
This will be subject to consolidation and refactoring over the next few months.
"""

from __future__ import annotations

import os
import re
import urllib.parse
import warnings
from collections.abc import Iterable, Mapping, MutableMapping
from copy import copy
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, TypeVar, Union, overload
from uuid import UUID

import jsonschema
import pendulum
import pendulum.tz

from prefect.utilities.collections import isiterable
from prefect.utilities.filesystem import relative_path_to_current_platform
from prefect.utilities.importtools import from_qualified_name
from prefect.utilities.names import generate_slug

if TYPE_CHECKING:
    from prefect.serializers import Serializer

T = TypeVar("T")
M = TypeVar("M", bound=Mapping[str, Any])
MM = TypeVar("MM", bound=MutableMapping[str, Any])


LOWERCASE_LETTERS_NUMBERS_AND_DASHES_ONLY_REGEX = "^[a-z0-9-]*$"
LOWERCASE_LETTERS_NUMBERS_AND_UNDERSCORES_REGEX = "^[a-z0-9_]*$"


@overload
def raise_on_name_alphanumeric_dashes_only(
    value: str, field_name: str = ...
) -> str: ...


@overload
def raise_on_name_alphanumeric_dashes_only(
    value: None, field_name: str = ...
) -> None: ...


def raise_on_name_alphanumeric_dashes_only(
    value: Optional[str], field_name: str = "value"
) -> Optional[str]:
    if value is not None and not bool(
        re.match(LOWERCASE_LETTERS_NUMBERS_AND_DASHES_ONLY_REGEX, value)
    ):
        raise ValueError(
            f"{field_name} must only contain lowercase letters, numbers, and dashes."
        )
    return value


@overload
def raise_on_name_alphanumeric_underscores_only(
    value: str, field_name: str = ...
) -> str: ...


@overload
def raise_on_name_alphanumeric_underscores_only(
    value: None, field_name: str = ...
) -> None: ...


def raise_on_name_alphanumeric_underscores_only(
    value: Optional[str], field_name: str = "value"
) -> Optional[str]:
    if value is not None and not re.match(
        LOWERCASE_LETTERS_NUMBERS_AND_UNDERSCORES_REGEX, value
    ):
        raise ValueError(
            f"{field_name} must only contain lowercase letters, numbers, and"
            " underscores."
        )
    return value


def validate_values_conform_to_schema(
    values: Optional[Mapping[str, Any]],
    schema: Optional[Mapping[str, Any]],
    ignore_required: bool = False,
) -> None:
    """
    Validate that the provided values conform to the provided json schema.

    TODO: This schema validation is outdated. The latest version is
    prefect.utilities.schema_tools.validate, which handles fixes to Pydantic v1
    schemas for null values and tuples.

    Args:
        values: The values to validate.
        schema: The schema to validate against.
        ignore_required: Whether to ignore the required fields in the schema. Should be
            used when a partial set of values is acceptable.

    Raises:
        ValueError: If the parameters do not conform to the schema.

    """
    from prefect.utilities.collections import remove_nested_keys

    if ignore_required:
        schema = remove_nested_keys(["required"], schema)

    try:
        if schema is not None and values is not None:
            jsonschema.validate(values, schema)
    except jsonschema.ValidationError as exc:
        if exc.json_path == "$":
            error_message = "Validation failed."
        else:
            error_message = (
                f"Validation failed for field {exc.json_path.replace('$.', '')!r}."
            )
        error_message += f" Failure reason: {exc.message}"
        raise ValueError(error_message) from exc
    except jsonschema.SchemaError as exc:
        raise ValueError(
            "The provided schema is not a valid json schema. Schema error:"
            f" {exc.message}"
        ) from exc


### DEPLOYMENT SCHEMA VALIDATORS ###


def validate_parameters_conform_to_schema(
    parameters: M, values: Mapping[str, Any]
) -> M:
    """Validate that the parameters conform to the parameter schema."""
    if values.get("enforce_parameter_schema"):
        validate_values_conform_to_schema(
            parameters, values.get("parameter_openapi_schema"), ignore_required=True
        )
    return parameters


@overload
def validate_parameter_openapi_schema(schema: M, values: Mapping[str, Any]) -> M: ...


@overload
def validate_parameter_openapi_schema(
    schema: None, values: Mapping[str, Any]
) -> None: ...


def validate_parameter_openapi_schema(
    schema: Optional[M], values: Mapping[str, Any]
) -> Optional[M]:
    """Validate that the parameter_openapi_schema is a valid json schema."""
    if values.get("enforce_parameter_schema"):
        try:
            if schema is not None:
                # Most closely matches the schemas generated by pydantic
                jsonschema.Draft202012Validator.check_schema(schema)
        except jsonschema.SchemaError as exc:
            raise ValueError(
                "The provided schema is not a valid json schema. Schema error:"
                f" {exc.message}"
            ) from exc

    return schema


def convert_to_strings(value: Union[Any, Iterable[Any]]) -> Union[str, list[str]]:
    if isiterable(value):
        return [str(item) for item in value]
    return str(value)


### SCHEDULE SCHEMA VALIDATORS ###


def reconcile_schedules_runner(values: MM) -> MM:
    from prefect.deployments.schedules import (
        normalize_to_deployment_schedule_create,
    )

    schedules = values.get("schedules")
    if schedules is not None and len(schedules) > 0:
        values["schedules"] = normalize_to_deployment_schedule_create(schedules)

    return values


@overload
def validate_schedule_max_scheduled_runs(v: int, limit: int) -> int: ...


@overload
def validate_schedule_max_scheduled_runs(v: None, limit: int) -> None: ...


def validate_schedule_max_scheduled_runs(v: Optional[int], limit: int) -> Optional[int]:
    if v is not None and v > limit:
        raise ValueError(f"`max_scheduled_runs` must be less than or equal to {limit}.")
    return v


def remove_old_deployment_fields(values: MM) -> MM:
    # 2.7.7 removed worker_pool_queue_id in lieu of worker_pool_name and
    # worker_pool_queue_name. Those fields were later renamed to work_pool_name
    # and work_queue_name. This validator removes old fields provided
    # by older clients to avoid 422 errors.
    values_copy = copy(values)
    worker_pool_queue_id = values_copy.pop("worker_pool_queue_id", None)
    worker_pool_name = values_copy.pop("worker_pool_name", None)
    worker_pool_queue_name = values_copy.pop("worker_pool_queue_name", None)
    work_pool_queue_name = values_copy.pop("work_pool_queue_name", None)
    if worker_pool_queue_id:
        warnings.warn(
            (
                "`worker_pool_queue_id` is no longer supported for creating or updating "
                "deployments. Please use `work_pool_name` and "
                "`work_queue_name` instead."
            ),
            UserWarning,
        )
    if worker_pool_name or worker_pool_queue_name or work_pool_queue_name:
        warnings.warn(
            (
                "`worker_pool_name`, `worker_pool_queue_name`, and "
                "`work_pool_name` are"
                "no longer supported for creating or updating "
                "deployments. Please use `work_pool_name` and "
                "`work_queue_name` instead."
            ),
            UserWarning,
        )
    return values_copy


def reconcile_paused_deployment(values: MM) -> MM:
    paused = values.get("paused")

    if paused is None:
        values["paused"] = False

    return values


def default_anchor_date(v: pendulum.DateTime) -> pendulum.DateTime:
    return pendulum.instance(v)


@overload
def default_timezone(v: str, values: Optional[Mapping[str, Any]] = ...) -> str: ...


@overload
def default_timezone(
    v: None, values: Optional[Mapping[str, Any]] = ...
) -> Optional[str]: ...


def default_timezone(
    v: Optional[str], values: Optional[Mapping[str, Any]] = None
) -> Optional[str]:
    values = values or {}
    timezones = pendulum.tz.timezones()

    if v is not None:
        if v and v not in timezones:
            raise ValueError(
                f'Invalid timezone: "{v}" (specify in IANA tzdata format, for example,'
                " America/New_York)"
            )
        return v

    # anchor schedules
    elif "anchor_date" in values:
        anchor_date: pendulum.DateTime = values["anchor_date"]
        tz = "UTC" if anchor_date.tz is None else anchor_date.tz.name
        # sometimes anchor dates have "timezones" that are UTC offsets
        # like "-04:00". This happens when parsing ISO8601 strings.
        # In this case we, the correct inferred localization is "UTC".
        return tz if tz in timezones else "UTC"

    # cron schedules
    return v


def validate_cron_string(v: str) -> str:
    from croniter import croniter

    # croniter allows "random" and "hashed" expressions
    # which we do not support https://github.com/kiorky/croniter
    if not croniter.is_valid(v):
        raise ValueError(f'Invalid cron string: "{v}"')
    elif any(c for c in v.split() if c.casefold() in ["R", "H", "r", "h"]):
        raise ValueError(
            f'Random and Hashed expressions are unsupported, received: "{v}"'
        )
    return v


# approx. 1 years worth of RDATEs + buffer
MAX_RRULE_LENGTH = 6500


def validate_rrule_string(v: str) -> str:
    import dateutil.rrule

    # attempt to parse the rrule string as an rrule object
    # this will error if the string is invalid
    try:
        dateutil.rrule.rrulestr(v, cache=True)
    except ValueError as exc:
        # rrules errors are a mix of cryptic and informative
        # so reraise to be clear that the string was invalid
        raise ValueError(f'Invalid RRule string "{v}": {exc}')
    if len(v) > MAX_RRULE_LENGTH:
        raise ValueError(
            f'Invalid RRule string "{v[:40]}..."\n'
            f"Max length is {MAX_RRULE_LENGTH}, got {len(v)}"
        )
    return v


### STATE SCHEMA VALIDATORS ###


def get_or_create_run_name(name: Optional[str]) -> str:
    return name or generate_slug(2)


### FILESYSTEM SCHEMA VALIDATORS ###


def stringify_path(value: Union[str, os.PathLike[str]]) -> str:
    return os.fspath(value)


def validate_basepath(value: str) -> str:
    scheme, netloc, _, _, _ = urllib.parse.urlsplit(value)

    if not scheme:
        raise ValueError(f"Base path must start with a scheme. Got {value!r}.")

    if not netloc:
        raise ValueError(
            f"Base path must include a location after the scheme. Got {value!r}."
        )

    if scheme == "file":
        raise ValueError(
            "Base path scheme cannot be 'file'. Use `LocalFileSystem` instead for"
            " local file access."
        )

    return value


### SERIALIZER SCHEMA VALIDATORS ###


def validate_picklelib(value: str) -> str:
    """
    Check that the given pickle library is importable and has dumps/loads methods.
    """
    try:
        pickler = from_qualified_name(value)
    except (ImportError, AttributeError) as exc:
        raise ValueError(
            f"Failed to import requested pickle library: {value!r}."
        ) from exc

    if not callable(getattr(pickler, "dumps", None)):
        raise ValueError(f"Pickle library at {value!r} does not have a 'dumps' method.")

    if not callable(getattr(pickler, "loads", None)):
        raise ValueError(f"Pickle library at {value!r} does not have a 'loads' method.")

    return value


def validate_dump_kwargs(value: M) -> M:
    # `default` is set by `object_encoder`. A user provided callable would make this
    # class unserializable anyway.
    if "default" in value:
        raise ValueError("`default` cannot be provided. Use `object_encoder` instead.")
    return value


def validate_load_kwargs(value: M) -> M:
    # `object_hook` is set by `object_decoder`. A user provided callable would make
    # this class unserializable anyway.
    if "object_hook" in value:
        raise ValueError(
            "`object_hook` cannot be provided. Use `object_decoder` instead."
        )
    return value


@overload
def cast_type_names_to_serializers(value: str) -> "Serializer[Any]": ...


@overload
def cast_type_names_to_serializers(value: "Serializer[T]") -> "Serializer[T]": ...


def cast_type_names_to_serializers(
    value: Union[str, "Serializer[Any]"],
) -> "Serializer[Any]":
    from prefect.serializers import Serializer

    if isinstance(value, str):
        return Serializer(type=value)
    return value


def validate_compressionlib(value: str) -> str:
    """
    Check that the given pickle library is importable and has compress/decompress
    methods.
    """
    try:
        compressor = from_qualified_name(value)
    except (ImportError, AttributeError) as exc:
        raise ValueError(
            f"Failed to import requested compression library: {value!r}."
        ) from exc

    if not callable(getattr(compressor, "compress", None)):
        raise ValueError(
            f"Compression library at {value!r} does not have a 'compress' method."
        )

    if not callable(getattr(compressor, "decompress", None)):
        raise ValueError(
            f"Compression library at {value!r} does not have a 'decompress' method."
        )

    return value


# TODO: if we use this elsewhere we can change the error message to be more generic
@overload
def list_length_50_or_less(v: list[float]) -> list[float]: ...


@overload
def list_length_50_or_less(v: None) -> None: ...


def list_length_50_or_less(v: Optional[list[float]]) -> Optional[list[float]]:
    if isinstance(v, list) and (len(v) > 50):
        raise ValueError("Can not configure more than 50 retry delays per task.")
    return v


# TODO: if we use this elsewhere we can change the error message to be more generic
@overload
def validate_not_negative(v: float) -> float: ...


@overload
def validate_not_negative(v: None) -> None: ...


def validate_not_negative(v: Optional[float]) -> Optional[float]:
    if v is not None and v < 0:
        raise ValueError("`retry_jitter_factor` must be >= 0.")
    return v


@overload
def validate_message_template_variables(v: str) -> str: ...


@overload
def validate_message_template_variables(v: None) -> None: ...


def validate_message_template_variables(v: Optional[str]) -> Optional[str]:
    from prefect.client.schemas.objects import FLOW_RUN_NOTIFICATION_TEMPLATE_KWARGS

    if v is not None:
        try:
            v.format(**{k: "test" for k in FLOW_RUN_NOTIFICATION_TEMPLATE_KWARGS})
        except KeyError as exc:
            raise ValueError(f"Invalid template variable provided: '{exc.args[0]}'")
    return v


def validate_default_queue_id_not_none(v: Optional[UUID]) -> UUID:
    if v is None:
        raise ValueError(
            "`default_queue_id` is a required field. If you are "
            "creating a new WorkPool and don't have a queue "
            "ID yet, use the `actions.WorkPoolCreate` model instead."
        )
    return v


@overload
def validate_max_metadata_length(v: MM) -> MM: ...


@overload
def validate_max_metadata_length(v: None) -> None: ...


def validate_max_metadata_length(v: Optional[MM]) -> Optional[MM]:
    max_metadata_length = 500
    if v is None:
        return v
    for key in v.keys():
        if len(str(v[key])) > max_metadata_length:
            v[key] = str(v[key])[:max_metadata_length] + "..."
    return v


### TASK RUN SCHEMA VALIDATORS ###


@overload
def validate_cache_key_length(cache_key: str) -> str: ...


@overload
def validate_cache_key_length(cache_key: None) -> None: ...


def validate_cache_key_length(cache_key: Optional[str]) -> Optional[str]:
    from prefect.settings import (
        PREFECT_API_TASK_CACHE_KEY_MAX_LENGTH,
    )

    if cache_key and len(cache_key) > PREFECT_API_TASK_CACHE_KEY_MAX_LENGTH.value():
        raise ValueError(
            "Cache key exceeded maximum allowed length of"
            f" {PREFECT_API_TASK_CACHE_KEY_MAX_LENGTH.value()} characters."
        )
    return cache_key


def set_run_policy_deprecated_fields(values: MM) -> MM:
    """
    If deprecated fields are provided, populate the corresponding new fields
    to preserve orchestration behavior.
    """
    if not values.get("retries", None) and values.get("max_retries", 0) != 0:
        values["retries"] = values["max_retries"]

    if (
        not values.get("retry_delay", None)
        and values.get("retry_delay_seconds", 0) != 0
    ):
        values["retry_delay"] = values["retry_delay_seconds"]

    return values


### PYTHON ENVIRONMENT SCHEMA VALIDATORS ###


@overload
def return_v_or_none(v: str) -> str: ...


@overload
def return_v_or_none(v: None) -> None: ...


def return_v_or_none(v: Optional[str]) -> Optional[str]:
    """Make sure that empty strings are treated as None"""
    if not v:
        return None
    return v


### BLOCK SCHEMA VALIDATORS ###


def validate_parent_and_ref_diff(values: M) -> M:
    parent_id = values.get("parent_block_document_id")
    ref_id = values.get("reference_block_document_id")
    if parent_id and ref_id and parent_id == ref_id:
        raise ValueError(
            "`parent_block_document_id` and `reference_block_document_id` cannot be"
            " the same"
        )
    return values


def validate_name_present_on_nonanonymous_blocks(values: M) -> M:
    # anonymous blocks may have no name prior to actually being
    # stored in the database
    if not values.get("is_anonymous") and not values.get("name"):
        raise ValueError("Names must be provided for block documents.")
    return values


### PROCESS JOB CONFIGURATION VALIDATORS ###


@overload
def validate_working_dir(v: str) -> Path: ...


@overload
def validate_working_dir(v: None) -> None: ...


def validate_working_dir(v: Optional[Path | str]) -> Optional[Path]:
    """Make sure that the working directory is formatted for the current platform."""
    if isinstance(v, str):
        return relative_path_to_current_platform(v)
    return v


### UNCATEGORIZED VALIDATORS ###

# the above categories seem to be getting a bit unwieldy, so this is a temporary
# catch-all for validators until we organize these into files


@overload
def validate_block_document_name(value: str) -> str: ...


@overload
def validate_block_document_name(value: None) -> None: ...


def validate_block_document_name(value: Optional[str]) -> Optional[str]:
    if value is not None:
        raise_on_name_alphanumeric_dashes_only(value, field_name="Block document name")
    return value


def validate_artifact_key(value: str) -> str:
    raise_on_name_alphanumeric_dashes_only(value, field_name="Artifact key")
    return value


@overload
def validate_variable_name(value: str) -> str: ...


@overload
def validate_variable_name(value: None) -> None: ...


def validate_variable_name(value: Optional[str]) -> Optional[str]:
    if value is not None:
        raise_on_name_alphanumeric_underscores_only(value, field_name="Variable name")
    return value


def validate_block_type_slug(value: str):
    raise_on_name_alphanumeric_dashes_only(value, field_name="Block type slug")
    return value
