# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The checks on what enters the core: argument types, and strings every store can keep.

A value's type is checked where it enters the library, so a wrong type fails
the same way on every store instead of being stored as it is by one and
converted by another. The checks are built from the annotations the first
time they're needed, as one plain function each, since they run on every
call.
"""

from __future__ import annotations

import dataclasses
import enum
import inspect
import types
import typing
from collections.abc import Callable, Iterable, Mapping
from functools import wraps
from typing import Any, TypeVar

from calendaring_sync.error import LoneSurrogateError

_T = TypeVar("_T")

#: Built-in types a value must be exactly, not a subclass of: ``True`` is
#: no local key, and a ``str`` subclass could change how two values compare.
_EXACT = (str, int, bytes, bool)

#: Value dataclasses that check their own fields when built. Being frozen,
#: one inside another value only needs to be of its class.
_CHECKED_WHEN_BUILT: set[type] = set()

# Marks an argument the caller left to its default.
_MISSING = object()


class _Kind(enum.Enum):
    """The kinds of type hint the checks can read."""

    NONE = enum.auto()
    UNION = enum.auto()
    EXACT = enum.auto()
    NUMBER = enum.auto()
    FROZENSET = enum.auto()
    MAPPING = enum.auto()
    CALLABLE = enum.auto()
    #: A dataclass whose fields are checked one by one.
    FIELDS = enum.auto()
    #: Any other class, checked with ``isinstance``.
    INSTANCE = enum.auto()


def _kind(hint: Any) -> tuple[_Kind, tuple[Any, ...]]:
    """What kind of type a hint asks for, and the hints or class inside it.

    Raises:
        TypeError: If the checks can't read the hint.
    """
    origin = typing.get_origin(hint)
    if hint is type(None):
        return _Kind.NONE, ()
    if origin in (typing.Union, types.UnionType):
        return _Kind.UNION, typing.get_args(hint)
    if hint in _EXACT:
        return _Kind.EXACT, (hint,)
    if hint is float:
        return _Kind.NUMBER, ()
    if origin is frozenset:
        return _Kind.FROZENSET, typing.get_args(hint)
    if isinstance(origin, type) and issubclass(origin, Mapping):
        return _Kind.MAPPING, typing.get_args(hint)
    if origin is not None and getattr(origin, "__name__", "") == "Callable":
        return _Kind.CALLABLE, ()
    cls = origin if isinstance(origin, type) else hint
    if not isinstance(cls, type):
        raise TypeError(f"Can't check a value against {hint!r}")
    if dataclasses.is_dataclass(cls) and cls not in _CHECKED_WHEN_BUILT:
        return _Kind.FIELDS, (cls,)
    return _Kind.INSTANCE, (cls,)


def _describe(hint: Any) -> str:
    """The type a hint asks for, as an error message names it."""
    kind, inner = _kind(hint)
    match kind:
        case _Kind.NONE:
            return "None"
        case _Kind.UNION:
            names = [_describe(part) for part in inner]
            return ", ".join(names[:-1]) + " or " + names[-1]
        case _Kind.NUMBER:
            return "float or int"
        case _Kind.FROZENSET:
            return f"frozenset of {_describe(inner[0])}"
        case _Kind.MAPPING:
            return f"mapping of {_describe(inner[0])} to {_describe(inner[1])}"
        case _Kind.CALLABLE:
            return "callable"
    # A class, exact or not.
    return str(inner[0].__name__)


def _test(hint: Any, value: str, env: dict[str, object]) -> str:
    """Python source that's true when ``value`` matches ``hint``.

    Names the source needs go into ``env``.
    """
    kind, inner = _kind(hint)
    match kind:
        case _Kind.NONE:
            return f"{value} is None"
        case _Kind.UNION:
            return "(" + " or ".join(_test(part, value, env) for part in inner) + ")"
        case _Kind.EXACT:
            return f"type({value}) is {inner[0].__name__}"
        case _Kind.NUMBER:
            return f"type({value}) in (float, int)"
        case _Kind.FROZENSET:
            items = f"all({_test(inner[0], '_i', env)} for _i in {value})"
            return f"(type({value}) is frozenset and {items})"
        case _Kind.MAPPING:
            env["Mapping"] = Mapping
            pairs = f"{_test(inner[0], '_k', env)} and {_test(inner[1], '_v', env)}"
            return f"(isinstance({value}, Mapping) and all({pairs} for _k, _v in {value}.items()))"
        case _Kind.CALLABLE:
            return f"callable({value})"
    cls = inner[0]
    name = f"_{cls.__module__.replace('.', '_')}_{cls.__qualname__.replace('.', '_')}"
    env[name] = cls
    if kind is _Kind.FIELDS:
        env[f"{name}_fields"] = _fields(cls)
        return f"(isinstance({value}, {name}) and {name}_fields({value}))"
    return f"isinstance({value}, {name})"


def _compile(source: str, env: dict[str, object]) -> Callable[..., Any]:
    """Run the source of one function named ``check`` and return it."""
    exec(source, env)  # noqa: S102, the source is built above from annotations only
    return env["check"]  # type: ignore[return-value]


_FIELDS: dict[type, Callable[[object], bool]] = {}


def _fields(cls: type) -> Callable[[object], bool]:
    """A function that's true when every field of a dataclass instance has its declared type."""
    if cls not in _FIELDS:
        env: dict[str, object] = {}
        hints = typing.get_type_hints(cls)
        tests = [_test(hints[f.name], f"o.{f.name}", env) for f in dataclasses.fields(cls)]
        _FIELDS[cls] = _compile("def check(o):\n    return " + " and ".join(tests or ["True"]), env)
    return _FIELDS[cls]


