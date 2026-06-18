"""Illustration packs — map a pack name to the companion/accent image paths.

Each tab (catalog / companies / pipeline / archive) shows a companion image and
an accent image. The committed ``"default"`` pack points at the generic art in
``public/images/``. Any other pack name resolves its images from a per-pack
subdirectory ``public/images/<pack>/`` — that directory can be kept local-only
(gitignored) so personal art never enters git while still being deployable.

The generator bakes the resolved ``{slot: relative_url}`` map into
``public/data.js`` (``config.pack_images``); the frontend swaps each
``img[data-img="<slot>"]`` ``src`` to the pack's URL at load time.
"""

from __future__ import annotations

# The image slots the dashboard wires per tab. Stable keys — the HTML marks each
# image with data-img="<slot>" and the frontend swaps src by slot.
SLOTS = (
    "illus-catalog",
    "illus-companies",
    "illus-pipeline",
    "illus-archive",
    "accent-catalog",
    "accent-companies",
    "accent-pipeline",
    "accent-archive",
)

DEFAULT_PACK = "default"


def pack_images(pack: str) -> dict[str, str]:
    """Return ``{slot: relative_image_url}`` for ``pack``.

    ``"default"`` (or empty/unknown) → the committed generic art directly under
    ``images/``. Any other pack → ``images/<pack>/<slot>.png``. URLs are relative
    to the dashboard root, matching how ``index.html`` already references images.
    """
    name = (pack or "").strip()
    if not name or name == DEFAULT_PACK:
        return {slot: f"images/{slot}.png" for slot in SLOTS}
    return {slot: f"images/{name}/{slot}.png" for slot in SLOTS}
