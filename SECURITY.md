# Security

Fiscus holds a household's finances, so reports of a weakness are welcome.

## Reporting a vulnerability

Please report it privately, not in a public issue: on GitHub, open
**Security**, then **Report a vulnerability**
(<https://github.com/f3nici/fiscus/security/advisories/new>). Include the
version (the More page, or `/readyz`), what you did and what happened.

A fix goes out as a new release, and the advisory is published once it is
out.

## Supported versions

Only the newest release (`latest` on Docker Hub) gets fixes. `beta` is the
newest change merged to `main` and is not a release.

## Scope

How Fiscus protects its data is in [docs/06-security.md](docs/06-security.md).
Fiscus is meant to be reached over HTTPS through a reverse proxy; a weakness
that needs it served over plain HTTP, or its port opened beyond the proxy
(see the [guide](docs/guide.md#reverse-proxy-npmplus)), is out of scope.
