"""What kind and backend plugins share: loading entry points, the plugin API, guarded calls.

Kinds (``prov.kinds``) and backends (``prov.backends``) are both plugins registered through
entry points, prov's own as well as third-party ones. This module is neutral: it knows no
particular kind or backend, and no particular entry-point group.

- ``PLUGIN_API``: the contract version a plugin declares in its ``PLUGIN_API`` member.
- ``DISTRIBUTION``: the distribution whose entry points are prov's built-ins
  (``from_distribution``); only those may use bare kind names.
- ``load(ep, what)``: load one entry point; any failure is a ConfigError (exit 2).
- ``check_api(plugin, who, required)``: refuse a plugin written for another contract.
- ``guarded_call(who, obj, method, args, check, expected)``: call into a plugin; exceptions
  and wrongly typed results become a ConfigError naming the plugin, with escaped text.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Optional, Sequence

from .schema import ConfigError
from .text import esc

PLUGIN_API = 1
DISTRIBUTION = "provision-hub"


def normalise(name: str) -> str:
    """A distribution name in its PEP 503 normal form (``Provision_Hub`` -> ``provision-hub``)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def distribution(ep: Any) -> Optional[str]:
    """The normalised name of the distribution that registered ``ep``, if known."""
    dist = getattr(ep, "dist", None)
    name = getattr(dist, "name", None)
    return normalise(name) if isinstance(name, str) else None


def from_distribution(ep: Any) -> bool:
    """True when ``ep`` was registered by prov's own distribution (a built-in plugin)."""
    return distribution(ep) == DISTRIBUTION


def origin(ep: Any) -> str:
    """``<name> = <value>`` of an entry point, escaped for messages."""
    return esc(f"{ep.name} = {ep.value}")


def load(ep: Any, what: str) -> Any:
    """Load an entry point; a failing import is a ConfigError naming it (``what``: "kind", "backend")."""
    try:
        return ep.load()
    except (Exception, SystemExit) as exc:  # a broken plugin must not hide the others' errors; KeyboardInterrupt still stops prov
        raise ConfigError(f"cannot load {what} plugin {origin(ep)}: {esc(str(exc))}") from exc


def check_api(plugin: Any, who: str, required: bool) -> None:
    """Refuse a plugin whose ``PLUGIN_API`` is not this prov's; a missing one is 1 unless ``required``."""
    if not hasattr(plugin, "PLUGIN_API"):
        if required:
            raise ConfigError(f"{who} lacks required member(s): PLUGIN_API (this prov supports PLUGIN_API = {PLUGIN_API})")
        return
    api = plugin.PLUGIN_API
    if isinstance(api, bool) or not isinstance(api, int) or api != PLUGIN_API:
        raise ConfigError(f"{who}: PLUGIN_API {esc(repr(api))[:40]} is not supported (this prov supports PLUGIN_API = {PLUGIN_API})")


def guarded_call(
    who: str,
    obj: Any,
    method: str,
    args: Sequence[Any],
    check: Callable[[Any], bool] = lambda value: True,
    expected: str = "",
) -> Any:
    """Run ``obj.<method>(*args)``; exceptions and results failing ``check`` become a ConfigError.

    ``who`` names the plugin (``backend 'x'``, ``kind 'y'``). Plugin text in the message
    (exception text, a plugin's own ConfigError) is escaped with ``prov.text.esc``, so it is
    always one printable line.
    """
    try:
        result = getattr(obj, method)(*args)
    except ConfigError as exc:  # a plugin's own error: keep its text, escaped, and name the plugin
        raise ConfigError(f"{who} failed in {method}: {esc(str(exc))}") from exc
    except (Exception, SystemExit) as exc:  # a plugin calling sys.exit is a plugin failure too
        raise ConfigError(f"{who} failed in {method}: {esc(type(exc).__name__)}: {esc(str(exc))}") from exc
    if not check(result):
        raise ConfigError(f"{who} failed in {method}: returned {esc(type(result).__name__)}, expected {expected}")
    return result
