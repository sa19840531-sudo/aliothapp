"""将脱敏静态结构转换为真实提取器消费的快照字段。"""

from collections import Counter
from html.parser import HTMLParser
from pathlib import Path


class _SnapshotParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements = []
        self.stack = []
        self.positions = Counter()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        parent = self.stack[-1] if self.stack else None
        parent_selector = parent["selector"] if parent else ""
        self.positions[(parent_selector, tag)] += 1
        selector = (
            "#" + attrs["id"]
            if attrs.get("id")
            else f"{parent_selector} > {tag}:nth-of-type({self.positions[(parent_selector, tag)]})"
        ).strip(" >")
        node = {
            "tag": tag,
            "selector": selector,
            "parent_selector": parent_selector,
            "class_tokens": attrs.get("class", "").split(),
            "visible": "hidden" not in attrs and (parent is None or parent["visible"]),
            "text": "",
            **{
                key.replace("-", "_"): value
                for key, value in attrs.items()
                if key in {"src", "href", "role", "type", "aria-label", "poster"}
            },
        }
        if "controls" in attrs:
            node["controls"] = True
        if tag == "source":
            node["media_parent_tag"] = parent["tag"] if parent else None
        if attrs.get("width") and attrs.get("height"):
            node["rect"] = {
                "x": 0,
                "y": 0,
                "width": float(attrs["width"]),
                "height": float(attrs["height"]),
            }
        self.elements.append(node)
        if tag not in {"source", "img", "input", "br", "hr", "meta", "link"}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]["tag"] == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        for node in self.stack:
            node["text"] += data


def snapshot_from_html(html):
    parser = _SnapshotParser()
    parser.feed(html)
    groups = Counter(
        (node["parent_selector"], node["tag"], tuple(node["class_tokens"]))
        for node in parser.elements
    )
    for node in parser.elements:
        size = groups[(node["parent_selector"], node["tag"], tuple(node["class_tokens"]))]
        node["repeat_group_size"] = size
        node["within_repeated_structure"] = size >= 2
    return {"elements": parser.elements}


def load_fixture(name):
    return snapshot_from_html(
        (Path(__file__).parent / "fixtures" / "web_detail" / name).read_text(encoding="utf-8")
    )
