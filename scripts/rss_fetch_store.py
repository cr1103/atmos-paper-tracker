#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rss_fetch_store.py  (hardened)
- 读取 config.json 的 feeds
- 抓取 RSS 并解析文章页
- 计算去重 UID（强化：DOI/链接标准化、hash 截断位数提升）
- 提取字段 + 主题打标 +（可选）标题/摘要翻译（空值保护）
- 统一写入 SQLite（强化：WAL、busy_timeout、每 feed 事务）
- 失败不立即写入 seen，转入 pending 并带重试计数
- 按域名限速 + 指数退避重试；内容类型判断
- 冷启动/长 feed 防护：仅处理最新 N 条
- 记录 runs_log，并在 meta 表记录 last_global_update_at（本次运行完成时间）
"""

from __future__ import annotations
import os, sys, json, time, re, sqlite3, hashlib
from typing import Dict, Any, Optional, List, Tuple
from urllib.parse import urlparse

from rss_common import (
    db_connect, now_ts, fetch_feed, parse_feed, fetch, extract_article_fields,
    classify_topic, classify_topic_heu, sha256, TOPICS, ensure_dir, HEADERS, translate_batch_en2zh, log_run
)

# -----------------------------
# 常量 & 简易配置（可在 config.json 覆盖）
# -----------------------------
DEFAULT_HTTP_TIMEOUT = 25
DEFAULT_SLEEP_BETWEEN = 0.5
DEFAULT_TRANSLATE = True
DEFAULT_MAX_ITEMS_PER_FEED = 200          # 冷启动/长 feed 防护
DEFAULT_PER_DOMAIN_RPS = 1.5              # 每域名最大请求频率（req/s），用于限速
DEFAULT_MAX_RETRIES = 3                   # 请求最大重试次数
DEFAULT_BACKOFF_BASE = 0.6                # 初始退避秒
DEFAULT_BACKOFF_FACTOR = 2.0              # 指数倍数

UID_HASH_HEX_LEN = 24                     # linkhash/hash 截断长度（hex）

TEXT_HTML_MIME_RE = re.compile(r'text/html', re.I)

# 每域名上次请求时间戳（秒）
_last_request_at: Dict[str, float] = {}

# -----------------------------
# 实用函数
# -----------------------------
def _normalize_doi(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    doi = raw.strip()
    # 去掉前缀 https?://(dx.)?doi.org/ 或 doi:
    doi = re.sub(r'^(https?://(dx\.)?doi\.org/)', '', doi, flags=re.I)
    doi = re.sub(r'^doi:\s*', '', doi, flags=re.I)
    return doi.strip().lower() or None

def _normalize_link(url: Optional[str]) -> Optional[str]:
    if not url:
        return url
    s = url.strip()
    # 去掉常见跟踪参数
    s = re.sub(r'([?&])(utm_[^=&]+|spm|fbclid|gclid)=[^&#]*', r'\1', s, flags=re.I)
    s = re.sub(r'[?&]+$', '', s)
    # 去掉多余的末尾斜杠（但保留根域）
    if len(s) > 1 and s.endswith('/'):
        s = s[:-1]
    return s

def _domain_of(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return None

def _per_domain_rate_limit(url: Optional[str], per_domain_rps: float):
    """简单按域名限速：若距离上次请求不足 1/RPS，则 sleep。"""
    if not url or per_domain_rps <= 0:
        return
    domain = _domain_of(url)
    if not domain:
        return
    now = time.time()
    min_interval = 1.0 / per_domain_rps
    last = _last_request_at.get(domain, 0.0)
    delta = now - last
    if delta < min_interval:
        time.sleep(min_interval - delta)
    _last_request_at[domain] = time.time()

def _with_retries(fn, *, url_for_limit: Optional[str], max_retries: int, backoff_base: float, backoff_factor: float):
    """对网络请求提供统一的重试与按域限速。"""
    ex: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            _per_domain_rate_limit(url_for_limit, _with_retries.per_domain_rps)
            return fn()
        except Exception as e:
            ex = e
            if attempt >= max_retries:
                break
            sleep_s = backoff_base * (backoff_factor ** attempt)
            time.sleep(sleep_s)
    assert ex is not None
    raise ex

# 将可调整的每域 RPS 挂在函数属性上，便于在 run() 中覆盖
_with_retries.per_domain_rps = DEFAULT_PER_DOMAIN_RPS  # type: ignore[attr-defined]

def _safe_translate(title_en: Optional[str], abstract_en: Optional[str], config: Dict[str,Any]) -> Tuple[Optional[str], Optional[str]]:
    t = title_en or ""
    a = abstract_en or ""
    if not t and not a:
        return None, None
    try:
        t_cn, a_cn = translate_batch_en2zh([t, a], config)
        t_cn = (t_cn or None)
        a_cn = (a_cn or None)
        return t_cn, a_cn
    except Exception:
        # 翻译失败不影响英文入库
        return None, None

def classify_topics_multi(title: Optional[str], abstract: Optional[str], config: Dict[str,Any]) -> List[str]:
    """
    独立检查每个分类的关键词，标题和摘要中所有匹配的分类都返回。
    一篇论文可以有多个标签（eg. 标题匹配"雷达"，摘要匹配"云降水"）。
    """
    text_lower = f"{title or ''}\n{abstract or ''}".lower()
    
    # 每个分类及其对应的关键词正则（与 classify_topic_heu 保持一致）
    category_patterns = [
        ("雷达气象", r"\b("
            r"radar\s*meteorology|weather\s*radar|cloud\s*radar|"
            r"polarimetric\s*radar|dual[- ]polarization|dual-pol|"
            r"ka-band|w-band|mm[- ]wave\s*radar|millimeter[- ]wave\s*radar|"
            r"doppler\s*spectrum|doppler\s*velocity|spectral\s*breadth|"
            r"zdr|kdp|differential\s*reflectivity|specific\s*differential\s*phase|"
            r"radar\s*reflectivity|reflectivity\s*factor|attenuation\s*correction|"
            r"precipitation\s*radar|profiling\s*radar|cloud\s*profiling|"
            r"rhi|ppi|radar\s*retrieval|radar\s*observation"
            r")\b"),
        ("云降水物理", r"\b("
            r"cloud\s*microphysics|precipitation\s*microphysics|"
            r"riming|rimed|aggregation|aggregated|"
            r"melting\s*layer|bright\s*band|saggy\s*bright\s*band|sbb|"
            r"supercooled\s*liquid\s*water|slw|"
            r"ice\s*nucleation|secondary\s*ice\s*production|sip|"
            r"hallett[- ]mossop|"
            r"droplet\s*activation|collision[- ]coalescence|warm\s*rain|drizzle|"
            r"graupel|hail|hydrometeor|"
            r"particle\s*size\s*distribution|dsd|drop\s*size\s*distribution|"
            r"rain\s*rate|rainfall|precipitation\s*intensity|"
            r"liquid\s*water\s*path|lwp|ice\s*water\s*content|iwc|liquid\s*water\s*content|lwc|"
            r"cloud\s*condensation\s*nuclei|ccn|"
            r"ice\s*nucleating\s*particles|inp|"
            r"freezing\s*drizzle|fzra|ice\s*phase|mixed[- ]phase"
            r")\b"),
        ("卫星遥感", r"\b("
            r"satellite\s*remote\s*sensing|satellite\s*retrieval|"
            r"gpm|cloudsat|calipso|earthcare|"
            r"passive\s*microwave|microwave\s*radiometer|"
            r"modis|viirs|avhrr|sentinel|landsat|"
            r"infrared\s*sounding|microwave\s*sounding|"
            r"satellite\s*precipitation|satellite\s*cloud|"
            r"gridded\s*products|imerg|cmorph|"
            r"spaceborne\s*radar|spaceborne\s*lidar|"
            r"aerosol\s*optical\s*depth|aod|"
            r"radiance|brightness\s*temperature|"
            r"satellite\s*observation|active\s*sensor|passive\s*sensor"
            r")\b"),
        ("气溶胶-云相互作用", r"\b("
            r"aerosol[- ]cloud\s*interaction|aerosol[- ]cloud[- ]radiation|"
            r"aerosol\s*indirect\s*effect|aerosol\s*direct\s*effect|"
            r"cloud\s*condensation\s*nuclei|ccn|"
            r"ice\s*nucleating\s*particles|inp|"
            r"aerosol\s*radiative\s*effect|aerosol\s*radiative\s*forcing|"
            r"aerosol\s*activation|aerosol\s*hygroscopicity|"
            r"twomey\s*effect|albrecht\s*effect|"
            r"dust\s*aerosol|smoke\s*aerosol|biomass\s*burning|"
            r"anthropogenic\s*aerosol|sulfate\s*aerosol|"
            r"aerosol\s*optical\s*depth|aod|"
            r"aerosol\s*type|aerosol\s*composition|"
            r"sea\s*salt\s*aerosol|mineral\s*dust|"
            r"aerosol\s*concentration|particulate\s*matter|pm2\.5|pm10"
            r")\b"),
        ("大气动力过程", r"\b("
            r"atmospheric\s*dynamics|atmospheric\s*circulation|"
            r"deep\s*convection|shallow\s*convection|convective\s*storm|"
            r"mesoscale\s*convective\s*system|mcs|squall\s*line|"
            r"tropical\s*cyclone|hurricane|typhoon|"
            r"frontal\s*system|cold\s*front|warm\s*front|"
            r"boundary\s*layer|planetary\s*boundary\s*layer|pbl|"
            r"turbulence|turbulent\s*kinetic\s*energy|tke|"
            r"gravity\s*wave|mountain\s*wave|"
            r"cold\s*pool|outflow|density\s*current|"
            r"jet\s*stream|polar\s*jet|subtropical\s*jet|"
            r"atmospheric\s*blocking|blocking\s*high|"
            r"organized\s*convection|convective\s*organization|"
            r"atmospheric\s*river|ar|moisture\s*transport|"
            r"storm\s*track|cyclogenesis|baroclinic|barotropic"
            r")\b"),
        ("人工智能+气象", r"\b("
            r"machine\s*learning|deep\s*learning|neural\s*network|"
            r"convolutional\s*neural|cnn|transformer|"
            r"graph\s*neural\s*network|gnn|"
            r"encoder[- ]decoder|autoencoder|"
            r"generative\s*model|diffusion\s*model|gan|"
            r"nowcasting|precipitation\s*nowcasting|"
            r"weather\s*forecasting|weather\s*prediction|"
            r"data\s*assimilation|ensemble\s*kalman\s*filter|"
            r"physics[- ]informed|pinn|"
            r"supervised\s*learning|unsupervised\s*learning|"
            r"transfer\s*learning|representation\s*learning|"
            r"attention\s*mechanism|self-attention|"
            r"artificial\s*intelligence|ai[- ]based|"
            r"deepmind|graphcast|pangu|fourcastnet|climatenet|"
            r"forecast\s*model|weather\s*model|"
            r"emulation|surrogate\s*model"
            r")\b"),
        ("雷达遥感", r"\b("
            r"synthetic\s*aperture\s*radar|sar|insar|"
            r"radar\s*remote\s*sensing|radar\s*imaging|"
            r"ground[- ]penetrating\s*radar|gpr|"
            r"scatterometer|radar\s*altimeter|"
            r"interferometric\s*radar|"
            r"radar\s*backscatter|radar\s*cross[- ]section|"
            r"radar\s*interferometry|polsar|"
            r"mm-wave\s*imaging|millimeter[- ]wave\s*imaging"
            r")\b"),
        ("激光雷达遥感", r"\b("
            r"lidar|laser\s*radar|"
            r"lidar\s*remote\s*sensing|lidar\s*observation|"
            r"lidar\s*profiling|doppler\s*lidar|"
            r"raman\s*lidar|rayleigh\s*lidar|"
            r"mie\s*lidar|differential\s*absorption\s*lidar|dial|"
            r"wind\s*lidar|aerosol\s*lidar|cloud\s*lidar|"
            r"lidar\s*retrieval|lidar\s*measurement|"
            r"lidar\s*ratio|depolarization\s*ratio|"
            r"high[- ]spectral[- ]resolution\s*lidar|hsrl|"
            r"ceilometer|micropulse\s*lidar"
            r")\b"),
        ("大气遥感探测", r"\b("
            r"atmospheric\s*remote\s*sensing|atmospheric\s*sounding|"
            r"remote\s*sensing\s*of\s*atmosphere|"
            r"atmospheric\s*composition\s*remote|"
            r"trace\s*gas\s*remote|trace\s*gas\s*retrieval|"
            r"atmospheric\s*profile\s*retrieval|"
            r"nadir\s*sounding|limb\s*sounding|"
            r"occultation|gnss\s*radio\s*occultation|"
            r"atmospheric\s*column\s*retrieval|"
            r"total\s*column\s*amount|column\s*density\s*retrieval|"
            r"hyperspectral\s*sounding|infrared\s*sounder|"
            r"microwave\s*sounder|microwave\s*radiometer|"
            r"remote\s*sensing\s*retrieval|"
            r"aerosol\s*retrieval|aerosol\s*profile|"
            r"water\s*vapor\s*retrieval|temperature\s*retrieval"
            r")\b"),
    ]
    
    import re
    tags: List[str] = []
    for tag_name, pattern in category_patterns:
        if re.search(pattern, text_lower):
            tags.append(tag_name)
    
    # 兜底
    if not tags:
        tags.append("其他")
    return tags[:5]

    # 最多 5 个，且去重
    return tags[:5]

def _ensure_tables(conn: sqlite3.Connection):
    cur = conn.cursor()
    # pending：失败条目与重试计数（不改变原有接口，新增表）
    cur.execute("""
    CREATE TABLE IF NOT EXISTS pending(
        uid TEXT PRIMARY KEY,
        feed_url TEXT,
        article_url TEXT,
        doi TEXT,
        pub_date TEXT,
        fail_count INTEGER DEFAULT 0,
        last_error TEXT,
        first_seen_at INTEGER,
        last_attempt_at INTEGER
    )
    """)
    # meta：保存全局元信息（例如本次运行完成时间）
    cur.execute("""
    CREATE TABLE IF NOT EXISTS meta(
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)
    # 提升并发与稳定性
    cur.execute("PRAGMA journal_mode=WAL;")
    cur.execute("PRAGMA busy_timeout=5000;")
    conn.commit()

