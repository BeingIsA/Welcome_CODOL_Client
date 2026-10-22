"""为 build 1649370 生成进入原 CRT 的独立试运行候选；本模块不执行游戏。

输入必须是已验证的 455 导入静态候选。恢复 PE 入口、固定基址和资源目录，
保留 TLS、原代码和 TVM 数据；没有返回成功的保护/登录桩。仅在新副本的
RT_MANIFEST 中把 requireAdministrator 降为 asInvoker，避免请求提权。
"""
from __future__ import annotations
from codol_rebuild.core.paths import REBUILD_ROOT as REBUILD_ROOT_PATH

import argparse
import json
import struct
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pefile

from codol_rebuild.core.baseline import BaselineError, save_new_json
from codol_rebuild.analysis.bootstrap import align, digest


SOURCE_SHA256 = "893f20f82cb81e54aab99090eea6e9f0304762278637ab8a441af7f2ea2c3a38"
ENTRY_RVA = 0x8427DB
ENTRY_BYTES = bytes.fromhex("e8dbff0000e989feffff")
FORBIDDEN_MODULES = {
    "codomp_client_shipretailbase.dll", "tersafe.dll", "tenproxy.dll", "tenslx.dll",
    "gamerpcs.dll", "tenrpcs.dll", "ace-ats32.dll", "ace-drv32.dll", "tp3helper.exe",
    "vsddrvdll.dll", "policyprobe.dll",
}


def demote_elevation_manifest(content: bytes) -> tuple[bytes, int]:
    """只改 manifest 的提权属性，保持资源字节长度和合法 XML。"""
    ET.fromstring(content)
    before = b'level="requireAdministrator"'
    after = b'level="asInvoker"'
    count = content.count(before)
    replaced = content.replace(before, after + b" " * (len(before) - len(after)))
    ET.fromstring(replaced)
    if len(replaced) != len(content):
        raise ValueError("manifest 大小变化，不能原位替换资源")
    return replaced, count


def section_for_rva(pe: Any, rva: int, size: int = 1) -> Any:
    """依据虚拟范围查找节，允许 .data 中合法的未初始化尾部。"""
    for section in pe.sections:
        extent = max(section.Misc_VirtualSize, section.SizeOfRawData)
        if section.VirtualAddress <= rva and rva + size <= section.VirtualAddress + extent:
            return section
    raise ValueError(f"RVA 不在有效节范围内：{rva:#x}, size={size}")


def inspect_tls(pe: Any) -> dict[str, Any]:
    """检查 TLS 模板、可写 index 和回调表，不关闭或伪造 TLS 初始化。"""
    directory = getattr(pe, "DIRECTORY_ENTRY_TLS", None)
    if directory is None:
        raise ValueError("预期 TLS 目录缺失")
    tls = directory.struct
    base = pe.OPTIONAL_HEADER.ImageBase
    start, end = tls.StartAddressOfRawData - base, tls.EndAddressOfRawData - base
    if start < 0 or end < start or end - start > 1024 * 1024:
        raise ValueError("TLS 模板区间无效")
    template = section_for_rva(pe, start, max(1, end - start))
    if start + (end - start) > template.VirtualAddress + template.SizeOfRawData:
        raise ValueError("TLS 模板必须有完整磁盘数据")
    index_rva = tls.AddressOfIndex - base
    index_section = section_for_rva(pe, index_rva, 4)
    if not index_section.Characteristics & 0x80000000:
        raise ValueError("TLS index 位于不可写节")
    callbacks = []
    callback_rva = tls.AddressOfCallBacks - base if tls.AddressOfCallBacks else 0
    if callback_rva:
        for offset in range(0, 256, 4):
            section_for_rva(pe, callback_rva + offset, 4)
            raw = pe.get_data(callback_rva + offset, 4)
            if len(raw) != 4:
                raise ValueError("TLS 回调表被截断")
            value = struct.unpack("<I", raw)[0]
            if not value:
                break
            target = section_for_rva(pe, value - base)
            if not target.Characteristics & 0x20000000:
                raise ValueError("TLS 回调位于不可执行节")
            callbacks.append(hex(value))
        else:
            raise ValueError("TLS 回调表缺少终止符")
    return {"template_rva": hex(start), "template_bytes": end - start,
            "index_rva": hex(index_rva), "index_writable": True,
            "callbacks_rva": hex(callback_rva), "callbacks": callbacks,
            "preserved": True}


def inspect_imports(pe: Any) -> dict[str, Any]:
    """确保恢复后的导入没有保护 DLL；未知 DLL 不转换成成功桩。"""
    rows = []
    count = 0
    for group in getattr(pe, "DIRECTORY_ENTRY_IMPORT", []):
        name = group.dll.decode("ascii")
        if name.casefold() in FORBIDDEN_MODULES:
            raise ValueError(f"候选仍依赖保护组件：{name}")
        count += len(group.imports)
        rows.append({"dll": name, "count": len(group.imports)})
    if len(rows) != 19 or count != 455:
        raise ValueError(f"恢复导入数量不符：{len(rows)} DLL / {count} imports")
    return {"dll_count": len(rows), "import_count": count, "groups": rows}


