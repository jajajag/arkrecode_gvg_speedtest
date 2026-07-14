"""Universal Ark Re:Code battle-speed analyzer using Frida packets."""

from __future__ import annotations

import argparse
from collections import Counter, deque
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sqlite3
import sys
import time
import unicodedata

try:
    from .helper import (
        DATA_DIR,
        DEFAULT_MASTER_DB,
        MasterData,
        SpeedError,
        calculate_role_stats,
        calculate_team_stats,
        ensure_master_db,
        load_master_data,
    )
except ImportError:
    from helper import (
        DATA_DIR,
        DEFAULT_MASTER_DB,
        MasterData,
        SpeedError,
        calculate_role_stats,
        calculate_team_stats,
        ensure_master_db,
        load_master_data,
    )


DEFAULT_PROCESS = "Ark ReCode.exe"
DEFAULT_DUMP = DATA_DIR / "dump.cs"
MASTER: MasterData | None = None


@dataclass(frozen=True)
class DumpType:
    namespace: str
    name: str
    body: str

    @property
    def qualified_name(self):
        return f"{self.namespace}.{self.name}" if self.namespace else self.name


@dataclass(frozen=True)
class HookLayout:
    send_rva: int
    decode_rva: int
    frame_data_offset: int
    frame_text_offset: int


@dataclass(frozen=True)
class PreparedBattle:
    role_info: dict
    label: str = ""


_NAMESPACE_RE = re.compile(r"(?m)^// Namespace:\s*(?P<name>[^\r\n]*)\s*$")
_TYPE_RE = re.compile(
    r"(?m)^(?!\s*//)[^\r\n]*\b(?:class|struct)\s+"
    r"(?P<name>[A-Za-z_]\w*)\b[^\r\n]*$"
)
_RVA_RE = re.compile(r"// RVA:\s*(?P<rva>0x[0-9A-Fa-f]+|-1)\b")
_FIELD_RE = re.compile(
    r"^(?P<decl>.*?)//\s*0x(?P<offset>[0-9A-Fa-f]+)\s*$"
)
_FIELD_NAME_RE = re.compile(
    r"(?P<name><[^>]+>k__BackingField|[A-Za-z_]\w*)\s*(?:;|=)"
)


def parse_dump_types(text):
    markers = list(_NAMESPACE_RE.finditer(text))
    result = []
    for index, marker in enumerate(markers):
        end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        body = text[marker.end():end]
        declaration = _TYPE_RE.search(body)
        if declaration:
            result.append(DumpType(
                marker.group("name").strip(), declaration.group("name"), body
            ))
    if not result:
        raise SpeedError("dump.cs 中没有类型定义")
    return result


def _split_parameters(parameters):
    result, start, depth = [], 0, 0
    for index, char in enumerate(parameters):
        if char in "<[(":
            depth += 1
        elif char in ">])":
            depth = max(depth - 1, 0)
        elif char == "," and depth == 0:
            result.append(parameters[start:index].strip())
            start = index + 1
    if parameters[start:].strip():
        result.append(parameters[start:].strip())
    return result


def _parameter_type(parameter):
    parameter = parameter.split("=", 1)[0].strip()
    parameter = re.sub(r"^(?:ref|out|in|params|this)\s+", "", parameter)
    parts = parameter.rsplit(None, 1)
    return parts[0] if len(parts) == 2 else parameter


def _canonical_type(name):
    name = re.sub(r"\s+", "", name).replace("global::", "")
    name = {"System.String": "string", "String": "string"}.get(name, name)
    if "." in name and not name.endswith("[]"):
        name = name.rsplit(".", 1)[-1]
    return name


def _parse_signature(signature):
    match = re.search(
        r"(?P<name>\.?[A-Za-z_]\w*)\s*\((?P<params>.*)\)\s*"
        r"(?:\{\s*\}|;)?\s*$",
        signature.strip(),
    )
    if not match:
        return None
    params = match.group("params").strip()
    types = tuple(
        _canonical_type(_parameter_type(item))
        for item in _split_parameters(params)
    ) if params else ()
    return match.group("name"), types


