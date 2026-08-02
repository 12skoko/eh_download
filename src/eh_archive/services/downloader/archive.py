from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin

from ...domain.errors import ArchiveError, ErrorClass
from ...domain.states import DownloadMethod


def _cost(text: str, *, free_word: str) -> int:
    text = text.strip()
    if text == free_word:
        return 0
    match = re.search(r"[\d,]+", text)
    if not match:
        raise ArchiveError("invalid_cost", f"cannot parse cost: {text}", ErrorClass.ITEM)
    return int(match.group().replace(",", ""))


def determine_download_method(html: str) -> DownloadMethod:
    """Keep the old direct/H@H cost rule behind a testable pure function."""
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise RuntimeError("beautifulsoup4 is required") from exc
    soup = BeautifulSoup(html, "lxml")
    direct_node = soup.select_one("div[style*='width:180px'] strong")
    original = next(
        (
            td
            for td in soup.find_all("td")
            if td.find("p") and td.find("p").get_text(strip=True) == "Original"
        ),
        None,
    )
    hah_node = original.find_all("p")[2] if original and len(original.find_all("p")) > 2 else None
    if direct_node is None or hah_node is None:
        raise ArchiveError(
            "download_options_missing",
            "archive page lacks Original download options",
            ErrorClass.ITEM,
        )
    direct_cost = _cost(direct_node.get_text(), free_word="Free!")
    hah_cost = _cost(hah_node.get_text(), free_word="Free")
    return (
        DownloadMethod.HAH
        if direct_cost == 0 or direct_cost < hah_cost or hah_cost > 8000 or hah_cost < 400
        else DownloadMethod.DIRECT
    )


def extract_direct_download_url(html: str, *, base_url: str) -> str:
    """Extract the one-shot direct URL returned by EH's archive POST.

    The archive page itself is an options page. EH returns the actual archive
    URL in ``#continue a`` only after a ``dltype=org`` POST, and the old
    downloader added ``start=1`` before streaming it. Keep that exchange
    separate from the byte downloader so temporary URLs never reach storage.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise RuntimeError("beautifulsoup4 is required") from exc
    soup = BeautifulSoup(html, "lxml")
    node = soup.select_one("#continue a[href]")
    if node is None:
        raise ArchiveError(
            "direct_link_missing",
            "archive response has no direct download link",
            ErrorClass.ITEM,
        )
    href = str(node.get("href", "")).strip()
    if not href:
        raise ArchiveError(
            "direct_link_missing",
            "archive response has an empty direct download link",
            ErrorClass.ITEM,
        )
    link = urljoin(base_url, href)
    if re.search(r"(?:[?&])start=", link):
        return link
    return f"{link}{'&' if '?' in link else '?'}start=1"


def request_direct_download_url(
    client: Any,
    archive_url: str,
    *,
    role: str = "archive",
    timeout: float = 30.0,
    headers: dict[str, str] | None = None,
) -> str:
    """Perform EH's archive POST and return its non-persisted direct URL."""
    response = client.post(
        archive_url.replace("--", "-"),
        role=role,
        data={"dltype": "org", "dlcheck": "Download Original Archive"},
        headers=headers or {},
        timeout=timeout,
    )
    response.raise_for_status()
    if "This gallery is unavailable due to a copyright claim" in response.text:
        raise ArchiveError("gallery_unavailable", "gallery is unavailable", ErrorClass.ITEM)
    return extract_direct_download_url(response.text, base_url=archive_url)
