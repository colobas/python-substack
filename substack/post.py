"""

Post Utilities

"""

import json
import random
import re
import string
from typing import Dict, List, Optional

__all__ = ["Post", "parse_inline"]

from substack.exceptions import SectionNotExistsException


def parse_inline(text: str) -> List[Dict]:
    """Backward-compatible inline markdown parsing.

    This returns the legacy token format used by older parts of the library:

        {"content": "...", "marks": [{"type": "strong"}, ...]}

    Newer functionality (math, footnotes) is implemented inside `Post.from_markdown`.
    """

    if not text:
        return []

    tokens = []

    link_pattern = r"\[([^\]]+)\]\(([^)]+)\)"
    bold_pattern = r"\*\*([^*]+)\*\*"
    italic_pattern = r"(?<!\*)\*([^*]+)\*(?!\*)"

    matches = []
    for match in re.finditer(link_pattern, text):
        # Skip if it's an image link (starts with ![)
        if match.start() > 0 and text[match.start() - 1 : match.start() + 1] != "![":
            matches.append((match.start(), match.end(), "link", match.group(1), match.group(2)))

    for match in re.finditer(bold_pattern, text):
        if not any(start <= match.start() < end for start, end, *_ in matches):
            matches.append((match.start(), match.end(), "bold", match.group(1), None))

    for match in re.finditer(italic_pattern, text):
        if not any(start <= match.start() < end for start, end, *_ in matches):
            matches.append((match.start(), match.end(), "italic", match.group(1), None))

    matches.sort(key=lambda x: x[0])

    last_pos = 0
    for start, end, match_type, content, url in matches:
        if start > last_pos:
            tokens.append({"content": text[last_pos:start]})

        if match_type == "link":
            tokens.append({"content": content, "marks": [{"type": "link", "attrs": {"href": url}}]})
        elif match_type == "bold":
            tokens.append({"content": content, "marks": [{"type": "strong"}]})
        elif match_type == "italic":
            tokens.append({"content": content, "marks": [{"type": "em"}]})

        last_pos = end

    if last_pos < len(text):
        tokens.append({"content": text[last_pos:]})

    return [t for t in tokens if t.get("content")]


def _pm_text(text: str, marks: Optional[list] = None) -> Dict:
    node: Dict = {"type": "text", "text": text}
    if marks:
        node["marks"] = marks
    return node


def _pm_mark_strong() -> Dict:
    return {"type": "strong"}


def _pm_mark_em() -> Dict:
    return {"type": "em"}


def _pm_mark_link(href: str) -> Dict:
    return {"type": "link", "attrs": {"href": href}}


def _pm_paragraph(inlines: List[Dict]) -> Dict:
    return {"type": "paragraph", "attrs": {"textAlign": None}, "content": inlines}


def _pm_footnote(footnote_text: str) -> Dict:
    # Substack accepts a `footnote` inline node with nested content.
    return {
        "type": "footnote",
        "content": [
            {
                "type": "paragraph",
                "attrs": {"textAlign": None},
                "content": [{"type": "text", "text": footnote_text}],
            }
        ],
    }


def _new_latex_id(n: int = 10) -> str:
    # Observed ids look like uppercase alpha strings (e.g. "CYTDOISRRI").
    alphabet = string.ascii_uppercase
    return "".join(random.choice(alphabet) for _ in range(n))


def _pm_latex_block(expr: str) -> Dict:
    # Substack uses `latex_block` with attrs.persistentExpression.
    return {
        "type": "latex_block",
        "attrs": {
            "persistentExpression": expr,
            "id": _new_latex_id(),
            "dirty": True,
        },
    }