def resolve_method_rva(dump_type, method_name, parameter_types):
    expected = tuple(_canonical_type(item) for item in parameter_types)
    lines = dump_type.body.splitlines()
    matches = []
    for index, line in enumerate(lines):
        rva_match = _RVA_RE.search(line)
        if not rva_match:
            continue
        signature = ""
        for following in lines[index + 1:index + 8]:
            following = following.strip()
            if not following or following.startswith("["):
                continue
            if following.startswith(("//", "|-")):
                break
            signature = following
            break
        parsed = _parse_signature(signature)
        if parsed == (method_name, expected):
            if rva_match.group("rva") == "-1":
                raise SpeedError(
                    f"{dump_type.qualified_name}.{method_name} 没有 RVA"
                )
            matches.append(int(rva_match.group("rva"), 16))
    matches = sorted(set(matches))
    if len(matches) != 1:
        raise SpeedError(
            f"dump.cs 无法唯一定位 {dump_type.qualified_name}."
            f"{method_name}({', '.join(parameter_types)})"
        )
    return matches[0]


def resolve_field_offset(dump_type, candidates):
    fields_text = dump_type.body.split("// Properties", 1)[0]
    fields = {}
    for line in fields_text.splitlines():
        match = _FIELD_RE.match(line.strip())
        if not match:
            continue
        name_match = _FIELD_NAME_RE.search(match.group("decl"))
        if name_match:
            fields[name_match.group("name")] = int(match.group("offset"), 16)
    for candidate in candidates:
        if candidate in fields:
            return fields[candidate]
    raise SpeedError(
        f"dump.cs 的 {dump_type.qualified_name} 缺少字段："
        + " / ".join(candidates)
    )


def _find_dump_type(types, namespace, name, method, parameters):
    exact = [item for item in types if item.namespace == namespace and item.name == name]
    if len(exact) == 1:
        return exact[0]
    compatible = []
    for item in types:
        if item.name != name:
            continue
        try:
            resolve_method_rva(item, method, parameters)
        except SpeedError:
            continue
        compatible.append(item)
    if len(compatible) == 1:
        return compatible[0]
    raise SpeedError(f"dump.cs 无法定位类型 {namespace}.{name}")


def load_hook_layout(path: Path) -> HookLayout:
    if not path.is_file():
        raise SpeedError(f"找不到 dump.cs：{path}")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise SpeedError(f"无法读取 dump.cs：{exc}") from exc
    types = parse_dump_types(text)
    websocket = _find_dump_type(
        types, "BestHTTP.WebSocket", "WebSocket", "Send", ("string",)
    )
    frame = _find_dump_type(
        types,
        "BestHTTP.WebSocket.Frames",
        "WebSocketFrameReader",
        "DecodeWithExtensions",
        ("WebSocket",),
    )
    return HookLayout(
        send_rva=resolve_method_rva(websocket, "Send", ("string",)),
        decode_rva=resolve_method_rva(
            frame, "DecodeWithExtensions", ("WebSocket",)
        ),
        frame_data_offset=resolve_field_offset(
            frame, ("<Data>k__BackingField", "Data", "data")
        ),
        frame_text_offset=resolve_field_offset(
            frame,
            ("<DataAsText>k__BackingField", "DataAsText", "dataAsText"),
        ),
    )


