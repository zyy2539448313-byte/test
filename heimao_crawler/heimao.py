# -*- coding: utf-8 -*-
"""
黑猫投诉公开数据采集脚本（上门维修行业分析用）
================================================

只访问【匿名状态下公开】的接口与页面，不登录、不绕过任何访问控制：

  1. 最新投诉流   GET /api/index/feed        （匿名上限约 50 页 x 10 条 = 最新 500 条）
  2. 商家检索     GET /api/company/search    （按名称查商家 uid / 30 天投诉量）
  3. 商家检索     GET /api/company/main_search
  4. 投诉详情页   GET /complaint/view/{sn}/?sld={sld}   （公开 SSR 页面，需带 sld）

已知被平台关闭匿名访问的接口（脚本【不会】尝试访问或绕过）：
  - 关键词搜索  /api/index/s                       -> 10005（需登录）
  - 商家投诉列表 /api/company/received_complaints   -> 10005（需登录+签名）
  - feed 翻页 > 50 页                              -> 10017（需登录）

用法：
  python heimao.py latest   --pages 50 --kw-file keywords_repair.txt
  python heimao.py poll     --interval 30          # 每 30 分钟增量抓一轮（可挂定时任务）
  python heimao.py merchant 啄木鸟 万师傅 鲁班到家   # 查竞品商家投诉量
  python heimao.py detail   --input data/complaints.jsonl --limit 20

依赖：httpx（pip install -r requirements.txt）
"""

import argparse
import csv
import hashlib
import json
import random
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx

BASE = "https://tousu.sina.com.cn"
FEED_API = BASE + "/api/index/feed"
COMPANY_SEARCH_API = BASE + "/api/company/search"
COMPANY_MAIN_SEARCH_API = BASE + "/api/company/main_search"

CST = timezone(timedelta(hours=8))

# 匿名访问上限（实测 2026-10）：feed 第 51 页起返回 10017（登录查看更多）
MAX_ANON_PAGE = 50
PAGE_SIZE = 10  # 服务端固定每页 10 条，page_size 传大无效

UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
]

# 两级关键词匹配：
#   强词：出现即命中（补漏/疏通/加氟/上门维修/竞品商家名……）
#   弱词：单独出现太容易误报（"防水夹克""地漏假货""热水器质量"），
#         必须与上下文词（维修/师傅/上门/收费等）同现才算命中。
CONTEXT_WORDS = ["维修", "师傅", "上门", "收费", "报价", "修理", "坏了修", "修不好"]
WEAK_KEYWORDS = {"防水", "漏水", "渗水", "马桶", "地漏", "空调", "热水器",
                 "冰箱", "洗衣机", "油烟机", "燃气灶", "家电", "管道"}

DEFAULT_KEYWORDS = [
    "防水", "补漏", "漏水", "疏通", "下水道", "马桶", "地漏",
    "空调维修", "空调加氟", "家电维修", "上门维修", "维修费", "维修师傅",
    "开锁", "换锁", "水电维修", "管道", "清洗空调", "洗衣机维修",
    "冰箱维修", "热水器", "啄木鸟", "万师傅", "鲁班到家", "极客修",
]


class LoginWall(Exception):
    """触发平台登录墙 / 风控，必须立即停止而不是硬闯。"""


class RiskControl(Exception):
    """接口返回风控错误码（如 10005）。"""


def log(msg):
    print(f"[{datetime.now(CST):%H:%M:%S}] {msg}", flush=True)


def mask_text(s):
    """文本脱敏：手机号打码。只用于内部分析，外发前还应人工复核。"""
    if not s:
        return s
    s = re.sub(r"(1[3-9]\d)\d{4}(\d{4})", r"\1****\2", s)
    s = re.sub(r"(\d{3,4})[- ]?(\d{3,4})[- ]?(\d{4})", r"\1****\3", s)  # 座机/长号
    return s