def _parse_inline_nodes(text: str, footnotes: Dict[str, str]) -> List[Dict]:
    """Parse a single-line markdown string into Substack/ProseMirror inline nodes.

    Supported:
    - links: [text](url)
    - bold: **text**
    - italic: *text*
    - inline math: $...$
    - footnote refs: [^key] (requires a definition collected by from_markdown)

    This is intentionally simple and non-nesting.
    """

    if not text:
        return []

    out: List[Dict] = []

    i = 0
    buf = ""

    def flush_buf():
        nonlocal buf
        if buf:
            out.append(_pm_text(buf))
            buf = ""

    while i < len(text):
        # Footnote reference: [^key]
        if text.startswith("[^", i):
            j = text.find("]", i + 2)
            if j != -1:
                key = text[i + 2 : j]
                note = footnotes.get(key)
                if note is not None:
                    flush_buf()
                    out.append(_pm_footnote(note))
                    i = j + 1
                    continue

        # Link: [text](url)
        if text.startswith("[", i):
            m = re.match(r"\[([^\]]+)\]\(([^)]+)\)", text[i:])
            if m:
                label, url = m.group(1), m.group(2)
                flush_buf()
                out.append(_pm_text(label, marks=[_pm_mark_link(url)]))
                i += m.end()
                continue


        # Bold: **...**
        if text.startswith("**", i):
            j = text.find("**", i + 2)
            if j != -1:
                flush_buf()
                out.append(_pm_text(text[i + 2 : j], marks=[_pm_mark_strong()]))
                i = j + 2
                continue

        # Italic: *...*
        if text[i] == "*" and not text.startswith("**", i):
            j = text.find("*", i + 1)
            if j != -1:
                flush_buf()
                out.append(_pm_text(text[i + 1 : j], marks=[_pm_mark_em()]))
                i = j + 1
                continue

        buf += text[i]
        i += 1

    flush_buf()

    # Merge adjacent plain text nodes
    merged: List[Dict] = []
    for node in out:
        if (
            merged
            and node.get("type") == "text"
            and merged[-1].get("type") == "text"
            and not node.get("marks")
            and not merged[-1].get("marks")
        ):
            merged[-1]["text"] += node.get("text", "")
        else:
            merged.append(node)

    return merged


def _is_table_separator(line: str) -> bool:
    # Match a typical markdown table separator row like:
    # |---|:---:|---:|
    s = line.strip()
    if "|" not in s:
        return False
    s = s.strip("|")
    parts = [p.strip() for p in s.split("|")]
    if not parts:
        return False
    return all(re.fullmatch(r":?-{3,}:?", p or "") for p in parts)


def _split_table_row(line: str) -> List[str]:
    s = line.strip().strip("|")
    return [c.strip() for c in s.split("|")]


def _pm_table(rows: List[List[List[Dict]]]) -> Dict:
    """Build a prosemirror table.

    rows is a list of rows; each row is a list of cells; each cell is a list of inline nodes.
    First row is treated as header.
    """

    table_rows = []
    for r_i, row in enumerate(rows):
        cells = []
        for cell_inlines in row:
            cell_type = "table_header" if r_i == 0 else "table_cell"
            cells.append(
                {
                    "type": cell_type,
                    "attrs": {"colspan": 1, "rowspan": 1, "colwidth": None},
                    "content": [_pm_paragraph(cell_inlines or [_pm_text("")])],
                }
            )
        table_rows.append({"type": "table_row", "content": cells})

    return {"type": "table", "content": table_rows}


