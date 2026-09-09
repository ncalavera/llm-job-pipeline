"""Combined nightly discovery scoring/facts orchestration (no model calls)."""

from __future__ import annotations
import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import prepare_screening as prep
import score_vacancies as scoremod


def _text(row):
    return (row.get("full_description") or "").strip()


def _fp(text):
    return prep.fingerprint(text)


def _score_fp():
    return prep._sha(scoremod.SYSTEM_PROMPT)


def _cap(limit):
    import settings

    if limit is not None and limit < 0:
        raise ValueError("limit must be non-negative")

    vals = [
        settings.screening().get("nightly_limit"),
        settings.nightly().get("max_items_per_night"),
    ]
    vals = [int(v) for v in vals if isinstance(v, (int, float)) and v > 0]
    cap = min(vals) if vals else 0
    return min(cap, limit) if limit is not None else cap


def build_payload(row):
    text = _text(row)
    current = prep.is_current(row)
    score_requested = row.get("llm_score") is None or row.get("llm_score") < 0
    facts_requested = not current
    return {
        "payload_kind": "discovery",
        "id": str(row["id"]),
        "org": row.get("org"),
        "title": row.get("title"),
        "fingerprint": _fp(text),
        "existing_score": row.get("llm_score"),
        "score_prompt_fingerprint": _score_fp(),
        "scoring": (
            {
                "member_ids": [str(row["id"])],
                "org": row.get("org"),
                "title": row.get("title"),
                "system_prompt": scoremod.SYSTEM_PROMPT,
                "user_msg": scoremod._build_user_msg(row),
            }
            if score_requested
            else None
        ),
        "screening": (prep.build_payload(row) if facts_requested else None),
    }


def select_payloads(limit=None):
    from database_supabase import load_vacancies
    import quality

    rows = load_vacancies(
        status="unseen", include_candidate_companies=True, include_scoring_excluded=False
    )
    eligible = []
    for row in rows.values():
        row = dict(row)
        text = _text(row)
        if not text or quality.is_boilerplate_junk(text):
            continue
        score = row.get("llm_score")
        if score is not None and score >= 40:
            continue
        if prep.is_current(row) and score is not None and score >= 0:
            continue
        eligible.append(row)
    eligible.sort(key=lambda r: (str(r.get("first_seen") or ""), str(r.get("id"))))
    cap = _cap(limit)
    return [build_payload(r) for r in eligible[:cap]]


def _load_row(cur, vid):
    cur.execute(
        "SELECT v.*, c.status AS company_status FROM vacancy v "
        "JOIN company c ON c.id=v.company_id WHERE v.id=%s",
        (vid,),
    )
    return cur.fetchone()


def _allowed(row, payload):
    return (
        row
        and row.get("status") == "unseen"
        and row.get("company_status") in {"active", "candidate"}
        and row.get("scoring_excluded_reason") is None
        and _fp(_text(row)) == payload.get("fingerprint")
    )


def completion(payloads):
    from database_supabase import get_conn
    from db_backend import RealDictCursor

    out = {}
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    for p in payloads:
        row = _load_row(cur, str(p.get("id")))
        if not _allowed(row, p):
            out[str(p.get("id"))] = "skipped"
            continue
        score_ok = p.get("scoring") is None or (
            row.get("llm_score") is not None and row.get("llm_score") >= 0
        )
        facts_ok = p.get("screening") is None or prep.is_current(row)
        protected = row.get("llm_score") is not None and row["llm_score"] >= 40
        out[str(p["id"])] = (
            "ready" if score_ok and facts_ok else "skipped" if protected else "pending"
        )
    cur.close()
    return out


def _validate_score(s, p):
    if not isinstance(s, dict):
        return None, "scoring missing"
    n = scoremod._coerce_score(s.get("score"))
    if n is None:
        return None, "invalid score"
    if not isinstance(s.get("reasoning"), str) or not s["reasoning"].strip():
        return None, "reasoning missing"
    if not isinstance(s.get("short_summary"), str) or len(s["short_summary"]) < 200:
        return None, "summary too short"
    if any(not isinstance(s.get(k, ""), str) for k in ("country", "work_mode")):
        return None, "invalid geography type"
    if not isinstance(s.get("hard_requirements", []), list) or any(
        not isinstance(v, str) for v in s.get("hard_requirements", [])
    ):
        return None, "invalid hard requirements"
    return scoremod._make_score_data({**s, "score": n}, p), None


def _posting_from_payload(payload):
    for section in (payload.get("screening"), payload.get("scoring")):
        if isinstance(section, dict):
            msg = section.get("user_msg", "")
            if "**Posting text:**\n" in msg:
                return msg.split("**Posting text:**\n", 1)[1]
            if "**Full Description:**\n" in msg:
                return msg.split("**Full Description:**\n", 1)[1]
    return ""


