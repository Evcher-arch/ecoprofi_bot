"""Атомарный импорт разрешений пилота: python -m ecoprofi.bot.import_policy /input/policy.json

Файл целиком заменяет действующую политику одной транзакцией: участник или
материал, отсутствующий в новом файле, теряет доступ без перезапуска сервисов.
Формат — pilot-policy.json, версии 1 и 2 (contracts/operator-commands.md).
"""
import json
import sys
from datetime import date
from pathlib import Path

from ecoprofi.bot.access import SIGNAL_SQL
from ecoprofi.db import connect
from ecoprofi.textkeys import doc_numbers, parse_material_key

ROLES = {"public", "club", "unknown"}


class PolicyError(ValueError):
    pass


def _unique_keys(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise PolicyError(f"Ключ «{key}» указан дважды: у участника одна роль, у материала одна аудитория.")
        result[key] = value
    return result


def load_policy(text: str) -> dict:
    """Разобрать текст файла. Повтор ключа — ошибка: иначе вторая запись молча заменила бы первую."""
    return parse_policy(json.loads(text, object_pairs_hook=_unique_keys))


def _material(section: str, key) -> tuple[str, int, int]:
    parsed = parse_material_key(key)
    if parsed is None:
        raise PolicyError(f"{section}: ключ «{key}» должен иметь вид video<owner_id>_<id> или doc<owner_id>_<id>, "
                          "например video-95098598_456240391.")
    return parsed


def parse_policy(data: dict) -> dict:
    """Проверить структуру файла и привести к нормальному виду."""
    if not isinstance(data, dict):
        raise PolicyError("Ожидается объект JSON.")
    if data.get("version", 1) not in (1, 2):
        raise PolicyError("version — 1 или 2.")
    group_id = data.get("group_id")
    if not isinstance(group_id, int) or isinstance(group_id, bool) or group_id <= 0:
        raise PolicyError("group_id — положительное число (ID тестового сообщества).")

    testers = {}
    raw_testers = data.get("testers", {})
    if not isinstance(raw_testers, dict):
        raise PolicyError("testers — объект «VK ID: роль».")
    for key, role in raw_testers.items():
        if not str(key).isdigit() or int(key) <= 0:
            raise PolicyError(f"testers: «{key}» не является положительным VK ID.")
        if role not in ROLES:
            raise PolicyError(f"testers[{key}]: роль должна быть public, club или unknown.")
        testers[int(key)] = role

    materials = {}
    raw_materials = data.get("materials", {})
    if not isinstance(raw_materials, dict):
        raise PolicyError("materials — объект «ключ материала: аудитория».")
    for key, audience in raw_materials.items():
        if audience not in ROLES:
            raise PolicyError(f"materials[{key}]: аудитория должна быть public, club или unknown.")
        parsed = _material("materials", key)
        if parsed in materials:
            raise PolicyError(f"materials: материал «{key}» указан дважды.")
        materials[parsed] = audience

    lectures = data.get("lectures")
    if lectures is not None:
        if not isinstance(lectures, list) or not all(isinstance(x, str) and x.strip() for x in lectures):
            raise PolicyError("lectures — список ключей строк расписания из отчёта.")
        lectures = [x.strip() for x in lectures]
        if len(set(lectures)) != len(lectures):
            raise PolicyError("lectures: ключ строки расписания указан дважды.")

    confirmed = data.get("confirmed", [])
    if not isinstance(confirmed, list):
        raise PolicyError("confirmed — список ключей материалов, чья аудитория подтверждена заново.")
    confirmed = {_material("confirmed", key) for key in confirmed}
    if confirmed - set(materials):
        raise PolicyError("confirmed: подтвердить можно только материал из раздела materials.")

    requisites = {}
    raw_requisites = data.get("requisites", {})
    if not isinstance(raw_requisites, dict):
        raise PolicyError("requisites — объект «ключ материала: {date, number}».")
    for key, value in raw_requisites.items():
        if not isinstance(value, dict) or not str(value.get("number") or "").strip():
            raise PolicyError(f"requisites[{key}]: нужен номер документа (number).")
        try:
            when = date.fromisoformat(value["date"]) if value.get("date") else None
        except (TypeError, ValueError):
            raise PolicyError(f"requisites[{key}]: дата должна иметь вид ГГГГ-ММ-ДД.") from None
        requisites[_material("requisites", key)] = {
            "date": when, "number": str(value["number"]).strip(),
            "sender": (str(value["sender"]).strip() or None) if value.get("sender") else None,
        }
    return {"group_id": group_id, "testers": testers, "materials": materials, "lectures": lectures,
            "confirmed": confirmed, "requisites": requisites}


def apply_policy(conn, policy: dict) -> dict:
    """Заменить политику одной транзакцией. Вернуть счётчики."""
    group_id = policy["group_id"]
    lectures = policy.get("lectures")
    with conn.transaction():
        conn.execute("LOCK TABLE test_group, participant, material_policy IN EXCLUSIVE MODE")
        conn.execute("DELETE FROM test_group WHERE group_id <> %s", (group_id,))
        conn.execute("INSERT INTO test_group (group_id) VALUES (%s) ON CONFLICT DO NOTHING", (group_id,))
        conn.execute("DELETE FROM participant WHERE group_id = %s", (group_id,))
        for vk_id, role in policy["testers"].items():
            # Допуск к тесту хранится отдельно от роли.
            conn.execute(
                "INSERT INTO participant (group_id, vk_id, tester, role) VALUES (%s, %s, true, %s)",
                (group_id, vk_id, role),
            )

        previous = {(r["item_type"], r["owner_id"], r["item_id"]): r for r in conn.execute(
            "SELECT item_type, owner_id, item_id, audience, seen_signal, decided_at FROM material_policy").fetchall()}
        conn.execute("DELETE FROM material_policy")
        for key, audience in policy["materials"].items():
            old = previous.get(key)
            if (old and old["audience"] == audience and old["seen_signal"] is not None
                    and key not in policy.get("confirmed", ())):
                # Прежнее решение без изменений: открытое расхождение с расписанием не закрывается молча.
                conn.execute(
                    "INSERT INTO material_policy (item_type, owner_id, item_id, audience, seen_signal, decided_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s)", (*key, audience, old["seen_signal"], old["decided_at"]))
                continue
            # Новое или подтверждённое решение фиксирует сигнал расписания на момент импорта.
            conn.execute(
                f"""
                INSERT INTO material_policy (item_type, owner_id, item_id, audience, seen_signal, decided_at)
                VALUES (%(t)s, %(o)s, %(i)s, %(a)s,
                        (SELECT s.signal FROM material m LEFT JOIN LATERAL ({SIGNAL_SQL}) s(signal) ON true
                          WHERE m.item_type = %(t)s AND m.owner_id = %(o)s AND m.item_id = %(i)s), now())
                """,
                {"t": key[0], "o": key[1], "i": key[2], "a": audience},
            )

        if lectures is not None:
            known = {r["schedule_row"] for r in conn.execute(
                "SELECT schedule_row FROM lecture WHERE schedule_row = ANY(%s)", (lectures,)).fetchall()}
            missing = [key for key in lectures if key not in known]
            if missing:
                # Исключение откатывает всю транзакцию: политика остаётся прежней.
                raise PolicyError("lectures: строки расписания не найдены: " + "; ".join(missing)
                                  + ". Сначала обновите расписание и возьмите ключи из отчёта.")
            conn.execute("DELETE FROM pilot_lecture")
            for key in lectures:
                conn.execute("INSERT INTO pilot_lecture (schedule_row) VALUES (%s)", (key,))

        conn.execute("DELETE FROM requisite_decision")
        for key, req in policy.get("requisites", {}).items():
            conn.execute(
                "INSERT INTO requisite_decision (item_type, owner_id, item_id, req_date, req_number, req_sender) "
                "VALUES (%s, %s, %s, %s, %s, %s)", (*key, req["date"], req["number"], req["sender"]))
            row = conn.execute(
                "SELECT id, title FROM material WHERE item_type = %s AND owner_id = %s AND item_id = %s",
                key).fetchone()
            if row:
                # Реквизиты, подтверждённые редактором, сразу входят в карточку и в поисковую подпись.
                conn.execute(
                    "UPDATE material SET req_date = %s, req_number = %s, req_sender = coalesce(%s, req_sender), "
                    "req_state = 'confirmed', doc_numbers = %s WHERE id = %s",
                    (req["date"], req["number"], req["sender"], doc_numbers(row["title"], req["number"]), row["id"]))
    return {"group_id": group_id, "testers": len(policy["testers"]), "materials": len(policy["materials"]),
            "lectures": None if lectures is None else len(lectures),
            "requisites": len(policy.get("requisites", {}))}


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("Использование: python -m ecoprofi.bot.import_policy <файл политики>", file=sys.stderr)
        return 2
    try:
        policy = load_policy(Path(argv[0]).read_text(encoding="utf-8-sig"))
        with connect(env="MIGRATION_DATABASE_URL", vector=False) as conn:
            counts = apply_policy(conn, policy)
    except (OSError, json.JSONDecodeError, PolicyError) as exc:
        print(f"Политика не импортирована: {exc}", file=sys.stderr)
        return 1
    sample = "без изменений" if counts["lectures"] is None else str(counts["lectures"])
    print(f"Политика импортирована: группа {counts['group_id']}, "
          f"тестировщиков {counts['testers']}, материалов {counts['materials']}, "
          f"лекций в выборке {sample}, подтверждённых реквизитов {counts['requisites']}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
