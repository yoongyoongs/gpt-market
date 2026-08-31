class RepositoryConflictError(RuntimeError):
    """An immutable identity or optimistic version points to different content."""


class RepositoryNotFoundError(LookupError):
    """A required referenced snapshot, run, revision, pack or profile does not exist."""
