"""Фильтр отрезков и сборка эпизодов из расшифровки (R6, FR-05): без БД."""
from ecoprofi.asr import MIN_SPEECH_CHARS
from ecoprofi.asr.publish import content_hash, episodes_from_segments, filter_segments, keep_segment


def seg(start, end, text, **extra):
    return {"start_ms": start, "end_ms": end, "text": text, "avg_logprob": -0.2, "no_speech_prob": 0.05,
            "compression_ratio": 1.4, **extra}


def test_silence_and_loops_are_dropped():
    assert keep_segment(seg(0, 1000, "речь"))
    assert not keep_segment(seg(0, 1000, "Продолжение следует...", no_speech_prob=0.9, avg_logprob=-1.4))
    assert keep_segment(seg(0, 1000, "тихая, но уверенная речь", no_speech_prob=0.9, avg_logprob=-0.3))
    assert not keep_segment(seg(0, 1000, "да да да да да да да да", compression_ratio=3.1))
    assert not keep_segment(seg(0, 1000, "   "))


def test_repeated_segment_on_silence_is_dropped():
    cues = filter_segments([seg(0, 2000, "Спасибо за внимание."), seg(2000, 4000, "Спасибо за внимание!"),
                            seg(4000, 6000, "Новая мысль.")])
    assert [c.text for c in cues] == ["Спасибо за внимание.", "Новая мысль."]


def test_episodes_have_timecodes_and_no_overlap_repeats():
    text = "Обращение с отходами первого класса опасности требует лицензии. "
    segments = [seg(i * 5000, i * 5000 + 4800, f"{text}Пункт {i}.") for i in range(12)]
    episodes, chars = episodes_from_segments(segments)
    assert chars >= MIN_SPEECH_CHARS
    assert episodes and all(e.start_ms < e.end_ms for e in episodes)
    assert episodes[0].start_ms == 0
    joined = " ".join(e.text for e in episodes)
    assert joined.count("Пункт 3.") == 1


def test_too_little_speech_is_counted():
    episodes, chars = episodes_from_segments([seg(0, 1000, "Алло.")])
    assert chars < MIN_SPEECH_CHARS


def test_content_hash_is_stable():
    assert content_hash("v1") == content_hash("v1") != content_hash("v2")
