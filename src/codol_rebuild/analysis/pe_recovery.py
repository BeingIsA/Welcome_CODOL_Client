"""build 1649370 的 PE 恢复与静态分析；不执行输入代码。

新版主 EXE 的代码节使用逐块 LZ4，旧版 AP32 恢复路径不能直接套用。
本工具仅生成独立二进制分析数据和 PE 静态候选；不修改原客户端、
不改入口为猜测的 OEP、不重建未经证实的导入，也不调用 LoadLibrary。
LZ4 编码依据：https://github.com/lz4/lz4/blob/dev/doc/lz4_Block_format.md
"""
from __future__ import annotations
from codol_rebuild.core.paths import REBUILD_ROOT as REBUILD_ROOT_PATH

import argparse
import hashlib
import json
import math
import re
import struct
import zlib
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Any

import capstone
import pefile

from codol_rebuild.core.baseline import BaselineError, load_manifest, save_new_json, source_path


def digest(data: bytes) -> str:
    """计算内存数据指纹；每份恢复数据都记录独立 SHA-256。"""
    return hashlib.sha256(data).hexdigest()


def align(value: int, boundary: int) -> int:
    """按 PE 中声明的正整数边界向上对齐。"""
    if boundary <= 0:
        raise ValueError("对齐大小必须大于零")
    return (value + boundary - 1) // boundary * boundary


def decode_lz4_block(data: bytes, expected_size: int) -> bytes:
    """解码无字典的 LZ4 数据块，检查读写边界、回溯距离和精确输出大小。

    不依赖输入提供内存上限，单块上限 4 MiB。允许重叠回溯，拒绝零距离。
    输出长度校验不等于 checksum 校验；本格式的 4 字节尾字段尚未解释。
    """
    if not 0 < expected_size <= 4 * 1024 * 1024:
        raise ValueError("LZ4 声明输出长度越界")
    cursor = 0
    output = bytearray()

    def read_length(value: int) -> int:
        """读取 token 长度扩展，截断输入必须失败。"""
        nonlocal cursor
        if value == 15:
            while True:
                if cursor >= len(data):
                    raise ValueError("LZ4 长度扩展被截断")
                extra = data[cursor]
                cursor += 1
                value += extra
                if value > expected_size:
                    raise ValueError("LZ4 扩展长度超过输出上限")
                if extra != 255:
                    break
        return value

    while cursor < len(data):
        token = data[cursor]
        cursor += 1
        literal_size = read_length(token >> 4)
        if cursor + literal_size > len(data) or len(output) + literal_size > expected_size:
            raise ValueError("LZ4 字面量超过输入或输出边界")
        output.extend(data[cursor:cursor + literal_size])
        cursor += literal_size
        if cursor == len(data):
            break
        if cursor + 2 > len(data):
            raise ValueError("LZ4 回溯距离被截断")
        distance = struct.unpack_from("<H", data, cursor)[0]
        cursor += 2
        length = read_length(token & 15) + 4
        if not 0 < distance <= len(output):
            raise ValueError("LZ4 回溯距离无效")
        if len(output) + length > expected_size:
            raise ValueError("LZ4 匹配超过输出边界")
        # 逐字节追加使重叠复制符合 LZ4 格式，而非误用不重叠切片。
        for _ in range(length):
            output.append(output[-distance])
    if len(output) != expected_size:
        raise ValueError(f"LZ4 输出长度不符：{len(output)} != {expected_size}")
    return bytes(output)


