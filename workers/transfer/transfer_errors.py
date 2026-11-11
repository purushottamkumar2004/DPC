"""The two kinds of trouble a transfer job can meet.

TemporaryError: retrying later can fix it (no network, timeout, Graph 5xx/429, an expired
upload session). The job waits and continues where it stopped, without a new click.
PermanentError: retrying cannot fix it (permission refused, a bad model zip, staging full,
not enough disk). The job stops as `failed` with the reason; its partial data is kept, and the
next click on the same button continues from where it stopped once the cause is fixed.

Anything else is a programming or config error: the program exits and the Supervisor restarts
it, with last_error.json (D-073 D7).
"""


class TransferError(Exception):
    pass


class TemporaryError(TransferError):
    def __init__(self, message: str, retry_after_s: float = 0.0):
        super().__init__(message)
        self.retry_after_s = retry_after_s            # Graph's Retry-After, if it sent one


class PermanentError(TransferError):
    pass
