"""Shared constants for the documentation website builder.

The public origin is the absolute URL the site is served from. It is distinct
from ``base_url``, which is the *path prefix* used for asset and navigation
links and is deliberately empty for root-served deployments. Canonical tags,
Open Graph URLs, ``sitemap.xml`` and ``robots.txt`` all need the absolute
origin, so they must not read ``base_url``.
"""

# Fallback origin used when no ``--site-url`` is supplied. Deployments should
# pass the origin explicitly; this default exists so that local builds and the
# test-suite produce stable, non-empty absolute URLs.
DEFAULT_PUBLIC_ORIGIN = "https://cbtw-apac.github.io/HarborRAG"


def resolve_public_origin(base_url: str, public_origin: str | None = None) -> str:
    """Return the absolute origin used for canonical, Open Graph and SEO URLs.

    ``public_origin`` (from ``--site-url``) wins when set. ``base_url`` is
    honoured next to preserve the historical behaviour of builds that pass an
    absolute base URL. ``DEFAULT_PUBLIC_ORIGIN`` is the last resort.
    """
    if public_origin:
        return public_origin.rstrip("/")
    if base_url:
        return base_url.rstrip("/")
    return DEFAULT_PUBLIC_ORIGIN
