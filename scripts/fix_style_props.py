"""一次性脚本：把前端 JS 中的 style="..." 字符串属性转为 React 对象形式。"""
import pathlib
import re
import sys

BASE = pathlib.Path(__file__).resolve().parent.parent / "static" / "js"

CAMEL = {
    "align-items": "alignItems",
    "justify-content": "justifyContent",
    "margin-top": "marginTop",
    "margin-bottom": "marginBottom",
    "margin-right": "marginRight",
    "margin-left": "marginLeft",
    "padding-top": "paddingTop",
    "padding-bottom": "paddingBottom",
    "padding-left": "paddingLeft",
    "padding-right": "paddingRight",
    "font-weight": "fontWeight",
    "line-height": "lineHeight",
    "white-space": "whiteSpace",
    "font-size": "fontSize",
    "border-radius": "borderRadius",
    "grid-column": "gridColumn",
    "text-align": "textAlign",
    "background-color": "backgroundColor",
    "max-width": "maxWidth",
    "min-width": "minWidth",
}


def convert(css: str) -> str:
    props = []
    for item in css.split(";"):
        item = item.strip()
        if not item:
            continue
        key, _, value = item.partition(":")
        key = key.strip()
        value = value.strip()
        ck = CAMEL.get(key, key)
        props.append(f'{ck}: "{value}"')
    return "{" + ", ".join(props) + "}"


pattern = re.compile(r'style="([^"]*)"')
count = 0
for js in BASE.rglob("*.js"):
    text = js.read_text(encoding="utf-8")
    new_text, n = pattern.subn(lambda m: f"style=${{{convert(m.group(1))}}}", text)
    if n:
        js.write_text(new_text, encoding="utf-8")
        count += n
        print(f"{js.relative_to(BASE.parent.parent)}: {n} 处")
print(f"共替换 {count} 处")
