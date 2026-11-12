"""The web app rejects direct public peers regardless of forwarded headers."""
import asyncio
from types import SimpleNamespace as NS

from app import access_control, main


def test_private_and_vpn_clients_allowed_but_public_addresses_blocked():
    for address in ("127.0.0.1","192.168.1.2","172.18.0.5",
                    "10.0.0.9","100.97.1.2","::1"):
        assert access_control.allowed(address)
    for address in ("8.8.8.8","1.1.1.1","2606:4700:4700::1111",
                    "invalid",None):
        assert not access_control.allowed(address)


def test_custom_trusted_networks_and_unsafe_override_are_explicit():
    assert access_control.allowed("203.0.113.5","203.0.113.0/24")
    assert not access_control.allowed("203.0.114.5","203.0.113.0/24")
    assert access_control.allowed("8.8.8.8","*")
    assert not access_control.allowed("8.8.8.8","invalid-network")


def test_middleware_rejects_public_socket_peer_even_when_forwarded_as_local():
    async def exercise():
        invoked=False
        async def downstream(request):
            nonlocal invoked
            invoked=True
        request=NS(client=NS(host="8.8.8.8"),
                   headers={"x-forwarded-for":"127.0.0.1"})
        response=await main.private_workspace_network(request,downstream)
        assert response.status_code==403
        assert not invoked
    asyncio.run(exercise())