def recover_lz4_section(data: bytes, expected_size: int) -> tuple[bytes, dict[str, Any]]:
    """解析实测逐块包装：两个长度、压缩数据、未知 4 字节尾字段、零填充。

    只接受连续块恰好覆盖目标大小。把尾字段原样留证，不将其误标为 CRC。
    """
    cursor = 0
    chunks = []
    records = []
    total = 0
    if not 0 < expected_size <= 64 * 1024 * 1024:
        raise ValueError("恢复节大小超过允许上限")
    while total < expected_size:
        if cursor + 12 > len(data):
            raise ValueError("LZ4 包装被截断")
        unpacked, packed = struct.unpack_from("<II", data, cursor)
        if not 0 < unpacked <= 0x20000 or total + unpacked > expected_size:
            raise ValueError("LZ4 包装的解压长度无效")
        if not 0 < packed <= len(data) - cursor - 12:
            raise ValueError("LZ4 包装的压缩长度无效")
        blob = data[cursor + 8:cursor + 8 + packed]
        result = decode_lz4_block(blob, unpacked)
        trailer = struct.unpack_from("<I", data, cursor + 8 + packed)[0]
        records.append({"section_offset": hex(cursor), "output_offset": hex(total),
                        "packed_bytes": packed, "unpacked_bytes": unpacked,
                        "trailer_raw_u32": hex(trailer), "packed_crc32": hex(zlib.crc32(blob)),
                        "unpacked_crc32": hex(zlib.crc32(result)), "sha256": digest(result)})
        chunks.append(result)
        total += unpacked
        cursor += 12 + packed
    if any(data[cursor:]):
        raise ValueError("LZ4 节尾存在未解释的非零字节")
    recovered = b"".join(chunks)
    return recovered, {"format": "u32 unpacked, u32 packed, LZ4 payload, unknown u32 trailer",
                       "block_count": len(records), "blocks": records,
                       "bytes": len(recovered), "sha256": digest(recovered),
                       "consumed_bytes": cursor, "zero_padding_bytes": len(data) - cursor,
                       "all_output_sizes_match": True, "trailer_integrity_verified": False}


def scan_ap32(data: bytes, decoder: Any = None) -> tuple[list[dict[str, Any]], dict[int, bytes]]:
    """筛选 AP32 并验证压缩及解压 CRC；无效字符串命中也保留失败原因。

    AP32 只有 packed/unpacked CRC，没有单独的头 CRC，头部采用尺寸约束。
    默认使用已安装 aplib 的严格模式；不复制第三方源码。
    """
    records: list[dict[str, Any]] = []
    recovered: dict[int, bytes] = {}
    for match in re.finditer(b"AP32", data):
        offset = match.start()
        row: dict[str, Any] = {"file_offset": hex(offset), "verified": False}
        records.append(row)
        if offset + 24 > len(data):
            row["rejected_reason"] = "truncated_header"
            continue
        header, packed, packed_crc, unpacked, unpacked_crc = struct.unpack_from("<5I", data, offset + 4)
        row.update(header_bytes=header, packed_bytes=packed, unpacked_bytes=unpacked,
                   packed_crc32=hex(packed_crc), unpacked_crc32=hex(unpacked_crc))
        if not (24 <= header <= 4096 and 0 < packed <= len(data) - offset - header
                and 0 < unpacked <= 64 * 1024 * 1024):
            row["rejected_reason"] = "header_size_or_bounds"
            continue
        payload = data[offset + header:offset + header + packed]
        row["packed_crc_matches"] = zlib.crc32(payload) == packed_crc
        if not row["packed_crc_matches"]:
            row["rejected_reason"] = "packed_crc_mismatch"
            continue
        try:
            if decoder is None:
                import aplib
                decoder = aplib.decompress
            decoded = decoder(data[offset:offset + header + packed], strict=True)
            if len(decoded) != unpacked or zlib.crc32(decoded) != unpacked_crc:
                raise ValueError("AP32 解压长度或 CRC 不匹配")
        except ImportError as error:
            raise BaselineError("需要安装 aplib==0.6 才能验证 AP32；不能把缺依赖当作数据损坏") from error
        except (RuntimeError, ValueError, IndexError) as error:
            row.update(rejected_reason="decoder_failed", error=str(error))
            continue
        row.update(verified=True, unpacked_crc_matches=True, sha256=digest(decoded),
                   classification="non_PE_binary_metadata_candidate" if not decoded.startswith(b"MZ") else "PE_candidate",
                   starts_with_mz=decoded.startswith(b"MZ"),
                   ascii_strings_8_or_more=len(re.findall(rb"[ -~]{8,}", decoded)),
                   zero_fraction=round(decoded.count(0) / len(decoded), 6))
        recovered[offset] = decoded
    return records, recovered


