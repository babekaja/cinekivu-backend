"""Request-local, validated client context for provider requests.

Only Render's Cloudflare-provided address is trusted in Render. Arbitrary
X-Forwarded-For / X-Real-IP values supplied by API clients are never trusted.
"""
import ipaddress
import os
from contextvars import ContextVar
from starlette.requests import Request

_client_ip = ContextVar('cinekivu_provider_client_ip', default=None)


def forwarding_enabled():
    default = '1' if os.getenv('RENDER') == 'true' else '0'
    return os.getenv('CINEKIVU_FORWARD_CLIENT_IP', default) == '1'


def public_ip(value):
    try:
        address = ipaddress.ip_address(value or '')
        if address.is_global and not address.is_multicast:
            return str(address)
    except ValueError:
        pass
    return None


def provider_client_ip(request):
    if not forwarding_enabled():
        return None
    if os.getenv('RENDER') == 'true':
        # Render routes public service traffic through Cloudflare and its LB.
        # CF-Connecting-IP is overwritten at that boundary, unlike a caller's
        # arbitrary leftmost X-Forwarded-For value. Fail closed if unavailable.
        return public_ip(request.headers.get('cf-connecting-ip'))
    return public_ip(request.client.host if request.client else None)


def provider_client_headers():
    address = _client_ip.get()
    return {'X-Forwarded-For': address, 'X-Real-IP': address} if address else {}


class ProviderClientContextMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        token = _client_ip.set(provider_client_ip(Request(scope)))
        try:
            await self.app(scope, receive, send)
        finally:
            _client_ip.reset(token)
