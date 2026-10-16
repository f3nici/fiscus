"""Reminders through ntfy: the public ntfy.sh or a server of the person's own.

Off by default, per user. Public servers are allowed, and so are private ranges,
where a homelab ntfy lives; loopback, link-local (which includes cloud metadata
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


def normalise(url: str, topic: str) -> tuple[str, str]:
    """The server and topic as typed, made whole: `ntfy.sh` is https://ntfy.sh,
    and a topic given in the address (https://ntfy.sh/mytopic, as ntfy's own
    pages show one) moves to the topic, unless a different topic was typed."""
    url, topic = url.strip(), topic.strip()
    if url and '://' not in url:
        url = f'https://{url}'
    parts = urlsplit(url)
    path = parts.path.strip('/')
    if path and '/' not in path and not parts.query and not parts.fragment and topic in ('', path):
        url, topic = urlunsplit((parts.scheme, parts.netloc, '', '', '')), path
    return url.rstrip('/'), topic


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
        raise NtfyError('Give the server address only, e.g. https://ntfy.sh')
    try:
        port = parts.port
    except ValueError:
        raise NtfyError('The port in the ntfy server address is not a number') from None
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
            infos = socket.getaddrinfo(host, port or 443, proto=socket.IPPROTO_TCP)
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
    path = f'/{prefs.ntfy_topic.strip()}'
    if addrs and str(addrs[0]) != host:
        # A name: connect to an address that was checked, still asking for
        # (and verifying the certificate of) the name. Each in turn, as a
        # plain connection would, in case the first is unreachable.
        port = f':{parts.port}' if parts.port else ''
        tries = [
            (
                urlunsplit(
                    (parts.scheme, (f'[{ip}]' if ip.version == 6 else str(ip)) + port, path, '', '')
                ),
                {**headers, 'Host': parts.netloc},
                {'sni_hostname': host},
            )
            for ip in dict.fromkeys(addrs)
        ]
    else:
        tries = [(urlunsplit((parts.scheme, parts.netloc, path, '', '')), headers, {})]
    resp: httpx.Response | None = None
    failed: Exception | None = None
    with httpx.Client(timeout=TIMEOUT, follow_redirects=False, trust_env=False) as client:
        for url, sent, extensions in tries:
            try:
                resp = client.post(
                    url, content=message.encode(), headers=sent, extensions=extensions
                )
                break
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                failed = exc
            except httpx.HTTPError as exc:
                raise NtfyError(
                    f'Could not reach the ntfy server: {exc.__class__.__name__}'
                ) from exc
    if resp is None:
        name = failed.__class__.__name__ if failed else 'no address'
        raise NtfyError(f'Could not reach the ntfy server: {name}') from failed
    if resp.status_code >= 300:
        raise NtfyError(f'The ntfy server answered {resp.status_code}')
