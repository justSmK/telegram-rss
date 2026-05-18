#!/usr/bin/env python3
"""Deduplicate Telegram RSS-Bridge entry titles from entry content."""

from __future__ import annotations

import argparse
import html
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path


ATOM_NS = "http://www.w3.org/2005/Atom"
ET.register_namespace("", ATOM_NS)

URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.IGNORECASE)
MEDIA_RE = re.compile(r"<(?:img|video|audio|iframe)\b", re.IGNORECASE)
MESSAGE_DIV_RE = re.compile(
    r"(?P<open><div\b(?=[^>]*\btgme_widget_message_text\b)[^>]*>)"
    r"(?P<body>.*?)"
    r"(?P<close></div>)",
    re.IGNORECASE | re.DOTALL,
)
FIRST_PARAGRAPH_RE = re.compile(
    r"(?P<head>.*?)(?:\s*<br\s*/?>\s*){2,}(?P<rest>.*)\Z",
    re.IGNORECASE | re.DOTALL,
)
BLOCKQUOTE_RE = re.compile(
    r"(?P<open><blockquote\b[^>]*>)(?P<body>.*?)(?P<close></blockquote>)",
    re.IGNORECASE | re.DOTALL,
)
PREVIEW_TITLE_RE = re.compile(
    r"(?P<prefix>(?:\s*<a\b[^>]*>\s*(?:<img\b[^>]*>\s*)?</a>\s*<br\s*/?>\s*)?)"
    r"(?P<link><a\b[^>]*>(?P<title>.*?)</a>)"
    r"(?P<after>\s*<br\s*/?>\s*(?P<rest>.*))\Z",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class Preview:
    title: str
    has_rest: bool


@dataclass(frozen=True)
class SplitResult:
    title: str
    content: str


class VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"br", "blockquote", "div", "li", "p"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"blockquote", "div", "li", "p"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if data:
            self.parts.append(data)


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def normalize_text(value: str) -> str:
    return " ".join(html.unescape(value).split())


def visible_text(fragment: str) -> str:
    parser = VisibleTextParser()
    parser.feed(html.unescape(fragment))
    return normalize_text(" ".join(parser.parts))


def title_text(fragment: str) -> str:
    text = visible_text(fragment)
    without_urls = normalize_text(URL_RE.sub("", text))
    return without_urls or text


def is_url_only(text: str) -> bool:
    return bool(text) and not normalize_text(URL_RE.sub("", text))


def is_title_like(text: str) -> bool:
    return bool(text) and len(text) <= 180 and not is_url_only(text)


def comparable_text(text: str) -> str:
    text = normalize_text(URL_RE.sub("", text)).casefold()
    text = re.sub(r"[^\w\s]+", " ", text, flags=re.UNICODE)
    return normalize_text(text)


def texts_overlap(left: str, right: str) -> bool:
    left_cmp = comparable_text(left)
    right_cmp = comparable_text(right)
    if not left_cmp or not right_cmp:
        return False

    if left_cmp == right_cmp:
        return True

    left_words = left_cmp.split()
    right_words = right_cmp.split()
    if min(len(left_cmp), len(right_cmp)) >= 16 and (left_cmp in right_cmp or right_cmp in left_cmp):
        return True

    if len(left_words) < 3 or len(right_words) < 3:
        return False

    shorter, longer = sorted((left_words, right_words), key=len)
    window = len(shorter)
    return any(longer[index : index + window] == shorter for index in range(len(longer) - window + 1))


def child(element: ET.Element, name: str) -> ET.Element | None:
    for candidate in element:
        if local_name(candidate.tag) == name:
            return candidate
    return None


def feed_items(root: ET.Element) -> list[ET.Element]:
    return [element for element in root.iter() if local_name(element.tag) in {"entry", "item"}]


def has_content(fragment: str) -> bool:
    return bool(visible_text(fragment) or MEDIA_RE.search(fragment))


def first_preview(fragment: str) -> Preview | None:
    blockquote = BLOCKQUOTE_RE.search(fragment)
    if not blockquote:
        return None

    preview = PREVIEW_TITLE_RE.match(blockquote.group("body"))
    if not preview:
        return None

    title = title_text(preview.group("title"))
    if not title:
        return None

    return Preview(title=title, has_rest=has_content(preview.group("rest")))


def remove_preview_title(fragment: str, title: str) -> str:
    def replace(match: re.Match[str]) -> str:
        preview = PREVIEW_TITLE_RE.match(match.group("body"))
        if not preview:
            return match.group(0)

        preview_title = title_text(preview.group("title"))
        rest = preview.group("rest")
        if not has_content(rest) or not texts_overlap(title, preview_title):
            return match.group(0)

        return f"{match.group('open')}{preview.group('prefix')}{rest.lstrip()}{match.group('close')}"

    return BLOCKQUOTE_RE.sub(replace, fragment, count=1)


def split_message_heading(fragment: str) -> SplitResult | None:
    message = MESSAGE_DIV_RE.search(fragment)
    if not message:
        return None

    first_paragraph = FIRST_PARAGRAPH_RE.match(message.group("body"))
    if not first_paragraph:
        return None

    title = title_text(first_paragraph.group("head"))
    rest = first_paragraph.group("rest").lstrip()
    if not is_title_like(title) or not has_content(rest):
        return None

    updated = (
        fragment[: message.start()]
        + message.group("open")
        + rest
        + message.group("close")
        + fragment[message.end() :]
    )
    return SplitResult(title=title, content=updated)


def split_link_post(fragment: str) -> SplitResult | None:
    message = MESSAGE_DIV_RE.search(fragment)
    preview = first_preview(fragment)
    if not message or not preview:
        return None

    message_text = visible_text(message.group("body"))
    message_title = title_text(message.group("body"))
    if not message_text:
        return None

    if is_url_only(message_text) or texts_overlap(message_title, preview.title):
        title = preview.title
    elif is_title_like(message_title):
        title = message_title
    else:
        return None

    updated = fragment[: message.start()] + fragment[message.end() :]
    if not has_content(updated):
        return None

    return SplitResult(title=title, content=updated)


def remove_duplicate_message(fragment: str, title: str) -> str:
    message = MESSAGE_DIV_RE.search(fragment)
    if not message:
        return fragment

    message_text = visible_text(message.group("body"))
    if not texts_overlap(title, message_text):
        return fragment

    updated = fragment[: message.start()] + fragment[message.end() :]
    if not has_content(updated):
        return fragment

    return updated


def remove_redundant_content(fragment: str, title: str) -> str:
    if not title:
        return fragment

    without_duplicate_message = remove_duplicate_message(fragment, title)
    if without_duplicate_message != fragment:
        return without_duplicate_message

    content_text = visible_text(fragment)
    if not content_text or MEDIA_RE.search(fragment):
        return fragment

    if comparable_text(content_text) == comparable_text(title):
        return ""

    return fragment


def normalize_entry(entry: ET.Element) -> bool:
    title = child(entry, "title")
    content = child(entry, "content")
    if content is None:
        content = child(entry, "description")
    if title is None or content is None or not content.text:
        return False

    original_content = content.text
    next_title: str | None = None
    next_content = original_content

    split_heading = split_message_heading(next_content)
    if split_heading:
        next_title = split_heading.title
        next_content = split_heading.content
    else:
        split_link = split_link_post(next_content)
        if split_link:
            next_title = split_link.title
            next_content = split_link.content
        else:
            preview = first_preview(next_content)
            if preview and preview.has_rest:
                next_title = preview.title

    if next_title:
        next_content = remove_preview_title(next_content, next_title)
        title.text = next_title
        if "type" in title.attrib:
            title.set("type", "text")

    next_content = remove_redundant_content(next_content, title.text or "")

    if next_content != original_content:
        content.text = next_content

    return bool(next_title or next_content != original_content)


def compact_entry_author(entry: ET.Element, channel: str) -> bool:
    if not channel:
        return False

    author = child(entry, "author")
    if author is None:
        return False

    name = child(author, "name")
    if name is None or not name.text:
        return False

    compact_channel = channel if channel.startswith("@") else f"@{channel}"
    if normalize_text(name.text) == compact_channel:
        return False

    name.text = compact_channel
    return True


def normalize_feed(input_path: Path, output_path: Path, channel: str) -> int:
    tree = ET.parse(input_path)
    root = tree.getroot()
    changed = 0

    for entry in feed_items(root):
        if normalize_entry(entry):
            changed += 1
        if compact_entry_author(entry, channel):
            changed += 1

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(output_path, encoding="utf-8", xml_declaration=True)
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--channel", default="")
    args = parser.parse_args()

    changed_count = normalize_feed(args.input, args.output, args.channel)
    print(f"Normalized {changed_count} field(s) in {args.channel or args.input.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
