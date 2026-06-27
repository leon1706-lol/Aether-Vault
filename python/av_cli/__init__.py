try:
    from ._version import __version__
except ImportError:
    # No build has run yet (e.g. a fresh editable install before setuptools-scm has
    # generated _version.py) — placeholder so imports never fail.
    __version__ = "0.0.0.dev0"