def _field_mismatch(cls: type, value: object, prefix: str) -> str | None:
    """The message for the first field of a dataclass instance that has the wrong type."""
    hints = typing.get_type_hints(cls)
    found = (
        _mismatch(prefix + f.name, hints[f.name], getattr(value, f.name))
        for f in dataclasses.fields(cls)
    )
    return next((message for message in found if message is not None), None)


def _inner_mismatch(name: str, hint: Any, value: object) -> str | None:
    """For a value of the kind ``hint`` holds, the message naming what inside it is wrong."""
    kind, inner = _kind(hint)
    if kind is _Kind.FIELDS and isinstance(value, inner[0]):
        return _field_mismatch(inner[0], value, f"{name}.")
    if kind is _Kind.FROZENSET and type(value) is frozenset:
        bad = next((each for each in value if _mismatch("item", inner[0], each) is not None), None)
        return f"{name} must be {_describe(hint)}, but holds {type(bad).__name__}"
    if kind is _Kind.MAPPING and isinstance(value, Mapping):
        key, item = inner
        found = (
            _mismatch(f"{name}[{k!r}]", item, v) or _mismatch(f"{name} key", key, k)
            for k, v in value.items()
        )
        return next((message for message in found if message is not None), None)
    return None


def _mismatch(name: str, hint: Any, value: object) -> str | None:
    """The message for a value that doesn't match its hint, naming the part of it that's wrong."""
    env: dict[str, object] = {}
    if _compile(f"def check(v):\n    return {_test(hint, 'v', env)}", env)(value):
        return None
    kind, inner_hints = _kind(hint)
    parts = inner_hints if kind is _Kind.UNION else (hint,)
    inner = (_inner_mismatch(name, part, value) for part in parts)
    found = next((message for message in inner if message is not None), None)
    return found or f"{name} must be {_describe(hint)}, not {type(value).__name__}"


def check_types(obj: object) -> None:
    """Raise ``TypeError`` unless every field of a dataclass instance has its declared type.

    Fields holding a dataclass are checked in turn, unless that class checks
    its own fields when built.
    """
    if not _fields(type(obj))(obj):
        raise TypeError(_field_mismatch(type(obj), obj, ""))


