# -*- coding: utf-8 -*-
"""构建后一键注入 web-patches：vite build 重建 dist 会冲掉注入，本脚本重做。

做法：
  1. 把 services/web-patches/*.js 复制进 airi/apps/stage-web/dist/
  2. 在 dist/index.html 的 module 脚本前插入 <script src="/..."> 引用（幂等）

用法：
  python apply_web_patches.py          # 执行注入
  python apply_web_patches.py --check  # 只检查，不修改（CI/手动验证用）

补丁加载顺序按 PATCHES 列表：config-restore 必须最先（fetch 护栏要在
应用 bundle 之前装上），stage-monitor 随后。
"""
import argparse
import re
import shutil
import sys
from pathlib import Path

SERVICES_DIR = Path(__file__).resolve().parent
DIST_DIR = SERVICES_DIR.parent / "airi" / "apps" / "stage-web" / "dist"
PATCHES_DIR = SERVICES_DIR / "web-patches"
INDEX_HTML = DIST_DIR / "index.html"

# (文件名, 说明)；存在的才注入，机器私有补丁（config-restore.js）不入库也不报错
PATCHES = [
    ("config-restore.js", "localStorage 配置恢复 + 请求护栏"),
    ("stage-monitor.js", "流水线阶段指示器 + 服务状态面板"),
]


def fail(msg: str) -> None:
    print(f"[ERROR] {msg}")
    sys.exit(1)


def inject(index_path: Path, names: list) -> bool:
    """把缺失的 script 引用插到 module 脚本前。返回是否有改动。"""
    html = index_path.read_text(encoding="utf-8")
    missing = [n for n in names if f'"/{n}"' not in html]
    if not missing:
        return False
    m = re.search(r'^\s*<script type="module"', html, re.M)
    if not m:
        fail(f"{index_path} 里找不到 module script 标签，无法定位注入点")
    tags = "".join(f'    <script src="/{n}"></script>\n' for n in missing)
    html = html[:m.start()] + tags + html[m.start():]
    index_path.write_text(html, encoding="utf-8", newline="")
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只检查不修改")
    args = ap.parse_args()

    if not INDEX_HTML.is_file():
        fail(f"找不到 {INDEX_HTML}，先执行 vite build")

    names = []
    for fname, desc in PATCHES:
        src = PATCHES_DIR / fname
        if not src.is_file():
            print(f"[skip] {fname}（{desc}）不存在，跳过")
            continue
        dst = DIST_DIR / fname
        names.append(fname)
        if args.check:
            status = "一致" if (dst.is_file() and dst.read_bytes() == src.read_bytes()) else "缺失或不一致"
            print(f"[check] {fname}: {status}")
        else:
            if not dst.is_file() or dst.read_bytes() != src.read_bytes():
                shutil.copy2(src, dst)
                print(f"[copy] {fname} → dist/")
            else:
                print(f"[ok] {fname} 已是最新")

    if args.check:
        html = INDEX_HTML.read_text(encoding="utf-8")
        for n in names:
            print(f"[check] index.html 引用 /{n}: {'有' if f'\"/{n}\"' in html else '缺失'}")
        return

    if inject(INDEX_HTML, names):
        print("[inject] index.html 已插入 script 引用")
    else:
        print("[ok] index.html 引用齐全，无需注入")
    print("完成。浏览器 Ctrl+F5 强刷生效。")


if __name__ == "__main__":
    main()