class Post:
    """Post utility class"""

    def __init__(
        self,
        title: str,
        subtitle: str,
        user_id,
        audience: str = None,
        write_comment_permissions: str = None,
    ):
        self.draft_title = title
        self.draft_subtitle = subtitle
        self.draft_body = {"type": "doc", "content": []}
        self.draft_bylines = [{"id": int(user_id), "is_guest": False}]
        self.audience = audience if audience is not None else "everyone"
        self.draft_section_id = None
        self.section_chosen = True

        if write_comment_permissions is not None:
            self.write_comment_permissions = write_comment_permissions
        else:
            self.write_comment_permissions = self.audience

    def set_section(self, name: str, sections: list):
        section = [s for s in sections if s.get("name") == name]
        if len(section) != 1:
            raise SectionNotExistsException(name)
        section = section[0]
        self.draft_section_id = section.get("id")

    def add(self, item: Dict):
        """Add item to draft body."""

        self.draft_body["content"] = self.draft_body.get("content", []) + [
            {"type": item.get("type")}
        ]
        content = item.get("content")
        if item.get("type") == "captionedImage":
            self.captioned_image(**item)
        elif item.get("type") == "embeddedPublication":
            self.draft_body["content"][-1]["attrs"] = item.get("url")
        elif item.get("type") == "youtube2":
            self.youtube(item.get("src"))
        elif item.get("type") == "subscribeWidget":
            self.subscribe_with_caption(item.get("message"))
        elif item.get("type") == "codeBlock":
            self.code_block(item.get("content"), item.get("attrs", {}))
        else:
            if content is not None:
                self.add_complex_text(content)

        if item.get("type") == "heading":
            self.attrs(item.get("level", 1))

        marks = item.get("marks")
        if marks is not None:
            self.marks(marks)

        return self

    def paragraph(self, content=None):
        self.add({"type": "paragraph", "content": content})
        return self

    def heading(self, content=None, level=1):
        self.add({"type": "heading", "content": content, "level": level})
        return self

    def attrs(self, level=1):
        content_attrs = self.draft_body["content"][-1].get("attrs", {})
        content_attrs.update({"level": level})
        self.draft_body["content"][-1]["attrs"] = content_attrs
        return self

    def marks(self, marks):
        content = self.draft_body["content"][-1].get("content", [])[-1]
        content_marks = content.get("marks", [])
        for mark in marks:
            new_mark = {"type": mark.get("type")}
            if mark.get("type") == "link":
                href = mark.get("href")
                new_mark.update({"attrs": {"href": href}})
            content_marks.append(new_mark)
        content["marks"] = content_marks
        return self

    def text(self, value: str):
        content = self.draft_body["content"][-1].get("content", [])
        content += [{"type": "text", "text": value}]
        self.draft_body["content"][-1]["content"] = content
        return self

    def add_complex_text(self, text):
        if isinstance(text, str):
            self.text(text)
        else:
            for chunk in text:
                if chunk:
                    self.text(chunk.get("content")).marks(chunk.get("marks", []))

    def remove_last_paragraph(self):
        del self.draft_body.get("content")[-1]

    def get_draft(self):
        out = vars(self)
        out["draft_body"] = json.dumps(out["draft_body"])
        return out

    # --- Existing helpers below unchanged (captioned_image, youtube, code_block, etc.) ---

    def captioned_image(
        self,
        src: str,
        title: str = "",
        caption: str = "",
        alt: str = "",
        href: str = "",
        imageSize: str = "normal",
        belowTheFold: bool = False,
        internalRedirect: bool = False,
        **kwargs,
    ):
        content = [
            {
                "type": "image2",
                "attrs": {
                    "src": src,
                    "title": title,
                    "alt": alt,
                    "caption": caption,
                    "href": href,
                    "imageSize": imageSize,
                    "belowTheFold": belowTheFold,
                    "internalRedirect": internalRedirect,
                },
            }
        ]
        self.draft_body["content"][-1]["content"] = content
        return self

    def subscribe_with_caption(self, message: str = None):
        if message is None:
            message = """Thanks for reading this newsletter!
            Subscribe for free to receive new posts and support my work."""

        subscribe = self.draft_body["content"][-1]
        subscribe["attrs"] = {
            "url": "%%checkout_url%%",
            "text": "Subscribe",
            "language": "en",
        }
        subscribe["content"] = [
            {
                "type": "ctaCaption",
                "content": [
                    {
                        "type": "text",
                        "text": message,
                    }
                ],
            }
        ]
        return self

    def youtube(self, value: str):
        content_attrs = self.draft_body["content"][-1].get("attrs", {})
        content_attrs.update({"videoId": value})
        self.draft_body["content"][-1]["attrs"] = content_attrs
        return self

    def code_block(self, content, attrs=None):
        if attrs is None:
            attrs = {}

        if isinstance(content, str):
            code_content = [{"type": "text", "text": content}]
        elif isinstance(content, list):
            code_content = content
        else:
            code_content = []

        code_block = self.draft_body["content"][-1]
        code_block["content"] = code_content
        if attrs:
            code_block["attrs"] = attrs

        return self

    def from_markdown(self, markdown_content: str, api=None):
        """Parse Markdown content and add it to the post.

        Notes:
        - This is a small, pragmatic parser intended for common blog content.
        - It supports Substack-native nodes for tables and math.
        - It supports Substack-native footnotes from markdown footnote syntax.
        """

        # Collect footnote definitions and remove them from the source.
        footnotes: Dict[str, str] = {}
        cleaned_lines: List[str] = []
        for line in markdown_content.split("\n"):
            m = re.match(r"^\[\^([^\]]+)\]:\s*(.*)\s*$", line)
            if m:
                footnotes[m.group(1)] = m.group(2)
            else:
                cleaned_lines.append(line)

        lines = cleaned_lines

        blocks = []
        current_block: List[str] = []
        in_code_block = False
        code_block_language = None
        in_math_block = False
        math_block_lines: List[str] = []

        for line in lines:
            s = line.strip()

            # Fenced code blocks
            if s.startswith("```") and not in_math_block:
                if in_code_block:
                    if current_block:
                        blocks.append(
                            {
                                "type": "code",
                                "language": code_block_language,
                                "content": "\n".join(current_block),
                            }
                        )
                    current_block = []
                    in_code_block = False
                    code_block_language = None
                else:
                    if current_block:
                        blocks.append({"type": "text", "content": "\n".join(current_block)})
                        current_block = []
                    language = s[3:].strip()
                    code_block_language = language if language else None
                    in_code_block = True
                continue

            if in_code_block:
                current_block.append(line)
                continue

            # Display math blocks $$...$$
            if s.startswith("$$"):
                # one-line $$...$$
                if s.endswith("$$") and len(s) > 4 and not in_math_block:
                    latex = s[2:-2].strip()
                    if current_block:
                        blocks.append({"type": "text", "content": "\n".join(current_block)})
                        current_block = []
                    blocks.append({"type": "math", "content": latex})
                    continue

                # toggle multiline
                if in_math_block:
                    # end
                    tail = s[2:] if s != "$$" else ""
                    if tail:
                        math_block_lines.append(tail)
                    blocks.append({"type": "math", "content": "\n".join(math_block_lines).strip()})
                    in_math_block = False
                    math_block_lines = []
                else:
                    if current_block:
                        blocks.append({"type": "text", "content": "\n".join(current_block)})
                        current_block = []
                    in_math_block = True
                    head = s[2:] if s != "$$" else ""
                    if head:
                        math_block_lines.append(head)
                continue

            if in_math_block:
                # look for end marker
                if s.endswith("$$"):
                    inner = line
                    # strip trailing $$
                    inner = inner[: inner.rfind("$$")]
                    math_block_lines.append(inner)
                    blocks.append({"type": "math", "content": "\n".join(math_block_lines).strip()})
                    in_math_block = False
                    math_block_lines = []
                else:
                    math_block_lines.append(line)
                continue

            # Regular content
            if s == "":
                if current_block:
                    blocks.append({"type": "text", "content": "\n".join(current_block)})
                    current_block = []
            else:
                current_block.append(line)

        if current_block:
            blocks.append({"type": "text", "content": "\n".join(current_block)})

        # Process blocks
        for block in blocks:
            if block["type"] == "code":
                code_content = block.get("content", "").strip()
                if code_content:
                    code_attrs = {}
                    if block.get("language"):
                        code_attrs["language"] = block["language"]
                    self.add({"type": "codeBlock", "content": code_content, "attrs": code_attrs})
                continue

            if block["type"] == "math":
                latex = block.get("content", "").strip()
                if latex:
                    self.draft_body["content"].append(_pm_latex_block(latex))
                continue

            text_content = block.get("content", "").strip("\n")
            if not text_content.strip():
                continue

            # Headings
            if text_content.lstrip().startswith("#") and "\n" not in text_content:
                level = len(text_content) - len(text_content.lstrip("#"))
                heading_text = text_content.lstrip("#").strip()
                if heading_text:
                    self.heading(content=heading_text, level=min(level, 6))
                continue

            # Images
            if text_content.startswith("!") or (text_content.startswith("[") and "![" in text_content):
                linked_image_match = re.match(r"\[!\[([^\]]*)\]\(([^)]+)\)\]\(([^)]+)\)", text_content)
                if linked_image_match:
                    alt_text = linked_image_match.group(1)
                    image_url = linked_image_match.group(2)
                    link_url = linked_image_match.group(3)

                    image_url = image_url[1:] if image_url.startswith("/") else image_url
                    if api is not None:
                        try:
                            image = api.get_image(image_url)
                            image_url = image.get("url")
                        except Exception:
                            pass

                    self.add(
                        {
                            "type": "captionedImage",
                            "src": image_url,
                            "alt": alt_text,
                            "href": link_url,
                        }
                    )
                else:
                    match = re.match(r"!\[.*?\]\((.*?)\)", text_content)
                    if match:
                        image_url = match.group(1)
                        image_url = image_url[1:] if image_url.startswith("/") else image_url
                        if api is not None:
                            try:
                                image = api.get_image(image_url)
                                image_url = image.get("url")
                            except Exception:
                                pass
                        self.add({"type": "captionedImage", "src": image_url})
                continue

            # Tables
            if "\n" in text_content:
                lines2 = [ln.rstrip() for ln in text_content.split("\n") if ln.strip()]
                if len(lines2) >= 2 and "|" in lines2[0] and _is_table_separator(lines2[1]):
                    header = _split_table_row(lines2[0])
                    body_rows = [_split_table_row(ln) for ln in lines2[2:]]
                    rows_nodes: List[List[List[Dict]]] = []
                    rows_nodes.append([_parse_inline_nodes(c, footnotes) for c in header])
                    for row in body_rows:
                        rows_nodes.append([_parse_inline_nodes(c, footnotes) for c in row])
                    self.draft_body["content"].append(_pm_table(rows_nodes))
                    continue

            # Paragraphs / bullet-ish lines
            for line in text_content.split("\n"):
                ln = line.strip()
                if not ln:
                    continue

                # bullets: keep as plain paragraphs (Substack will format as separate lines)
                if ln.startswith("* "):
                    ln = ln[2:].strip()
                elif ln.startswith("- "):
                    ln = ln[2:].strip()
                elif ln.startswith("*") and not ln.startswith("**"):
                    ln = ln[1:].strip()

                inlines = _parse_inline_nodes(ln, footnotes)
                if inlines:
                    self.draft_body["content"].append(_pm_paragraph(inlines))

        return self
