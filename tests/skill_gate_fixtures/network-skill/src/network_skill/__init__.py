import socket


def run(host: str) -> str:
    """Resolve a host name through the system resolver."""
    return socket.gethostbyname(host)
