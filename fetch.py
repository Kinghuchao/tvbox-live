#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TVBox CCTV 直播源自动维护脚本

流程：抓取上游 m3u 源 -> 过滤央视频道 -> 并发验活测速 ->
      每个频道保留最优 N 条 -> 输出 docs/cctv.m3u 和 docs/tvbox.json

用法：
    python fetch.py --domain live.example.top            # 生成文件
    python fetch.py --domain live.example.top --push     # 生成并 git 提交推送

只依赖 Python 标准库（3.8+），NAS 上无需安装任何第三方包。
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.request import Request, urlopen

# ---------------- 配置 ----------------

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
HTTP_TIMEOUT = 8            # 单条源验活超时（秒）
PROBE_WORKERS = 24          # 验活并发数
MAX_CANDIDATES = 10         # 每个频道最多送检多少条候选源
KEEP_PER_CHANNEL = 5        # 每个频道最终保留几条可用源
OUTPUT_DIR = "docs"         # GitHub Pages 从 main 分支 /docs 目录发布

# 免费 EPG 与台标服务（可换成你喜欢的）
EPG_URL = "http://epg.51zmt.top:8000/e.xml"
LOGO_TPL = "http://epg.51zmt.top:8000/tb/logo/gt/{name}.png"

# CCTV 频道号 -> 标准显示名
CHANNEL_NAMES = {
    1: "CCTV-1 综合", 2: "CCTV-2 财经", 3: "CCTV-3 综艺", 4: "CCTV-4 中文国际",
    5: "CCTV-5 体育", 6: "CCTV-6 电影", 7: "CCTV-7 国防军事", 8: "CCTV-8 电视剧",
    9: "CCTV-9 纪录", 10: "CCTV-10 科教", 11: "CCTV-11 戏曲", 12: "CCTV-12 社会与法",
    13: "CCTV-13 新闻", 14: "CCTV-14 少儿", 15: "CCTV-15 音乐",
    16: "CCTV-16 奥林匹克", 17: "CCTV-17 农业农村",
}

# ---------------- 抓取与解析 ----------------

def http_get(url, timeout=HTTP_TIMEOUT, max_bytes=None):
    req = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise IOError(f"HTTP {resp.status}")
        data = resp.read(max_bytes) if max_bytes else resp.read()
    return data


