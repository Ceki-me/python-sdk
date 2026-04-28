"""Login flow — inject credentials + handle 2FA via human action."""
import asyncio

from ceki_browser import Browser


async def main():
    async with Browser(token="YOUR_TOKEN") as br:
        async with await br.session(mode="persona", domain_hints=["app.example.com"]) as s:
            await s.navigate("https://app.example.com/login")

            # Inject stored credentials (requires verified provider)
            await s.inject_credentials(
                secret_id="secret-abc-123",
                target={
                    "username_selector": "#email",
                    "password_selector": "#password",
                    "submit_selector": "#login-btn",
                },
            )

            # If 2FA is required, ask the browser owner to complete it
            result = await s.request_human_action(
                action_type="2fa",
                message="Please enter the 2FA code from your authenticator app",
                timeout_sec=120,
            )
            print(f"2FA status: {result.status}")


if __name__ == "__main__":
    asyncio.run(main())
