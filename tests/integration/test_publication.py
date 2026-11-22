"""Целостность публикации: повтор, сбой, восстановление, удаление, конкуренция."""
import threading

from ecoprofi.indexer.retry import SourceDenied, SourceTimeout

CONTROL = "загрязняющих веществ"
RAZBOR_POST = 9001
NEW_TRACK = ("WEBVTT\n\n00:00:05.000 --> 00:00:09.000\n"
             "обновлённый перечень загрязняющих веществ вступил в силу\n").encode()


def job(stand, video_id=111):
    return stand.owner.execute("SELECT state, failure_reason, attempt FROM index_job WHERE item_id = %s",
                               (video_id,)).fetchone()


def test_repeated_import_creates_no_duplicates(loaded):
    before = loaded.counts()
    assert before == {"versions": 4, "episodes": 8, "published": 4}
    results = loaded.index()
    assert [r["outcome"] for r in results] == ["unchanged"] * 4
    assert loaded.counts() == before


def test_card_without_track_is_published_with_absent_track_state(loaded):
    row = loaded.owner.execute(
        "SELECT v.text_state, (SELECT count(*) FROM fragment e WHERE e.version_id = v.id) AS n "
        "FROM material m JOIN material_version v ON v.id = m.published_version_id WHERE m.item_id = 444"
    ).fetchone()
    assert (row["text_state"], row["n"]) == ("absent", 0)


def test_temporary_failure_retries_with_fresh_track_url(loaded):
    loaded.transport.tracks["razbor_club.vtt"] = NEW_TRACK
    loaded.transport.get_failures = [SourceTimeout("t1"), SourceTimeout("t2")]
    loaded.transport.calls.clear()
    delays = []
    (result,) = loaded.index([RAZBOR_POST], sleep=delays.append)
    assert result["outcome"] == "published"
    tracks = [c for c in loaded.transport.calls if c.startswith("track:")]
    assert len(tracks) == 3 and len(set(tracks)) == 3          # три попытки, каждый раз новый адрес
    assert len(delays) == 2 and delays[1] > delays[0]
    assert loaded.transport.calls.count("wall.getById") == 1    # повторялась только затронутая операция


def test_exhausted_temporary_failure_keeps_previous_version(loaded):
    before = loaded.counts()
    loaded.transport.tracks["razbor_club.vtt"] = NEW_TRACK
    loaded.transport.get_failures = [SourceTimeout("t")] * 3
    (result,) = loaded.index([RAZBOR_POST])
    assert result["outcome"] == "retry:timeout"
    state = job(loaded)
    assert (state["state"], state["failure_reason"], state["attempt"]) == ("retry", "timeout", 3)
    assert loaded.counts() == before
    answer = loaded.ask("club", CONTROL)
    card = answer["cards"][loaded.keys(answer).index("razbor_club")]
    assert card["start_ms"] == 1_563_000                         # прежняя версия доступна

    (result,) = loaded.index([RAZBOR_POST])                      # восстановление
    assert result["outcome"] == "published"
    assert loaded.counts() == {"versions": 4, "episodes": 6, "published": 4}
    answer = loaded.ask("club", CONTROL)
    assert loaded.keys(answer).count("razbor_club") == 1         # одна запись — одна карточка
    card = answer["cards"][loaded.keys(answer).index("razbor_club")]
    assert card["start_ms"] == 5_000 and "обновлённый" in card["excerpt"]


def test_failure_statuses_are_distinct(loaded):
    before = loaded.counts()
    loaded.transport.get_failures = [SourceDenied("403")]
    assert loaded.index([RAZBOR_POST])[0]["outcome"] == "failed:denied"
    assert job(loaded)["failure_reason"] == "denied"

    loaded.transport.tracks["razbor_club.vtt"] = b"<html>error</html>"
    assert loaded.index([RAZBOR_POST])[0]["outcome"] == "failed:parse"
    assert job(loaded)["failure_reason"] == "parse"

    loaded.transport.tracks.clear()
    loaded.transport.missing_posts.add(RAZBOR_POST)
    assert loaded.index([RAZBOR_POST])[0]["outcome"] == "failed:empty"
    assert loaded.counts() == before
    assert "razbor_club" in loaded.keys(loaded.ask("club", CONTROL))


