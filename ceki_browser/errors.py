class CekiBrowserError(Exception):
    def __init__(self, message: str, code: int = 0):
        super().__init__(message)
        self.code = code


class AuthError(CekiBrowserError):
    pass


class ProviderDisconnected(CekiBrowserError):
    pass


class NavigationTimeout(CekiBrowserError):
    pass


class CommandTimeout(CekiBrowserError):
    pass


class RateLimited(CekiBrowserError):
    pass


class ProviderNotVerified(CekiBrowserError):
    pass


class HumanActionDeclined(CekiBrowserError):
    pass


class HumanActionTimeout(CekiBrowserError):
    pass


ERROR_CODE_MAP: dict[int, type[CekiBrowserError]] = {
    -1010: ProviderDisconnected,
    -1013: RateLimited,
    -1014: ProviderNotVerified,
}
