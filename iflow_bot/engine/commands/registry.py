from __future__ import annotations

from .base import ChatCommand
from .handlers.registry_builtins import builtin_commands


def build_command_registry() -> dict[str, ChatCommand]:
    registry: dict[str, ChatCommand] = {}
    for command in builtin_commands():
        registry[command.name] = command
        for alias in getattr(command, "aliases", ()):
            registry[alias] = command
    return registry