def recover_lz4_metadata(data: bytes, import_offset: int) -> tuple[list[dict[str, Any]], list[bytes]]:
    """解码新版三段 LZ4 标记元数据；边界来自相邻标记和现存导入目录。

    前两段末尾各有未知 4 字节，最后一段在保护导入描述符处结束。
    只记录其原值，不猜测加密含义，也不把它们叫作已验证校验码。
    """
    offsets = [match.start() for match in re.finditer(b"LZ4\0", data)]
    if len(offsets) != 3 or import_offset <= offsets[-1]:
        raise ValueError("此构建预期的三个 LZ4 元数据标记或边界不符")
    records, decoded = [], []
    for index, (start, end) in enumerate(zip(offsets, offsets[1:] + [import_offset])):
        trailer_size = 4 if index < 2 else 0
        expected = struct.unpack_from("<I", data, start + 4)[0]
        packed = data[start + 8:end - trailer_size]
        raw = decode_lz4_block(packed, expected)
        records.append({"file_offset": hex(start), "end_file_offset": hex(end),
                        "packed_bytes": len(packed), "unpacked_bytes": len(raw), "sha256": digest(raw),
                        "unknown_trailer_hex": data[end - trailer_size:end].hex() if trailer_size else "",
                        "integrity_checksum_verified": False})
        decoded.append(raw)
    return records, decoded


def import_hashes(name: str) -> tuple[int, int]:
    """复用已确认的双哈希规则：BKDR131 与 FNV-1；只有双值相等才候选匹配。"""
    bkdr, fnv = 0, 0x811C9DC5
    for char in name.encode("ascii"):
        bkdr = (bkdr * 131 + char) & 0xFFFFFFFF
        fnv = ((fnv * 16777619) ^ char) & 0xFFFFFFFF
    return bkdr, fnv


def resolve_imports(pe: pefile.PE, dlls: bytes, functions: bytes, client: Path) -> dict[str, Any]:
    """用本机 x86 DLL 导出静态恢复导入名称，不加载 DLL、不联网下载模块。

    记录格式为 DLL 字符串偏移、零标记、两个名称哈希；记录间夹杂随机字节。
    各组顺序需要与原 IAT 零终止吻合，另用 CRT cookie 初始化调用交叉验证。
    """
    records = []
    for offset in range(max(0, len(functions) - 15)):
        pointer, marker, first, second = struct.unpack_from("<4I", functions, offset)
        if marker != 0 or pointer >= len(dlls):
            continue
        name = dlls[pointer:].split(b"\0", 1)[0]
        if not re.fullmatch(rb"[A-Za-z0-9_]+\.dll", name, re.I):
            continue
        records.append({"metadata_offset": hex(offset), "dll_offset": hex(pointer),
                        "dll": name.decode("ascii"), "hash_pair": [hex(first), hex(second)]})
    groups: dict[str, list[dict[str, Any]]] = OrderedDict()
    for row in records:
        groups.setdefault(row["dll"], []).append(row)
    iat = pe.sections[1].VirtualAddress
    sources, boundaries = [], []
    for dll, items in groups.items():
        candidates: dict[tuple[int, int], set[str]] = {}
        for path in [client / dll, Path("C:/Windows/SysWOW64") / dll]:
            if not path.is_file():
                continue
            content = path.read_bytes()
            exports = pefile.PE(data=content, fast_load=True)
            exports.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_EXPORT"]])
            if exports.FILE_HEADER.Machine != 0x14C:
                raise ValueError(f"导出参考不是 x86：{path}")
            sources.append({"path": str(path), "sha256": digest(content), "dll": dll})
            for item in getattr(getattr(exports, "DIRECTORY_ENTRY_EXPORT", None), "symbols", []):
                if item.name:
                    name = item.name.decode("ascii")
                    candidates.setdefault(import_hashes(name), set()).add(name)
        for row in items:
            first, second = (int(value, 16) for value in row["hash_pair"])
            thunk = struct.unpack("<I", pe.get_data(iat, 4))[0]
            row.update(iat_rva=hex(iat), original_thunk=hex(thunk),
                       candidates=sorted(candidates.get((first, second), set())))
            if thunk & 0x80000000:
                row["original_ordinal"] = thunk & 0xFFFF
                row["ordinal_hash_verified"] = import_hashes("#" + str(thunk & 0xFFFF)) == (first, second)
            row["resolved"] = row.get("ordinal_hash_verified", False) or len(row["candidates"]) == 1
            iat += 4
        boundary = {"dll": dll, "iat_terminator_rva": hex(iat), "zero_terminator": pe.get_data(iat, 4) == bytes(4)}
        boundaries.append(boundary)
        iat += 4
    expected = {0x8A81F4: "GetSystemTimeAsFileTime", 0x8A81F0: "GetCurrentProcessId",
                0x8A8164: "GetCurrentThreadId", 0x8A8368: "GetTickCount", 0x8A812C: "QueryPerformanceCounter"}
    indexed = {int(row["iat_rva"], 16): row for row in records}
    checks = [{"iat_rva": hex(rva), "expected": name,
               "actual": indexed.get(rva, {}).get("candidates", []),
               "pass": indexed.get(rva, {}).get("candidates", []) == [name]}
              for rva, name in expected.items()]
    complete = (len(records) == 455 and len(groups) == 19 and all(row["resolved"] for row in records)
                and all(row["zero_terminator"] for row in boundaries) and all(row["pass"] for row in checks))
    return {"method": "BKDR131 + FNV-1 matched against local PE exports, never loaded",
            "complete": complete, "record_count": len(records), "dll_count": len(groups),
            "records": records, "group_boundaries": boundaries, "independent_crt_checks": checks,
            "export_sources": sources, "iat_rva": hex(pe.sections[1].VirtualAddress),
            "iat_bytes": iat - pe.sections[1].VirtualAddress}