def build_hook_source(layout: HookLayout) -> str:
    config = json.dumps({
        "sendRva": f"0x{layout.send_rva:X}",
        "decodeRva": f"0x{layout.decode_rva:X}",
        "frameDataOffset": layout.frame_data_offset,
        "frameTextOffset": layout.frame_text_offset,
    })
    return r'''"use strict";
const CONFIG = __CONFIG__;
const BASE = Process.getModuleByName("GameAssembly.dll").base;

function addr(rva) { return BASE.add(ptr(rva)); }

function readIl2CppString(p) {
    if (p.isNull()) return null;
    try {
        const len = p.add(0x10).readS32();
        return p.add(0x14).readUtf16String(len);
    } catch (_) { return null; }
}

function readByteArrayAsUtf8(arr, maxLen = 1024 * 1024) {
    if (arr.isNull()) return null;
    try {
        const len = arr.add(0x18).readU32();
        if (len <= 0 || len > maxLen) return null;
        return new TextDecoder("utf-8").decode(
            arr.add(0x20).readByteArray(len));
    } catch (_) { return null; }
}

function isJsonText(s) {
    if (!s) return false;
    s = s.trim();
    return s.startsWith("{") || s.startsWith("[");
}

function emitPacket(tag, text) {
    if (isJsonText(text)) send({tag: tag, text: text.trim()});
}

function getFrameText(frame) {
    try {
        const text = readIl2CppString(
            frame.add(CONFIG.frameTextOffset).readPointer());
        if (isJsonText(text)) return text;
        const data = readByteArrayAsUtf8(
            frame.add(CONFIG.frameDataOffset).readPointer());
        return isJsonText(data) ? data : null;
    } catch (_) { return null; }
}

Interceptor.attach(addr(CONFIG.sendRva), {
    onEnter(args) { emitPacket("SEND", readIl2CppString(args[1])); }
});

Interceptor.attach(addr(CONFIG.decodeRva), {
    onEnter(args) { this.frame = args[0]; },
    onLeave(_) { emitPacket("RECV", getFrameText(this.frame)); }
});
'''.replace("__CONFIG__", config)


def display_width(value):
    return sum(
        2 if unicodedata.east_asian_width(char) in ("F", "W") else 1
        for char in str(value)
    )


def print_table(headers, rows):
    if not rows:
        return
    widths = [
        max(display_width(row[index]) for row in [headers, *rows])
        for index in range(len(headers))
    ]
    for row in [headers, ["-" * width for width in widths], *rows]:
        print(" ".join(
            str(value) + " " * max(widths[index] - display_width(value), 0)
            for index, value in enumerate(row)
        ))


def fmt_float(value, digits=6):
    if value is None:
        return "-"
    return f"{value:.{digits}f}".rstrip("0").rstrip(".")


def role_sort_key(role_id):
    try:
        return tuple(int(part) for part in role_id.split("-"))
    except ValueError:
        return 99, 99, 99


def action_delta(start, end):
    if start is None or end is None:
        return None
    delta = end - start
    return delta + 1 if delta < 0 else delta


def estimate_speed_value(values):
    rounded = [round(value) for value in values]
    counts = Counter(rounded)
    top_count = max(counts.values())
    modes = [value for value, count in counts.items() if count == top_count]
    if top_count > 1 and len(modes) == 1:
        return modes[0]
    values = sorted(values)
    middle = len(values) // 2
    median = (
        values[middle]
        if len(values) % 2
        else (values[middle - 1] + values[middle]) / 2
    )
    return round(median)


def role_static_id_from_skill(skill_id):
    match = re.match(r"^(H\d+)S\d", skill_id or "")
    return match.group(1) if match else None


def iter_team_maps(start_info):
    for side, key in (("1", "CampData1"), ("2", "CampData2")):
        role_map = (start_info.get(key) or {}).get("PositionRoleMap") or {}
        if role_map:
            yield side, 0, role_map
    if not (start_info.get("CampData1") or {}).get("PositionRoleMap"):
        for wave, camp in enumerate(start_info.get("WaveCampDatas") or []):
            role_map = (camp or {}).get("PositionRoleMap") or {}
            if role_map:
                yield "1", wave, role_map


