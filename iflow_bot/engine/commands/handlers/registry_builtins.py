from __future__ import annotations

from .cron import CronCommand
from .help import HelpCommand
from .language import LanguageCommand
from .model import ModelCommand
from .ralph import RalphCommand
from .session import CompactCommand, NewSessionCommand
from .skills import SkillsCommand
from .status import StatusCommand


def builtin_commands():
    return [
        HelpCommand(),
        StatusCommand(),
        NewSessionCommand(),
        CompactCommand(),
        ModelCommand(),
        CronCommand(),
        RalphCommand(),
        SkillsCommand(),
        LanguageCommand(),
    ]
