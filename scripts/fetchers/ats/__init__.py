"""ATS adapters — one provider per module, self-registering.

Every module in this package is imported automatically, so adding an ATS is
one new file: define ``fetch_<provider>()`` behind the ``@company_fetcher``
error boundary and register a ``@register_company("<strategy>")`` entry that
unpacks the company config. Nothing else needs to change.
"""

from importlib import import_module
from pkgutil import iter_modules

for _mod in iter_modules(__path__):
    import_module(f"{__name__}.{_mod.name}")