def build_role_info(start_info):
    result = {}
    for side, wave, role_map in iter_team_maps(start_info):
        positions = sorted(role_map, key=lambda position: int(position))
        roles = [role_map[position] for position in positions]
        team_stats = (
            calculate_team_stats(roles) if side == "1" else [None] * len(roles)
        )
        solo_stats = (
            [calculate_role_stats(role) for role in roles]
            if side == "1" else [None] * len(roles)
        )
        for position, role, stats, solo in zip(
            positions, roles, team_stats, solo_stats
        ):
            role_id = f"{side}-{wave}-{position}"
            speed = round(stats.get("Speed", 0)) if stats else None
            result[role_id] = {
                "name": MASTER.role_name(role.get("StaticID", "")),
                "static_id": role.get("StaticID", ""),
                "side": side,
                "speed": speed,
                "speed_imprint_affected": bool(
                    stats and solo
                    and abs(stats.get("Speed", 0) - solo.get("Speed", 0)) > 1e-9
                ),
            }
    return result


def build_ally_role_info(role_map):
    return build_role_info({"CampData1": {"PositionRoleMap": role_map}})


def prepare_battles_from_packet(data):
    """Normalize all known battle setup packets into one queue format."""
    start_info = data.get("StartBattleInfo")
    if isinstance(start_info, dict):
        return [PreparedBattle(build_role_info(start_info))]

    team_group = data.get("PlayerTeamGroup")
    if isinstance(team_group, dict):
        return [
            PreparedBattle(
                build_ally_role_info(
                    ((team_group.get(key) or {}).get("PositionRoleMap") or {})
                ),
                label,
            )
            for key, label in (
                ("FirstTeam", "上半场"),
                ("SecondTeam", "下半场"),
            )
        ]

    player_team = data.get("PlayerTeamData")
    if isinstance(player_team, dict):
        role_map = player_team.get("PositionRoleMap") or {}
        return [PreparedBattle(build_ally_role_info(role_map))]

    return None