def load_sources(sources_file):
    """读取 sources.txt，逐行一个上游 m3u 地址，# 开头为注释。"""
    urls = []
    with open(sources_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    return urls


def parse_m3u(text):
    """解析 m3u，返回 [(频道名, 播放地址), ...]"""
    entries = []
    name = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXTINF"):
            m = re.search(r",(.+)$", line)
            name = m.group(1).strip() if m else ""
        elif line and not line.startswith("#") and name is not None:
            entries.append((name, line))
            name = None
    return entries


def channel_key(name):
    """从频道名提取 CCTV 频道号；不是央视频道返回 None。"""
    n = name.upper().replace(" ", "").replace("-", "").replace("_", "")
    if re.search(r"CCTV5\+|CCTV\+5", n):
        return "5+"
    m = re.search(r"CCTV(\d{1,2})", n)
    if m:
        num = int(m.group(1))
        if 1 <= num <= 17:
            return num
    return None

# ---------------- 验活 ----------------

def probe(url):
    """检测单条源可用性，返回 (是否可用, 延迟毫秒)。只读取前 32KB。"""
    start = time.time()
    try:
        data = http_get(url, max_bytes=32768)
        latency = int((time.time() - start) * 1000)
        head = data[:4096]
        if url.split("?")[0].lower().endswith(".m3u8") and b"#EXTM3U" not in head:
            return False, latency
        if len(data) == 0:
            return False, latency
        return True, latency
    except Exception:
        return False, int((time.time() - start) * 1000)

# ---------------- 主流程 ----------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", default=os.environ.get("TVBOX_DOMAIN", ""),
                    help="你的发布域名，如 live.example.top（也可用环境变量 TVBOX_DOMAIN）")
    ap.add_argument("--push", action="store_true", help="生成后执行 git 提交并推送")
    ap.add_argument("--keep", type=int, default=KEEP_PER_CHANNEL)
    args = ap.parse_args()

    if not args.domain:
        sys.exit("错误：请用 --domain 指定发布域名，例如 --domain live.example.top")

    base_url = f"https://{args.domain}"
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    # 1. 抓取上游
    candidates = {}  # channel_key -> set(url)
    for src in load_sources("sources.txt"):
        try:
            print(f"抓取上游: {src}")
            text = http_get(src).decode("utf-8", errors="ignore")
        except Exception as e:
            print(f"  失败，跳过: {e}")
            continue
        found = 0
        for name, url in parse_m3u(text):
            if not url.startswith(("http://", "https://")):
                continue
            key = channel_key(name)
            if key is None:
                continue
            bucket = candidates.setdefault(key, set())
            if len(bucket) < MAX_CANDIDATES:
                bucket.add(url)
                found += 1
        print(f"  命中央视候选 {found} 条")

    total = sum(len(v) for v in candidates.values())
    print(f"\n共 {len(candidates)} 个频道、{total} 条候选，开始验活（并发 {PROBE_WORKERS}）...")

    # 2. 并发验活
    alive = {}  # channel_key -> [(latency, url), ...]
    with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as pool:
        futures = {}
        for key, urls in candidates.items():
            for u in urls:
                futures[pool.submit(probe, u)] = (key, u)
        done = 0
        for fut in as_completed(futures):
            key, u = futures[fut]
            ok, latency = fut.result()
            done += 1
            if done % 20 == 0:
                print(f"  已检测 {done}/{total}")
            if ok:
                alive.setdefault(key, []).append((latency, u))

    # 3. 每个频道取延迟最低的前 N 条
    channels = []  # (排序键, key, [(latency, url)...])
    for key, items in alive.items():
        items.sort(key=lambda x: x[0])
        channels.append((999 if key == "5+" else key, key, items[:args.keep]))
    channels.sort(key=lambda x: x[0])

    if not channels:
        print("\n警告：本轮没有任何可用源，保留旧文件不覆盖。")
        sys.exit(1)

    # 4. 生成 m3u
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    m3u_lines = [f'#EXTM3U x-tvg-url="{EPG_URL}"']
    kept = 0
    for _, key, items in channels:
        display = CHANNEL_NAMES.get(key, f"CCTV-{key}") if key != "5+" else "CCTV-5+ 体育赛事"
        short = display.split()[0]
        for latency, url in items:
            m3u_lines.append(
                f'#EXTINF:-1 tvg-id="{short}" tvg-name="{short}" '
                f'tvg-logo="{LOGO_TPL.format(name=short)}" group-title="央视频道",'
                f'{display} ({latency}ms)'
            )
            m3u_lines.append(url)
            kept += 1
    m3u_path = os.path.join(OUTPUT_DIR, "cctv.m3u")
    with open(m3u_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(m3u_lines) + "\n")

    # 5. 生成 TVBox 接口 JSON
    tvbox = {
        "lives": [
            {
                "name": "自建央视直播",
                "type": 0,
                "url": f"{base_url}/cctv.m3u",
                "playerType": 1,
                "epg": EPG_URL,
                "logo": LOGO_TPL,
                "ua": USER_AGENT,
            }
        ]
    }
    json_path = os.path.join(OUTPUT_DIR, "tvbox.json")
    with open(json_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(tvbox, f, ensure_ascii=False, indent=2)

    # 6. 首页（可选，浏览器打开域名时有个说明页）
    with open(os.path.join(OUTPUT_DIR, "index.html"), "w", encoding="utf-8", newline="\n") as f:
        f.write(
            "<!doctype html><meta charset=utf-8><title>TVBox Live</title>"
            "<h3>自建 CCTV 直播源</h3><ul>"
            "<li>TVBox 配置地址: <code>/tvbox.json</code></li>"
            "<li>m3u 直播源: <code>/cctv.m3u</code></li>"
            f"</ul><p>最近更新: {time.strftime('%Y-%m-%d %H:%M:%S')}</p>"
        )

    print(f"\n完成：{len(channels)} 个频道、{kept} 条可用源")
    print(f"  m3u  -> {m3u_path}")
    print(f"  json -> {json_path}")

    # 7. 可选：git 提交推送
    if args.push:
        subprocess.run(["git", "add", OUTPUT_DIR], check=True)
        msg = f"update sources {time.strftime('%Y-%m-%d %H:%M')}"
        subprocess.run(["git", "commit", "-m", msg], check=False)  # 无变化时允许失败
        subprocess.run(["git", "push"], check=True)
        print("已推送到 GitHub，Pages 约 1 分钟后更新。")


if __name__ == "__main__":
    main()