def audit_local_dependencies(pe: pefile.PE, client: Path) -> dict[str, Any]:
    """只读枚举原客户端中实际存在的传递依赖；不加载/复制任何 DLL。"""
    required = ["GCloudVoice.dll", "bink2w32.dll", "dbghelp.dll", "libcef.dll", "mss32.dll"]
    missing = [name for name in required if not (client / name).is_file()]
    if missing:
        raise BaselineError("缺少本地运行依赖：" + ", ".join(missing))
    queue = [group.dll.decode("ascii") for group in pe.DIRECTORY_ENTRY_IMPORT]
    visited, local, system = set(), [], set()
    while queue:
        name = queue.pop(0)
        normalized = name.casefold()
        if normalized in visited:
            continue
        visited.add(normalized)
        if normalized in FORBIDDEN_MODULES:
            raise ValueError(f"传递依赖包含保护组件：{name}")
        path = (client / name).resolve()
        if not path.is_relative_to(client.resolve()):
            raise ValueError("DLL 路径越出客户端目录")
        if not path.is_file():
            system.add(name)
            continue
        data = path.read_bytes()
        module = pefile.PE(data=data, fast_load=True)
        module.parse_data_directories(directories=[1, 13])
        if module.FILE_HEADER.Machine != 0x14C:
            raise ValueError(f"本地依赖不是 x86：{name}")
        dependencies = [group.dll.decode("ascii") for group in getattr(module, "DIRECTORY_ENTRY_IMPORT", [])]
        delayed = [group.dll.decode("ascii") for group in getattr(module, "DIRECTORY_ENTRY_DELAY_IMPORT", [])]
        local.append({"name": name, "source": str(path), "bytes": len(data), "sha256": digest(data),
                      "imports": dependencies, "delay_imports": delayed})
        queue.extend(dependencies + delayed)
    return {"local_modules": local, "system_or_api_set_names": sorted(system, key=str.casefold),
            "dlls_loaded": False, "dlls_copied": False,
            "note": "仅覆盖 PE import/delay-import；LoadLibrary 和子进程仍须受控宿主监测。"}


def prepare_image(data: bytes) -> tuple[bytes, dict[str, Any]]:
    """修改独立镜像的加载头和提权 manifest；原始代码逐节回读不变。"""
    if digest(data) != SOURCE_SHA256:
        raise BaselineError("输入不是经过验证的 build 1649370 导入恢复候选")
    pe = pefile.PE(data=data)
    if pe.FILE_HEADER.Machine != 0x14C or pe.OPTIONAL_HEADER.ImageBase != 0x400000:
        raise ValueError("候选机器类型或首选基址不符")
    if pe.get_data(ENTRY_RVA, len(ENTRY_BYTES)) != ENTRY_BYTES:
        raise ValueError("CRT 候选入口字节不符")
    if pe.OPTIONAL_HEADER.DATA_DIRECTORY[5].VirtualAddress or pe.OPTIONAL_HEADER.DATA_DIRECTORY[5].Size:
        raise ValueError("候选已有重定位目录，不能套用无重定位固定基址策略")
    imports = inspect_imports(pe)
    tls_before = inspect_tls(pe)
    if tls_before["callbacks"]:
        raise ValueError("出现未审计 TLS 回调，拒绝试运行候选")
    code_before = {index: section.get_data() for index, section in enumerate(pe.sections)
                   if section.Characteristics & 0x20000000}
    old_entry = pe.OPTIONAL_HEADER.AddressOfEntryPoint
    pe.OPTIONAL_HEADER.AddressOfEntryPoint = ENTRY_RVA
    pe.FILE_HEADER.Characteristics |= 0x0001  # IMAGE_FILE_RELOCS_STRIPPED：没有可用重定位表。
    pe.OPTIONAL_HEADER.DllCharacteristics &= ~0x0040  # 固定在 0x400000，不能启用 ASLR。
    resource = pe.sections[4]
    pe.OPTIONAL_HEADER.DATA_DIRECTORY[2].VirtualAddress = resource.VirtualAddress
    pe.OPTIONAL_HEADER.DATA_DIRECTORY[2].Size = resource.Misc_VirtualSize
    names = [b".text", b".rdata", b".data", b".tls", b".rsrc", b".tvm0", b".protect", b".rimp"]
    if len(pe.sections) != len(names):
        raise ValueError("候选节数量不符")
    for section, name in zip(pe.sections, names):
        section.Name = name.ljust(8, b"\0")
    # 原静态候选已将 .rdata 设为只读，保留此属性使 CRT 的 FP 初始化检查成立。
    if pe.sections[1].Characteristics != 0x40000040:
        raise ValueError("恢复候选的 .rdata 必须为只读数据节")
    pe.OPTIONAL_HEADER.SizeOfCode = sum(s.SizeOfRawData for s in pe.sections if s.Characteristics & 0x20)
    pe.OPTIONAL_HEADER.SizeOfInitializedData = sum(s.SizeOfRawData for s in pe.sections if s.Characteristics & 0x40)
    pe.OPTIONAL_HEADER.CheckSum = 0
    output = bytearray(pe.write())
    pe = pefile.PE(data=output)
    manifests = []
    for group in pe.DIRECTORY_ENTRY_RESOURCE.entries:
        if group.id != 24:
            continue
        for entry in group.directory.entries:
            for language in entry.directory.entries:
                item = language.data.struct
                content = pe.get_data(item.OffsetToData, item.Size)
                patched, count = demote_elevation_manifest(content)
                offset = pe.get_offset_from_rva(item.OffsetToData)
                output[offset:offset + item.Size] = patched
                manifests.append({"resource_id": entry.id, "language": language.id,
                                  "rva": hex(item.OffsetToData), "bytes": item.Size,
                                  "elevation_attributes_demoted": count, "sha256": digest(patched)})
    if sum(row["elevation_attributes_demoted"] for row in manifests) != 1:
        raise ValueError("提权 manifest 的预期唯一属性没有匹配")
    final = pefile.PE(data=output)
    final.OPTIONAL_HEADER.CheckSum = final.generate_checksum()
    output = bytes(final.write())
    check = pefile.PE(data=output)
    for index, expected in code_before.items():
        if check.sections[index].get_data() != expected:
            raise ValueError("生成加载候选时意外改动了代码节")
    if inspect_tls(check) != tls_before:
        raise ValueError("TLS 元数据发生意外变化")
    if inspect_imports(check) != imports:
        raise ValueError("恢复导入发生意外变化")
    return output, {"source_sha256": SOURCE_SHA256, "sha256": digest(output), "bytes": len(output),
                    "old_entry_rva": hex(old_entry), "entry_rva": hex(ENTRY_RVA), "entry_bytes": ENTRY_BYTES.hex(),
                    "image_base": hex(check.OPTIONAL_HEADER.ImageBase), "fixed_base_no_relocations": True,
                    "resource_directory_restored": True, "tls": tls_before, "imports": imports,
                    "manifests": manifests, "code_sections_unchanged": True,
                    "checksum_valid": check.verify_checksum(), "stub_modules": [],
                    "candidate_kind": "original_CRT_entry_trial_image", "runtime_verified": False,
                    "offline_lobby_verified": False,
                    "remaining": ["TVM 运行初始化未观测", "动态保护组件加载必须由受控宿主拒绝",
                                  "缺少本地依赖/资源的候选不能视为完整运行安装", "需要实际调试事件确认进入 CRT 和游戏初始化"]}


