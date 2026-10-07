"""Reminders through a self-hosted ntfy server.

Off by default, per user. A homelab ntfy usually lives on a private address, so
private ranges are allowed; loopback, link-local (which includes cloud metadata
at 169.254.169.254) and unspecified addresses are refused, so the setting cannot
be used to make the Fiscus container poke itself.

Messages carry a bill name, a date and an amount, never an account number or a
transaction description.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlsplit, urlunsplit

import httpx

from fiscus.models import UserSettings

TOPIC = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
TIMEOUT = 10.0


class NtfyError(ValueError):
    pass


def _blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved
    )


Address = ipaddress.IPv4Address | ipaddress.IPv6Address


def validate_target(url: str, topic: str, *, resolve: bool = True) -> list[Address]:
    """Refuse a server this container must not reach. Returns the addresses
    checked, so the request can go to one of them and not to whatever the
    name resolves to a moment later."""
    parts = urlsplit(url.strip())
    if parts.scheme not in {'http', 'https'} or not parts.hostname:
        raise NtfyError('The ntfy server must be an http:// or https:// address')
    if parts.username or parts.password:
        raise NtfyError('Put credentials in the access token field, not the URL')
    if parts.path not in ('', '/') or parts.query or parts.fragment:
        raise NtfyError('Give the server address only, e.g. https://ntfy.home.lan')
    if not TOPIC.match(topic.strip()):
        raise NtfyError('The topic is letters, numbers, dash and underscore, up to 64 characters')
    host = parts.hostname
    try:
        addrs = [ipaddress.ip_address(host)]
    except ValueError:
        if host.lower() in {'localhost'} or host.lower().endswith('.localhost'):
            raise NtfyError('The ntfy server cannot be this machine') from None
        if not resolve:
            return []
        try:
            infos = socket.getaddrinfo(host, parts.port or 443, proto=socket.IPPROTO_TCP)
        except OSError as exc:
            raise NtfyError(f'Could not find {host}') from exc
        addrs = [ipaddress.ip_address(i[4][0]) for i in infos]
    for ip in addrs:
        if _blocked(ip):
            raise NtfyError('That address is not allowed for the ntfy server')
    return addrs


def send(
    prefs: UserSettings, title: str, message: str, *, priority: str = 'default', tags: str = ''
) -> None:
    if not prefs.ntfy_url or not prefs.ntfy_topic:
        raise NtfyError('Set the ntfy server and topic first')
    addrs = validate_target(prefs.ntfy_url, prefs.ntfy_topic)
    parts = urlsplit(prefs.ntfy_url.strip())
    host = parts.hostname or ''
    headers = {'Title': title.encode('ascii', 'replace').decode(), 'Priority': priority}
    if tags:
        headers['Tags'] = tags
    if prefs.ntfy_token:
        headers['Authorization'] = f'Bearer {prefs.ntfy_token}'
    netloc, extensions = parts.netloc, {}
    if addrs and str(addrs[0]) != host:
        # A name: connect to the address that was checked, still asking for
        # (and verifying the certificate of) the name.
        ip = addrs[0]
        netloc = (f'[{ip}]' if ip.version == 6 else str(ip)) + (
            f':{parts.port}' if parts.port else ''
        )
        headers['Host'] = parts.netloc
        extensions['sni_hostname'] = host
    url = urlunsplit((parts.scheme, netloc, f'/{prefs.ntfy_topic.strip()}', '', ''))
    try:
        with httpx.Client(timeout=TIMEOUT, follow_redirects=False, trust_env=False) as client:
            resp = client.post(
                url, content=message.encode(), headers=headers, extensions=extensions
            )
    except httpx.HTTPError as exc:
        raise NtfyError(f'Could not reach the ntfy server: {exc.__class__.__name__}') from exc
    if resp.status_code >= 300:
        raise NtfyError(f'The ntfy server answered {resp.status_code}')