def checked_when_built(cls: type[_T]) -> type[_T]:
    """Check a value dataclass's field types every time one is built, before its own checks.

    The class's own ``__post_init__``, if it has one, runs after the check.
    """
    original = cls.__dict__.get("__post_init__")
    if original is None:
        # A dataclass calls __post_init__ only if it had one when built.
        init: Callable[..., None] = cls.__init__

        def __init__(self: object, *args: Any, **kwargs: Any) -> None:
            init(self, *args, **kwargs)
            check_types(self)

        cls.__init__ = wraps(init)(__init__)  # type: ignore[method-assign]
    else:

        def __post_init__(self: object) -> None:
            check_types(self)
            original(self)

        cls.__post_init__ = __post_init__  # type: ignore[attr-defined]
    _CHECKED_WHEN_BUILT.add(cls)
    return cls


#: Checks one call's positional and keyword arguments.
_ArgumentCheck = Callable[[tuple[Any, ...], dict[str, Any]], None]


def argument_check(func: Callable[..., object]) -> _ArgumentCheck:
    """Build the function that checks a call's arguments against ``func``'s annotations."""
    hints = typing.get_type_hints(func)
    env: dict[str, object] = {"_MISSING": _MISSING, "_fail": _fail}
    lines = ["def check(args, kwargs):", "    n = len(args)"]
    for i, param in enumerate(inspect.signature(func).parameters.values()):
        if param.name in ("self", "cls"):
            continue
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            raise TypeError(f"{func.__qualname__} takes *{param.name}, which isn't checked")
        if param.name not in hints:
            raise TypeError(f"{func.__qualname__} takes {param.name} without a type to check")
        env[f"_h_{param.name}"] = hints[param.name]
        if param.kind is param.KEYWORD_ONLY:
            lines.append(f"    v = kwargs.get({param.name!r}, _MISSING)")
        else:
            lines.append(f"    v = args[{i}] if n > {i} else kwargs.get({param.name!r}, _MISSING)")
        test = _test(hints[param.name], "v", env)
        lines.append(f"    if v is not _MISSING and not ({test}):")
        lines.append(f"        _fail({param.name!r}, _h_{param.name}, v)")
    return _compile("\n".join(lines), env)


def _fail(name: str, hint: Any, value: object) -> typing.NoReturn:
    raise TypeError(_mismatch(name, hint, value))


def check_argument_types(cls: type[_T]) -> type[_T]:
    """Check the argument types of every public method and ``__init__`` a class defines.

    Each check is built from the method's annotations on its first call, so
    an annotation may name a class defined later in the module.
    """
    for name, func in list(vars(cls).items()):
        if inspect.isfunction(func) and (not name.startswith("_") or name == "__init__"):
            setattr(cls, name, _checking(func))
    return cls


def _checking(func: Callable[..., _T]) -> Callable[..., _T]:
    check: _ArgumentCheck | None = None

    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> _T:
        nonlocal check
        if check is None:
            check = argument_check(func)
        check(args, kwargs)
        return func(*args, **kwargs)

    wrapper._checks_arguments = True  # type: ignore[attr-defined]
    return wrapper


def check_text(**values: str | Iterable[str] | None) -> None:
    """Raise :class:`~calendaring_sync.error.LoneSurrogateError` if a string holds a lone surrogate.

    The design page's lone surrogates section says why no store can keep
    one; refusing it where the string comes in keeps every store the same.

    Args:
        values: Each value by its argument's name: a string, strings, or
            ``None``.
    """
    for name, value in values.items():
        if value is None:
            continue
        for text in [value] if isinstance(value, str) else value:
            try:
                text.encode()
            except UnicodeEncodeError as exc:
                raise LoneSurrogateError(
                    f"{name} holds a lone surrogate at {exc.start}, which can't be stored"
                ) from exc


def check_data(
    fingerprint: str | None,
    containers: frozenset[str],
    logical_key: str | None,
    **values: str | None,
) -> None:
    """Check the strings of a copy of an object with :func:`check_text`.

    Args:
        fingerprint: The copy's fingerprint.
        containers: The copy's containers.
        logical_key: The copy's logical key.
        values: Any other strings that come with the copy, by name.
    """
    check_text(fingerprint=fingerprint, containers=containers, logical_key=logical_key, **values)