class SpeedAnalyzer:
    def __init__(self):
        self.battle_no = 0
        self.pending_battles = deque()
        self.role_info = {}
        self.wave_phase = 0
        self.wave_phase_count = 1
        self.reset_phase_state()

    def handle_packet(self, _tag, text):
        try:
            data = json.loads(text)
        except (TypeError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        prepared = prepare_battles_from_packet(data)
        if prepared is not None:
            self.pending_battles = deque(prepared)
        if data.get("StartBattle") is True:
            self.start_pending_battle()

        step = data.get("Step")
        round_result = data.get("RoundResult")
        if isinstance(round_result, dict):
            self.handle_round_result(step, round_result)
        action_result = data.get("ActionResult")
        if isinstance(action_result, dict):
            self.handle_action_result(action_result)

    def start_pending_battle(self):
        if not self.pending_battles:
            return False
        prepared = self.pending_battles.popleft()
        if not prepared.role_info:
            return False
        self.start_battle(prepared.role_info, prepared.label)
        return True

    def start_battle(self, role_info, label=""):
        self.role_info = role_info
        ally_waves = {
            role_id.split("-")[1]
            for role_id in role_info
            if role_id.startswith("1-") and role_id.count("-") == 2
        }
        self.wave_phase = 0
        self.wave_phase_count = max(len(ally_waves), 1)
        if not label and self.wave_phase_count > 1:
            label = self.phase_name(0)
        self.start_phase(label)

    def reset_phase_state(self):
        self.start_times = None
        self.printed = False
        self.enemy_estimates = {}
        self.enemy_roles = {}
        self.enemy_base_max_hp = {}
        self.enemy_injury_rates = {}
        self.announced_enemy_roles = set()
        self.announced_enemy_artifacts = set()

    def start_phase(self, label=""):
        self.battle_no += 1
        self.reset_phase_state()
        suffix = f"（{label}）" if label else ""
        print(f"\n===== 战斗 {self.battle_no}{suffix} =====")

    def phase_name(self, index):
        if self.wave_phase_count == 2:
            return ("上半场", "下半场")[index]
        return f"第 {index + 1} 波"

    def handle_round_result(self, step, round_result):
        role_time_map = round_result.get("RoleTimeMap")
        if not isinstance(role_time_map, dict):
            return
        times = {
            role_id: float(value)
            for role_id, value in role_time_map.items()
            if role_id != "TurnRole"
        }
        if not times:
            return
        if (
            step == "StartWave"
            and self.printed
            and self.wave_phase + 1 < self.wave_phase_count
        ):
            self.wave_phase += 1
            self.start_phase(self.phase_name(self.wave_phase))
            self.start_times = times
            return
        if self.start_times is None:
            self.start_times = times
            return
        if self.printed:
            return
        if step == "StartRound":
            self.print_speed_report(times)
            self.printed = True
            self.announce_pending_enemy_roles()

    def handle_action_result(self, action_result):
        events = action_result.get("SkillEventList") or []
        for event in events:
            for role_event in (event or {}).get("RoleEventList") or []:
                self.update_enemy_max_hp(role_event or {})

        artifact_triggers = set()
        for event in events:
            action = (event or {}).get("Action") or {}
            role_id = action.get("SourceID")
            if isinstance(role_id, str) and role_id.startswith("2-"):
                skill_id = (action.get("SkillData") or {}).get("StaticID")
                static_id = role_static_id_from_skill(skill_id)
                if static_id and self.enemy_roles.get(role_id) != static_id:
                    self.enemy_roles[role_id] = static_id
                    self.role_info.setdefault(role_id, {}).update({
                        "name": MASTER.role_name(static_id),
                        "static_id": static_id,
                        "side": "2",
                        "speed": None,
                    })
                    if self.printed:
                        self.announce_enemy_role(role_id)

            for role_event in (event or {}).get("RoleEventList") or []:
                role_event = role_event or {}
                tip = role_event.get("TipInfo") or {}
                owner_id = role_event.get("TargetRoleID")
                artifact_id = tip.get("ID")
                if (
                    tip.get("Tip") == "Artifect"
                    and isinstance(owner_id, str)
                    and owner_id.startswith("2-")
                    and isinstance(artifact_id, str)
                    and artifact_id
                ):
                    artifact_triggers.add((owner_id, artifact_id))

        for owner_id, artifact_id in sorted(
            artifact_triggers, key=lambda item: (role_sort_key(item[0]), item[1])
        ):
            trigger = owner_id, artifact_id
            if trigger in self.announced_enemy_artifacts:
                continue
            print(
                f"敌方羁绊触发｜{owner_id} → {artifact_id} → "
                f"{MASTER.artifact_name(artifact_id)}"
            )
            self.announced_enemy_artifacts.add(trigger)

    def update_enemy_max_hp(self, role_event):
        role_id = role_event.get("TargetRoleID")
        if not isinstance(role_id, str) or not role_id.startswith("2-"):
            return

        hit_info = role_event.get("HitInfo") or {}
        last_hp = role_event.get("LastHP")
        now_hp = role_event.get("NowHP")
        hp_diff = hit_info.get("HPDiffValue")
        add_injury_rate = role_event.get("AddInjuryRate")
        new_max_hp = hit_info.get("newMaxHPValue")
        if not any(
            self.is_number(value) and value != 0
            for value in (
                last_hp,
                now_hp,
                hp_diff,
                add_injury_rate,
                new_max_hp,
            )
        ):
            return

        injury_rate = role_event.get("NowInjuryRate")
        if self.is_number(injury_rate):
            injury_rate = self.clamp_injury_rate(injury_rate)
            self.enemy_injury_rates[role_id] = injury_rate
        else:
            injury_rate = self.enemy_injury_rates.get(role_id, 0.0)

        if self.is_number(new_max_hp) and new_max_hp > 0:
            self.enemy_base_max_hp[role_id] = float(new_max_hp) / (
                1.0 - injury_rate
            )
            return

        if role_id in self.enemy_base_max_hp:
            return

        previous_injury_rate = injury_rate
        if self.is_number(add_injury_rate):
            previous_injury_rate = self.clamp_injury_rate(
                injury_rate - float(add_injury_rate)
            )

        if self.is_number(last_hp) and last_hp > 0:
            observed_hp = float(last_hp)
            observed_injury_rate = previous_injury_rate
        elif self.is_number(now_hp) and now_hp > 0:
            observed_hp = float(now_hp)
            observed_injury_rate = injury_rate
        else:
            return

        self.enemy_base_max_hp[role_id] = observed_hp / (
            1.0 - observed_injury_rate
        )

    @staticmethod
    def is_number(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    @staticmethod
    def clamp_injury_rate(value):
        return min(max(float(value), 0.0), 0.999999)

    def enemy_current_max_hp(self, role_id):
        base_max_hp = self.enemy_base_max_hp.get(role_id)
        if base_max_hp is None:
            return None
        injury_rate = self.enemy_injury_rates.get(role_id, 0.0)
        return max(round(base_max_hp * (1.0 - injury_rate)), 0)

    def announce_pending_enemy_roles(self):
        for role_id in sorted(self.enemy_roles, key=role_sort_key):
            self.announce_enemy_role(role_id)

    def announce_enemy_role(self, role_id):
        if role_id in self.announced_enemy_roles:
            return
        static_id = self.enemy_roles.get(role_id)
        if not static_id:
            return
        name = self.role_info.get(role_id, {}).get("name") or static_id
        current_max_hp = self.enemy_current_max_hp(role_id)
        max_hp_text = (
            f"｜当前最大生命 {current_max_hp}"
            if current_max_hp is not None
            else ""
        )
        print(
            f"识别敌方｜{role_id} → {static_id} → {name}"
            f"｜测速 {self.enemy_estimates.get(role_id, '-')}"
            f"{max_hp_text}"
        )
        self.announced_enemy_roles.add(role_id)

    def print_speed_report(self, end_times):
        if not self.start_times:
            return
        rows, ally_refs, unaffected_refs = [], [], []
        role_ids = sorted(
            set(self.start_times) | set(end_times), key=role_sort_key
        )
        for role_id in role_ids:
            info = self.role_info.get(role_id, {})
            start = self.start_times.get(role_id)
            end = end_times.get(role_id)
            delta = action_delta(start, end)
            speed = info.get("speed")
            if role_id.startswith("1-") and speed and delta and delta > 0:
                reference = role_id, speed, delta
                ally_refs.append(reference)
                if not info.get("speed_imprint_affected", False):
                    unaffected_refs.append(reference)
            rows.append({
                "role_id": role_id,
                "side": "我方" if role_id.startswith("1-") else "敌方",
                "name": info.get("name") or info.get("static_id") or "?",
                "start": start,
                "end": end,
                "delta": delta,
                "speed": speed,
            })
        self.enemy_estimates = self.estimate_enemy_speeds(
            rows, unaffected_refs or ally_refs
        )
        table_rows = []
        for row in rows:
            speed = row["speed"]
            if row["role_id"].startswith("2-"):
                speed = self.enemy_estimates.get(row["role_id"], "-")
            table_rows.append([
                row["side"],
                row["role_id"],
                row["name"],
                fmt_float(row["start"]),
                fmt_float(row["end"]),
                fmt_float(row["delta"]),
                speed,
            ])
        print_table(
            ["阵营", "RoleID", "角色", "乱速值", "行动值", "差值", "速度"],
            table_rows,
        )

    @staticmethod
    def estimate_enemy_speeds(rows, ally_refs):
        result = {}
        if not ally_refs:
            return result
        for row in rows:
            if not row["role_id"].startswith("2-") or row["delta"] is None:
                continue
            values = [
                ally_speed / ally_delta * row["delta"]
                for _, ally_speed, ally_delta in ally_refs
                if ally_delta
            ]
            if not values:
                continue
            speed = estimate_speed_value(values)
            low, high = min(values), max(values)
            result[row["role_id"]] = (
                f"{speed}"
                if round(low) == round(high)
                else f"{speed}({round(low)}-{round(high)})"
            )
        return result


def _find_process(device, target):
    if target.isdecimal():
        pid = int(target)
        return next(
            (process for process in device.enumerate_processes() if process.pid == pid),
            None,
        )
    target = target.casefold()
    return next(
        (
            process for process in device.enumerate_processes()
            if process.name.casefold() == target
        ),
        None,
    )


def _wait_for_process(device, target, interval=1):
    process = _find_process(device, target)
    if process is not None:
        return process

    print(f"找不到进程：{target}，请先打开游戏，正在等待...")
    try:
        while process is None:
            time.sleep(interval)
            process = _find_process(device, target)
    except KeyboardInterrupt:
        print("\n已停止")
        return None

    print(f"已检测到 {process.name} (PID {process.pid})，正在附加...")
    return process


def run_live(process_name, layout):
    try:
        import frida
    except ImportError:
        raise SpeedError("缺少 frida，请先运行：pip install frida")
    device = frida.get_local_device()
    process = _wait_for_process(device, process_name)
    if process is None:
        return

    analyzer = SpeedAnalyzer()
    session = None
    try:
        session = device.attach(process.pid)
        script = session.create_script(build_hook_source(layout))

        def on_message(message, _data):
            if message.get("type") == "send":
                payload = message.get("payload") or {}
                analyzer.handle_packet(payload.get("tag"), payload.get("text", ""))
            elif message.get("type") == "error":
                print(message.get("stack") or message, file=sys.stderr)

        script.on("message", on_message)
        script.load()
        print(f"已附加到 {process.name} (PID {process.pid})，等待战斗数据...")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        if session is not None:
            try:
                session.detach()
            except Exception:
                pass


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="实时计算 Ark Re:Code GVG/PVP/深渊敌方速度"
    )
    parser.add_argument(
        "-p", "--process", default=DEFAULT_PROCESS,
        help=f"进程名或 PID（默认：{DEFAULT_PROCESS}）",
    )
    parser.add_argument(
        "--dump", type=Path, default=DEFAULT_DUMP,
        help="dump.cs 路径（默认：脚本旁 data/dump.cs）",
    )
    parser.add_argument(
        "--master-db", type=Path, default=DEFAULT_MASTER_DB,
        help="master.db 路径（默认：脚本旁 data/master.db）",
    )
    parser.add_argument(
        "--offline", action="store_true",
        help="跳过在线 catalog 检查，直接使用现有 master.db",
    )
    parser.add_argument(
        "--force-master", action="store_true",
        help="强制下载最新 catalog 并重建 master.db",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="只检查/更新 dump.cs 与 master.db，不附加游戏",
    )
    args = parser.parse_args(argv)

    dump_path = args.dump.expanduser().resolve()
    master_path = args.master_db.expanduser().resolve()
    try:
        ensure_master_db(master_path, args.offline, args.force_master)
        global MASTER
        MASTER = load_master_data(master_path)
        layout = load_hook_layout(dump_path)
        print(
            f"[hook] Send RVA=0x{layout.send_rva:X}，"
            f"Decode RVA=0x{layout.decode_rva:X}，"
            f"Data=0x{layout.frame_data_offset:X}，"
            f"DataAsText=0x{layout.frame_text_offset:X}"
        )
        print(f"[master] 已加载 {len(MASTER.roles)} 个角色：{master_path}")
        if args.check:
            print("检查通过")
            return 0
        run_live(args.process, layout)
        return 0
    except (SpeedError, OSError, sqlite3.Error) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
