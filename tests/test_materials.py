from materials import add, load


def test_versions_are_preserved_and_sent_needs_evidence(tmp_path):
    a = add(b"first", "cv.txt", "Example", "cv", "source", root=tmp_path)
    assert add(b"first", "cv.txt", "Example", "cv", "source", root=tmp_path) == a
    b = add(b"second", "cv.txt", "Example", "cv", "source", root=tmp_path)
    assert a["id"] != b["id"]
    assert len(load(tmp_path)) == 2
    assert (tmp_path / "objects" / a["sha256"]).read_bytes() == b"first"
    confirmed = add(
        b"first",
        "cv.txt",
        "Example",
        "cv",
        "source",
        status="sent",
        evidence="Receipt",
        root=tmp_path,
    )
    assert confirmed["id"] == a["id"]
    assert confirmed["status"] == "sent"
    assert len(load(tmp_path)) == 2
    try:
        add(b"bad", "cv.txt", "Example", "cv", "source", status="sent", root=tmp_path)
    except ValueError:
        pass
    else:
        raise AssertionError("Submission without evidence accepted")
