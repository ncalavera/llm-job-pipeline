"""Import-cheap helper for the pre-import ``--help`` guard.

Entrypoint scripts connect to the database and load the user profile at import
time (config.py / company_registry.py build their module-level state on import),
so they must short-circuit ``--help`` BEFORE those heavy imports run — otherwise
``script.py --help`` needlessly opens the DB and parses the profile.

This module centralises the argv check so the logic lives in one place instead
of being copy-pasted into every entrypoint. It imports nothing but stdlib, so
importing it triggers no side effects.

Usage at the very top of an entrypoint, ABOVE the heavy imports::

    from cli_help import wants_help
    if __name__ == "__main__" and wants_help():
        build_parser().parse_args()   # argparse prints help and exits 0
"""

import sys


def wants_help(argv: list[str] | None = None) -> bool:
    """True if ``-h``/``--help`` appears as an option.

    Tokens after the conventional ``--`` end-of-options marker are treated as
    values, not flags, so a positional value of ``-h`` (e.g. ``mark <id> -- -h``)
    does not trigger the help short-circuit.
    """
    args = sys.argv[1:] if argv is None else list(argv)
    if "--" in args:
        args = args[: args.index("--")]
    return any(a in ("-h", "--help") for a in args)