def mask_name(name):
    """昵称打码：正义喵 -> 正*喵。"""
    if not name or len(name) <= 1:
        return "*"
    if len(name) == 2:
        return name[0] + "*"
    return name[0] + "*" * (len(name) - 2) + name[-1]


def make_client():
    return httpx.Client(
        headers={"Accept": "application/json, text/html", "Accept-Language": "zh-CN,zh;q=0.9"},
        timeout=20,
        follow_redirects=True,
    )


def request(client, url, *, params=None, referer=BASE + "/", delay=1.5, retries=3):
    """
    低频 + UA 轮换 + 指数退避的请求封装。
    遇到登录墙/风控码立即抛异常终止本轮，绝不自动重试硬闯。
    """
    last_err = None
    for attempt in range(retries):
        time.sleep(delay + random.uniform(0, delay))  # 每次请求前都等，含第一次
        try:
            resp = client.get(
                url,
                params=params,
                headers={"User-Agent": random.choice(UA_POOL), "Referer": referer},
            )
            if resp.status_code in (429, 403):
                raise RiskControl(f"HTTP {resp.status_code}（疑似频控），本轮停止")
            if resp.status_code >= 500:
                last_err = f"HTTP {resp.status_code}"
                time.sleep(2 ** attempt * 5)
                continue
            resp.raise_for_status()
            return resp
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_err = str(e)
            time.sleep(2 ** attempt * 5)
    raise RuntimeError(f"请求失败（已重试 {retries} 次）: {url} :: {last_err}")


def api_json(client, url, *, params=None, referer=BASE + "/", delay=1.5):
    resp = request(client, url, params=params, referer=referer, delay=delay)
    data = resp.json()
    result = data.get("result") or {}
    status = result.get("status") or {}
    code = status.get("code")
    if code == 0:
        return result.get("data")
    if code == 10017:
        raise LoginWall("接口要求登录（10017），已到匿名上限")
    raise RiskControl(f"接口返回错误 code={code} msg={status.get('msg')}")


# ---------------------------------------------------------------- latest

def parse_feed_item(item):
    m = item.get("main") or {}
    ts = m.get("timestamp")
    try:
        created = datetime.fromtimestamp(int(ts), CST).strftime("%Y-%m-%d %H:%M:%S") if ts else ""
    except (ValueError, OSError):
        created = ""
    url = m.get("url") or ""
    if url.startswith("//"):
        url = "https:" + url
    return {
        "sn": str(m.get("sn") or ""),
        "title": m.get("title") or "",
        "merchant": m.get("cotitle") or "",
        "couid": str(m.get("couid") or ""),
        "appeal": m.get("appeal") or "",        # 投诉要求（如 请求协商补偿）
        "issue": m.get("issue") or "",          # 投诉问题（如 未告知商品效期）
        "status": m.get("status"),              # 进度代码（枚举未公开，原样保留）
        "upvote": m.get("upvote_amount"),
        "share": m.get("share_amount"),
        "created_at": created,
        "summary": mask_text(m.get("summary") or ""),
        "url": url,
    }


def match_keywords(record, keywords):
    if not keywords:
        return True
    text = record["title"] + " " + record["summary"] + " " + record["merchant"]
    has_context = any(w in text for w in CONTEXT_WORDS)
    for kw in keywords:
        if kw not in text:
            continue
        if kw in WEAK_KEYWORDS and not has_context:
            continue  # 弱词无上下文，视为误报
        return True
    return False


def load_seen(path):
    if path.exists():
        return set(path.read_text(encoding="utf-8").split())
    return set()


def save_seen(path, seen):
    path.write_text("\n".join(sorted(seen)), encoding="utf-8")


