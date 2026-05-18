#!/usr/bin/env python3
"""Remove unwanted Telegram posts from generated RSS/Atom feeds."""

from __future__ import annotations

import argparse
import html
import re
import sys
import tomllib
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlparse


ATOM_NS = "http://www.w3.org/2005/Atom"
ET.register_namespace("", ATOM_NS)

URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.IGNORECASE)


@dataclass(frozen=True)
class QueryValuePattern:
    param: str | None
    param_pattern: re.Pattern[str] | None
    value_pattern: re.Pattern[str]


@dataclass(frozen=True)
class Rule:
    rule_id: str
    text_patterns: tuple[re.Pattern[str], ...]
    link_patterns: tuple[re.Pattern[str], ...]
    link_query_params: tuple[str, ...]
    link_query_param_patterns: tuple[re.Pattern[str], ...]
    link_query_param_value_patterns: tuple[QueryValuePattern, ...]
    link_domains: tuple[str, ...]
    link_domain_patterns: tuple[re.Pattern[str], ...]


class EntryHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.text_parts: list[str] = []
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        for attr_name in ("href", "src"):
            attr_value = attr_map.get(attr_name)
            if attr_value:
                self.links.append(attr_value)

        if tag.lower() in {"br", "div", "li", "p"}:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if data:
            self.text_parts.append(data)


def decode_html(value: str) -> str:
    previous = None
    while previous != value:
        previous = value
        value = html.unescape(value)
    return value


def decode_url(value: str) -> str:
    value = decode_html(value)
    for _ in range(4):
        decoded = unquote(value)
        if decoded == value:
            break
        value = decoded
    return value


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def normalize_text(value: str) -> str:
    return " ".join(value.split())


def compile_regex(pattern: str, *, case_sensitive: bool = False) -> re.Pattern[str]:
    flags = re.DOTALL
    if not case_sensitive:
        flags |= re.IGNORECASE
    return re.compile(pattern, flags)