def add_analysis_imports(candidate: bytes, imports: dict[str, Any]) -> tuple[bytes, dict[str, Any]]:
    """向静态容器追加 .rimp，恢复名称供反汇编使用；保留原保护入口。

    原保护入口的 ordinal 槽不属于恢复后的业务导入，尚不能运行该候选。
    不尝试猜测修补保护依赖或选择新入口。
    """
    if not imports["complete"]:
        raise ValueError("导入恢复未完整验证，拒绝生成导入候选")
    pe = pefile.PE(data=candidate)
    groups: dict[str, list[dict[str, Any]]] = OrderedDict()
    for row in imports["records"]:
        groups.setdefault(row["dll"], []).append(row)
    new_rva = align(pe.OPTIONAL_HEADER.SizeOfImage, pe.OPTIONAL_HEADER.SectionAlignment)
    directory_size = (len(groups) + 1) * 20
    table = bytearray(directory_size)

    def append(content: bytes, alignment: int = 1) -> int:
        """添加导入名称/INT，并返回相对镜像地址。"""
        table.extend(bytes(align(len(table), alignment) - len(table)))
        result = new_rva + len(table)
        table.extend(content)
        return result

    out = bytearray(candidate)
    for index, (dll, items) in enumerate(groups.items()):
        name_rva = append(dll.encode("ascii") + b"\0")
        thunks = []
        for row in items:
            thunk = (0x80000000 | row["original_ordinal"]) if row.get("ordinal_hash_verified") else append(b"\0\0" + row["candidates"][0].encode("ascii") + b"\0", 2)
            thunks.append(thunk)
            struct.pack_into("<I", out, pe.get_offset_from_rva(int(row["iat_rva"], 16)), thunk)
        lookup_rva = append(struct.pack("<" + "I" * (len(thunks) + 1), *thunks, 0), 4)
        struct.pack_into("<5I", table, index * 20, lookup_rva, 0, 0, name_rva, int(items[0]["iat_rva"], 16))
    # 用刚修改过 IAT 的数据重建 pefile 状态，避免 write() 再写回旧 thunk。
    pe = pefile.PE(data=out)
    last = pe.sections[-1]
    header_offset = last.get_file_offset() + 40
    if header_offset + 40 > pe.OPTIONAL_HEADER.SizeOfHeaders:
        raise ValueError("PE 头没有新增节空间")
    section = pefile.SectionStructure(pe.__IMAGE_SECTION_HEADER_format__)
    section.__unpack__(bytes(40))
    section.set_file_offset(header_offset)
    section.Name = b".rimp\0\0\0"
    section.VirtualAddress = new_rva
    section.Misc_VirtualSize = len(table)
    section.SizeOfRawData = align(len(table), pe.OPTIONAL_HEADER.FileAlignment)
    section.PointerToRawData = len(out)
    section.Characteristics = 0x40000040
    pe.__structures__.append(section)
    pe.sections.append(section)
    pe.FILE_HEADER.NumberOfSections += 1
    pe.OPTIONAL_HEADER.SizeOfImage = align(new_rva + len(table), pe.OPTIONAL_HEADER.SectionAlignment)
    pe.OPTIONAL_HEADER.DATA_DIRECTORY[1].VirtualAddress = new_rva
    pe.OPTIONAL_HEADER.DATA_DIRECTORY[1].Size = directory_size
    pe.OPTIONAL_HEADER.DATA_DIRECTORY[12].VirtualAddress = int(imports["iat_rva"], 16)
    pe.OPTIONAL_HEADER.DATA_DIRECTORY[12].Size = imports["iat_bytes"]
    out = bytearray(pe.write())
    out.extend(table)
    out.extend(bytes(section.SizeOfRawData - len(table)))
    parsed = pefile.PE(data=out)
    actual = {(group.dll.decode(), item.address - parsed.OPTIONAL_HEADER.ImageBase):
              (item.name.decode() if item.name else None, item.ordinal)
              for group in parsed.DIRECTORY_ENTRY_IMPORT for item in group.imports}
    for row in imports["records"]:
        expected = (None, row["original_ordinal"]) if row.get("ordinal_hash_verified") else (row["candidates"][0], None)
        observed = actual.get((row["dll"], int(row["iat_rva"], 16)))
        # pefile 会为部分已知系统 ordinal 补充 name；ordinal 一致即为原始导入语义。
        matches = observed is not None and (observed[1] == expected[1] if row.get("ordinal_hash_verified") else observed == expected)
        if not matches:
            raise ValueError(f"导入候选重新解析未逐项匹配：{row['dll']} {row['iat_rva']} expected={expected} actual={actual.get((row['dll'], int(row['iat_rva'], 16)))}")
    return bytes(out), {"bytes": len(out), "sha256": digest(out), "import_count": len(actual),
                        "dll_count": len(parsed.DIRECTORY_ENTRY_IMPORT), "imports_readback_verified": True,
                        "launch_ready": False, "runtime_tested": False,
                        "entry_rva_unchanged": hex(parsed.OPTIONAL_HEADER.AddressOfEntryPoint),
                        "blocker": "Entry still calls the original protection IAT slot; original OEP/TVM initialization is unverified."}