def crawl_latest(client, *, pages, keywords, out_dir, seen, delay=1.5):
    """抓最新投诉流，关键词过滤 + SN 去重。返回新记录列表。"""
    records = []
    pages = min(pages, MAX_ANON_PAGE)
    for page in range(1, pages + 1):
        try:
            data = api_json(
                client, FEED_API,
                params={"page_size": PAGE_SIZE, "page": page},
                delay=delay,
            )
        except LoginWall as e:
            log(f"第 {page} 页触达登录墙，提前结束本轮（这是平台规则，属正常停止）: {e}")
            break
        except RiskControl as e:
            log(f"第 {page} 页触发风控，本轮停止，请至少间隔 30 分钟再试: {e}")
            break
        items = (data or {}).get("lists") or []
        if not items:
            log(f"第 {page} 页无数据，结束")
            break
        new_in_page = 0
        for item in items:
            rec = parse_feed_item(item)
            if not rec["sn"] or rec["sn"] in seen:
                continue
            if not match_keywords(rec, keywords):
                continue
            seen.add(rec["sn"])
            rec["source"] = "feed"
            records.append(rec)
            new_in_page += 1
        log(f"第 {page}/{pages} 页：{len(items)} 条，命中新增 {new_in_page} 条（累计 {len(records)}）")
    return records


# ---------------------------------------------------------------- detail

