"""hyperun gateway server.

The package a user installs is called `hyperun` and is the CLI (stage 2). It was
`ddpsrun` until 2026-09-10; only the command word and the PyPI project changed,
and this python package keeps its own name because every deployment artefact
refers to it (the release workflow copies `ddpsrun_server/` into the zip). This
one runs in the cluster. See `docs/00-overview.md` for why the two exist.
"""

__version__ = "0.1.0"