def describe_pe(data: bytes) -> tuple[pefile.PE, dict[str, Any]]:
    """报告磁盘映射范围和静态依赖；虚拟零区不误作已经恢复的运行时代码。"""
    pe = pefile.PE(data=data)
    sections = []
    for index, section in enumerate(pe.sections):
        blob = section.get_data()
        counts = Counter(blob)
        entropy = -sum(n / len(blob) * math.log2(n / len(blob)) for n in counts.values()) if blob else 0
        sections.append({"index": index, "name": section.Name.rstrip(b"\0").decode("ascii", "replace"),
                         "rva": hex(section.VirtualAddress), "virtual_bytes": section.Misc_VirtualSize,
                         "file_offset": hex(section.PointerToRawData), "raw_bytes": section.SizeOfRawData,
                         "virtual_only_bytes": max(0, section.Misc_VirtualSize - section.SizeOfRawData),
                         "characteristics": hex(section.Characteristics), "entropy": round(entropy, 4)})
    groups = [{"dll": group.dll.decode("ascii", "replace"), "imports": [
        {"name": item.name.decode("ascii", "replace") if item.name else None,
         "ordinal": item.ordinal, "iat_rva": hex(item.address - pe.OPTIONAL_HEADER.ImageBase)}
        for item in group.imports]} for group in getattr(pe, "DIRECTORY_ENTRY_IMPORT", [])]
    exports = [{"ordinal": item.ordinal, "rva": hex(item.address),
                "name": item.name.decode("ascii", "replace") if item.name else None}
               for item in getattr(getattr(pe, "DIRECTORY_ENTRY_EXPORT", None), "symbols", [])]
    return pe, {"machine": hex(pe.FILE_HEADER.Machine), "image_base": hex(pe.OPTIONAL_HEADER.ImageBase),
                "image_bytes": pe.OPTIONAL_HEADER.SizeOfImage, "entry_rva": hex(pe.OPTIONAL_HEADER.AddressOfEntryPoint),
                "sections": sections, "imports": groups, "exports": exports,
                "directories": [{"name": directory.name, "rva_or_security_file_offset": hex(directory.VirtualAddress),
                                 "bytes": directory.Size} for directory in pe.OPTIONAL_HEADER.DATA_DIRECTORY
                                if directory.VirtualAddress or directory.Size]}


