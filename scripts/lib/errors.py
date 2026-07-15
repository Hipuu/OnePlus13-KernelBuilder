class BuildToolError(RuntimeError):
    """A user-facing, deterministic build orchestration error."""


class SourceChanged(BuildToolError):
    """The official manifest branch moved beyond the pinned lock."""
