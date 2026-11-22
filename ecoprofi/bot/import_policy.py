"""Атомарный импорт разрешений пилота: python -m ecoprofi.bot.import_policy /input/policy.json

Файл целиком заменяет действующую политику одной транзакцией: участник или
материал, отсутствующий в новом файле, теряет доступ без перезапуска сервисов.
Формат — pilot-policy.json.
"""
import json
import sys
from pathlib import Path

from ecoprofi.db import connect

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


def parse_policy(data: dict) -> dict:
    """Проверить структуру файла и привести к нормальному виду."""
    if not isinstance(data, dict):
        raise PolicyError("Ожидается объект JSON.")
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
        raise PolicyError("materials — объект «owner_id_video_id: аудитория».")
    for key, audience in raw_materials.items():
        owner, sep, video = str(key).partition("_")
        try:
            pair = (int(owner), int(video))
        except ValueError:
            pair = None
        if not sep or pair is None:
            raise PolicyError(f"materials: ключ «{key}» должен иметь вид owner_id_video_id, например -95098598_456240391.")
        if audience not in ROLES:
            raise PolicyError(f"materials[{key}]: аудитория должна быть public, club или unknown.")
        materials[pair] = audience
    return {"group_id": group_id, "testers": testers, "materials": materials}


def apply_policy(conn, policy: dict) -> dict:
    """Заменить политику одной транзакцией. Вернуть счётчики."""
    group_id = policy["group_id"]
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
        conn.execute("DELETE FROM material_policy")
        for (owner_id, video_id), audience in policy["materials"].items():
            conn.execute(
                "INSERT INTO material_policy (owner_id, video_id, audience) VALUES (%s, %s, %s)",
                (owner_id, video_id, audience),
            )
    return {"group_id": group_id, "testers": len(policy["testers"]), "materials": len(policy["materials"])}


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("Использование: python -m ecoprofi.bot.import_policy <файл политики>", file=sys.stderr)
        return 2
    try:
        policy = load_policy(Path(argv[0]).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, PolicyError) as exc:
        print(f"Политика не импортирована: {exc}", file=sys.stderr)
        return 1
    with connect(env="MIGRATION_DATABASE_URL", vector=False) as conn:
        counts = apply_policy(conn, policy)
    print(f"Политика импортирована: группа {counts['group_id']}, "
          f"тестировщиков {counts['testers']}, материалов {counts['materials']}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