def _insert_or_update_meta(conn: sqlite3.Connection, key: str, value: str):
    cur = conn.cursor()
    cur.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value))
    conn.commit()

def _record_pending(cur: sqlite3.Cursor, uid: str, feed_url: str, article_url: str,
                    doi: Optional[str], pub_date: Optional[str],
                    error_msg: str, first_seen_at: int):
    nowi = now_ts()
    cur.execute("""
        INSERT INTO pending(uid, feed_url, article_url, doi, pub_date, fail_count, last_error, first_seen_at, last_attempt_at)
        VALUES(?,?,?,?,?,?,?, ?,?)
        ON CONFLICT(uid) DO UPDATE SET
            fail_count = pending.fail_count + 1,
            last_error = excluded.last_error,
            last_attempt_at = excluded.last_attempt_at
    """, (uid, feed_url, article_url, doi, pub_date, 1, error_msg[:500], first_seen_at, nowi))

def _content_type_is_html(resp) -> bool:
    ctype = ""
    try:
        ctype = resp.headers.get("Content-Type", "")
    except Exception:
        pass
    return bool(TEXT_HTML_MIME_RE.search(ctype or ""))

# ----- 写入文章辅助函数 -----
def _write_article(cur: sqlite3.Cursor, record: dict, uid: str):
    """将 record 写入 articles 表（INSERT ON CONFLICT UPDATE）"""
    cur.execute(
        """INSERT INTO articles(uid, feed_url, journal, title_en, title_cn, type, pub_date, doi, article_url,
                                 abstract_en, abstract_cn, raw_jsonld, fetched_at, last_updated_at, topic_tag)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(uid) DO UPDATE SET
             journal=excluded.journal, title_en=excluded.title_en, title_cn=excluded.title_cn,
             type=excluded.type, pub_date=excluded.pub_date, doi=excluded.doi, article_url=excluded.article_url,
             abstract_en=excluded.abstract_en, abstract_cn=excluded.abstract_cn, raw_jsonld=excluded.raw_jsonld,
             last_updated_at=excluded.last_updated_at, topic_tag=excluded.topic_tag
        """,
        (uid, record.get("feed_url"), record["journal"], record["title_en"], record["title_cn"], record["type"],
         record["pub_date"], record["doi"], record["article_url"], record["abstract_en"],
         record["abstract_cn"], record["raw_jsonld"], record["fetched_at"], record["last_updated_at"], record["topic_tag"])
    )