def test_missing_post_does_not_confirm_deletion(loaded):
    loaded.transport.missing_posts.add(RAZBOR_POST)
    loaded.index([RAZBOR_POST])
    state = loaded.owner.execute("SELECT state FROM material WHERE item_id = 111").fetchone()["state"]
    assert state == "active"
    assert "razbor_club" in loaded.keys(loaded.ask("club", CONTROL))


def test_confirmed_deletion_removes_material_and_restore_returns_it(loaded):
    loaded.transport.deleted_posts.add(RAZBOR_POST)
    results = loaded.index([RAZBOR_POST])
    assert results[0]["outcome"] == "deleted"
    answer = loaded.ask("club", CONTROL)
    assert "razbor_club" not in loaded.keys(answer)

    loaded.transport.deleted_posts.clear()
    (result,) = loaded.index([RAZBOR_POST])
    assert result["outcome"] in ("published", "unchanged")
    assert loaded.counts() == {"versions": 4, "episodes": 8, "published": 4}
    assert loaded.keys(loaded.ask("club", CONTROL)).count("razbor_club") == 1


def test_embedding_failure_before_switch_keeps_old_version(loaded):
    before = loaded.counts()
    loaded.transport.tracks["razbor_club.vtt"] = NEW_TRACK
    loaded.ai.fail_embed = True
    (result,) = loaded.index([RAZBOR_POST])
    assert result["outcome"] == "retry:timeout"
    assert loaded.counts() == before
    loaded.ai.fail_embed = False
    assert loaded.index([RAZBOR_POST])[0]["outcome"] == "published"


def test_disappeared_track_does_not_replace_episodes_with_card(loaded):
    before = loaded.counts()
    for item in loaded.transport.items:
        if item["id"] == RAZBOR_POST:
            item["attachments"][0]["video"].pop("subtitles")
    (result,) = loaded.index([RAZBOR_POST])
    assert result["outcome"] == "failed:empty"
    assert loaded.counts() == before


def test_concurrent_updates_of_one_material_are_serialized(loaded):
    loaded.transport.tracks["razbor_club.vtt"] = NEW_TRACK
    outcomes, errors = [], []

    def worker():
        try:
            outcomes.append(loaded.index([RAZBOR_POST])[0]["outcome"])
        except Exception as exc:   # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert sorted(set(outcomes)) <= ["published", "unchanged"] and "published" in outcomes
    assert loaded.counts() == {"versions": 4, "episodes": 6, "published": 4}


def test_new_version_is_never_partially_visible(loaded):
    """Каждый эпизод в выдаче принадлежит опубликованной версии целиком."""
    loaded.transport.tracks["razbor_club.vtt"] = NEW_TRACK
    loaded.index([RAZBOR_POST])
    orphan = loaded.owner.execute(
        "SELECT count(*) AS n FROM fragment e WHERE NOT EXISTS "
        "(SELECT 1 FROM material m WHERE m.published_version_id = e.version_id)").fetchone()["n"]
    assert orphan == 0


def test_operation_log_has_measurements_without_secrets(loaded):
    answer = loaded.ask("club", CONTROL)
    row = loaded.owner.execute("SELECT outcome, details FROM operation_log WHERE operation_id = %s",
                               (answer["operation_id"],)).fetchone()
    assert row["outcome"] == "found"
    details = row["details"]
    assert {"materials", "versions", "search_ms", "generation_ms", "tokens", "duration_ms"} <= set(details)
    assert details["tokens"] == 21 and len(details["materials"]) == 3
    dump = str(loaded.owner.execute("SELECT details FROM operation_log").fetchall())
    for secret in ("fixture-token", "test-service-key", "fixture.invalid", "fresh="):
        assert secret not in dump
    indexer = loaded.owner.execute(
        "SELECT details FROM operation_log WHERE component = 'indexer' AND outcome = 'published' LIMIT 1").fetchone()
    assert {"material", "version_id", "episodes", "embedding_model", "duration_ms"} <= set(indexer["details"])
