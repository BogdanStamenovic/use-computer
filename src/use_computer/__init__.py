"""use-computer: see and drive a GNOME Wayland desktop from an agent."""

from __future__ import annotations

__version__ = "0.1.0"


class UseComputerError(Exception):
    """Raised when an operation cannot be completed; the message is shown to the agent."""


__all__ = ["UseComputerError", "__version__"]