def build_offline_image(source: Path, client: Path, output: Path) -> dict[str, Any]:
    """生成独立 EXE 和加载清单；拒绝覆盖已有实验或落入原客户端目录。"""
    source, client, output = source.resolve(), client.resolve(), output.resolve()
    if output.exists():
        raise BaselineError("离线候选目录已存在，拒绝覆盖")
    if output == client or output.is_relative_to(client) or client.is_relative_to(output):
        raise BaselineError("离线候选必须位于原客户端目录之外且不能互为父目录")
    if source.is_relative_to(output):
        raise BaselineError("输出目录不能包含恢复输入")
    data = source.read_bytes()
    candidate, report = prepare_image(data)
    dependencies = audit_local_dependencies(pefile.PE(data=candidate), client)
    output.mkdir(parents=True, exist_ok=False)
    target = output / "codol1649370-offline.exe"
    with target.open("xb") as stream:
        stream.write(candidate)
    report.update(source=str(source), output=str(target), dependencies=dependencies,
                  source_unchanged=digest(source.read_bytes()) == digest(data),
                  dynamic_loader_iat={"LoadLibraryA": "0xca8158", "LoadLibraryW": "0xca8358",
                                      "GetProcAddress": "0xca8150", "CreateProcessA": "0xca8108", "CreateProcessW": "0xca81d0"},
                  dynamic_protection_calls=[{"module": "TenProxy.dll", "string_rva": "0x9c6edc", "load_call_va": "0x63e79f"},
                                            {"module": "TerSafe.dll", "string_rva": "0x9c6ef4", "load_call_va": "0x642766"}],
                  forbidden_runtime_modules=sorted(FORBIDDEN_MODULES))
    save_new_json(output / "offline-image.json", report)
    return report


def main(argv: list[str] | None = None) -> int:
    """构建命令入口，不提供 launch 功能。"""
    project = REBUILD_ROOT_PATH
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=project / "workspaces/bootstrap1649370-imports/codol1649370-imports-analysis-candidate.bin")
    parser.add_argument("--client", type=Path, default=project.parent / "Original Client")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = build_offline_image(args.source, args.client, args.output_dir)
    except (OSError, ValueError, pefile.PEFormatError, ET.ParseError) as error:
        print(json.dumps({"status": "offline_image_failed", "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps({"status": "trial_image_created", "output": report["output"], "sha256": report["sha256"],
                      "runtime_verified": False, "dependencies": [row["name"] for row in report["dependencies"]["local_modules"]]},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
