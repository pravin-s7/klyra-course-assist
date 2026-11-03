"""Klyra: multi-agent question-paper and coding-problem generation."""
import socket

# Prioritize IPv4 over IPv6 to avoid connection timeouts on networks with broken IPv6 routing
_orig_getaddrinfo = socket.getaddrinfo


def _prefer_ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    res = _orig_getaddrinfo(host, port, family, type, proto, flags)
    return sorted(res, key=lambda x: 0 if x[0] == socket.AF_INET else 1)


socket.getaddrinfo = _prefer_ipv4_getaddrinfo
