import json
from pathlib import Path

import pytest

from ecoprofi.indexer.subtitles import Cue, SubtitleParseError, build_episodes, episodes_from_vtt, parse_vtt

FIXTURES = Path(__file__).parent / "fixtures"
EXPECTED = json.loads((FIXTURES / "expectations.json").read_text(encoding="utf-8"))["subtitles"]


def episodes(name):
    return episodes_from_vtt((FIXTURES / name).read_bytes())


def test_control_episode_matches_expected_interval_and_text():
    control = EXPECTED["razbor_club.vtt"]["control_episode"]
    found = [e for e in episodes("razbor_club.vtt") if e.start_ms == control["start_ms"]]
    assert len(found) == 1
    assert found[0].end_ms == control["end_ms"]
    assert found[0].text == control["text"]


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_episode_counts(name):
    assert len(episodes(name)) == EXPECTED[name]["episode_count"]


def test_overlapping_cue_does_not_repeat_tail():
    text = next(e.text for e in episodes("razbor_club.vtt") if e.start_ms == 1_563_000)
    assert text.count("включены взвешенные вещества") == 1


def test_real_repeat_after_pause_is_kept():
    text = next(e.text for e in episodes("razbor_club.vtt") if e.start_ms == 1_563_000)
    assert text.count("это важно для расчёта платы") == 2


def test_identical_overlapping_cue_is_dropped_entirely():
    result = build_episodes([Cue(0, 3000, "один два три"), Cue(2000, 5000, "один два три")])
    assert [(e.start_ms, e.end_ms, e.text) for e in result] == [(0, 5000, "один два три")]


def test_rolling_captions_keep_each_line_once_across_episode_boundary():
    # Форма реальной дорожки VK: строка, она же с новой строкой, новая строка отдельно; реплики стыкуются.
    lines = [f"строка{i} слово{i}" for i in range(8)]
    cues, t = [Cue(0, 4000, lines[0])], 4000
    for prev, cur in zip(lines, lines[1:]):
        cues += [Cue(t, t + 10, prev), Cue(t + 10, t + 4000, f"{prev} {cur}")]
        t += 4000
    result = build_episodes(cues)
    assert " ".join(e.text for e in result) == " ".join(lines)
    assert len(result) == 2 and result[1].start_ms == 24_010
    assert result[0].end_ms == result[1].start_ms


def test_touching_repeat_after_pause_is_still_kept():
    result = build_episodes([Cue(0, 2000, "да"), Cue(2000, 3000, "нет"), Cue(4000, 5000, "нет")])
    assert result[0].text == "да нет нет"


def test_overlap_ignores_case_and_punctuation():
    result = build_episodes([Cue(0, 3000, "Нормативы сбросов,"), Cue(2500, 6000, "нормативы сбросов утверждены")])
    assert result[0].text == "Нормативы сбросов, утверждены"


def test_markup_entities_and_service_blocks_are_removed():
    raw = ("WEBVTT\n\nNOTE служебная\nзаметка\n\nSTYLE\n::cue {}\n\n1\n"
           "00:00:01.000 --> 00:00:02.500 align:start\n<v Лектор>ПДК &amp; <b>НДС</b></v>\n").encode()
    assert parse_vtt(raw) == [Cue(1000, 2500, "ПДК & НДС")]


def test_hours_and_milliseconds():
    raw = b"WEBVTT\n\n01:02:03.004 --> 01:02:04.000\ntext\n"
    assert parse_vtt(raw)[0].start_ms == 3_723_004


def test_long_gap_and_long_run_split_episodes():
    cues = [Cue(0, 4000, "a"), Cue(10_000, 12_000, "b"), Cue(12_000, 20_000, "c"), Cue(34_000, 36_000, "d")]
    assert [e.text for e in build_episodes(cues)] == ["a", "b c", "d"]


def test_not_vtt_is_rejected():
    with pytest.raises(SubtitleParseError):
        parse_vtt(b"<html>403</html>")
