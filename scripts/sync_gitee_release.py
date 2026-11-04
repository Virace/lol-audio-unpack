"""将已发布的 GitHub Release 同步到仅分发成品的 Gitee 仓库。"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any, BinaryIO
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

API_ROOT = "https://gitee.com/api/v5"
PAGE_SIZE = 100
CHUNK_SIZE = 1024 * 1024
PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATUS_START = "<!-- release-status:start -->"
STATUS_END = "<!-- release-status:end -->"
DISPLAY_ZONE = timezone(timedelta(hours=8))


def _run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """捕获外部工具输出，避免失败时将请求凭证或响应正文带入日志。"""
    kwargs.setdefault("timeout", 600)
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False, **kwargs)
    if result.returncode:
        raise RuntimeError(f"{command[0]} 执行失败，退出码 {result.returncode}；未输出原始诊断以保护凭证")
    return result


def _request(
    path: str,
    token: str,
    *,
    method: str = "GET",
    data: dict[str, str] | None = None,
    missing_ok: bool = False,
) -> Any:
    """请求 Gitee 元数据，仅将明确的 404 视为资源不存在。"""
    request = Request(
        f"{API_ROOT}/{path}",
        data=urlencode(data).encode("utf-8") if data is not None else None,
        headers={"Authorization": f"Bearer {token}", "User-Agent": "lol-audio-unpack-release-sync"},
        method=method,
    )
    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        if missing_ok and error.code == HTTPStatus.NOT_FOUND:
            return None
        raise RuntimeError(f"Gitee {method} {path} 返回 HTTP {error.code}") from None
    except URLError:
        raise RuntimeError(f"Gitee {method} {path} 网络请求失败") from None


def _list_assets(repo_path: str, release_id: int, token: str) -> dict[str, dict[str, Any]]:
    """分页读取附件，防止重跑时重复上传已有文件。"""
    assets = {}
    page = 1
    while True:
        batch = _request(f"{repo_path}/releases/{release_id}/attach_files?page={page}&per_page={PAGE_SIZE}", token)
        for asset in batch:
            name = asset["name"]
            if name in assets:
                raise RuntimeError(f"Gitee 存在重名附件 {name}，请人工处理后重试")
            assets[name] = asset
        if len(batch) < PAGE_SIZE:
            return assets
        page += 1


def _hash_stream(stream: BinaryIO) -> str:
    """分块计算摘要，避免将大型 EXE 载入内存。"""
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _remote_hash(url: str) -> str:
    """下载公开附件核对摘要；下载及重定向均不携带 Token。"""
    if urlsplit(url).scheme != "https":
        raise RuntimeError("Gitee 附件下载地址必须使用 HTTPS")
    try:
        with urlopen(url, timeout=60) as response:
            return _hash_stream(response)
    except (HTTPError, URLError):
        raise RuntimeError("Gitee 已有附件下载失败，无法确认是否与 GitHub 一致") from None


def _upload(repo_path: str, release_id: int, path: Path, token: str) -> None:
    """通过 curl 流式上传，Token 仅经标准输入传递，不进入参数或文件。"""
    result = _run(
        [
            "curl",
            "--disable",
            "--config",
            "-",
            "--silent",
            "--show-error",
            "--fail-with-body",
            "--connect-timeout",
            "30",
            "--max-time",
            "1200",
            "--form",
            f'file=@"{path.as_posix()}"',
            f"{API_ROOT}/{repo_path}/releases/{release_id}/attach_files",
        ],
        input=f"header = {json.dumps(f'Authorization: Bearer {token}')}\n",
        timeout=1220,
    )
    uploaded = json.loads(result.stdout)
    if uploaded.get("name") != path.name:
        raise RuntimeError(f"Gitee 未返回预期附件 {path.name}，请检查远端状态后重试")


def _write_status(repo_path: str, release: dict[str, Any], token: str, status: str, count: int) -> None:
    """只更新 README 的版本区域，保留正文；补传旧版本时不回退首页版本。"""
    path = f"{repo_path}/contents/README.md"
    current = _request(path, token, missing_ok=True)
    text = base64.b64decode(current["content"]).decode("utf-8") if current else ""
    published = datetime.fromisoformat(release["publishedAt"].replace("Z", "+00:00"))
    previous = re.search(r"<!-- github-published-at: (.+?) -->", text)
    if previous and datetime.fromisoformat(previous[1].replace("Z", "+00:00")) > published:
        print("已保留 README 中较新版本的信息", flush=True)
        return
    # 旧版 README 没有状态区域时使用模板；此后只替换标记之间的内容。
    if STATUS_START not in text or STATUS_END not in text:
        text = (PROJECT_ROOT / ".github" / "gitee" / "README.md").read_text(encoding="utf-8")
    date = published.astimezone(DISPLAY_ZONE).strftime("%Y-%m-%d %H:%M:%S")
    updated = datetime.now(DISPLAY_ZONE).strftime("%Y-%m-%d %H:%M:%S")
    channel = "（预发布）" if release["isPrerelease"] else ""
    total = len(release["assets"])
    repo = repo_path.removeprefix("repos/")
    tag = release["tagName"]
    block = (
        f"{STATUS_START}\n<!-- github-published-at: {release['publishedAt']} -->\n\n"
        "| 版本信息 | 当前状态 |\n| --- | --- |\n"
        f"| 当前版本 | [{tag}](https://gitee.com/{repo}/releases/tag/{quote(tag, safe='')}){channel} |\n"
        f"| GitHub 发布时间 | [{date}]({release['url']})（北京时间） |\n"
        f"| 同步状态 | {status}，已核对 {count}/{total} 个附件 |\n"
        f"| 最近同步 | {updated}（北京时间） |\n\n{STATUS_END}"
    )
    start = text.index(STATUS_START)
    end = text.index(STATUS_END, start) + len(STATUS_END)
    data = {
        "content": base64.b64encode((text[:start] + block + text[end:]).encode("utf-8")).decode("ascii"),
        "message": f"docs: 更新 {tag} 同步状态",
    }
    if current:
        data["sha"] = current["sha"]
    _request(path, token, method="PUT" if current else "POST", data=data)


def _sync(args: argparse.Namespace, token: str) -> str:
    """读取真实发布结果，准备并验证附件，再创建或补全对应 Gitee Release。"""
    release = json.loads(
        _run(
            [
                "gh",
                "release",
                "view",
                args.tag,
                "--repo",
                args.github_repo,
                "--json",
                "tagName,name,body,isDraft,isPrerelease,url,assets,publishedAt",
            ]
        ).stdout
    )
    if release["isDraft"] or release["tagName"] != args.tag or not release["assets"]:
        raise RuntimeError("GitHub Release 尚未公开、标签不一致或没有附件，停止同步")
    for asset in release["assets"]:
        name = asset["name"]
        if not name or name in {".", ".."} or any(char in name for char in '/\\"\r\n') or asset["state"] != "uploaded":
            raise RuntimeError("GitHub Release 含无效附件名称或尚未上传完成的附件")

    repo_path = "repos/" + "/".join(quote(part, safe="") for part in args.gitee_repo.split("/"))
    repo = _request(repo_path, token)
    branch = repo.get("default_branch")
    if not branch:
        raise RuntimeError("Gitee 仓库未初始化，请先创建 README 和默认分支")
    mirrored = _request(f"{repo_path}/releases/tags/{quote(args.tag, safe='')}", token, missing_ok=True)
    existing = _list_assets(repo_path, mirrored["id"], token) if mirrored else {}
    if args.dry_run:
        print(f"只读预检通过：{args.tag}，GitHub {len(release['assets'])} 个附件，Gitee 已有 {len(existing)} 个")
        return "只读预检通过；未下载附件，也未修改 Gitee。"

    output = PROJECT_ROOT / ".temp" / ".gitee-sync" / uuid4().hex
    output.mkdir(parents=True)
    print(f"下载 GitHub Release 附件至 {output}", flush=True)
    _run(["gh", "release", "download", args.tag, "--repo", args.github_repo, "--dir", str(output)])
    hashes = {}
    for asset in release["assets"]:
        path = output / asset["name"]
        if not path.is_file() or path.stat().st_size != asset["size"]:
            raise RuntimeError(f"GitHub 附件下载不完整：{path.name}")
        with path.open("rb") as stream:
            hashes[path.name] = _hash_stream(stream)
        digest = asset.get("digest")
        if digest and digest != f"sha256:{hashes[path.name]}":
            raise RuntimeError(f"GitHub 附件摘要不一致：{path.name}")
        if path.name in existing:
            if _remote_hash(existing[path.name]["browser_download_url"]) != hashes[path.name]:
                raise RuntimeError(f"Gitee 同名附件内容不同：{path.name}；保留现有文件，请人工处理")

    data = {
        "tag_name": args.tag,
        "name": release["name"] or args.tag,
        "body": f"本版本与 [GitHub Release]({release['url']}) 的程序和附件一致。\n\n" + (release["body"] or ""),
        "prerelease": str(release["isPrerelease"]).lower(),
        # Gitee 标签只指向分发仓库的 README，绝不使用 GitHub 的源码提交。
        "target_commitish": branch,
    }
    if mirrored:
        mirrored = _request(f"{repo_path}/releases/{mirrored['id']}", token, method="PATCH", data=data)
    else:
        mirrored = _request(f"{repo_path}/releases", token, method="POST", data=data)
    count = len(hashes.keys() & existing.keys())
    _write_status(repo_path, release, token, "正在同步", count)
    try:
        for asset in release["assets"]:
            name = asset["name"]
            if name in existing:
                print(f"已验证，跳过已有附件：{name}", flush=True)
                continue
            print(f"上传附件：{name}", flush=True)
            _upload(repo_path, mirrored["id"], output / name, token)
            uploaded = _list_assets(repo_path, mirrored["id"], token)
            if name not in uploaded or _remote_hash(uploaded[name]["browser_download_url"]) != hashes[name]:
                raise RuntimeError(f"Gitee 附件下载或摘要核对失败：{name}")
            count += 1
        uploaded = _list_assets(repo_path, mirrored["id"], token)
        missing = hashes.keys() - uploaded.keys()
        if missing:
            raise RuntimeError(f"Gitee 缺少附件：{', '.join(sorted(missing))}")
        _write_status(repo_path, release, token, "已同步", count)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
        try:
            _write_status(repo_path, release, token, "同步未完成", count)
        except (OSError, ValueError, RuntimeError):
            print("::warning::未能更新 README 中的同步状态，请查看 Actions 同步日志", flush=True)
        raise
    return f"已同步 {args.tag} 的 {len(hashes)} 个附件到 https://gitee.com/{args.gitee_repo}/releases。"


def main() -> int:
    """解析同步参数，输出成功或失败摘要。

    Returns:
        同步或只读预检成功返回 0，失败返回 1；由 workflow 隔离失败。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="已公开发布的 GitHub 标签")
    parser.add_argument("--github-repo", default=os.environ.get("GITHUB_REPOSITORY", "Virace/lol-audio-unpack"))
    parser.add_argument("--gitee-repo", default="x-item/lol-audio-unpack")
    parser.add_argument("--token-file", type=Path, help="本地使用的 Token 文件；Actions 使用 GITEE_TOKEN")
    parser.add_argument("--dry-run", action="store_true", help="只读检查版本、仓库与附件清单，不下载或写入")
    args = parser.parse_args()
    token = ""
    try:
        token = (
            args.token_file.read_text(encoding="utf-8-sig") if args.token_file else os.environ["GITEE_TOKEN"]
        ).strip()
        if not token or any(char in token for char in '\r\n"\\'):
            raise RuntimeError("GITEE_TOKEN 缺失或格式无效")
        print(f"开始 Gitee Release 同步：{args.tag}", flush=True)
        summary = _sync(args, token)
        code = 0
    except (OSError, KeyError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        # 禁止输出响应正文、子进程 stderr 或 traceback，避免远端回显凭证。
        detail = str(error).replace(token, "[REDACTED]") if token else str(error)
        summary = f"Gitee 同步失败，GitHub Release 不受影响：{detail}"
        print(f"::warning::{summary}", flush=True)
        code = 1
    print(summary, flush=True)
    if summary_path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary_path).open("a", encoding="utf-8") as stream:
            stream.write(summary + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