def _strip_tags(html):
    return re.sub(r"\s{2,}", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def parse_detail_html(html, sn):
    """解析公开详情页。字段缺失时留空，不让单条失败拖垮整体。"""
    rec = {"sn": sn}
    m = re.search(r'<h1 class="article">(.*?)</h1>', html, re.S)
    if m:
        rec["title"] = _strip_tags(m.group(1))

    m = re.search(r'<span class="u-name">(.*?)</span>', html, re.S)
    if m:
        rec["nickname_masked"] = mask_name(_strip_tags(m.group(1)))

    m = re.search(r'<span class="u-date">发布于\s*(.*?)</span>', html, re.S)
    if m:
        rec["published_at"] = m.group(1).strip()

    # <ul class="ts-q-list"> 里的字段。页面 HTML 有未闭合 <li>（如"使用服务"），
    # 不能按 <li>...</li> 切，按 <label> 切段：值 = 到下一个 <label> 或 </ul> 为止。
    block = re.search(r'<ul class="ts-q-list">(.*?)</ul>', html, re.S)
    if block:
        field_map = {
            "投诉编号": "sn_check", "投诉对象": "merchant", "使用服务": "service",
            "投诉问题": "issue", "投诉要求": "appeal",
            "涉诉金额": "amount", "投诉进度": "progress",
        }
        parts = re.split(r"<label>", block.group(1))
        for part in parts[1:]:
            lm = re.match(r"([^<]+)</label>(.*)", part, re.S)
            if not lm:
                continue
            key = lm.group(1).strip()
            if key in field_map:
                rec[field_map[key]] = _strip_tags(lm.group(2))

    # 时间线：逐条 ts-d-item 解析（谁、事件、时间、内容），"发起"那条的内容即投诉正文
    timeline = []
    for item_html in re.findall(r'<div class="ts-d-item">(.*?)(?=<div class="ts-d-item">|$)', html, re.S):
        who = re.search(r'<span class="u-name">(.*?)</span>', item_html, re.S)
        event = re.search(r'<span class="u-status">(.*?)</span>', item_html, re.S)
        date = re.search(r'<span class="u-date">(.*?)</span>', item_html, re.S)
        cont = re.search(r'<div class="ts-d-cont">(.*?)</div>\s*</div>', item_html, re.S)
        entry = {
            "who": mask_name(_strip_tags(who.group(1))) if who else "",
            "event": _strip_tags(event.group(1)) if event else "",
            "time": _strip_tags(date.group(1)) if date else "",
        }
        if cont:
            paras = re.findall(r"<p[^>]*>(.*?)</p>", cont.group(1), re.S)
            text = "\n".join(_strip_tags(p) for p in paras if _strip_tags(p))
            if not text:  # 正文不在 <p> 里的情况，整段兜底
                text = _strip_tags(cont.group(1))
            entry["text"] = mask_text(text)
            if "发起" in entry["event"] and text:
                rec["detail_text"] = mask_text(text)
        timeline.append(entry)
    if timeline:
        rec["timeline"] = timeline
    return rec


def crawl_details(client, input_path, out_dir, seen, *, limit=None, delay=2.0):
    """按 JSONL 里的 sn/url 抓详情页，输出 detail_*.jsonl。"""
    src = [json.loads(line) for line in Path(input_path).read_text(encoding="utf-8").splitlines() if line.strip()]
    done_path = out_dir / "seen_detail.txt"
    done = load_seen(done_path)
    todo = [r for r in src if r.get("sn") and r["sn"] not in done]
    if limit:
        todo = todo[:limit]
    log(f"待抓详情 {len(todo)} 条（已跳过 {len(done)} 条历史）")
    out_path = out_dir / "details.jsonl"
    n_ok = 0
    with out_path.open("a", encoding="utf-8") as f:
        for i, r0 in enumerate(todo, 1):
            url = r0.get("url") or f"{BASE}/complaint/view/{r0['sn']}/"
            if url.startswith("//"):
                url = "https:" + url
            try:
                resp = request(client, url, referer=BASE + "/", delay=delay)
            except (RiskControl, RuntimeError) as e:
                log(f"第 {i} 条 sn={r0['sn']} 失败，停止本轮: {e}")
                break
            html = resp.text
            if "页面不存在" in html:
                log(f"第 {i} 条 sn={r0['sn']} 页面不存在（可能已删除或缺少 sld），跳过")
                done.add(r0["sn"])
                continue
            rec = parse_detail_html(html, r0["sn"])
            rec["url"] = url
            rec["fetched_at"] = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            done.add(r0["sn"])
            n_ok += 1
            if i % 10 == 0 or i == len(todo):
                log(f"详情进度 {i}/{len(todo)}")
    save_seen(done_path, done)
    log(f"详情抓取完成，新增 {n_ok} 条 -> {out_path}")


# ---------------------------------------------------------------- merchant

def cmd_merchant(client, names, delay):
    """商家检索：输出 uid、名称、近 30 天投诉量等，用于竞品监控名单。"""
    rows = []
    for name in names:
        data = api_json(client, COMPANY_SEARCH_API, params={"keywords": name}, delay=delay)
        lists = (data or {}).get("lists") or []
        if not lists:
            log(f"「{name}」未找到商家")
            continue
        for it in lists[:3]:
            rows.append({
                "query": name,
                "title": it.get("title") or "",
                "uid": str(it.get("uid") or ""),
                "valid30d": it.get("valid30d") or "",
                "complaint_amount": it.get("complaint_amount") or "",
                "url": f"{BASE}/company/view/?couid={it.get('uid')}",
            })
        top = lists[0]
        log(f"「{name}」-> {top.get('title')} (uid={top.get('uid')}, 近30天投诉 {top.get('valid30d', '?')})")
    return rows


# ---------------------------------------------------------------- io

def append_jsonl(path, records):
    with path.open("a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def append_csv(path, records):
    if not records:
        return
    fields = ["sn", "title", "merchant", "couid", "appeal", "issue", "status",
              "upvote", "share", "created_at", "summary", "url", "source"]
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if not exists:
            w.writeheader()
        w.writerows(records)


def load_keywords(args):
    kws = []
    if args.kw_file and Path(args.kw_file).exists():
        kws += [ln.strip() for ln in Path(args.kw_file).read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.startswith("#")]
    if args.kw:
        kws += [k.strip() for k in args.kw.split(",") if k.strip()]
    return kws or DEFAULT_KEYWORDS


def run_one_round(client, args, out_dir):
    seen_path = out_dir / "seen_sns.txt"
    seen = load_seen(seen_path)
    keywords = load_keywords(args)
    log(f"关键词 {len(keywords)} 个；历史去重 {len(seen)} 条")
    records = crawl_latest(client, pages=args.pages, keywords=keywords, out_dir=out_dir, seen=seen, delay=args.delay)
    if records:
        append_jsonl(out_dir / "complaints.jsonl", records)
        append_csv(out_dir / "complaints.csv", records)
        save_seen(seen_path, seen)
    log(f"本轮新增 {len(records)} 条命中记录")
    return len(records)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="黑猫投诉公开数据采集（低频、匿名、合规优先）")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--kw-file", default=str(Path(__file__).parent / "keywords_repair.txt"), help="关键词文件，一行一个")
        sp.add_argument("--kw", default="", help="逗号分隔的额外关键词")
        sp.add_argument("--out", default=str(Path(__file__).parent / "data"), help="输出目录")
        sp.add_argument("--delay", type=float, default=1.5, help="请求基础间隔秒数（实际=基础+0~基础随机抖动）")

    sp_latest = sub.add_parser("latest", help="抓一轮最新投诉流（关键词过滤+去重）")
    common(sp_latest)
    sp_latest.add_argument("--pages", type=int, default=50, help=f"翻页数，匿名上限 {MAX_ANON_PAGE} 页")

    sp_poll = sub.add_parser("poll", help="循环模式：每隔 N 分钟增量抓一轮")
    common(sp_poll)
    sp_poll.add_argument("--pages", type=int, default=50)
    sp_poll.add_argument("--interval", type=int, default=30, help="轮询间隔分钟数")

    sp_m = sub.add_parser("merchant", help="商家检索（竞品监控）")
    sp_m.add_argument("names", nargs="+", help="商家名称关键词")
    sp_m.add_argument("--delay", type=float, default=1.5)
    sp_m.add_argument("--out", default=str(Path(__file__).parent / "data"))

    sp_d = sub.add_parser("detail", help="按 JSONL 抓投诉详情页全文")
    sp_d.add_argument("--input", required=True, help="complaints.jsonl 路径")
    sp_d.add_argument("--limit", type=int, default=0, help="本轮最多抓几条（0=全部待抓）")
    sp_d.add_argument("--delay", type=float, default=2.0, help="详情页请求间隔（比列表更慢）")
    sp_d.add_argument("--out", default=str(Path(__file__).parent / "data"))

    args = p.parse_args()
    out_dir = Path(getattr(args, "out", Path(__file__).parent / "data"))
    out_dir.mkdir(parents=True, exist_ok=True)

    with make_client() as client:
        # 先访问首页建立正常会话 Cookie（模拟真实用户的自然路径）
        try:
            request(client, BASE + "/", params=None, delay=0.8)
        except Exception as e:
            log(f"首页预热失败（不阻断）: {e}")

        if args.cmd == "latest":
            run_one_round(client, args, out_dir)
        elif args.cmd == "poll":
            log(f"进入轮询模式：每 {args.interval} 分钟一轮，Ctrl+C 停止")
            while True:
                try:
                    run_one_round(client, args, out_dir)
                except Exception as e:
                    log(f"本轮异常（下轮继续）: {e}")
                log(f"休眠 {args.interval} 分钟...")
                time.sleep(args.interval * 60)
        elif args.cmd == "merchant":
            rows = cmd_merchant(client, args.names, args.delay)
            if rows:
                stamp = datetime.now(CST).strftime("%Y%m%d_%H%M%S")
                path = out_dir / f"merchants_{stamp}.csv"
                with path.open("w", encoding="utf-8-sig", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                    w.writeheader()
                    w.writerows(rows)
                log(f"商家结果已保存 -> {path}")
        elif args.cmd == "detail":
            seen = load_seen(out_dir / "seen_sns.txt")
            crawl_details(client, args.input, out_dir, seen,
                          limit=args.limit or None, delay=args.delay)


if __name__ == "__main__":
    main()
