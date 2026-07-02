"""Job-board adapters — one board per module, self-registering.

Every module in this package is imported automatically, so adding a board is
one new file: define ``fetch_<board>_board(board_cfg)`` decorated with
``@board_fetcher("<strategy>")`` and add a ``[boards.<id>]`` block to
``config/defaults.toml``. Nothing else needs to change.
"""

from importlib import import_module
from pkgutil import iter_modules

for _mod in iter_modules(__path__):
    import_module(f"{__name__}.{_mod.name}")
