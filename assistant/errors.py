class AssistantError(Exception):
    """An intentional, safe-to-display error (never include credentials)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status
