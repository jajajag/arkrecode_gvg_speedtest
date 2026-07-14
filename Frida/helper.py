"""Data and stat helpers shared by the standalone Frida tools."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
from urllib.parse import urlparse
from urllib.request import Request, urlopen


GAME_URL = "https://game-arkre-labs.ecchi.xxx/Router/RouterHandler.ashx"
GAME_HEADERS = {
    "Content-Type": "application/octet-stream",
    "User-Agent": (
        "UnityPlayer/2022.3.62f2 "
        "(UnityWebRequest/1.0, libcurl/8.10.1-DEV)"
    ),
}


def _application_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = _application_dir()
DATA_DIR = APP_DIR / "data"
DEFAULT_MASTER_DB = DATA_DIR / "master.db"

BASE_STATS = ("HP", "Attack", "Defence", "Speed")
EXTRA_STATS = (
    "CriticalRate",
    "CriticalDamageRate",
    "EffectHitRate",
    "ResistanceRate",
    "PinchRate",
)
STATS = (*BASE_STATS, *EXTRA_STATS)
VALUE_PROPS = {
    "HPValue": "HP",
    "AttackValue": "Attack",
    "DefenceValue": "Defence",
    "SpeedValue": "Speed",
}
RATE_PROPS = {
    "HPRate": "HP",
    "AttackRate": "Attack",
    "DefenceRate": "Defence",
    "SpeedRate": "Speed",
    "CriticalRate": "CriticalRate",
    "CriticalDamageRate": "CriticalDamageRate",
    "EffectHitRate": "EffectHitRate",
    "ResistanceRate": "ResistanceRate",
    "PinchRate": "PinchRate",
}
PASSIVE_COLUMNS = (
    "PassiveProp.DynamicField1",
    "PassiveProp.DynamicField2",
    "PassiveProp.DynamicField3",
)

# The complete database is retained. These are only the tables/columns loaded
# into memory for panel-stat calculation and role-name lookup.
MASTER_COLUMNS = {
    "Role": (
        "ID", "NAME", "RolePropertyID", "TeamImprint", "SelfImprint",
        *STATS,
    ),
    "RoleProperty": ("ID", "LV", *STATS),
    "RoleAwaken": ("RoleID", "LV", *VALUE_PROPS, *RATE_PROPS),
    "RoleImprint": (
        "ID", "Base.DynamicField1", "LevelAdd.DynamicField1",
    ),
    "Artifact": (
        "ID", "Base.AttackValue", "Base.HPValue",
        "Max.AttackValue", "Max.HPValue",
    ),
    "Skill": ("ID", *PASSIVE_COLUMNS),
    "SkillLevel": ("SkillID", "LV", *PASSIVE_COLUMNS),
    "EquipmentSet": ("ID", "Count", *RATE_PROPS),
    "CHS": ("Key", "Value"),
}
OPTIONAL_MASTER_COLUMNS = {
    "RoleAwaken": {"DefenceValue", "SpeedRate"},
    "SkillLevel": {
        "PassiveProp.DynamicField2",
        "PassiveProp.DynamicField3",
    },
}
MASTER_META_TABLE = "__speed_meta"
LANGUAGE_TABLES = {
    "CHS", "CHT", "DEU", "ENG", "FRA", "JPN", "KOR", "SPA", "THA", "VIE",
}


class SpeedError(RuntimeError):
    pass


def _qident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _safe_text(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8-sig", errors="replace")
    if not isinstance(value, str):
        value = str(value)
    return value.encode("utf-8", errors="replace").decode("utf-8")


def _clean_identifier(name: str, fallback: str) -> str:
    name = re.sub(r"\s+", "_", (name or "").strip())
    name = re.sub(
        r"[^0-9A-Za-z_\u4e00-\u9fff.\-]", "_", name
    ).strip("._-")
    return name or fallback


def _unique_names(names) -> list[str]:
    used = {}
    result = []
    for index, raw_name in enumerate(names, 1):
        name = _clean_identifier(raw_name, f"col_{index}")
        used[name] = used.get(name, 0) + 1
        result.append(name if used[name] == 1 else f"{name}_{used[name]}")
    return result


def _parse_text_table(text: str) -> tuple[list[str], list[list[str]]]:
    lines = [line.rstrip("\r") for line in text.splitlines() if line.rstrip("\r")]
    if not lines:
        return [], []
    if "@" not in lines[0]:
        return ["value"], [[_safe_text(line)] for line in lines]

    headers = _unique_names(lines[0].split("@"))
    width = len(headers)
    rows = []
    for line in lines[1:]:
        cells = _safe_text(line).split("@")
        if len(cells) < width:
            cells.extend("" for _ in range(width - len(cells)))
        elif len(cells) > width:
            extra = len(cells) - width
            headers.extend(f"extra_{index + 1}" for index in range(extra))
            for row in rows:
                row.extend("" for _ in range(extra))
            width = len(headers)
        rows.append(cells)
    return headers, rows


def _existing_tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        row[1]
        for row in conn.execute(f"PRAGMA table_info({_qident(table)})")
    }


def validate_master_db(path: Path) -> tuple[bool, str | None]:
    if not path.is_file():
        return False, "文件不存在"
    try:
        conn = sqlite3.connect(path)
        try:
            tables = _existing_tables(conn)
            for table, columns in MASTER_COLUMNS.items():
                if table not in tables:
                    return False, f"缺少表 {table}"
                missing = (
                    set(columns)
                    - _table_columns(conn, table)
                    - OPTIONAL_MASTER_COLUMNS.get(table, set())
                )
                if missing:
                    return False, f"{table} 缺少列 {', '.join(sorted(missing))}"
                if conn.execute(
                    f"SELECT 1 FROM {_qident(table)} LIMIT 1"
                ).fetchone() is None:
                    return False, f"{table} 是空表"
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return False, f"SQLite 错误：{exc}"
    return True, None


def master_catalog_name(path: Path) -> str | None:
    valid, _ = validate_master_db(path)
    if not valid:
        return None
    conn = sqlite3.connect(path)
    try:
        tables = _existing_tables(conn)
        if MASTER_META_TABLE in tables:
            row = conn.execute(
                f"SELECT value FROM {_qident(MASTER_META_TABLE)} "
                "WHERE key='catalog_name'"
            ).fetchone()
            if row and row[0]:
                return row[0]
        if "__table_manifest" in tables:
            for (source_file,) in conn.execute(
                "SELECT source_file FROM __table_manifest"
            ):
                match = re.search(r"catalog_\d+", source_file or "")
                if match:
                    return match.group(0)
    finally:
        conn.close()
    return None


def _create_data_table(conn, table, columns):
    definitions = ", ".join(f"{_qident(column)} TEXT" for column in columns)
    conn.execute(f"CREATE TABLE {_qident(table)} ({definitions})")


def _insert_rows(conn, table, columns, rows):
    placeholders = ", ".join("?" for _ in columns)
    names = ", ".join(_qident(column) for column in columns)
    conn.executemany(
        f"INSERT INTO {_qident(table)} ({names}) VALUES ({placeholders})",
        rows,
    )


def _create_meta(conn, catalog_name):
    conn.execute(
        f"CREATE TABLE {_qident(MASTER_META_TABLE)} "
        "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.execute(
        f"INSERT INTO {_qident(MASTER_META_TABLE)} (key, value) VALUES (?, ?)",
        ("catalog_name", catalog_name),
    )


def _post_json(url, payload, timeout=30):
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=GAME_HEADERS,
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def fetch_bulletin():
    return _post_json(GAME_URL, {
        "data": {},
        "route": "GameServerDBSettingHandler.QueryBulletinInfoResult",
    })


def _catalog_info(bulletin):
    info = bulletin.get("Info", bulletin)
    if not isinstance(info, dict):
        raise SpeedError("bulletin 缺少 Info")
    catalog_name = info.get("NewCatalogName")
    domains = info.get("PathDomain") or info.get("PathDomains")
    if not isinstance(catalog_name, str) or not catalog_name.strip():
        raise SpeedError("bulletin 缺少 NewCatalogName")
    if not isinstance(domains, str):
        raise SpeedError("bulletin 缺少 PathDomain")
    patch_domain = next(
        (part.strip() for part in re.split(r"[,;|]", domains) if part.strip()),
        None,
    )
    if not patch_domain:
        raise SpeedError("bulletin 中没有可用的补丁域名")
    return catalog_name.strip(), patch_domain.rstrip("/")


def _get_json(url, timeout=30):
    with urlopen(url, timeout=timeout) as response:
        return json.load(response)


def _master_bundle_urls(catalog, patch_domain):
    result = []
    for internal_id in catalog.get("m_InternalIds", []):
        if not isinstance(internal_id, str):
            continue
        bundle_name = Path(urlparse(internal_id).path).name.lower()
        if not bundle_name.endswith(".bundle"):
            continue
        if not bundle_name.startswith(("staticdata_", "text_")):
            continue
        url = internal_id.replace("http://PatchDomain", patch_domain)
        url = url.replace("https://PatchDomain", patch_domain)
        if url.startswith("//"):
            url = "https:" + url
        elif not url.startswith(("http://", "https://")):
            url = f"{patch_domain}/{url.lstrip('/')}"
        if url not in result:
            result.append(url)
    return result


def _download_file(url, path):
    request = Request(url, headers={"User-Agent": GAME_HEADERS["User-Agent"]})
    with urlopen(request, timeout=120) as response, path.open("wb") as output:
        shutil.copyfileobj(response, output, length=1024 * 1024)


def _build_full_master_db(bundle_paths, output_path, catalog_name):
    try:
        import UnityPy
    except ImportError as exc:
        raise SpeedError(
            "更新 master.db 需要 UnityPy，请先运行：pip install UnityPy"
        ) from exc

    conn = sqlite3.connect(output_path)
    table_count = 0
    row_count = 0
    seen_tables = {}
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(
            "CREATE TABLE __table_manifest "
            "(table_name TEXT PRIMARY KEY, source_file TEXT, asset_name TEXT, "
            "row_count INTEGER, column_count INTEGER)"
        )
        _create_meta(conn, catalog_name)
        for bundle_path in bundle_paths:
            environment = UnityPy.load(str(bundle_path))
            bundle_tables = 0
            bundle_rows = 0
            for obj in environment.objects:
                if obj.type.name != "TextAsset":
                    continue
                try:
                    data = obj.read()
                    asset_name = _clean_identifier(
                        getattr(data, "m_Name", ""),
                        f"textasset_{obj.path_id}",
                    )
                    if asset_name in LANGUAGE_TABLES and asset_name != "CHS":
                        continue
                    columns, rows = _parse_text_table(
                        _safe_text(getattr(data, "m_Script", ""))
                    )
                    if not columns and not rows:
                        continue
                    seen_tables[asset_name] = seen_tables.get(asset_name, 0) + 1
                    suffix = seen_tables[asset_name]
                    table = asset_name if suffix == 1 else f"{asset_name}_{suffix}"
                    column_tuple = tuple(columns)
                    _create_data_table(conn, table, column_tuple)
                    _insert_rows(conn, table, column_tuple, rows)
                    conn.execute(
                        "INSERT INTO __table_manifest "
                        "(table_name, source_file, asset_name, row_count, "
                        "column_count) VALUES (?, ?, ?, ?, ?)",
                        (
                            table,
                            f"{catalog_name}/{bundle_path.name}",
                            asset_name,
                            len(rows),
                            len(columns),
                        ),
                    )
                    table_count += 1
                    row_count += len(rows)
                    bundle_tables += 1
                    bundle_rows += len(rows)
                except Exception as exc:
                    print(
                        f"[master] WARN 跳过 {bundle_path.name}:"
                        f"{getattr(obj, 'path_id', '?')}：{exc}"
                    )
            if bundle_tables:
                print(
                    f"[master] 已读取 {bundle_path.name}："
                    f"{bundle_tables} 表 / {bundle_rows} 行"
                )
        conn.commit()
    finally:
        conn.close()

    valid, reason = validate_master_db(output_path)
    if not valid:
        raise SpeedError(f"新 master.db 验证失败：{reason}")
    return table_count, row_count


def rebuild_master_db(path, catalog_name, patch_domain):
    catalog_url = f"{patch_domain}/Android/{catalog_name}.json"
    print(f"[master] 获取 catalog：{catalog_name}")
    catalog = _get_json(catalog_url)
    urls = _master_bundle_urls(catalog, patch_domain)
    if not urls:
        raise SpeedError(f"{catalog_name} 中没有 staticdata/text bundle")

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".new")
    if temp_path.exists():
        temp_path.unlink()
    try:
        with tempfile.TemporaryDirectory(prefix="arkrecode_speed_") as temp_dir:
            bundle_paths = []
            for url in urls:
                name = (
                    Path(urlparse(url).path).name
                    or f"bundle_{len(bundle_paths)}"
                )
                bundle_path = Path(temp_dir) / name
                print(f"[master] 下载 {name}")
                _download_file(url, bundle_path)
                bundle_paths.append(bundle_path)
            tables, rows = _build_full_master_db(
                bundle_paths, temp_path, catalog_name
            )
        os.replace(temp_path, path)
    except Exception:
        if temp_path.exists():
            temp_path.unlink()
        raise
    print(f"[master] 已更新 {tables} 表 / {rows} 行：{path}")


def ensure_master_db(path=DEFAULT_MASTER_DB, offline=False, force=False):
    path = Path(path)
    valid, invalid_reason = validate_master_db(path)
    local_catalog = master_catalog_name(path) if valid else None

    if offline:
        if not valid:
            raise SpeedError(f"离线模式下 master.db 不可用：{invalid_reason}")
        print(f"[master] 离线使用现有数据库：{local_catalog or '版本未知'}")
        return local_catalog

    try:
        latest_catalog, patch_domain = _catalog_info(fetch_bulletin())
    except Exception as exc:
        if not valid or force:
            raise SpeedError(f"无法检查最新 master 且本地数据库不可用：{exc}") from exc
        print(f"[master] 在线检查失败，继续使用本地数据库：{exc}")
        return local_catalog

    if valid and local_catalog == latest_catalog and not force:
        print(f"[master] 已是最新：{latest_catalog}")
        return latest_catalog

    reason = "强制更新" if force else (
        f"本地 {local_catalog or invalid_reason or '版本未知'} -> {latest_catalog}"
    )
    print(f"[master] 需要重建：{reason}")
    try:
        rebuild_master_db(path, latest_catalog, patch_domain)
    except Exception as exc:
        if valid and not force:
            print(f"[master] 更新失败，继续使用原数据库：{exc}")
            return local_catalog
        raise
    return latest_catalog


class MasterData:
    def __init__(self, path=DEFAULT_MASTER_DB):
        self.path = Path(path)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            self.roles = self._keyed(conn, "Role", "ID")
            self.role_properties = self._grouped(conn, "RoleProperty", "ID")
            self.role_awaken = self._grouped(conn, "RoleAwaken", "RoleID")
            self.role_imprints = self._keyed(conn, "RoleImprint", "ID")
            self.artifacts = self._keyed(conn, "Artifact", "ID")
            self.skills = self._keyed(conn, "Skill", "ID")
            self.skill_levels = self._grouped(conn, "SkillLevel", "SkillID")
            self.equipment_sets = self._keyed(conn, "EquipmentSet", "ID")
            artifact_name_keys = {
                artifact_id: f"T_Item_Name_{artifact_id}"
                for artifact_id in self.artifacts
            }
            try:
                artifact_name_keys.update({
                    row["ID"]: row["Name"]
                    for row in conn.execute(
                        "SELECT Artifact.ID, Item.Name "
                        "FROM Artifact LEFT JOIN Item ON Item.ID = Artifact.ID"
                    )
                    if row["Name"]
                })
            except sqlite3.OperationalError:
                pass
            self.artifact_name_keys = artifact_name_keys
            role_names = {
                row.get("NAME") for row in self.roles.values() if row.get("NAME")
            }
            localized_names = role_names | set(artifact_name_keys.values())
            self.localization = {
                row["Key"]: row["Value"]
                for row in conn.execute("SELECT Key, Value FROM CHS")
                if row["Key"] in localized_names
            }
        finally:
            conn.close()

    @staticmethod
    def _keyed(conn, table, key):
        return {row[key]: dict(row) for row in conn.execute(
            f"SELECT * FROM {_qident(table)}"
        )}

    @staticmethod
    def _grouped(conn, table, key):
        result = {}
        for row in conn.execute(f"SELECT * FROM {_qident(table)}"):
            data = dict(row)
            result.setdefault(data[key], []).append(data)
        return result

    def role_name(self, role_id):
        name_key = (self.roles.get(role_id) or {}).get("NAME")
        if not name_key:
            return role_id
        if name_key.startswith(("T_", "UI_")):
            return self.localization.get(name_key) or role_id
        return name_key

    def artifact_name(self, artifact_id):
        name_key = self.artifact_name_keys.get(artifact_id)
        if not name_key:
            return artifact_id
        if name_key.startswith(("T_", "UI_")):
            return self.localization.get(name_key) or artifact_id
        return name_key


MASTER: MasterData | None = None


def load_master_data(path=DEFAULT_MASTER_DB):
    global MASTER
    MASTER = MasterData(path)
    return MASTER


def num(value, default=0.0):
    try:
        return default if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return default


def intv(value, default=0):
    try:
        return default if value in (None, "") else int(value)
    except (TypeError, ValueError):
        return default


def bucket():
    return {stat: 0.0 for stat in STATS}


def add_prop(flat, rate, prop, value):
    if prop in VALUE_PROPS:
        flat[VALUE_PROPS[prop]] += num(value)
    elif prop in RATE_PROPS:
        rate[RATE_PROPS[prop]] += num(value)


def base_stats(role):
    row = MASTER.roles.get(role.get("StaticID", "")) if MASTER else None
    if not row:
        return bucket(), None
    prop_id = row.get("RolePropertyID") or "HERO"
    level = intv(role.get("LV") or role.get("Level"), 60)
    rows = MASTER.role_properties.get(prop_id, [])
    prop = next((item for item in rows if item.get("LV") == str(level)), None)
    prop = prop or max(rows, key=lambda item: intv(item.get("LV")), default=None)
    return {
        stat: num(prop.get(stat) if prop else 0) * num(row.get(stat), 1)
        for stat in STATS
    }, row


def add_awaken(role_id, awaken_level, flat, rate):
    for row in MASTER.role_awaken.get(role_id, []) if MASTER else []:
        if intv(row.get("LV")) > awaken_level:
            continue
        for prop, stat in VALUE_PROPS.items():
            flat[stat] += num(row.get(prop))
        for prop, stat in RATE_PROPS.items():
            rate[stat] += num(row.get(prop))


def add_equips(equips, flat, rate):
    for equip in (equips or {}).values():
        main = equip.get("MainProp") or {}
        add_prop(
            flat, rate, main.get("PropertyType"),
            main.get("Value", main.get("SValue")),
        )
        for prop in (equip.get("SubProps") or {}).get("SourceValues") or []:
            add_prop(
                flat, rate, prop.get("PropertyType"),
                prop.get("Value", prop.get("SValue")),
            )


def add_sets(equips, rate):
    counts = {}
    for equip in (equips or {}).values():
        set_id = equip.get("Set")
        if set_id:
            counts[set_id] = counts.get(set_id, 0) + 1
    for set_id, owned in counts.items():
        row = MASTER.equipment_sets.get(set_id) if MASTER else None
        if not row:
            continue
        active = owned // max(intv(row.get("Count"), 1), 1)
        for prop, stat in RATE_PROPS.items():
            rate[stat] += num(row.get(prop)) * active


def bond_value(base, max_value, level):
    if level <= 1:
        value = base
    else:
        value = base + (max_value - base) * min(max(level - 1, 0), 29) / 29
    return math.floor(value + 1e-6)


def add_bond(bond, flat):
    if not bond or not MASTER:
        return
    row = MASTER.artifacts.get(bond.get("StaticID"))
    if not row:
        return
    level = intv(bond.get("LV"), 1)
    flat["Attack"] += bond_value(
        num(row.get("Base.AttackValue")),
        num(row.get("Max.AttackValue")),
        level,
    )
    flat["HP"] += bond_value(
        num(row.get("Base.HPValue")),
        num(row.get("Max.HPValue")),
        level,
    )


def add_passive(raw, flat, rate):
    if not raw or "#" not in raw or raw.startswith("Fun#"):
        return
    prop, value = raw.split("#", 1)
    add_prop(flat, rate, prop, value)


def add_skills(role, flat, rate):
    if not MASTER:
        return
    for skill in (role.get("Skills") or {}).get("Skills") or []:
        skill_id = skill.get("StaticID")
        if not skill_id:
            continue
        static_row = MASTER.skills.get(skill_id) or {}
        for column in PASSIVE_COLUMNS:
            add_passive(static_row.get(column), flat, rate)
        for row in MASTER.skill_levels.get(skill_id, []):
            if intv(row.get("LV")) > intv(skill.get("Level"), 1):
                continue
            for column in PASSIVE_COLUMNS:
                add_passive(row.get(column), flat, rate)


def imprint(imprint_id, level):
    row = MASTER.role_imprints.get(imprint_id or "") if MASTER else None
    if not row or level <= 0:
        return []
    result = []
    fields = (
        (row.get("Base.DynamicField1"), 1),
        (row.get("LevelAdd.DynamicField1"), max(level - 1, 0)),
    )
    for raw, times in fields:
        if raw and "#" in raw:
            prop, value = raw.split("#", 1)
            result.append((prop, num(value) * times))
    return result


def team_bonuses(roles):
    bonuses = {
        index: {"flat": bucket(), "rate": bucket()}
        for index in range(len(roles))
    }
    for source_index, role in enumerate(roles):
        if role.get("IsSelfImprint"):
            continue
        _, row = base_stats(role)
        if not row:
            continue
        for prop, value in imprint(
            row.get("TeamImprint"), intv(role.get("ImprintLV"))
        ):
            for target_index, bonus in bonuses.items():
                if target_index != source_index:
                    add_prop(bonus["flat"], bonus["rate"], prop, value)
    return bonuses


def calculate_role_stats(role, team_bonus=None):
    base, row = base_stats(role)
    flat, rate = bucket(), bucket()
    if team_bonus:
        for stat, value in team_bonus.get("flat", {}).items():
            flat[stat] += value
        for stat, value in team_bonus.get("rate", {}).items():
            rate[stat] += value
    add_awaken(role.get("StaticID", ""), intv(role.get("AwakenLV")), flat, rate)
    add_equips(role.get("EquipmentMap"), flat, rate)
    add_sets(role.get("EquipmentMap"), rate)
    add_bond(role.get("ArtifactData"), flat)
    add_skills(role, flat, rate)
    if row and role.get("IsSelfImprint"):
        for prop, value in imprint(
            row.get("SelfImprint"), intv(role.get("ImprintLV"))
        ):
            add_prop(flat, rate, prop, value)
    stats = {
        stat: base[stat] * (1 + rate[stat]) + flat[stat]
        for stat in BASE_STATS
    }
    stats.update({
        stat: base[stat] + rate[stat] + flat[stat]
        for stat in EXTRA_STATS
    })
    stats["CriticalRate"] = min(stats.get("CriticalRate", 0), 1)
    stats["CriticalDamageRate"] = min(
        stats.get("CriticalDamageRate", 0), 3.5
    )
    return stats


def calculate_team_stats(roles):
    bonuses = team_bonuses(roles)
    return [
        calculate_role_stats(role, bonuses.get(index))
        for index, role in enumerate(roles)
    ]