def validate_result(payload, result):
    """Pure validation for a combined file result; raises ValueError on failure."""
    if not isinstance(payload, dict) or not isinstance(result, dict):
        raise ValueError("payload and result must be objects")
    if str(result.get("id")) != str(payload.get("id")):
        raise ValueError("wrong result id")
    if result.get("fingerprint") != payload.get("fingerprint"):
        raise ValueError("wrong result fingerprint")
    if isinstance(payload.get("scoring"), dict) and payload["scoring"].get("system_prompt"):
        if _score_fp() != prep._sha(payload["scoring"]["system_prompt"]):
            raise ValueError("stale scoring prompt fingerprint")
    if (
        payload.get("scoring") is not None
        and payload.get("score_prompt_fingerprint") != _score_fp()
    ):
        raise ValueError("stale scoring prompt fingerprint")
    post = _posting_from_payload(payload)
    if not post:
        raise ValueError("posting text missing from trusted payload")
    clean = dict(result)
    if payload.get("scoring") is not None:
        data, err = _validate_score(result.get("scoring"), payload)
        if err:
            raise ValueError(err)
        clean["scoring"] = result["scoring"]
    elif result.get("scoring") is not None:
        raise ValueError("unexpected scoring result")
    if payload.get("screening") is not None:
        facts, err = prep.validate_result(result.get("screening"), post)
        if facts is None:
            raise ValueError(err or "invalid screening result")
        if "work_profile" not in facts:
            raise ValueError("work_profile missing")
        clean["screening"] = facts
    elif result.get("screening") is not None:
        raise ValueError("unexpected screening result")
    return clean


def save_results(results, payloads, model=None):
    """Commit validated per-record CAS updates; never change a decision or an old score."""
    from database_supabase import get_conn, _scored_by_supported
    from db_backend import Json, RealDictCursor

    byid = {str(p["id"]): p for p in payloads}
    if len(byid) != len(payloads):
        raise ValueError("duplicate IDs in trusted payload")
    duplicates = Counter(str(r.get("id")) for r in results if isinstance(r, dict))
    counts = {"prepared": 0, "scored": 0, "skipped": 0, "errors": []}
    conn = get_conn()
    provenance = _scored_by_supported()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        for item in results:
            vid = str(item.get("id") or "") if isinstance(item, dict) else ""
            if vid not in byid or duplicates[vid] > 1:
                counts["errors"].append((vid, "duplicate or unknown id"))
                continue
            payload = byid[vid]
            row = _load_row(cur, vid)
            if not _allowed(row, payload):
                counts["skipped"] += 1
                continue
            existing = row.get("llm_score")
            if (
                existing != payload.get("existing_score")
                or (existing is not None and existing >= 40)
                or (payload.get("scoring") is not None and existing is not None and existing >= 0)
            ):
                counts["skipped"] += 1
                continue
            # A repeated sweep of a facts-only result costs nothing and doesn't rewrite timestamps.
            if payload.get("scoring") is None and prep.is_current(row):
                counts["skipped"] += 1
                continue
            try:
                clean = validate_result(payload, item)
            except (ValueError, TypeError) as exc:
                counts["errors"].append((vid, str(exc)))
                continue
            assignments, values = [], []
            score_data = None
            if payload.get("scoring") is not None:
                score_data, _ = _validate_score(clean["scoring"], payload)
                assignments += [
                    "llm_score=%s",
                    "llm_reasoning=%s",
                    "llm_summary=%s",
                    "llm_hard_requirements=%s",
                    "llm_scored_at=now()",
                ]
                values += [
                    score_data["llm_score"],
                    score_data["llm_reasoning"],
                    score_data["llm_summary"],
                    Json(score_data.get("llm_hard_requirements", [])),
                ]
                if provenance:
                    assignments.append("scored_by=%s")
                    values.append(model)
            facts = clean.get("screening")
            if facts is not None:
                facts = {**facts, "model": model, "prompt_version": prep.prompt_fingerprint()}
                assignments += [
                    "screening=%s",
                    "screening_state='ready'",
                    "screening_prepared_at=now()",
                    "screening_fingerprint=%s",
                ]
                values += [Json(facts), payload["fingerprint"]]
            if not assignments:
                continue
            values += [vid, row["full_description"]]
            where = (
                " WHERE id=%s AND status='unseen' AND scoring_excluded_reason IS NULL "
                "AND full_description=%s AND EXISTS (SELECT 1 FROM company c "
                "WHERE c.id=vacancy.company_id AND c.status IN ('active','candidate'))"
            )
            if existing is None:
                where += " AND llm_score IS NULL"
            else:
                where += " AND llm_score=%s"
                values.append(existing)
            cur.execute("UPDATE vacancy SET " + ", ".join(assignments) + where, values)
            if cur.rowcount:
                counts["scored"] += int(score_data is not None)
                counts["prepared"] += int(facts is not None)
            else:
                counts["skipped"] += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
    return counts


def build_parser():
    p = argparse.ArgumentParser()
    p.add_argument("--local", action="store_true")
    p.add_argument("--limit", type=int)
    p.add_argument("--save", action="store_true")
    p.add_argument("--payload")
    p.add_argument("--files", nargs="+")
    p.add_argument("--prepared-by")
    return p


def main():
    a = build_parser().parse_args()
    if a.local:
        json.dump(select_payloads(a.limit), sys.stdout, ensure_ascii=False)
        return
    if a.save:
        from llm_json import read_result_files

        if not a.payload or not a.files:
            build_parser().error("--save requires --payload and --files")
        payloads = json.loads(Path(a.payload).read_text())
        results, bad_files = read_result_files(a.files)
        for path in bad_files:
            print(f"Malformed result skipped: {path}", file=sys.stderr)
        print(json.dumps(save_results(results, payloads, a.prepared_by), ensure_ascii=False))
        return
    build_parser().error("choose --local or --save")


if __name__ == "__main__":
    main()
