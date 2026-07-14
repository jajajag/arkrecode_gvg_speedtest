"""Monitor Ark Re:Code WebSocket JSON traffic with Frida.

Hook RVAs and managed field offsets are resolved from data/dump.cs at startup,
so updating dump.cs is enough when GameAssembly.dll changes.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys
import time


DEFAULT_PROCESS = "Ark ReCode.exe"


def _application_dir() -> Path:
    """Return the script/executable directory, independent of the current cwd."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


DEFAULT_DUMP = _application_dir() / "data" / "dump.cs"


class DumpParseError(RuntimeError):
    pass


@dataclass(frozen=True)
class DumpType:
    namespace: str
    name: str
    body: str

    @property
    def qualified_name(self) -> str:
        return f"{self.namespace}.{self.name}" if self.namespace else self.name


@dataclass(frozen=True)
class HookLayout:
    send_rva: int
    decode_rva: int
    frame_data_offset: int
    frame_text_offset: int
    websocket_type: str
    frame_reader_type: str


_NAMESPACE_RE = re.compile(r"(?m)^// Namespace:\s*(?P<name>[^\r\n]*)\s*$")
_TYPE_RE = re.compile(
    r"(?m)^(?!\s*//)[^\r\n]*\b(?:class|struct)\s+"
    r"(?P<name>[A-Za-z_]\w*)\b[^\r\n]*$"
)
_RVA_RE = re.compile(r"// RVA:\s*(?P<rva>0x[0-9A-Fa-f]+|-1)\b")
_FIELD_RE = re.compile(
    r"^(?P<declaration>.*?)//\s*0x(?P<offset>[0-9A-Fa-f]+)\s*$"
)
_FIELD_NAME_RE = re.compile(
    r"(?P<name><[^>]+>k__BackingField|[A-Za-z_]\w*)\s*(?:;|=)"
)


def parse_dump_types(text: str) -> list[DumpType]:
    namespace_matches = list(_NAMESPACE_RE.finditer(text))
    result = []
    for index, namespace_match in enumerate(namespace_matches):
        end = (
            namespace_matches[index + 1].start()
            if index + 1 < len(namespace_matches)
            else len(text)
        )
        body = text[namespace_match.end():end]
        type_match = _TYPE_RE.search(body)
        if type_match:
            result.append(DumpType(
                namespace=namespace_match.group("name").strip(),
                name=type_match.group("name"),
                body=body,
            ))
    if not result:
        raise DumpParseError("dump.cs 中没有找到任何类型定义")
    return result


