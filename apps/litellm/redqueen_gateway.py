"""Use the upstream CLI unchanged, with a pre-start ASGI error boundary."""
import importlib.metadata
from contextlib import asynccontextmanager

import litellm

from redqueen_reliability import RedqueenErrors


def install_boundary():
    # The hook and error contract is tested against this exact release. Fail
    # closed on an upgrade until the same provider/proxy contracts pass again.
    for package, expected in (("litellm", "1.104.2"), ("openai", "2.33.0"), ("httpx", "0.28.1")):
        if importlib.metadata.version(package) != expected:
            raise RuntimeError(f"redqueen gateway policy requires {package} {expected}")
    from litellm.proxy.proxy_server import app
    app.add_middleware(RedqueenErrors)
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        try:
            async with original_lifespan(application):
                yield
        finally:
            client = litellm.aclient_session
            if client is not None and getattr(client, "_redqueen_policy", False):
                await client.aclose()

    app.router.lifespan_context = lifespan
    return app


if __name__ == "__main__":
    install_boundary()
    from litellm.proxy.proxy_cli import run_server
    run_server()
