"""006, история 2 (SC-02): эпизод из распознанной речи находится, помечен и закрыт неподписчику."""
from psycopg.types.json import Jsonb

from ecoprofi.asr.publish import publish_transcript

PHRASE = "рекультивация нарушенных земель после закрытия полигона твёрдых коммунальных отходов"


def recognize(stand, key: str) -> int:
    """Опубликовать расшифровку записи-карточки так, как это делает asr-gateway."""
    material_id = stand.material_id(key)
    job = stand.owner.execute("SELECT * FROM asr_job WHERE material_id = %s", (material_id,)).fetchone()
    assert job is not None, "запись без дорожки должна стоять в очереди распознавания (FR-01)"
    version = job["source_version"] or "fixture-version"
    stand.owner.execute("UPDATE asr_job SET source_version = %s, state = 'transcribed' WHERE material_id = %s",
                        (version, material_id))
    segments = [{"start_ms": 60_000 + i * 6000, "end_ms": 60_000 + i * 6000 + 5800,
                 "text": f"Сегодня разбираем, как проводится {PHRASE}, этап {i}.", "avg_logprob": -0.2,
                 "no_speech_prob": 0.02, "compression_ratio": 1.3} for i in range(6)]
    stand.owner.execute(
        "INSERT INTO asr_transcript (owner_id, item_id, source_version, processor_version, language, segments, "
        "audio_seconds, processing_seconds) VALUES (%s, %s, %s, 'asr-1/large-v3', 'ru', %s, 100, 10)",
        (job["owner_id"], job["item_id"], version, Jsonb(segments)))
    from ecoprofi.db import connect
    with connect(stand.indexer_dsn, vector=False) as conn:
        conn.autocommit = True
        assert publish_transcript(conn, stand.ai, job["owner_id"], job["item_id"]) == "published"
    return material_id


def test_recognized_episode_is_found_and_marked(loaded):
    material_id = recognize(loaded, "card_only_public")
    answer = loaded.ask("club", PHRASE)
    cards = [c for c in answer["cards"] if c["material_id"] == material_id]
    assert cards, answer
    card = cards[0]
    assert card["type"] == "episode" and card["recognized"] is True
    assert 60_000 <= card["start_ms"] < card["end_ms"]
    assert "?t=" in card["url"]                                 # ссылка на минуту записи
    from ecoprofi.bot.cards import message_text
    assert "речь распознана автоматически" in message_text("found", [card])


def test_recognized_text_is_closed_to_non_subscriber(loaded):
    recognize(loaded, "card_only_public")
    loaded.refused(loaded.post(loaded.users["public"], PHRASE))
