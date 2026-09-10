"""hyperun — submit a GPU job and get results back.

The command is `hyperun`. It talks to a gateway server over HTTPS and needs no
kubectl, no kubeconfig, and no cloud account. See `docs/00-overview.md`.
"""

# ★ READ FROM THE INSTALLED METADATA, NOT WRITTEN HERE. This was the literal
# "0.1.0" while pyproject.toml said 0.1.1, and the published wheel shipped BOTH:
# hyperun-0.1.1.dist-info/METADATA says `Version: 0.1.1` and this file inside the
# same wheel said 0.1.0. Two answers to "which version am I running" is exactly
# the thing that has to be right when somebody reports a bug against a deployed
# CLI. pyproject.toml is the one place a version is now set.
#
# The fallback is for running out of a source tree with nothing installed, where
# there is no metadata to read; it says so rather than inventing a number.
try:
    from importlib.metadata import PackageNotFoundError, version as _installed_version

    try:
        __version__ = _installed_version("hyperun")
    except PackageNotFoundError:            # pragma: no cover - source checkout
        __version__ = "0+unknown"
except ImportError:                         # pragma: no cover - very old Python
    __version__ = "0+unknown"