def disassemble_path(pe: pefile.PE, rva: int, recovered: dict[int, bytes], max_steps: int = 40) -> list[dict[str, Any]]:
    """有界追踪入口基本块：跟随直接 jmp，遇到间接分支/返回停止；不模拟指令。"""
    engine = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    engine.detail = True
    base = pe.OPTIONAL_HEADER.ImageBase
    visited: set[int] = set()
    result = []
    for _ in range(max_steps):
        if rva in visited:
            break
        visited.add(rva)
        section = next((s for s in pe.sections if s.contains_rva(rva)), None)
        if section is None:
            break
        index = pe.sections.index(section)
        blob = recovered.get(index, section.get_data())
        offset = rva - section.VirtualAddress
        if not 0 <= offset < len(blob):
            break
        instruction = next(engine.disasm(blob[offset:offset + 15], base + rva), None)
        if instruction is None:
            break
        row = {"rva": hex(rva), "va": hex(base + rva), "bytes": instruction.bytes.hex(),
               "mnemonic": instruction.mnemonic, "operands": instruction.op_str,
               "section_index": index}
        result.append(row)
        if instruction.mnemonic == "jmp":
            if instruction.operands and instruction.operands[0].type == capstone.x86.X86_OP_IMM:
                rva = instruction.operands[0].imm - base
                continue
            break
        if instruction.mnemonic.startswith("ret") or instruction.mnemonic == "int3":
            break
        # 入口的 import 间接 call 之后可能是填充；它是静态边界，不能继续推断返回行为。
        if instruction.mnemonic == "call" and instruction.operands[0].type != capstone.x86.X86_OP_IMM:
            break
        rva += instruction.size
    return result


def anchor_references(pe: pefile.PE, recovered: dict[int, bytes]) -> list[dict[str, Any]]:
    """在恢复代码中重新查找关键字符串绝对地址；只标为候选引用。"""
    needles = [b'getchallenge 0 "%s"', b"connectResponse", b"https://%s:%d/auth/",
               b"dwLogonStart", b"DW_ACCOUNT_LOGIN_COMPLETE", b"DW_LOBBY_CONNECTED", b"dwauth.ini"]
    rows = []
    for needle in needles:
        for section in pe.sections:
            haystack = section.get_data()
            at = haystack.find(needle)
            if at < 0:
                continue
            target = pe.OPTIONAL_HEADER.ImageBase + section.VirtualAddress + at
            references = []
            for index, code in recovered.items():
                for match in re.finditer(re.escape(struct.pack("<I", target)), code):
                    offset = match.start()
                    rva = pe.sections[index].VirtualAddress + offset
                    preceding = code[offset - 1] if offset else None
                    references.append({"operand_rva": hex(rva), "operand_va": hex(pe.OPTIONAL_HEADER.ImageBase + rva),
                                       "preceding_byte": hex(preceding) if preceding is not None else None,
                                       "push_imm32_candidate": preceding == 0x68,
                                       "context_rva": hex(max(pe.sections[index].VirtualAddress, rva - 16)),
                                       "context_hex": code[max(0, offset - 16):offset + 24].hex()})
            rows.append({"anchor": needle.decode(), "rva": hex(target - pe.OPTIONAL_HEADER.ImageBase),
                         "preferred_va": hex(target), "reference_kind": "absolute_pointer_bytes_in_recovered_section",
                         "references": references})
    return rows


def build_static_candidate(data: bytes, recovered: dict[int, bytes]) -> tuple[bytes, dict[str, Any]]:
    """把已解压节重新排入独立 PE 数据文件；入口、保护依赖和未解导入保留。

    清除证书及调试目录，因为重新布局后旧文件偏移失效。该候选是供静态
    反汇编器读取的容器；导入表/OEP/TVM 尚未重建，不能当作可加载版本。
    """
    pe = pefile.PE(data=data)
    bodies = [recovered.get(index, section.get_data()) for index, section in enumerate(pe.sections)]
    cursor = pe.OPTIONAL_HEADER.SizeOfHeaders
    for section, body in zip(pe.sections, bodies):
        section.PointerToRawData = cursor
        section.SizeOfRawData = align(len(body), pe.OPTIONAL_HEADER.FileAlignment)
        cursor += section.SizeOfRawData
    for index in [4, 6]:
        pe.OPTIONAL_HEADER.DATA_DIRECTORY[index].VirtualAddress = 0
        pe.OPTIONAL_HEADER.DATA_DIRECTORY[index].Size = 0
    pe.OPTIONAL_HEADER.CheckSum = 0
    out = bytearray(pe.write()[:pe.OPTIONAL_HEADER.SizeOfHeaders])
    for section, body in zip(pe.sections, bodies):
        if len(out) != section.PointerToRawData:
            raise ValueError("静态候选的节文件偏移不连续")
        out.extend(body)
        out.extend(bytes(section.SizeOfRawData - len(body)))
    check = pefile.PE(data=out)
    for index, content in recovered.items():
        if check.sections[index].get_data()[:len(content)] != content:
            raise ValueError("静态候选回读与恢复段不符")
    return bytes(out), {"sha256": digest(out), "bytes": len(out), "pe_parse_passed": True,
                        "restored_sections_readback_matches": True,
                        "entry_rva_unchanged": hex(check.OPTIONAL_HEADER.AddressOfEntryPoint),
                        "launch_ready": False, "runtime_tested": False,
                        "unresolved": ["original import names/table", "original CRT/OEP selection",
                                       "remaining TVM initialization", "unknown block trailer semantics"],
                        "removed_directories": ["SECURITY", "DEBUG"]}


