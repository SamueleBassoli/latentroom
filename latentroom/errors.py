"""Errors a user can see. Every engine module raises these; the web server turns them into
{"error": {"code", "message"}} with `status` as the HTTP status. Anything else is a bug:
the server answers 500 "internal" with a generic message and logs the details."""


class LatentroomError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code  # short machine word, e.g. "offline" or "model_missing"
        self.message = message  # one plain English sentence + what to do; never a prompt
        self.status = status
