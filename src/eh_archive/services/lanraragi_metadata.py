"""Remote metadata operations, independent of workflows, workers and Web UI."""

import hashlib
import json
import re
from dataclasses import fields

from ..domain.models import MangaInfo
from .uploader.lanraragi import build_tags, comparison_tags, metadata_differences, tag_values

ARCHIVE_ID = re.compile(r"[0-9a-fA-F]{40}")
GALLERY = re.compile(r"https?://(?:exhentai|e-hentai)\.org/g/(\d+/[a-zA-Z0-9]+)/?")


def manga_info(value):
    if value is None:
        raise ValueError("缺少本地详细元数据，请先补采详情")
    return MangaInfo(
        **{f.name: getattr(value, f.name) for f in fields(MangaInfo) if hasattr(value, f.name)}
    )


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def source_ids(tags):
    return {
        m.group(1)
        for tag in tag_values(tags)
        if tag.startswith("source:")
        for m in [GALLERY.fullmatch(tag[7:])]
        if m
    }


def remote_snapshot(payload):
    return {
        k: payload.get(k) for k in ("arcid", "id", "size", "filename", "extension", "title", "tags")
    }


class MetadataMaintenance:
    def __init__(self, gateway):
        self.gateway = gateway
        self._archives = None

    def resolve(self, candidate):
        archive_id = candidate.get("archive_id")
        if archive_id:
            if not ARCHIVE_ID.fullmatch(archive_id):
                raise ValueError("保存的 LANraragi ID 格式无效")
            return archive_id
        if self._archives is None:
            self._archives = self.gateway.list_archives()
        matches = {
            str(a.get("arcid") or a.get("id") or "")
            for a in self._archives
            if source_ids(str(a.get("tags") or "")) == {candidate["manga_id"]}
        }
        if len(matches) != 1:
            raise ValueError(f"按完整来源地址找到 {len(matches)} 个档案，无法唯一定位")
        archive_id = matches.pop()
        if not ARCHIVE_ID.fullmatch(archive_id):
            raise ValueError("远端返回无效档案 ID")
        return archive_id

    def read(self, candidate, archive_id):
        status, payload = self.gateway.metadata(archive_id)
        if status != 200 or not payload:
            raise ValueError(f"读取 LANraragi 元数据失败（HTTP {status}）")
        if not candidate.get("filename") or candidate.get("size") is None:
            raise ValueError("本地缺少文件名或大小，无法核实远端文件身份")
        if not self.gateway.metadata_matches_artifact(
            payload, archive_id=archive_id, size=candidate["size"], filename=candidate["filename"]
        ):
            raise ValueError("远端文件 ID、文件名或大小与本地记录不符")
        tags = payload.get("tags")
        if not isinstance(tags, str):
            raise ValueError("远端标签格式无效")  # noqa: TRY004 - reportable remote validation error
        sources = [tag for tag in tag_values(tags) if tag.startswith("source:")]
        if sources and any(source_ids(tag) != {candidate["manga_id"]} for tag in sources):
            raise ValueError("远端来源地址与当前档案不符")
        return payload

    def preview(self, candidate, info):
        archive_id = self.resolve(candidate)
        payload = self.read(candidate, archive_id)
        dates = [t for t in tag_values(payload["tags"]) if t.startswith("date_added:")]
        # Retain server-owned history; absence is retained as absence too.
        tags = [
            t for t in tag_values(build_tags(info, date_added=1)) if not t.startswith("date_added:")
        ]
        expected = {"title": info.name, "tags": ",".join([*tags, *dates])}
        if (
            not info.name
            or not info.link
            or source_ids(expected["tags"]) != {candidate["manga_id"]}
        ):
            raise ValueError("本地标题或来源地址无效")
        wanted, actual = comparison_tags(expected["tags"]), comparison_tags(payload["tags"])
        differences = metadata_differences(payload, expected)
        return {
            **candidate,
            "archive_id": archive_id,
            "expected": expected,
            "actual": remote_snapshot(payload),
            "missing_tags": sorted(wanted[tag] for tag in wanted.keys() - actual.keys()),
            "extra_tags": sorted(actual[tag] for tag in actual.keys() - wanted.keys()),
            "title_changed": "title" in differences,
            "action": "update" if differences else "verify",
        }

    def apply(self, preview, info, *, checkpoint=lambda: None):
        checkpoint()
        current = self.read(preview, preview["archive_id"])
        different = metadata_differences(current, preview["expected"])
        if different and remote_snapshot(current) != preview["actual"]:
            raise ValueError("远端元数据在预览后发生变化，请重新预览")
        if different:
            checkpoint()
            outcome = self.gateway.update_metadata(
                preview["archive_id"], info, metadata=preview["expected"]
            )
            if outcome.kind != "success":
                raise ValueError(f"元数据更新未确认：{outcome.error_code or outcome.kind}")
        # Re-read even when no PUT was necessary. No blind write retries.
        checkpoint()
        final = self.read(preview, preview["archive_id"])
        if metadata_differences(final, preview["expected"]):
            raise ValueError("更新后元数据仍不一致，请重新预览差异")
        return "updated" if different else "verified"
