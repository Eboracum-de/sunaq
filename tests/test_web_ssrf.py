import inspect
from pathlib import Path
import socket

import pytest

import rag.web_research as web_research


def test_public_web_blocks_loopback_link_local_and_ipv6_loopback():
    for url in (
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.100.100.200/",
        "http://[::ffff:100.100.100.200]/",
        "http://[::1]/",
    ):
        with pytest.raises(RuntimeError):
            web_research._validate_public_url(url, allowed_ports={80, 443})


def test_public_web_rejects_userinfo_and_non_web_ports():
    with pytest.raises(RuntimeError, match="userinfo"):
        web_research._validate_public_url(
            "https://user:secret@example.org/",
            allowed_ports={80, 443},
        )

    with pytest.raises(RuntimeError, match="port 8080"):
        web_research._validate_public_url(
            "http://example.org:8080/",
            allowed_ports={80, 443},
        )


def test_public_web_rejects_mixed_public_private_dns(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", 0)),
        ],
    )
    assert web_research._is_public_host("rebind.example") is False


def test_validated_web_target_is_ip_pinned(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        ],
    )
    targets = web_research._validated_connect_targets(
        "https://example.org/path?q=1",
        allowed_ports={80, 443},
    )
    assert targets == [
        ("https://93.184.216.34/path?q=1", "example.org", "example.org")
    ]


def test_web_fetcher_disables_environment_proxy_and_pins_connect_target():
    fetcher = web_research.WebFetcher({"fetch": {}})
    assert fetcher.allowed_ports == {80, 443}

    source = inspect.getsource(web_research.WebFetcher.fetch)
    assert "trust_env=False" in source
    assert "asyncio.to_thread" in source
    assert "_validated_connect_targets" in source
    assert '"Host": host_header' in source
    assert '{"sni_hostname": sni_hostname}' in source
    assert "content-length" in source
    assert "aiter_bytes" in source


def test_playwright_renderer_is_network_free_snapshot_renderer():
    renderer = (
        Path(__file__).resolve().parent.parent
        / "install"
        / "components"
        / "playwright-renderer"
        / "app"
        / "renderer.py"
    ).read_text(encoding="utf-8")

    assert "html: str = Field" in renderer
    assert 'await context.route("**/*", _route_guard)' in renderer
    assert 'route_web_socket("**/*", _websocket_guard)' in renderer
    assert 'await page.set_content(' in renderer
    assert "page.goto(" not in renderer
    assert 'await route.abort("blockedbyclient")' in renderer



def test_web_fetcher_creates_client_inside_each_redirect_hop():
    source = inspect.getsource(web_research.WebFetcher.fetch)
    loop_pos = source.index("for _ in range(self.max_redirects + 1):")
    client_pos = source.index("async with httpx.AsyncClient(")
    stream_pos = source.index("async with client.stream(")
    assert loop_pos < client_pos < stream_pos
    # Pool lifetime is therefore bounded to one validated target attempt/hop.
    assert source.count("async with httpx.AsyncClient(") == 1
