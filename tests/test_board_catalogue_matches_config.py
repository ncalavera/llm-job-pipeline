"""Guard: the committed board catalogue matches config — one board count, not three.

Three docs used to state three different board counts. The fix made
`docs/job-boards-catalogue.md` the single source: its board table is generated
from `config._ALL_JOB_BOARDS` (== `settings.boards()`, a pure `defaults.toml`
read) by `scripts/gen_board_table.py`. This test regenerates the table and
asserts the committed block is byte-identical — so a new/renamed `[boards.*]`
block that isn't re-rendered fails CI instead of silently drifting.

Regenerate after a board change: `python3 scripts/gen_board_table.py --write`.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import gen_board_table  # noqa: E402


def test_catalogue_table_matches_config():
    committed = gen_board_table.committed_block()
    rendered = gen_board_table.render_table()
    assert committed == rendered, (
        "docs/job-boards-catalogue.md is out of sync with config/defaults.toml "
        "[boards.*] — run `python3 scripts/gen_board_table.py --write` and commit."
    )


def test_generator_source_is_all_job_boards():
    """The renderer must read the same set config exposes as _ALL_JOB_BOARDS, so
    the doc count can never diverge from what the pipeline actually loads."""
    import settings

    assert gen_board_table._boards() == settings.boards()


def test_every_board_row_is_present_and_stated_count_matches():
    """The stated count in the block equals the number of table rows equals the
    number of configured boards."""
    import settings

    boards = settings.boards()
    block = gen_board_table.render_table()
    # One `| \`id\` |` row per board.
    rows = [ln for ln in block.splitlines() if ln.startswith("| `")]
    assert len(rows) == len(boards)
    assert f"**{len(boards)} built-in job boards**" in block
    for board_id in boards:
        assert f"| `{board_id}` |" in block, f"{board_id} missing from the table"


# --- docs/index.html onboarding board picker (BOARD_CATALOG JS block) -------


def test_no_generated_doc_is_stale():
    """BOTH generated docs (the .md table AND the docs/index.html board picker)
    must match config — a new/renamed [boards.*] block that wasn't regenerated
    fails CI instead of silently drifting."""
    stale = gen_board_table.stale_targets()
    assert not stale, (
        "Generated board doc(s) out of sync with config/defaults.toml [boards.*]: "
        + ", ".join(p.name for p in stale)
        + " — run `python3 scripts/gen_board_table.py --write` and commit."
    )


def test_onboarding_catalog_carries_every_board_with_both_languages():
    """The onboarding picker (docs/index.html BOARD_CATALOG) lists every board
    and — because a new user may run the questionnaire in either language — a
    non-empty EN (`audience`) and RU (`audience_ru`) line for each."""
    import settings

    boards = settings.boards()
    block = gen_board_table.render_board_catalog_js()
    for board_id, cfg in boards.items():
        assert f'id: "{board_id}"' in block, f"{board_id} missing from BOARD_CATALOG"
        assert cfg.get("audience"), f"{board_id} has no EN audience in config"
        assert cfg.get("audience_ru"), f"{board_id} has no RU audience in config"
    # One object per board, no more (the picker shows exactly the config set).
    assert block.count("{ id:") == len(boards)
