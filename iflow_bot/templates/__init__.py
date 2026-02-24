"""Templates module."""

from pathlib import Path
from importlib.resources import files


def get_template_path(name: str) -> Path:
    """Get the path to a template file."""
    templates_dir = Path(__file__).parent
    return templates_dir / name


def get_template_content(name: str) -> str:
    """Get the content of a template file."""
    template_path = get_template_path(name)
    if template_path.exists():
        return template_path.read_text(encoding="utf-8")
    return ""
