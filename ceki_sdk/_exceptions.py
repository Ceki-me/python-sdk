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


class ChatSendFailed(CekiError):
    def __init__(self, status: int, message: str):
        super().__init__(f"chat send failed [{status}]: {message}")
        self.status = status
        self.message_text = message


class ProviderOffline(CekiError):
    pass


class ProviderDisconnected(CekiError):
    """Provider's browser disconnected during rental and didn't reconnect within grace period."""
    pass


class SessionNotFound(CekiError):
    pass


class SessionExpired(SessionNotFound):
    pass


class NotOwner(CekiError):
    pass


class CaptchaError(CekiError):
    pass


class CaptchaTimeoutError(CaptchaError):
    def __init__(self, phase: str):
        super().__init__(f"captcha timeout: {phase}")
        self.phase = phase