def inspect_bootstrap(client: Path, output: Path, manifest_path: Path) -> dict[str, Any]:
    """在独立新目录生成所有恢复数据与证据，输入必须符合固定版本清单。"""
    root, destination = client.resolve(), output.resolve()
    if destination == root or destination.is_relative_to(root) or root.is_relative_to(destination):
        raise BaselineError("启动研究输出必须在客户端目录之外，且不能互为父目录")
    if destination.exists():
        raise BaselineError("启动研究输出目录已存在，拒绝覆盖")
    manifest = load_manifest(manifest_path)
    names = ["codoMP_client_shipRetail.exe", "codoMP_client_shipRetailBase.dll"]
    # 先检查必需条目再读文件；合法但不完整的清单也要给出受控诊断。
    entries = {row["path"]: row for row in manifest["files"]}
    missing = [name for name in names if name not in entries]
    if missing:
        raise BaselineError("启动研究版本清单缺少必需文件：" + ", ".join(missing))
    inputs = {}
    for name in names:
        entry = entries[name]
        blob = source_path(root, name).read_bytes()
        if len(blob) != entry["bytes"] or digest(blob) != entry["sha256"]:
            raise BaselineError(f"启动研究输入版本不符：{name}")
        inputs[name] = blob
    destination.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = {"schema_version": 1, "build": "1649370", "source": str(root),
                             "status": "static_recovery_only", "launch_ready": False, "game_started": False,
                             "files": [], "outputs": [], "references": [
                                 "https://github.com/lz4/lz4/blob/dev/doc/lz4_Block_format.md",
                                 "D:/Games/Launcher source code/diagnostics/extract_aplib_sections.py",
                                 "D:/Games/Launcher source code/diagnostics/build_recovered_image.py"]}
    for name, blob in inputs.items():
        pe, detail = describe_pe(blob)
        detail.update(file=name, source_sha256=digest(blob), source_bytes=len(blob))
        ap32_rows, ap32_data = scan_ap32(blob)
        detail["ap32"] = ap32_rows
        for offset, content in ap32_data.items():
            filename = f"{name}.ap32-{offset:x}.bin"
            (destination / filename).write_bytes(content)
            report["outputs"].append({"path": filename, "sha256": digest(content), "bytes": len(content),
                                      "kind": "crc_verified_AP32_data"})
        recovered = {}
        if name.endswith(".exe"):
            detail["lz4_sections"] = []
            for index in [0, 5]:
                section = pe.sections[index]
                content, evidence = recover_lz4_section(section.get_data(), align(section.Misc_VirtualSize, 0x1000))
                recovered[index] = content
                filename = f"section{index:02d}-rva-{section.VirtualAddress:x}.bin"
                (destination / filename).write_bytes(content)
                evidence.update(section_index=index, rva=hex(section.VirtualAddress), artifact=filename)
                detail["lz4_sections"].append(evidence)
                report["outputs"].append({"path": filename, "sha256": digest(content), "bytes": len(content), "kind": "LZ4_recovered_section"})
            detail["recovered_anchor_references"] = anchor_references(pe, recovered)
            candidate, evidence = build_static_candidate(blob, recovered)
            filename = "codol1649370-static-analysis-candidate.bin"
            (destination / filename).write_bytes(candidate)
            evidence["artifact"] = filename
            detail["static_candidate"] = evidence
            report["outputs"].append({"path": filename, "sha256": digest(candidate), "bytes": len(candidate), "kind": "static_analysis_PE_not_launch_ready"})
            # 压缩元数据独立留证；真实导入只通过双哈希和 IAT 分组校验恢复。
            meta_rows, meta_data = recover_lz4_metadata(blob, pe.get_offset_from_rva(pe.OPTIONAL_HEADER.DATA_DIRECTORY[1].VirtualAddress))
            detail["lz4_metadata"] = meta_rows
            for index, content in enumerate(meta_data):
                filename = f"lz4-metadata-{index}.bin"
                (destination / filename).write_bytes(content)
                meta_rows[index]["artifact"] = filename
                report["outputs"].append({"path": filename, "sha256": digest(content), "bytes": len(content), "kind": "LZ4_metadata_candidate"})
            imports = resolve_imports(pe, meta_data[0], meta_data[1], root)
            detail["recovered_imports"] = imports
            if imports["complete"]:
                imported_candidate, import_evidence = add_analysis_imports(candidate, imports)
                filename = "codol1649370-imports-analysis-candidate.bin"
                (destination / filename).write_bytes(imported_candidate)
                import_evidence["artifact"] = filename
                detail["imports_static_candidate"] = import_evidence
                report["outputs"].append({"path": filename, "sha256": digest(imported_candidate), "bytes": len(imported_candidate), "kind": "imports_analysis_PE_not_launch_ready"})
            detail["crt_candidate"] = {
                "cookie_initializer_va": "0xc527bb", "cookie_constant": "0xbb40e64e",
                "cookie_global_va": "0xe3f240", "entry_candidate_va": "0xc427db",
                "entry_candidate_rva": "0x8427db", "crt_startup_va": "0xc4266e",
                "basis": "唯一直接调用 cookie 初始化的 call，后接跳向 CRT 初始化的 jmp；静态候选，未观测执行。",
                "entry_candidate_path": disassemble_path(pe, 0x8427DB, recovered),
                "cookie_initializer_path": disassemble_path(pe, 0x8527BB, recovered),
                "selected_as_PE_entry": False, "runtime_verified": False}
        detail["entry_static_path"] = disassemble_path(pe, pe.OPTIONAL_HEADER.AddressOfEntryPoint, recovered)
        if name.endswith(".dll"):
            detail["export_static_paths"] = [{"ordinal": item.ordinal, "rva": hex(item.address),
                                             "instructions": disassemble_path(pe, item.address, {})}
                                            for item in pe.DIRECTORY_ENTRY_EXPORT.symbols]
        report["files"].append(detail)
    report["old_aplib_recipe_applicable"] = False
    report["conclusions"] = [
        "主 EXE 不含 AP32；新版代码节可按 LZ4 独立块精确解压。",
        "Base DLL 的 AP32 命中需逐项校验，已解压内容是非 PE 二进制元数据候选。",
        "原 EXE 入口调用 Base DLL ordinal 1，Base DLL 自身入口及导出指向 .tvm0。",
        "恢复代码段和静态 PE 不证明可加载、进入大厅或联机；没有执行游戏代码。",
        "LZ4 的每块未知尾字段未通过完整性校验，不等于旧 AP32 CRC 验证级别。"]
    executable = report["files"][0]
    report["conclusions"].append(
        f"导入元数据恢复 {executable['recovered_imports']['record_count']} 条；完整分组/双哈希/CRT交叉校验：{executable['recovered_imports']['complete']}。")
    report["originals_unchanged"] = all(digest(source_path(root, name).read_bytes()) == digest(blob)
                                        for name, blob in inputs.items())
    save_new_json(destination / "bootstrap-static.json", report)
    return report


def main(argv: list[str] | None = None) -> int:
    """独立命令行入口；输出使用新目录，避免覆盖任何已有实验。"""
    parser = argparse.ArgumentParser(description=__doc__)
    project = REBUILD_ROOT_PATH
    parser.add_argument("--client", type=Path, default=project.parent / "Original Client")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=project / "manifests/build1649370.json")
    args = parser.parse_args(argv)
    output_existed = args.output_dir.exists()
    try:
        report = inspect_bootstrap(args.client, args.output_dir, args.manifest)
    except (OSError, ValueError, pefile.PEFormatError) as error:
        if not output_existed and args.output_dir.is_dir() and not (args.output_dir / "bootstrap-failure.json").exists():
            save_new_json(args.output_dir / "bootstrap-failure.json", {"status": "bootstrap_failed", "error": str(error), "launch_ready": False})
        print(json.dumps({"status": "bootstrap_failed", "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps({"status": report["status"], "report": str(args.output_dir / "bootstrap-static.json"),
                      "outputs": report["outputs"], "originals_unchanged": report["originals_unchanged"]},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