def _split_parameters(parameters: str) -> list[str]:
    parts = []
    start = 0
    depth = 0
    for index, char in enumerate(parameters):
        if char in "<[(":
            depth += 1
        elif char in ">])":
            depth = max(depth - 1, 0)
        elif char == "," and depth == 0:
            parts.append(parameters[start:index].strip())
            start = index + 1
    tail = parameters[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _parameter_type(parameter: str) -> str:
    # Remove default values. The target methods do not use attributes or
    # defaults, but handling them makes the dump parser less version-sensitive.
    parameter = parameter.split("=", 1)[0].strip()
    parameter = re.sub(r"^(?:ref|out|in|params|this)\s+", "", parameter)
    type_and_name = parameter.rsplit(None, 1)
    return type_and_name[0] if len(type_and_name) == 2 else parameter


def _canonical_type(type_name: str) -> str:
    compact = re.sub(r"\s+", "", type_name).replace("global::", "")
    aliases = {
        "System.String": "string",
        "String": "string",
    }
    compact = aliases.get(compact, compact)
    # Namespace changes should not invalidate a method whose declaring type and
    # signature are otherwise unambiguous.
    if "." in compact and not compact.endswith("[]"):
        compact = compact.rsplit(".", 1)[-1]
    return compact


def _parse_signature(signature: str) -> tuple[str, tuple[str, ...]] | None:
    signature = signature.strip()
    match = re.search(
        r"(?P<name>\.?[A-Za-z_]\w*)\s*\((?P<parameters>.*)\)\s*"
        r"(?:\{\s*\}|;)?\s*$",
        signature,
    )
    if not match:
        return None
    parameters = match.group("parameters").strip()
    parameter_types = tuple(
        _canonical_type(_parameter_type(parameter))
        for parameter in _split_parameters(parameters)
    ) if parameters else ()
    return match.group("name"), parameter_types


def resolve_method_rva(
    dump_type: DumpType,
    method_name: str,
    parameter_types: tuple[str, ...],
) -> int:
    expected_types = tuple(_canonical_type(value) for value in parameter_types)
    lines = dump_type.body.splitlines()
    matches = []
    for index, line in enumerate(lines):
        rva_match = _RVA_RE.search(line)
        if not rva_match:
            continue
        signature = None
        for following in lines[index + 1:index + 8]:
            following = following.strip()
            if not following or following.startswith("["):
                continue
            if following.startswith("//") or following.startswith("|-"):
                break
            signature = following
            break
        parsed = _parse_signature(signature or "")
        if not parsed:
            continue
        found_name, found_types = parsed
        if found_name == method_name and found_types == expected_types:
            rva_text = rva_match.group("rva")
            if rva_text == "-1":
                raise DumpParseError(
                    f"{dump_type.qualified_name}.{method_name} 没有可用 RVA"
                )
            matches.append(int(rva_text, 16))

    unique_matches = sorted(set(matches))
    signature_text = ", ".join(parameter_types)
    display_name = f"{dump_type.qualified_name}.{method_name}({signature_text})"
    if not unique_matches:
        raise DumpParseError(f"dump.cs 中找不到方法：{display_name}")
    if len(unique_matches) > 1:
        values = ", ".join(f"0x{value:X}" for value in unique_matches)
        raise DumpParseError(f"方法匹配到多个 RVA：{display_name} -> {values}")
    return unique_matches[0]


def resolve_field_offset(dump_type: DumpType, candidates: tuple[str, ...]) -> int:
    # Field definitions precede the Properties/Methods section in Il2CppDumper
    # output. Restricting the scan avoids confusing properties with fields.
    fields_text = dump_type.body.split("// Properties", 1)[0]
    offsets: dict[str, int] = {}
    for line in fields_text.splitlines():
        field_match = _FIELD_RE.match(line.strip())
        if not field_match:
            continue
        name_match = _FIELD_NAME_RE.search(field_match.group("declaration"))
        if name_match:
            offsets[name_match.group("name")] = int(field_match.group("offset"), 16)

    for name in candidates:
        if name in offsets:
            return offsets[name]
    names = " / ".join(candidates)
    raise DumpParseError(
        f"dump.cs 的 {dump_type.qualified_name} 中找不到字段：{names}"
    )


def _find_type(
    types: list[DumpType],
    namespace: str,
    type_name: str,
    method_name: str,
    parameter_types: tuple[str, ...],
) -> DumpType:
    exact = [
        item for item in types
        if item.namespace == namespace and item.name == type_name
    ]
    if len(exact) == 1:
        return exact[0]

    # Fall back to a unique type+method match if a future library version moves
    # the type to another namespace.
    compatible = []
    for item in types:
        if item.name != type_name:
            continue
        try:
            resolve_method_rva(item, method_name, parameter_types)
        except DumpParseError:
            continue
        compatible.append(item)
    if len(compatible) == 1:
        return compatible[0]

    qualified_name = f"{namespace}.{type_name}"
    if not compatible:
        raise DumpParseError(f"dump.cs 中找不到类型：{qualified_name}")
    found = ", ".join(item.qualified_name for item in compatible)
    raise DumpParseError(f"类型匹配不唯一：{qualified_name} -> {found}")


def load_hook_layout(dump_path: Path) -> HookLayout:
    try:
        text = dump_path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise DumpParseError(f"找不到 dump.cs：{dump_path}") from exc
    except (OSError, UnicodeError) as exc:
        raise DumpParseError(f"无法读取 dump.cs：{dump_path}（{exc}）") from exc

    types = parse_dump_types(text)
    websocket = _find_type(
        types,
        "BestHTTP.WebSocket",
        "WebSocket",
        "Send",
        ("string",),
    )
    frame_reader = _find_type(
        types,
        "BestHTTP.WebSocket.Frames",
        "WebSocketFrameReader",
        "DecodeWithExtensions",
        ("WebSocket",),
    )

    return HookLayout(
        send_rva=resolve_method_rva(websocket, "Send", ("string",)),
        decode_rva=resolve_method_rva(
            frame_reader,
            "DecodeWithExtensions",
            ("WebSocket",),
        ),
        frame_data_offset=resolve_field_offset(
            frame_reader,
            ("<Data>k__BackingField", "Data", "data"),
        ),
        frame_text_offset=resolve_field_offset(
            frame_reader,
            ("<DataAsText>k__BackingField", "DataAsText", "dataAsText"),
        ),
        websocket_type=websocket.qualified_name,
        frame_reader_type=frame_reader.qualified_name,
    )


def build_hook_source(layout: HookLayout) -> str:
    config = json.dumps({
        "sendRva": f"0x{layout.send_rva:X}",
        "decodeRva": f"0x{layout.decode_rva:X}",
        "frameDataOffset": layout.frame_data_offset,
        "frameTextOffset": layout.frame_text_offset,
    })
    return r'''"use strict";

const CONFIG = __HOOK_CONFIG__;
const mod = Process.getModuleByName("GameAssembly.dll");
const BASE = mod.base;

console.log("[+] GameAssembly base = " + BASE);

function addr(rva) {
    return BASE.add(ptr(rva));
}

function readIl2CppString(p) {
    if (p.isNull()) return null;
    try {
        const len = p.add(0x10).readS32();
        return p.add(0x14).readUtf16String(len);
    } catch (_) {
        return null;
    }
}

function readByteArrayAsUtf8(arr, maxLen = 1024 * 1024) {
    if (arr.isNull()) return null;
    try {
        const len = arr.add(0x18).readU32();
        if (len <= 0 || len > maxLen) return null;
        const bytes = arr.add(0x20).readByteArray(len);
        return new TextDecoder("utf-8").decode(bytes);
    } catch (_) {
        return null;
    }
}

function isJsonText(s) {
    if (!s) return false;
    s = s.trim();
    return s.startsWith("{") || s.startsWith("[");
}

function printPacket(tag, s) {
    if (!isJsonText(s)) return;
    console.log("\n== " + tag + " ==");
    console.log(s.trim());
}

function getFrameText(fr) {
    try {
        const textPtr = fr.add(CONFIG.frameTextOffset).readPointer();
        const text = readIl2CppString(textPtr);
        if (isJsonText(text)) return text;

        const dataPtr = fr.add(CONFIG.frameDataOffset).readPointer();
        const dataText = readByteArrayAsUtf8(dataPtr);
        if (isJsonText(dataText)) return dataText;
        return null;
    } catch (_) {
        return null;
    }
}

function hookRva(name, rva, callbacks) {
    const target = addr(rva);
    console.log("[+] hook " + name + " " + target);
    Interceptor.attach(target, callbacks);
}

hookRva("SEND WebSocket.Send(string)", CONFIG.sendRva, {
    onEnter(args) {
        printPacket("SEND", readIl2CppString(args[1]));
    }
});

hookRva("RECV FrameReader.DecodeWithExtensions", CONFIG.decodeRva, {
    onEnter(args) {
        this.fr = args[0];
    },
    onLeave(_) {
        printPacket("RECV", getFrameText(this.fr));
    }
});
'''.replace("__HOOK_CONFIG__", config)


def print_layout(dump_path: Path, layout: HookLayout) -> None:
    print(f"[+] dump.cs = {dump_path}")
    print(
        f"[+] SEND {layout.websocket_type}.Send(string) "
        f"RVA=0x{layout.send_rva:X}"
    )
    print(
        f"[+] RECV {layout.frame_reader_type}.DecodeWithExtensions(WebSocket) "
        f"RVA=0x{layout.decode_rva:X}"
    )
    print(
        f"[+] frame fields: Data=0x{layout.frame_data_offset:X}, "
        f"DataAsText=0x{layout.frame_text_offset:X}"
    )


def _find_process(device, process_name: str):
    if process_name.isdecimal():
        pid = int(process_name)
        for process in device.enumerate_processes():
            if process.pid == pid:
                return process
        return None

    target = process_name.casefold()
    for process in device.enumerate_processes():
        if process.name.casefold() == target:
            return process
    return None


def run_live(process_name: str, dump_path: Path, check_dump: bool = False) -> int:
    try:
        layout = load_hook_layout(dump_path)
    except DumpParseError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1

    print_layout(dump_path, layout)
    if check_dump:
        print("[+] dump.cs 解析检查通过")
        return 0

    try:
        import frida
    except ImportError:
        print(
            "[!] 缺少 frida Python 包，请先运行：pip install frida",
            file=sys.stderr,
        )
        return 1

    session = None
    try:
        device = frida.get_local_device()
        process = _find_process(device, process_name)
        if process is None:
            print(f"[!] 找不到进程：{process_name}", file=sys.stderr)
            return 1

        session = device.attach(process.pid)
        script = session.create_script(build_hook_source(layout))

        def on_message(message, _data):
            message_type = message.get("type")
            if message_type == "log":
                print(message.get("payload", ""))
            elif message_type == "error":
                print(message.get("stack") or message, file=sys.stderr)
            elif message_type == "send":
                print(message.get("payload", ""))

        script.on("message", on_message)
        script.load()
        print(f"[+] attached to {process.name} (PID {process.pid})")
        print("[+] monitoring JSON traffic; press Ctrl+C to stop")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n[+] stopped")
        return 0
    except Exception as exc:
        print(f"[!] Frida 附加或注入失败：{exc}", file=sys.stderr)
        return 1
    finally:
        if session is not None:
            try:
                session.detach()
            except Exception:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="通过 dump.cs 动态解析地址并监控游戏 WebSocket JSON 数据流"
    )
    parser.add_argument(
        "-p",
        "--process",
        default=DEFAULT_PROCESS,
        help=f"进程名或 PID（默认：{DEFAULT_PROCESS}）",
    )
    parser.add_argument(
        "--dump",
        type=Path,
        default=DEFAULT_DUMP,
        help="dump.cs 路径（默认：脚本旁的 data/dump.cs）",
    )
    parser.add_argument(
        "--check-dump",
        action="store_true",
        help="只检查 dump.cs 解析结果，不连接游戏",
    )
    args = parser.parse_args(argv)
    return run_live(
        process_name=args.process,
        dump_path=args.dump.expanduser().resolve(),
        check_dump=args.check_dump,
    )


if __name__ == "__main__":
    raise SystemExit(main())
