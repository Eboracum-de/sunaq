import socket

import pytest

from rag.network_policy import validated_public_connect_targets


def test_public_connect_targets_pin_valid_public_dns(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        ],
    )

    assert validated_public_connect_targets(
        "https://example.org/document.pdf",
        allowed_ports={443},
        allowed_schemes={"https"},
    ) == [
        (
            "https://93.184.216.34/document.pdf",
            "example.org",
            "example.org",
        )
    ]


def test_public_connect_targets_reject_private_and_mixed_dns(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", 443)),
        ],
    )

    with pytest.raises(RuntimeError, match="private/local"):
        validated_public_connect_targets(
            "https://rebind.example/document.pdf",
            allowed_ports={443},
            allowed_schemes={"https"},
        )


def test_public_connect_targets_reject_non_https_and_nonstandard_port():
    with pytest.raises(RuntimeError, match="unsupported"):
        validated_public_connect_targets(
            "http://example.org/document.pdf",
            allowed_ports={443},
            allowed_schemes={"https"},
        )

    with pytest.raises(RuntimeError, match="port 8443"):
        validated_public_connect_targets(
            "https://example.org:8443/document.pdf",
            allowed_ports={443},
            allowed_schemes={"https"},
        )