def _guess_journal_from_feed(feed_url: str) -> str:
    """从 RSS feed URL 猜测期刊名称"""
    m = {
        "acp.copernicus.org": "Atmospheric Chemistry and Physics",
        "amt.copernicus.org": "Atmospheric Measurement Techniques",
        "gmd.copernicus.org": "Geoscientific Model Development",
        "essd.copernicus.org": "Earth System Science Data",
        "wcd.copernicus.org": "Weather and Climate Dynamics",
        "nhess.copernicus.org": "Natural Hazards and Earth System Sciences",
        "angeo.copernicus.org": "Annales Geophysicae",
        "tc.copernicus.org": "The Cryosphere",
        "21698996": "Journal of Geophysical Research: Atmospheres",
        "19448007": "Geophysical Research Letters",
        "1477870x": "Quarterly Journal of the Royal Meteorological Society",
        "00344257": "Remote Sensing of Environment",
        "nature.com/nature.rss": "Nature",
        "nature.com/nclimate.rss": "Nature Climate Change",
        "nature.com/ngeo.rss": "Nature Geoscience",
    }
    url_lower = feed_url.lower()
    for key, name in m.items():
        if key in url_lower:
            return name
    return feed_url.split("/")[2] or feed_url  # fallback: hostname

# -----------------------------
# UID 计算（改进：标准化 DOI/链接；hash 截断 24 hex）
# -----------------------------
def compute_uid(item: Dict[str, Any]) -> str:
    doi = _normalize_doi(item.get("doi"))
    if doi:
        return f"doi:{doi}"
    id_like = item.get("id_like")
    if id_like:
        return f"id:{id_like}"
    link = _normalize_link(item.get("link"))
    if link:
        return f"linkhash:{sha256(link)[:UID_HASH_HEX_LEN]}"
    # 最后兜底：对 item 的稳定序列化求 hash
    try:
        import json as _json
        payload = _json.dumps(item, sort_keys=True, ensure_ascii=False)
        h = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return f"hash:{h[:UID_HASH_HEX_LEN]}"
    except Exception:
        # 极端兜底（不建议到这里）
        h = hashlib.sha256(repr(item).encode("utf-8")).hexdigest()
        return f"hash:{h[:UID_HASH_HEX_LEN]}"

