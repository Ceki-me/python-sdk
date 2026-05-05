class CekiError(Exception):
    pass


class AuthFailed(CekiError):
    pass


class RateLimitExceeded(CekiError):
    def __init__(self, retry_after: float = 1.0, message: str = "rate_limit"):
        super().__init__(message)
        self.retry_after = retry_after


class InsufficientFunds(CekiError):
    pass


class SessionEnded(CekiError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class CdpUnrecoverable(CekiError):
    def __init__(self, last_error: str):
        super().__init__(last_error)
        self.last_error = last_error


class ConnectionLost(CekiError):
    pass
