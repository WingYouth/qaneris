"""Which model endpoint this process talks to.

Several complete endpoint sets — a different provider *and* a different model each — can be
declared side by side, and one of them is selected for the process:

```dotenv
# The unnamed default set. Kept working exactly as before this existed.
QANERIS_MODEL_BASE_URL=...
QANERIS_MODEL_API_KEY=...
QANERIS_MODEL_NAME=...

# Pick a named set for this process. Unset means "use the default set above".
QANERIS_MODEL_PROFILE=modelscope

# A named set: QANERIS_MODEL_<NAME>_{BASE_URL,API_KEY,NAME}
QANERIS_MODEL_MODELSCOPE_BASE_URL=https://api-inference.modelscope.cn/v1
QANERIS_MODEL_MODELSCOPE_API_KEY=...
QANERIS_MODEL_MODELSCOPE_NAME=deepseek-ai/DeepSeek-V4-Pro-0813
```

Only the active set is ever built; the others cost nothing and hold no client. Names are
matched case-insensitively.

An **absent** configuration stays absent (``None``) — that is the documented "model not
configured" state. An **explicitly selected** set that cannot be honoured raises instead, because
silently reading nothing after the user asked for a specific endpoint is worse than failing.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from qaneris.common.errors import ModelInvocationError

PREFIX = "QANERIS_MODEL_"

#: Names the set to use. Not a profile field of its own.
PROFILE_SELECTOR = "QANERIS_MODEL_PROFILE"

_SUFFIX_TO_FIELD = {
    "BASE_URL": "base_url",
    "API_KEY": "api_key",
    "NAME": "model",
}

#: ``QANERIS_MODEL_PROFILE`` occupies the ``PROFILE`` slot, so no set may claim that name.
_RESERVED_NAMES = frozenset({"PROFILE"})


@dataclass(frozen=True)
class ModelProfile:
    """One complete endpoint set."""

    base_url: str
    api_key: str
    model: str
    #: The declared name, or ``None`` for the unnamed default set.
    name: str | None = None


def profile_names(env: Mapping[str, str] | None = None) -> list[str]:
    """Names of the sets declared in ``env``, sorted.

    A set is discovered through its ``BASE_URL``, the one field every set must carry. The unnamed
    default set (``QANERIS_MODEL_BASE_URL``) leaves an empty name and is not listed.
    """
    source = os.environ if env is None else env
    marker = "_BASE_URL"
    names: set[str] = set()
    for key in source:
        if not key.startswith(PREFIX) or not key.endswith(marker):
            continue
        name = key[len(PREFIX) : -len(marker)]
        if name and name not in _RESERVED_NAMES:
            names.add(name)
    return sorted(names)


def resolve_model_profile(env: Mapping[str, str] | None = None) -> ModelProfile | None:
    """Return the selected endpoint set, or ``None`` when no model is configured.

    Raises :class:`ModelInvocationError` when a set was explicitly selected but is missing or
    incomplete, and when the selection names a set that was never declared.
    """
    source = os.environ if env is None else env
    selected = (source.get(PROFILE_SELECTOR) or "").strip()
    if not selected:
        return _read(PREFIX, source, label="默认模型配置", required=False)

    prefix = f"{PREFIX}{selected.upper()}_"
    if not any(_value(source, prefix, suffix) for suffix in _SUFFIX_TO_FIELD):
        # Nothing declared under that name at all — a different problem from a half-written one.
        declared = profile_names(source)
        hint = (
            f"已声明的 profile：{', '.join(declared)}"
            if declared
            else f"当前没有声明任何命名 profile（命名规则 {PREFIX}<名称>_BASE_URL）"
        )
        raise ModelInvocationError(
            f"未找到模型 profile“{selected}”，{hint}；"
            f"请修正 {PROFILE_SELECTOR}，或删除它以使用默认配置"
        )
    profile = _read(prefix, source, label=f"模型 profile“{selected}”", required=True)
    assert profile is not None  # required=True raises instead of returning None
    return ModelProfile(
        base_url=profile.base_url, api_key=profile.api_key, model=profile.model, name=selected
    )


def _read(
    prefix: str, env: Mapping[str, str], *, label: str, required: bool
) -> ModelProfile | None:
    missing = [f"{prefix}{suffix}" for suffix in _SUFFIX_TO_FIELD if not _value(env, prefix, suffix)]
    if missing:
        if not required:
            return None
        raise ModelInvocationError(f"{label}不完整，缺少：{', '.join(missing)}")
    return ModelProfile(
        base_url=_value(env, prefix, "BASE_URL"),
        api_key=_value(env, prefix, "API_KEY"),
        model=_value(env, prefix, "NAME"),
    )


def _value(env: Mapping[str, str], prefix: str, suffix: str) -> str:
    name = f"{prefix}{suffix}"
    # Environment variable names are conventionally upper case; accept either spelling so a
    # hand-written .env and an exported shell variable behave the same.
    for candidate in (name, name.lower()):
        value = (env.get(candidate) or "").strip()
        if value:
            return value.strip('"').strip("'")
    return ""
