# SPDX-License-Identifier: AGPL-3.0-or-later
"""验证 461 Zone 中可明确识别的 RawFile 与 NetConstStrings。

布局对照来自旧项目 codol_assets.c / codol_netconst.c（AGPL-3.0-or-later），
并已在新版样本核验。这里不猜测池化指针，也不把目录可读等同地图可加载。
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import struct
import sys
import zlib

from codol_rebuild.resources.fastfile import FastfileError, MAX_FILE_SIZE, validate_zone


class AssetError(ValueError):
    """资产内容不满足已验证布局，或者需要尚未实现的引用重定位。"""

    def __init__(self, reason: str, message: str):
        """保留失败类别，用于区分坏数据与尚未支持的引用。"""
        super().__init__(message)
        self.reason = reason


def _u32(data: bytes, offset: int) -> int:
    """在边界内读取一个小端整数。"""
    if offset < 0 or offset + 4 > len(data):
        raise AssetError("truncated", "资产字段越界")
    return struct.unpack_from("<I", data, offset)[0]


def _string(data: bytes, offset: int) -> tuple[str, int]:
    """读取有 NUL 终止符的 UTF-8 名称，不允许静默截断或替换字节。"""
    end = data.find(b"\0", offset)
    if offset < 0 or end < 0:
        raise AssetError("truncated", "字符串没有终止符")
    try:
        return data[offset:end].decode("utf-8"), end + 1
    except UnicodeDecodeError:
        raise AssetError("encoding", "名称不符合当前支持的 UTF-8 编码") from None


def decode_rawfile(data: bytes) -> tuple[str, bytes]:
    """严格解析内联 RawFile，校验压缩流结束、精确长度和无多余数据。"""
    if len(data) < 16:
        raise AssetError("truncated", "RawFile 头不足 16 字节")
    if _u32(data, 0) != 0xFFFFFFFF:
        raise AssetError("unresolved_reference", "RawFile 名称需要池化引用重定位")
    if _u32(data, 12) != 0xFFFFFFFF:
        raise AssetError("unresolved_reference", "RawFile 数据需要引用重定位")
    compressed, expected = _u32(data, 4), _u32(data, 8)
    if max(compressed, expected) > MAX_FILE_SIZE:
        raise AssetError("size_limit", "RawFile 声明长度超过上限")
    name, start = _string(data, 16)
    body = data[start:]
    if compressed:
        if compressed != len(body):
            raise AssetError("length", "压缩数据长度与头部不符")
        inflater = zlib.decompressobj()
        try:
            decoded = inflater.decompress(body, expected + 1)
        except zlib.error:
            raise AssetError("zlib", "RawFile zlib 流校验失败") from None
        if (len(decoded) != expected or not inflater.eof or inflater.unused_data
                or inflater.unconsumed_tail):
            raise AssetError("zlib", "RawFile 长度、流结束或尾部校验失败")
        return name, decoded
    if len(body) != expected + 1 or body[-1:] != b"\0":
        raise AssetError("length", "未压缩 RawFile 缺少精确长度或终止字节")
    return name, body[:-1]


def decode_netconst(data: bytes) -> dict:
    """解析内联网络常量表；池化名称或数组需另做重定位，不做猜测。"""
    if len(data) < 20:
        raise AssetError("truncated", "NetConstStrings 头不足 20 字节")
    if _u32(data, 0) != 0xFFFFFFFF:
        raise AssetError("unresolved_reference", "常量表名称需要池化引用")
    kind, source, count, pointer = struct.unpack_from("<4I", data, 4)
    name, pos = _string(data, 20)
    if pointer == 0:
        if count:
            raise AssetError("invalid_pointer", "非空常量表的数组指针为零")
        if pos != len(data):
            raise AssetError("length", "空常量表包含额外字节")
        return {"name": name, "kind": kind, "source": source, "values": []}
    if pointer != 0xFFFFFFFF:
        raise AssetError("unresolved_reference", "常量表数组需要池化引用")
    if count > (len(data) - pos) // 4:
        raise AssetError("truncated", "常量表引用数组越界")
    pointers = [_u32(data, pos + 4 * i) for i in range(count)]
    pos += count * 4
    values = []
    for reference in pointers:
        if reference != 0xFFFFFFFF:
            raise AssetError("unresolved_reference", "常量表字符串需要池化引用")
        value, pos = _string(data, pos)
        values.append(value)
    if pos != len(data):
        raise AssetError("length", "常量表未精确消费所有字节")
    return {"name": name, "kind": kind, "source": source, "values": values}


def inspect_assets(zone: bytes) -> tuple[dict, dict[str, bytes]]:
    """检查目录和两类资产，返回证据及待导出的内容，不产生文件写入。"""
    directory = validate_zone(zone)
    records, outputs = [], {}
    unparsed = Counter()
    for entry in directory["asset_entries"]:
        if entry["type"] not in (36, 44):
            unparsed[entry["type"]] += 1
            continue
        data = zone[entry["offset"]:entry["offset"] + entry["size"]]
        record = {key: entry[key] for key in ("index", "type", "offset", "size")}
        try:
            if entry["type"] == 36:
                name, payload = decode_rawfile(data)
                # 输出名称由工具生成，不将资源中的路径当作本机写入路径。
                suffix = Path(name).suffix.lower()
                if suffix not in (".gsc", ".vision", ".sun", ".bin", ".cfg", ".csv"):
                    suffix = ".dat"
                output_name = f"asset-{entry['index']:04d}{suffix}"
                outputs[output_name] = payload
                record.update(name=name, decoded_bytes=len(payload),
                              decoded_sha256=hashlib.sha256(payload).hexdigest(),
                              artifact_name=output_name)
            else:
                record.update(decode_netconst(data))
            record["status"] = "decoded"
        except AssetError as error:
            record.update(status="unresolved" if error.reason == "unresolved_reference" else "invalid",
                          reason=error.reason, detail=str(error))
        records.append(record)
    result = {"zone_sha256": directory["sha256"], "zone_bytes": len(zone),
              "directory_verified": True, "asset_count": directory["asset_count"],
              "script_string_count": directory["string_count"],
              "asset_type_counts": directory["asset_type_counts"],
              "examined": records, "unparsed_type_counts": dict(sorted(unparsed.items())),
              "status_counts": dict(Counter(row["status"] for row in records)),
              "map_load_verified": False,
              "scope": "RawFile and inline NetConstStrings only; no asset relocation or gameplay"}
    return result, outputs


def main(argv: list[str] | None = None) -> int:
    """分析单个 Zone，按需写新报告和独立文件目录，禁止覆盖历史产物。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("zone", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--extract-to", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.report.exists() or (args.extract_to and args.extract_to.exists()):
            raise AssetError("output_exists", "输出已存在，请选择新的报告或目录")
        with args.zone.open("rb") as stream:
            zone = stream.read(MAX_FILE_SIZE + 1)
        if len(zone) > MAX_FILE_SIZE:
            raise AssetError("size_limit", "Zone 超过大小限制")
        result, outputs = inspect_assets(zone)
        result["input"] = str(args.zone.resolve())
        result["extraction_directory"] = str(args.extract_to.resolve()) if args.extract_to else None
        if args.extract_to:
            args.extract_to.mkdir(parents=True, exist_ok=False)
            for name, payload in outputs.items():
                with (args.extract_to / name).open("xb") as stream:
                    stream.write(payload)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.report.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        print(json.dumps({"report": str(args.report), "assets": result["asset_count"],
                          "status_counts": result["status_counts"],
                          "unparsed": sum(result["unparsed_type_counts"].values())},
                         ensure_ascii=False))
        return 2 if result["status_counts"].get("invalid", 0) else 0
    except (OSError, AssetError, FastfileError) as error:
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