# -----------------------------
# 主流程
# -----------------------------
def run(config: Dict[str,Any]) -> int:
    feeds: List[str] = config.get("feeds") or []
    if not feeds:
        print("No feeds in config.json['feeds']", file=sys.stderr)
        return 0

    db_path = config.get("db", "./rss_state.db")
    http_timeout = int(config.get("http_timeout", DEFAULT_HTTP_TIMEOUT))
    sleep_between = float(config.get("sleep_between_fetches", DEFAULT_SLEEP_BETWEEN))
    do_translate = bool(config.get("translate_on_ingest", DEFAULT_TRANSLATE))
    max_items_per_feed = int(config.get("max_items_per_feed", DEFAULT_MAX_ITEMS_PER_FEED))
    per_domain_rps = float(config.get("per_domain_rps", DEFAULT_PER_DOMAIN_RPS))
    max_retries = int(config.get("max_retries", DEFAULT_MAX_RETRIES))
    backoff_base = float(config.get("backoff_base", DEFAULT_BACKOFF_BASE))
    backoff_factor = float(config.get("backoff_factor", DEFAULT_BACKOFF_FACTOR))

    # 将 RPS 注入统一重试器
    _with_retries.per_domain_rps = per_domain_rps  # type: ignore[attr-defined]

    conn = db_connect(db_path)
    _ensure_tables(conn)
    cur = conn.cursor()

    started_at = now_ts()
    inserted_or_updated = 0
    total_failures = 0

    try:
        for feed_url in feeds:
            # ---- 每个 feed 一个事务 ----
            try:
                cur.execute("BEGIN")
                # 条件获取（带限速与重试）
                row = cur.execute("SELECT etag,last_modified FROM feeds WHERE feed_url=?", (feed_url,)).fetchone()
                etag = row[0] if row else None
                last_mod = row[1] if row else None

                def _fetch_feed_call():
                    return fetch_feed(feed_url, etag, last_mod, timeout=http_timeout)

                status, headers, content = _with_retries(
                    _fetch_feed_call,
                    url_for_limit=feed_url,
                    max_retries=max_retries,
                    backoff_base=backoff_base,
                    backoff_factor=backoff_factor,
                )

                if status == 304:
                    cur.execute(
                        "INSERT OR REPLACE INTO feeds(feed_url, etag, last_modified, last_checked_at) VALUES (?,?,?,?)",
                        (feed_url, etag, last_mod, now_ts()))
                    items: List[Dict[str,Any]] = []
                else:
                    cur.execute(
                        "INSERT OR REPLACE INTO feeds(feed_url, etag, last_modified, last_checked_at) VALUES (?,?,?,?)",
                        (feed_url, headers.get("ETag"), headers.get("Last-Modified"), now_ts()))
                    # 冷启动/长 feed 防护：只取最新 N 条
                    parsed = parse_feed(content or b"") or []
                    if max_items_per_feed > 0 and len(parsed) > max_items_per_feed:
                        items = parsed[:max_items_per_feed]
                    else:
                        items = parsed

                # 新项去重
                new_items: List[Dict[str,Any]] = []
                for it in items:
                    uid = compute_uid(it)
                    seen = cur.execute("SELECT 1 FROM seen WHERE uid=?", (uid,)).fetchone()
                    if not seen:
                        it["_uid"] = uid
                        new_items.append(it)

                # 逐篇抓详情 + 分类 + 翻译 + 入库
                for it in new_items:
                    uid = it["_uid"]
                    link = _normalize_link(it.get("link") or "")
                    doi = _normalize_doi(it.get("doi"))
                    pub_date = it.get("pub_date")
                    first_seen = now_ts()

                    # 抓文章（带限速、重试、类型判断）
                    r = None
                    try:
                        def _fetch_article():
                            return fetch(link, HEADERS, timeout=http_timeout)
                        r = _with_retries(
                            _fetch_article,
                            url_for_limit=link,
                            max_retries=max_retries,
                            backoff_base=backoff_base,
                            backoff_factor=backoff_factor,
                        )
                        if not _content_type_is_html(r):
                            raise RuntimeError(f"Non-HTML content-type: {r.headers.get('Content-Type','')}")
                        html_text = r.text
                        fields = extract_article_fields(html_text, link or "")
                    except Exception as e:
                        # 文章页面抓取失败（如 Cloudflare 拦截），用 RSS 已有数据降级写入
                        # 记录 pending 供后续重试，但不跳过入库
                        _record_pending(cur, uid, feed_url, link or "", doi, pub_date, str(e), first_seen)
                        total_failures += 1
                        # 降级：用 RSS feed 提供的数据
                        rss_journal = _guess_journal_from_feed(feed_url)
                        topic_tags = classify_topics_multi(it.get("title") or "", it.get("abstract") or "", config)
                        topic_tags_json = json.dumps(topic_tags, ensure_ascii=False)
                        title_en = it.get("title")
                        abstract_en = it.get("abstract")
                        title_cn = None
                        abstract_cn = None
                        record = {
                            "journal": rss_journal,
                            "title_en": title_en,
                            "title_cn": title_cn,
                            "type": None,
                            "pub_date": pub_date,
                            "doi": doi,
                            "article_url": link or "",
                            "abstract_en": abstract_en,
                            "abstract_cn": abstract_cn,
                            "raw_jsonld": None,
                            "fetched_at": now_ts(),
                            "last_updated_at": now_ts(),
                            "topic_tag": topic_tags_json,
                            "feed_url": feed_url,
                        }
                        _write_article(cur, record, uid)
                        conn.commit()
                        time.sleep(sleep_between)
                        continue

                    # 多主题 tags（JSON 数组字符串入库）
                    topic_tags = classify_topics_multi(fields.get("title") or it.get("title") or "",
                                                    fields.get("abstract"), config)
                    topic_tags_json = json.dumps(topic_tags, ensure_ascii=False)

                    # 翻译（可选，入库阶段完成；失败不影响英文）
                    title_en = fields.get("title") or it.get("title")
                    abstract_en = fields.get("abstract")
                    title_cn: Optional[str] = None
                    abstract_cn: Optional[str] = None
                    if do_translate:
                        title_cn, abstract_cn = _safe_translate(title_en, abstract_en, config)

                    # 记录
                    record = {
                        "journal": fields.get("journal"),
                        "title_en": title_en,
                        "title_cn": title_cn,
                        "type": fields.get("type"),
                        "pub_date": fields.get("date_published") or pub_date,
                        "doi": fields.get("doi") or doi,
                        "article_url": fields.get("article_url") or (link or ""),
                        "abstract_en": abstract_en,
                        "abstract_cn": abstract_cn,
                        "raw_jsonld": fields.get("raw_jsonld"),
                        "fetched_at": now_ts(),
                        "last_updated_at": now_ts(),
                        "topic_tag": topic_tags_json,
                        "feed_url": feed_url,
                    }

                    _write_article(cur, record, uid)

                    # 成功后写 seen，并清理 pending（若存在）
                    cur.execute(
                        "INSERT OR IGNORE INTO seen(uid, feed_url, article_url, doi, pub_date, first_seen_at) VALUES (?,?,?,?,?,?)",
                        (uid, feed_url, record["article_url"], record["doi"], record["pub_date"], first_seen)
                    )
                    cur.execute("DELETE FROM pending WHERE uid=?", (uid,))
                    conn.commit()
                    inserted_or_updated += 1
                    time.sleep(sleep_between)

                # 提交本 feed 的事务
                conn.commit()

            except Exception as feed_err:
                # 回滚当前 feed 的事务，但不中断整个运行
                conn.rollback()
                print(f"[WARN] feed failed and rolled back: {feed_url} -> {feed_err}", file=sys.stderr)
                total_failures += 1
                # 仍然继续下一个 feed

        # 运行完成：写 runs_log & meta.last_global_update_at
        ended_at = now_ts()
        status_text = "ok" if total_failures == 0 else ("partial_ok" if inserted_or_updated > 0 else "error")
        log_run(conn, "rss_fetch_store", started_at, status_text, inserted_or_updated,
                f"feeds={len(feeds)} fails={total_failures}")
        _insert_or_update_meta(conn, "last_global_update_at", str(ended_at))

        return inserted_or_updated

    except Exception as e:
        # 致命异常：写 runs_log & meta
        log_run(conn, "rss_fetch_store", started_at, "error", inserted_or_updated, str(e))
        try:
            _insert_or_update_meta(conn, "last_global_update_at", str(now_ts()))
        except Exception:
            pass
        raise
    finally:
        conn.close()

def main():
    cfg_path = os.getenv("PIPELINE_CONFIG", "config.json")
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # 优先使用环境变量中的 API Key（安全：不写入配置文件）
    env_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY")
    if env_key and cfg.get("openai"):
        cfg["openai"]["api_key"] = env_key
    n = run(cfg)
    print(f"[OK] fetched/updated rows: {n}")

if __name__ == "__main__":
    main()