def as_string_list(value: object, field_name: str, rule_id: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{rule_id}: {field_name} must be a list of strings")
    return tuple(value)


def load_rules(config_path: Path) -> list[Rule]:
    with config_path.open("rb") as config_file:
        config = tomllib.load(config_file)

    raw_rules = config.get("rules")
    if not isinstance(raw_rules, list):
        raise ValueError(f"{config_path}: expected [[rules]] entries")

    rules: list[Rule] = []
    for raw_rule in raw_rules:
        if not isinstance(raw_rule, dict):
            raise ValueError(f"{config_path}: every rule must be a table")
        if raw_rule.get("enabled", True) is False:
            continue

        rule_id = raw_rule.get("id")
        if not isinstance(rule_id, str) or not rule_id:
            raise ValueError(f"{config_path}: every rule needs a non-empty id")

        case_sensitive = bool(raw_rule.get("case_sensitive", False))
        text_patterns = tuple(
            compile_regex(pattern, case_sensitive=case_sensitive)
            for pattern in as_string_list(raw_rule.get("text_patterns"), "text_patterns", rule_id)
        )
        link_patterns = tuple(
            compile_regex(pattern, case_sensitive=case_sensitive)
            for pattern in as_string_list(raw_rule.get("link_patterns"), "link_patterns", rule_id)
        )
        link_query_params = tuple(
            param.lower()
            for param in as_string_list(
                raw_rule.get("link_query_params"),
                "link_query_params",
                rule_id,
            )
        )
        link_query_param_patterns = tuple(
            compile_regex(pattern, case_sensitive=case_sensitive)
            for pattern in as_string_list(
                raw_rule.get("link_query_param_patterns"),
                "link_query_param_patterns",
                rule_id,
            )
        )
        link_domains = tuple(
            domain.lower().lstrip(".")
            for domain in as_string_list(raw_rule.get("link_domains"), "link_domains", rule_id)
        )
        link_domain_patterns = tuple(
            compile_regex(pattern, case_sensitive=case_sensitive)
            for pattern in as_string_list(
                raw_rule.get("link_domain_patterns"),
                "link_domain_patterns",
                rule_id,
            )
        )

        query_value_patterns = []
        raw_query_value_patterns = raw_rule.get("link_query_param_value_patterns", [])
        if not isinstance(raw_query_value_patterns, list):
            raise ValueError(
                f"{rule_id}: link_query_param_value_patterns must be a list of inline tables"
            )
        for index, matcher in enumerate(raw_query_value_patterns):
            if not isinstance(matcher, dict):
                raise ValueError(f"{rule_id}: query value matcher #{index + 1} must be a table")

            param = matcher.get("param")
            param_pattern = matcher.get("param_pattern")
            value_pattern = matcher.get("value_pattern")
            if param is not None and not isinstance(param, str):
                raise ValueError(f"{rule_id}: matcher param must be a string")
            if param_pattern is not None and not isinstance(param_pattern, str):
                raise ValueError(f"{rule_id}: matcher param_pattern must be a string")
            if not isinstance(value_pattern, str):
                raise ValueError(f"{rule_id}: matcher value_pattern must be a string")

            query_value_patterns.append(
                QueryValuePattern(
                    param=param.lower() if param else None,
                    param_pattern=compile_regex(param_pattern, case_sensitive=case_sensitive)
                    if param_pattern
                    else None,
                    value_pattern=compile_regex(value_pattern, case_sensitive=case_sensitive),
                )
            )

        rules.append(
            Rule(
                rule_id=rule_id,
                text_patterns=text_patterns,
                link_patterns=link_patterns,
                link_query_params=link_query_params,
                link_query_param_patterns=link_query_param_patterns,
                link_query_param_value_patterns=tuple(query_value_patterns),
                link_domains=link_domains,
                link_domain_patterns=link_domain_patterns,
            )
        )

    return rules


def entry_strings(entry: ET.Element) -> list[str]:
    strings: list[str] = []
    for element in entry.iter():
        if element.text:
            strings.append(element.text)
        if element.tail:
            strings.append(element.tail)
        for attr_name in ("href", "src", "url"):
            attr_value = element.attrib.get(attr_name)
            if attr_value:
                strings.append(attr_value)
    return strings


def extract_entry_content(entry: ET.Element) -> tuple[str, str, list[str]]:
    parser = EntryHtmlParser()
    raw_text = decode_html(" ".join(entry_strings(entry)))
    links: list[str] = []

    for part in entry_strings(entry):
        decoded = decode_html(part)
        parser.feed(decoded)
        links.extend(URL_RE.findall(decoded))

    links.extend(parser.links)

    for element in entry.iter():
        for attr_name in ("href", "src", "url"):
            attr_value = element.attrib.get(attr_name)
            if attr_value:
                links.append(attr_value)

    visible_text = normalize_text(" ".join(parser.text_parts))
    return raw_text, visible_text, links


def link_query_pairs(url: str) -> list[tuple[str, str]]:
    parsed = urlparse(url)
    return parse_qsl(parsed.query, keep_blank_values=True) + parse_qsl(
        parsed.fragment,
        keep_blank_values=True,
    )


def domain_matches(host: str, domains: tuple[str, ...]) -> bool:
    host = host.lower().rstrip(".")
    for domain in domains:
        if host == domain or host.endswith(f".{domain}"):
            return True
    return False


def query_value_pattern_matches(
    matcher: QueryValuePattern,
    key: str,
    value: str,
) -> bool:
    key_lower = key.lower()
    if matcher.param and key_lower != matcher.param:
        return False
    if matcher.param_pattern and not matcher.param_pattern.search(key):
        return False
    return bool(matcher.value_pattern.search(value))


def rule_matches_link(rule: Rule, link: str) -> bool:
    decoded = decode_url(link)
    parsed = urlparse(decoded)
    host = parsed.netloc.lower()
    query_pairs = link_query_pairs(decoded)

    if any(pattern.search(decoded) for pattern in rule.link_patterns):
        return True
    if rule.link_domains and domain_matches(host, rule.link_domains):
        return True
    if any(pattern.search(host) for pattern in rule.link_domain_patterns):
        return True
    if any(key.lower() in rule.link_query_params for key, _ in query_pairs):
        return True
    if any(pattern.search(key) for pattern in rule.link_query_param_patterns for key, _ in query_pairs):
        return True
    return any(
        query_value_pattern_matches(matcher, key, value)
        for matcher in rule.link_query_param_value_patterns
        for key, value in query_pairs
    )


def entry_title(entry: ET.Element, text: str) -> str:
    for element in entry.iter():
        if local_name(element.tag) == "title" and element.text:
            return normalize_text(decode_html(element.text))[:140]
    return text[:140]


def filter_reasons(entry: ET.Element, rules: list[Rule]) -> list[str]:
    raw_text, visible_text, links = extract_entry_content(entry)
    haystack = f"{raw_text} {visible_text}"
    reasons: list[str] = []

    for rule in rules:
        if any(pattern.search(haystack) for pattern in rule.text_patterns):
            reasons.append(rule.rule_id)
            continue
        if any(rule_matches_link(rule, link) for link in links):
            reasons.append(rule.rule_id)

    return reasons


def entry_id(entry: ET.Element) -> str:
    for element in entry.iter():
        if local_name(element.tag) in {"id", "guid"} and element.text:
            return normalize_text(element.text)
    for element in entry.iter():
        if local_name(element.tag) == "link":
            href = element.attrib.get("href")
            if href:
                return href
            if element.text:
                return normalize_text(element.text)
    return "unknown"


def feed_items(root: ET.Element) -> list[tuple[ET.Element, ET.Element]]:
    items: list[tuple[ET.Element, ET.Element]] = []
    root_name = local_name(root.tag)

    if root_name == "feed":
        items.extend((root, child) for child in list(root) if local_name(child.tag) == "entry")
        return items

    if root_name == "rss":
        for channel in root:
            if local_name(channel.tag) != "channel":
                continue
            items.extend(
                (channel, child) for child in list(channel) if local_name(child.tag) == "item"
            )
        return items

    parent_by_child = {child: parent for parent in root.iter() for child in parent}
    for child, parent in parent_by_child.items():
        if local_name(child.tag) in {"entry", "item"}:
            items.append((parent, child))
    return items


def append_log(log_path: Path, channel: str, removed: list[tuple[str, str, str]]) -> None:
    if not removed:
        return

    with log_path.open("a", encoding="utf-8") as log_file:
        for item_id, reasons, title in removed:
            log_file.write(f"{channel}\t{item_id}\t{reasons}\t{title}\n")


def filter_feed(
    input_path: Path,
    output_path: Path,
    channel: str,
    log_path: Path | None,
    rules: list[Rule],
) -> int:
    tree = ET.parse(input_path)
    root = tree.getroot()
    removed: list[tuple[str, str, str]] = []

    for parent, item in feed_items(root):
        reasons = filter_reasons(item, rules)
        if not reasons:
            continue

        _, text, _ = extract_entry_content(item)
        removed.append((entry_id(item), ",".join(reasons), entry_title(item, text)))
        parent.remove(item)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(output_path, encoding="utf-8", xml_declaration=True)

    if log_path:
        append_log(log_path, channel, removed)

    return len(removed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--channel", default="unknown")
    parser.add_argument("--config", type=Path, default=Path("config/ad_filters.toml"))
    parser.add_argument("--log", type=Path)
    args = parser.parse_args()

    rules = load_rules(args.config)
    removed_count = filter_feed(args.input, args.output, args.channel, args.log, rules)
    print(f"Filtered {removed_count} item(s) from {args.channel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
