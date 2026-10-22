"""固定版本输入与实验副本管理；只读取原客户端，不启动任何游戏代码。

所有复制均写入新建目录，禁止硬链接和覆盖已有实验。这里的“准备完成”
仅表示输入文件符合已记录版本，不表示游戏能启动或协议已经兼容。
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any


class BaselineError(ValueError):
    """输入版本、路径边界或实验输出不满足要求。"""


def sha256_file(path: Path) -> str:
    """分块计算文件指纹，避免一次读入大型资源。"""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(path: Path) -> dict[str, Any]:
    """读取版本清单并拒绝重复路径、目录穿越和不完整的文件指纹。"""
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise BaselineError("版本清单的顶层必须是对象")
    if manifest.get("schema_version") != 1 or manifest.get("build") != "1649370":
        raise BaselineError("不支持的版本清单；此工具仅面向 build 1649370")
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise BaselineError("版本清单没有输入文件")
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise BaselineError("版本清单中的文件条目必须是对象")
        name = entry.get("path", "")
        if not isinstance(name, str) or not name:
            raise BaselineError("文件路径必须是非空字符串")
        parts = PurePosixPath(name)
        if (parts.is_absolute() or PureWindowsPath(name).drive or "\\" in name
                or ".." in parts.parts or not parts.parts or name != parts.as_posix()
                or any(part.endswith((" ", ".")) or re.search(r'[:<>"|?*\x00-\x1f]', part)
                       or re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part, re.I)
                       for part in parts.parts)):
            raise BaselineError(f"版本清单含不安全路径：{name}")
        if name.casefold() in seen:
            raise BaselineError(f"重复的输入文件：{name}")
        seen.add(name.casefold())
        if not isinstance(entry.get("bytes"), int) or entry["bytes"] <= 0:
            raise BaselineError(f"无效文件长度：{name}")
        if not re.fullmatch(r"[0-9a-f]{64}", entry.get("sha256", "")):
            raise BaselineError(f"无效 SHA-256：{name}")
    return manifest


def source_path(root: Path, name: str) -> Path:
    """解析输入文件的真实路径，拒绝借助符号链接离开客户端目录。"""
    resolved_root = root.resolve()
    result = (resolved_root / name).resolve()
    if not result.is_relative_to(resolved_root):
        raise BaselineError(f"输入文件位于客户端目录之外：{name}")
    return result


def verify_inputs(client: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """逐文件比对长度和 SHA-256；缺失或不匹配都给出明确诊断。"""
    root = client.resolve()
    records = []
    for entry in manifest["files"]:
        record = {"path": entry["path"], "expected_sha256": entry["sha256"],
                  "expected_bytes": entry["bytes"]}
        try:
            path = source_path(root, entry["path"])
            if not path.is_file():
                record["status"] = "missing"
            else:
                stat = path.stat()
                actual_hash = sha256_file(path)
                record.update(bytes=stat.st_size, sha256=actual_hash,
                              hardlink_count=stat.st_nlink,
                              status="matched" if stat.st_size == entry["bytes"]
                              and actual_hash == entry["sha256"] else "mismatch")
        except (OSError, BaselineError) as error:
            record.update(status="error", error=str(error))
        records.append(record)
    return {"schema_version": 1, "build": manifest["build"], "client": str(root),
            "scope": "static input fingerprint verification; no game execution",
            "ready": all(row["status"] == "matched" for row in records),
            "files": records}


def save_new_json(path: Path, payload: dict[str, Any]) -> None:
    """只创建新证据文件，避免静默覆盖历史成功或失败记录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def stage_inputs(client: Path, destination: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """建立真正独立的分析副本；失败时保留现场和 failure.json 便于诊断。

    这里只复制清单中的关键模块，刻意不宣称它是可运行的完整安装。
    完整检查在创建输出前执行；复制后再次核对哈希并检查硬链接数。
    """
    root, output = client.resolve(), destination.resolve()
    if output == root or output.is_relative_to(root) or root.is_relative_to(output):
        raise BaselineError("实验目录必须与原客户端目录分离，且不能互为父目录")
    if output.exists():
        raise BaselineError(f"实验目录已存在，拒绝覆盖：{output}")
    verification = verify_inputs(root, manifest)
    if not verification["ready"]:
        raise BaselineError("输入版本校验失败；请先运行 check 查看逐文件结果")
    output.mkdir(parents=True, exist_ok=False)
    copied = []
    try:
        for entry in manifest["files"]:
            source = source_path(root, entry["path"])
            target = output / entry["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            # copyfile 创建新文件，绝不使用旧工程的硬链接优化。
            with source.open("rb") as reader, target.open("xb") as writer:
                shutil.copyfileobj(reader, writer, 1024 * 1024)
            actual = sha256_file(target)
            if target.samefile(source) or target.stat().st_nlink != 1:
                raise BaselineError(f"分析副本不是独立文件：{entry['path']}")
            if target.stat().st_size != entry["bytes"] or actual != entry["sha256"]:
                raise BaselineError(f"复制后指纹不匹配：{entry['path']}")
            copied.append({"path": entry["path"], "sha256": actual,
                           "bytes": target.stat().st_size, "hardlink_count": 1})
        result = {"schema_version": 1, "build": manifest["build"],
                  "status": "analysis_inputs_staged", "source": str(root),
                  "destination": str(output), "launch_ready": False,
                  "note": "Only selected analysis inputs; no game code executed.",
                  "files": copied}
        save_new_json(output / "stage.json", result)
        return result
    except (OSError, BaselineError) as error:
        save_new_json(output / "failure.json", {
            "status": "stage_failed", "error": str(error), "copied": copied,
            "source": str(root), "destination": str(output)})
        raise
