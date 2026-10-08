# -*- coding: utf-8 -*-
"""
tw_market_data.py — 台股公開資料（給 DeskNotes 觀察股／候選名單用）

1. 股票代號表 tw_stock_list.json：代號 → 名稱、上市/上櫃、產業別、類型。
   可用 refresh_stock_list() 從證交所 ISIN 公開頁更新（要能連 isin.twse.com.tw）。
2. 文字／截圖辨識：parse_text_for_stocks() 從任意文字抓出代號與股票名稱；
   ocr_image_text() 用 rapidocr（或 pytesseract）把截圖變文字。
3. 市場資料：本益比、殖利率、股價淨值比、收盤價、月營收、除權息預告。
   - fetch_snapshot()：直接從證交所／櫃買中心 OpenAPI 抓（家裡網路）
   - fetch_relay(url)：從 GitHub Actions 轉存的 JSON 抓（公司網路擋財經網站時用）
   - backfill_pe()：補抓上市股票過去 N 個月的每日本益比（本益比位置要用）
   MarketStore 負責存檔、累積歷史、算本益比在歷史中的位置。
4. 當 GitHub Actions 轉存程式：
   python tw_market_data.py --relay 輸出資料夾 [--codes codes.txt] [--backfill-months 24]
"""

import json
import os
import re
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
STOCK_LIST_FILE = os.path.join(HERE, "tw_stock_list.json")

UA = {"User-Agent": "Mozilla/5.0 (DeskNotes stock watchlist)"}
TIMEOUT = 25

URLS = {
    "twse_pe": "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL",
    "twse_close": "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
    "twse_rev": "https://openapi.twse.com.tw/v1/opendata/t187ap05_L",
    "twse_exdiv": "https://openapi.twse.com.tw/v1/exchangeReport/TWT48U_ALL",
    "tpex_pe": "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis",
    "tpex_close": "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes",
    "tpex_rev": "https://www.tpex.org.tw/openapi/v1/mopsfe_t187ap05_O",
    "tpex_exdiv": "https://www.tpex.org.tw/openapi/v1/tpex_exright_prepost_schedule",
}
TWSE_PE_HIST = "https://www.twse.com.tw/rwd/zh/afterTrading/BWIBBU?date={ymd}&stockNo={code}&response=json"
ISIN_URLS = ["https://isin.twse.com.tw/isin/C_public.jsp?strMode=2",
             "https://isin.twse.com.tw/isin/C_public.jsp?strMode=4"]


# ─────────────────────────── 股票代號表 ───────────────────────────

_STOCKS = None
_NAME_RX = None
_NAME2CODE = None


def load_stock_list(path=STOCK_LIST_FILE):
    """回傳 {代號: (名稱, 市場, 產業別, 類型)}。"""
    global _STOCKS, _NAME_RX, _NAME2CODE
    if _STOCKS is not None:
        return _STOCKS
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        _STOCKS = {k: tuple(v) for k, v in (d.get("stocks") or {}).items()}
    except (OSError, ValueError):
        _STOCKS = {}
    _NAME_RX = _NAME2CODE = None
    return _STOCKS


def stock_info(code):
    """(名稱, 市場, 產業別, 類型) 或 None。"""
    return load_stock_list().get(str(code).upper())


def industry_of(code):
    s = stock_info(code)
    return s[2] if s else ""


def _name_index():
    global _NAME_RX, _NAME2CODE
    if _NAME_RX is None:
        stocks = load_stock_list()
        rank = {"股票": 0, "創新板": 1, "ETF": 2, "臺灣存託憑證(TDR)": 3}
        best = {}
        for code, (name, _m, _ind, typ) in stocks.items():
            nm = unicodedata.normalize("NFKC", name).strip()
            if len(nm) < 2:
                continue
            r = rank.get(typ, 9)
            if nm not in best or r < best[nm][0]:
                best[nm] = (r, code)
        _NAME2CODE = {k: v[1] for k, v in best.items()}
        names = sorted(_NAME2CODE, key=len, reverse=True)
        _NAME_RX = re.compile("|".join(re.escape(n) for n in names)) if names else None
    return _NAME_RX, _NAME2CODE


def refresh_stock_list(path=STOCK_LIST_FILE):
    """從證交所 ISIN 公開頁更新代號表（上市＋上櫃）。回傳筆數。"""
    import requests
    keep = {"股票", "ETF", "ETN", "創新板", "特別股", "臺灣存託憑證(TDR)", "受益證券-不動產投資信託"}
    out = {}
    for url, market in zip(ISIN_URLS, ("上市", "上櫃")):
        r = requests.get(url, headers=UA, timeout=60)
        r.encoding = "big5hkscs"
        typ = ""
        for tr in re.findall(r"<tr>(.*?)</tr>", r.text, re.S | re.I):
            cells = [re.sub(r"<[^>]+>", "", c).strip()
                     for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S | re.I)]
            if len(cells) == 1 or (len(cells) >= 1 and all(not c for c in cells[1:])):
                typ = cells[0].strip()
                continue
            if len(cells) < 6 or "　" not in cells[0]:
                continue
            code, name = cells[0].split("　", 1)
            if typ not in keep:
                continue
            ind = cells[4] or ("ETF" if typ in ("ETF", "ETN") else typ)
            out[code.strip()] = [name.strip(), market, ind, typ]
    if len(out) < 1000:
        raise RuntimeError(f"只抓到 {len(out)} 筆，可能被擋或頁面格式改了，未更新")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"source": "TWSE ISIN 公開資料", "updated": date.today().isoformat(), "stocks": out},
                  f, ensure_ascii=False, separators=(",", ":"))
    global _STOCKS
    _STOCKS = None
    load_stock_list(path)
    return len(out)


# ─────────────────────────── 文字／截圖辨識 ───────────────────────────

_CODE_TOKEN = re.compile(r"(?<![0-9A-Za-z.,/*\-])([0-9]{4,6}[A-Z]?)(?![0-9])")
_SEP = set("、，,;；：:（）()[]【】「」/|·．。 \t")
_CJK = re.compile(r"[一-鿿]")
_NOT_CODE_TAIL = set("年月日季週元億萬倍")


def _fuzzy_name_near(line, m, name):
    """代號前後緊鄰的那一段文字跟正式名稱像不像（OCR 常把 貿聯 認成 贸聊）。"""
    import difflib
    left = re.split(r"[\s|]+", line[:m.start()].strip())[-1:] or [""]
    right = re.split(r"[\s|]+", line[m.end():].strip())[:1] or [""]
    for tok in (left[0], right[0]):
        tok = re.sub(r"[0-9,.+\-%]+$", "", tok) if not tok.endswith("-KY") else tok
        if len(tok) >= 2 and _CJK.search(tok) and difflib.SequenceMatcher(None, tok, name).ratio() >= 0.5:
            return True
    return False


def parse_text_for_stocks(text):
    """從任意文字（貼上的文章、表格、OCR 結果）找出台股。
    回傳 [{"code","name","industry","conf": "high"|"low","why"}]，依出現順序、代號不重複。
    high：代號＋名稱同一行、或代號緊貼中文、或整行只有代號；low：只對到名稱（2 個字的名稱容易誤判）。"""
    stocks = load_stock_list()
    text = unicodedata.normalize("NFKC", text or "")
    found = {}
    order = []

    def add(code, conf, why):
        info = stocks.get(code)
        if not info:
            return
        if code in found:
            if conf == "high" and found[code]["conf"] != "high":
                found[code].update(conf="high", why=why)
            return
        found[code] = {"code": code, "name": info[0], "industry": info[2], "conf": conf, "why": why}
        order.append(code)

    name_rx, name2code = _name_index()
    alias = {}           # 代號旁邊實際出現的字（OCR 結果）→ 代號；同一份文字別處出現同樣的字就以這個代號為準
    by_name = {}         # 只靠名稱比對到的：代號 → 出現的字
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        codes_here = []
        for m in _CODE_TOKEN.finditer(line):
            code = m.group(1).upper()
            tail = line[m.end():m.end() + 2]
            if tail.startswith(".") and tail[1:2].isdigit():
                continue                    # 1215.00 這種是價格
            if tail.startswith("%") or (tail[:1] and tail[0] in _NOT_CODE_TAIL):
                continue                    # 2027年、500億、3倍…不是代號
            if code not in stocks:
                continue
            codes_here.append((code, m))
        names_here = []
        if name_rx is not None:
            names_here = [(mm.group(0), mm) for mm in name_rx.finditer(line)]
        only_codes = bool(codes_here) and not re.sub(r"[0-9A-Z\s,，、;；/|.()（）-]", "", line.upper())
        for code, m in codes_here:
            info = stocks[code]
            for tok in (re.split(r"[\s|]+", line[:m.start()].strip())[-1:] +
                        re.split(r"[\s|]+", line[m.end():].strip())[:1]):
                if tok and _CJK.search(tok):
                    alias.setdefault(tok, code)
            near = line[max(0, m.start() - 3):m.end() + 3]
            if info[0] in line or (len(info[0]) >= 3 and info[0][:2] in line):
                add(code, "high", "代號＋名稱")
            elif _fuzzy_name_near(line, m, info[0]):
                add(code, "high", "代號＋相似名稱")
            elif _CJK.search(near):
                add(code, "high", "代號旁有中文")
            elif only_codes:
                add(code, "high", "代號清單")
            else:
                add(code, "low", "只有代號")
        code_spans = [(m.start(), m.end(), c) for c, m in codes_here]
        for nm, mm in names_here:
            code = name2code.get(nm)
            if not code or code in found:
                continue
            # 名稱緊貼著另一個代號（「台耀 6274」其實是 台燿 6274）→ 以代號為準，名稱不另外算
            if any(c != code and (abs(mm.end() - a) <= 3 or abs(mm.start() - b) <= 3) for a, b, c in code_spans):
                continue
            before = line[mm.start() - 1] if mm.start() > 0 else " "
            after = line[mm.end()] if mm.end() < len(line) else " "
            bounded = (before in _SEP or not _CJK.match(before)) and (after in _SEP or not _CJK.match(after))
            strong = len(nm) >= 3 or len(line) <= 12 or bounded
            add(code, "high" if strong else "low", "名稱" if strong else "只有名稱（2 字）")
            by_name.setdefault(code, nm)
    for code, nm in by_name.items():
        other = alias.get(nm)
        if other and other != code and other in found and found[code]["why"].startswith(("名稱", "只有名稱")):
            found.pop(code, None)
            order.remove(code)
    return [found[c] for c in order]


def ocr_image_text(img):
    """截圖 → 文字（每行一列）。先用 rapidocr，沒有再用 pytesseract。"""
    img = img.convert("RGB")
    if img.width < 1200:
        k = 1200 / img.width
        img = img.resize((int(img.width * k), int(img.height * k)))
    try:
        import numpy as np
        try:
            from rapidocr_onnxruntime import RapidOCR
            res, _ = RapidOCR()(np.array(img))
            items = [(b, t) for b, t, _s in (res or [])]
        except ImportError:
            from rapidocr import RapidOCR
            out = RapidOCR()(np.array(img))
            items = list(zip(getattr(out, "boxes", []) or [], getattr(out, "txts", []) or []))
        rows = []
        for box, t in items:
            ys = [p[1] for p in box]
            xs = [p[0] for p in box]
            rows.append(((min(ys) + max(ys)) / 2, max(ys) - min(ys), min(xs), str(t)))
        if not rows:
            return ""
        hs = sorted(r[1] for r in rows)
        tol = max(6, hs[len(hs) // 2] * 0.6)
        rows.sort(key=lambda r: r[0])
        lines, cur = [], []
        for r in rows:
            if cur and abs(r[0] - sum(c[0] for c in cur) / len(cur)) > tol:
                lines.append(cur)
                cur = []
            cur.append(r)
        if cur:
            lines.append(cur)
        return "\n".join(" ".join(c[3] for c in sorted(ln, key=lambda c: c[2])) for ln in lines)
    except ImportError:
        pass
    try:
        import pytesseract
        return pytesseract.image_to_string(img, lang="chi_tra+eng")
    except ImportError:
        raise RuntimeError("沒有 OCR 套件。請安裝其中一個：\npip install rapidocr_onnxruntime\n（或 pytesseract＋Tesseract）")


# ─────────────────────────── 抓資料 ───────────────────────────

def _num(v):
    if v is None:
        return None
    s = str(v).replace(",", "").replace("%", "").strip()
    if s in ("", "-", "--", "N/A", "X"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _pick(row, *keys, contains=()):
    for k in keys:
        if k in row and row[k] not in (None, ""):
            return row[k]
    for sub in contains:
        for k, v in row.items():
            if sub in k and v not in (None, ""):
                return v
    return None


def roc_to_date(s):
    """1151016 / 115/10/16 / 115年10月16日 / 20261016 / 2026-10-16 → date。"""
    if not s:
        return None
    s = str(s).strip()
    m = re.match(r"^(\d{2,3})[/年.-]?(\d{1,2})[/月.-]?(\d{1,2})日?$", s)
    if m and len(m.group(1)) <= 3 and int(m.group(1)) < 1000:
        y = int(m.group(1))
        y = y + 1911 if y < 1000 else y
        try:
            return date(y, int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.match(r"^(\d{4})[/.-]?(\d{1,2})[/.-]?(\d{1,2})$", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def roc_ym(s):
    """11509 / 115/09 → '2026-09'。"""
    s = re.sub(r"\D", "", str(s or ""))
    if len(s) in (5, 4) and len(s) - 2 >= 2:
        y, m = int(s[:-2]), int(s[-2:])
        if y < 1000:
            y += 1911
        if 1 <= m <= 12:
            return f"{y:04d}-{m:02d}"
    if len(s) == 6:
        return f"{s[:4]}-{s[4:]}"
    return None


URLS.update({
    "twse_index": "https://openapi.twse.com.tw/v1/exchangeReport/MI_INDEX",
    # 同一份資料有些來源換過網址：依序試，第一個抓得到就用
    "tpex_close": ["https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes",
                   "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes"],
    "tpex_rev": ["https://www.tpex.org.tw/openapi/v1/mopsfe_t187ap05_O",
                 "https://www.tpex.org.tw/openapi/v1/t187ap05_O"],
})
_DIAG = {}      # 每個來源最近一次的抓取結果（存進 market_cache.json 的 _diag，方便除錯）


def _diag(name, **kw):
    _DIAG[name] = dict(kw, at=datetime.now().strftime("%m-%d %H:%M"))


def _sample(rows):
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        r = rows[0]
        return {"keys": list(r.keys())[:25], "first": {k: str(v)[:20] for k, v in list(r.items())[:12]}}
    if isinstance(rows, dict):
        return {"keys": list(rows.keys())[:25]}
    return {"type": type(rows).__name__}
TWSE_PX_HIST = "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY?date={ymd}&stockNo={code}&response=json"
TPEX_PX_HIST = ("https://www.tpex.org.tw/web/stock/aftertrading/daily_trading_info/st43_result.php"
                "?l=zh-tw&d={roc_ym}&stkno={code}")
TWSE_MONTH_STAT = "https://www.twse.com.tw/rwd/zh/afterTrading/FMSRFK?date={y}0101&stockNo={code}&response=json"
TWSE_INDEX_HIST = "https://www.twse.com.tw/rwd/zh/indicesReport/MI_5MINS_HIST?date={ymd}&response=json"
TWSE_T86 = "https://www.twse.com.tw/rwd/zh/fund/T86?date={ymd}&selectType=ALLBUT0999&response=json"
TPEX_T86 = ("https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php"
            "?l=zh-tw&se=EW&t=D&d={roc}&o=json")
TWSE_MARGIN = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date={ymd}&selectType=ALL&response=json"
TDCC_URL = "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5"
INDEX_KEY = "^TAIEX"


def _get_json(url, session=None):
    import requests
    s = session or requests
    r = s.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _tables(j):
    """證交所／櫃買的舊式 JSON：{"fields":[...],"data":[...]} 或 {"tables":[{"fields","data"}]}
    或 {"aaData":[...]}，統一回傳 [(fields, rows)]。"""
    out = []
    if isinstance(j, dict):
        if j.get("fields") and j.get("data") is not None:
            out.append((j["fields"], j["data"]))
        for t in j.get("tables") or []:
            if isinstance(t, dict) and t.get("fields") and t.get("data") is not None:
                out.append((t["fields"], t["data"]))
        if j.get("aaData") is not None:
            out.append((j.get("fields") or [], j["aaData"]))
    return out


def _idx(fields, *subs, nth=0, exclude=()):
    hits = [i for i, f in enumerate(fields)
            if all(s in str(f) for s in subs) and not any(x in str(f) for x in exclude)]
    return hits[nth] if len(hits) > nth else None


def _roc(d):
    return f"{d.year - 1911}/{d.month:02d}/{d.day:02d}"


def _recent_weekdays(n=6):
    d = date.today()
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out


# ─────────────────────────── 籌碼 ───────────────────────────

def fetch_chips_day(d, sess=None):
    """某一天的三大法人買賣超（張）＋上市融資融券餘額（張）。回傳 {code: {...}}，該日沒資料回傳 {}。"""
    out = {}
    try:
        j = _get_json(TWSE_T86.format(ymd=d.strftime("%Y%m%d")), sess)
        for fields, rows in _tables(j):
            ic = _idx(fields, "代號")
            ifo = _idx(fields, "外", "買賣超")          # 第一個是「外陸資買賣超股數(不含外資自營商)」
            itr = _idx(fields, "投信", "買賣超")
            ide = _idx(fields, "自營商", "買賣超", exclude=("外",))
            for r in rows:
                if ic is None:
                    break
                code = str(r[ic]).strip()
                out.setdefault(code, {})
                for key, ix in (("foreign", ifo), ("trust", itr), ("dealer", ide)):
                    v = _num(r[ix]) if ix is not None and ix < len(r) else None
                    if v is not None:
                        out[code][key] = round(v / 1000, 1)
    except Exception:
        pass
    n_twse = len(out)
    _diag("twse_t86", ok=n_twse > 0, date=d.isoformat(), parsed=n_twse)
    try:
        j = _get_json(TPEX_T86.format(roc=_roc(d)), sess)
        _diag("tpex_t86", ok=False, date=d.isoformat(), **(_sample(j) if isinstance(j, dict) else {}),
              tables=[(list(f)[:20], len(rws)) for f, rws in _tables(j)][:3])
        for fields, rows in _tables(j):
            if not fields:
                continue
            ic = _idx(fields, "代號")
            ifo = _idx(fields, "外", "買賣超")
            itr = _idx(fields, "投信", "買賣超")
            ide = _idx(fields, "自營", "買賣超", exclude=("外",))
            for r in rows:
                if ic is None:
                    break
                code = str(r[ic]).strip()
                out.setdefault(code, {})
                for key, ix in (("foreign", ifo), ("trust", itr), ("dealer", ide)):
                    v = _num(r[ix]) if ix is not None and ix < len(r) else None
                    if v is not None:
                        out[code][key] = round(v / 1000, 1)
        if len(out) > n_twse:
            _DIAG["tpex_t86"]["ok"] = True
            _DIAG["tpex_t86"]["parsed"] = len(out) - n_twse
    except Exception as e:
        _diag("tpex_t86", ok=False, date=d.isoformat(), err=str(e)[:200])
    try:
        j = _get_json(TWSE_MARGIN.format(ymd=d.strftime("%Y%m%d")), sess)
        for fields, rows in _tables(j):
            ic = _idx(fields, "代號")
            ib = _idx(fields, "今日餘額", nth=0)
            ish = _idx(fields, "今日餘額", nth=1)
            if ic is None or ib is None:
                continue
            for r in rows:
                code = str(r[ic]).strip()
                o = out.setdefault(code, {})
                o["margin"] = _num(r[ib])
                if ish is not None and ish < len(r):
                    o["short"] = _num(r[ish])
    except Exception:
        pass
    return {k: v for k, v in out.items() if v}


def fetch_tdcc(sess=None):
    """集保股權分散表（每週）：{code: {"date","big1000","big400","holders"}}；百分比是占集保庫存比例。"""
    import csv
    import io
    import requests
    s = sess or requests
    r = s.get(TDCC_URL, headers=UA, timeout=90)
    r.raise_for_status()
    raw = r.content
    try:
        txt = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        txt = raw.decode("big5", errors="ignore")
    out = {}
    nrows = 0
    for row in csv.reader(io.StringIO(txt)):
        row = [x.strip().strip('"') for x in row]
        nrows += 1
        if len(row) < 6 or not row[1] or not row[2].isdigit():
            continue
        dd, code, lvl, people, _shares, pct = [x.strip() for x in row[:6]]
        lvl = int(lvl)
        o = out.setdefault(code, {"date": roc_to_date(dd).isoformat() if roc_to_date(dd) else dd,
                                  "big1000": 0.0, "big400": 0.0, "holders": None})
        p = _num(pct) or 0.0
        if lvl == 15:
            o["big1000"] += p
        if 12 <= lvl <= 15:
            o["big400"] += p
        if lvl == 17:
            o["holders"] = _num(people)
    _diag("tdcc", ok=bool(out), bytes=len(raw), rows=nrows, parsed=len(out), head=txt[:300],
          ctype=r.headers.get("content-type"))
    return out


# ─────────────────────────── 每日快照 ───────────────────────────

def fetch_snapshot(progress=None, chips=True, raw_dir=None):
    """直接抓公開資料，回傳 (snapshot, errors)。snapshot = {
        "date", "pe": {code: {"pe","yield","pb"}}, "close": {code: price}, "index": 加權指數收盤,
        "rev": {code: {...}}, "exdiv": {code: [...]}, "chips": {code: {"foreign","trust","dealer","margin","short"}},
        "chips_date", "tdcc": {code: {...}}}"""
    import requests
    sess = requests.Session()
    snap = {"date": date.today().isoformat(), "pe": {}, "close": {}, "chg": {}, "vol": {}, "rev": {}, "exdiv": {},
            "_src": {}}
    errors = []
    if raw_dir:
        try:
            os.makedirs(raw_dir, exist_ok=True)
        except OSError:
            raw_dir = None

    def keep_raw(key, url, rows, got):
        """原始回應存檔（market_raw/日期/來源.json），之後可以從「📄 資料來源」打開對照。"""
        src = {"url": url, "n": got, "at": datetime.now().strftime("%Y-%m-%d %H:%M")}
        if raw_dir:
            fp = os.path.join(raw_dir, key + ".json")
            try:
                with open(fp, "w", encoding="utf-8") as f:
                    json.dump(rows, f, ensure_ascii=False, indent=0)
                src["file"] = fp
            except OSError:
                pass
        snap["_src"][key] = src

    def step(key, fn):
        if progress:
            progress(key)
        urls = URLS[key] if isinstance(URLS[key], list) else [URLS[key]]
        last = None
        for url in urls:
            try:
                rows = _get_json(url, sess)
                before = sum(len(snap[k]) for k in ("pe", "close", "rev", "exdiv"))
                fn(rows)
                got = sum(len(snap[k]) for k in ("pe", "close", "rev", "exdiv")) - before
                _diag(key, ok=got > 0, url=url, n=len(rows) if isinstance(rows, list) else None, parsed=got,
                      **_sample(rows))
                if got > 0:
                    keep_raw(key, url, rows, got)
                if got > 0 or url == urls[-1]:
                    return
            except Exception as e:     # 單一來源失敗不影響其他
                last = e
                _diag(key, ok=False, url=url, err=str(e)[:200])
        if last is not None:
            errors.append(f"{key}: {last}")

    def pe_twse(rows):
        for r in rows:
            c = _pick(r, "Code", contains=("代號",))
            if not c:
                continue
            snap["pe"][str(c).strip()] = {
                "pe": _num(_pick(r, "PEratio", contains=("本益比",))),
                "yield": _num(_pick(r, "DividendYield", contains=("殖利率",))),
                "pb": _num(_pick(r, "PBratio", contains=("淨值比",)))}
            d = roc_to_date(_pick(r, "Date", contains=("日期",)))
            if d:
                snap["date"] = d.isoformat()

    def pe_tpex(rows):
        for r in rows:
            c = _pick(r, "SecuritiesCompanyCode", "Code", contains=("代號",))
            if not c:
                continue
            snap["pe"][str(c).strip()] = {
                "pe": _num(_pick(r, "PriceEarningRatio", contains=("本益比", "EarningRatio"))),
                "yield": _num(_pick(r, "YieldRatio", contains=("殖利率", "Yield"))),
                "pb": _num(_pick(r, "PriceBookRatio", contains=("淨值比", "BookRatio")))}

    def close_twse(rows):
        for r in rows:
            c = _pick(r, "Code", contains=("代號",))
            p = _num(_pick(r, "ClosingPrice", contains=("收盤",)))
            if c and p:
                snap["close"][str(c).strip()] = p
                ch = _num(_pick(r, "Change", contains=("漲跌",)))
                if ch is not None:
                    snap["chg"][str(c).strip()] = ch
                vo = _num(_pick(r, "TradeVolume", contains=("成交股數",)))
                if vo is not None:
                    snap["vol"][str(c).strip()] = round(vo / 1000)

    def close_tpex(rows):
        for r in rows:
            c = _pick(r, "SecuritiesCompanyCode", "Code", contains=("代號",))
            p = _num(_pick(r, "Close", "ClosingPrice", contains=("收盤", "Close")))
            if c and p:
                snap["close"][str(c).strip()] = p
                ch = _num(_pick(r, "Change", contains=("漲跌",)))
                if ch is not None:
                    snap["chg"][str(c).strip()] = ch
                vo = _num(_pick(r, "TradingShares", "TradeVolume", contains=("成交股數",)))
                if vo is not None:
                    snap["vol"][str(c).strip()] = round(vo / 1000)

    def index_twse(rows):
        for r in rows:
            name = str(_pick(r, "指數", "Index", contains=("指數",)) or "")
            if "發行量加權股價指數" in name and "報酬" not in name:
                v = _num(_pick(r, "收盤指數", "CloseIndex", contains=("收盤",)))
                if v:
                    snap["index"] = v
                break

    def rev(rows):
        for r in rows:
            c = _pick(r, "公司代號", contains=("代號",))
            if not c:
                continue
            snap["rev"][str(c).strip()] = {
                "ym": roc_ym(_pick(r, "資料年月", contains=("年月",))),
                "rev": _num(_pick(r, "營業收入-當月營收", contains=("當月營收",))),
                "prev": _num(_pick(r, "營業收入-上月營收", contains=("上月營收",))),
                "last_year": _num(_pick(r, "營業收入-去年當月營收", contains=("去年當月營收",))),
                "mom": _num(_pick(r, "營業收入-上月比較增減(%)", contains=("上月比較增減",))),
                "yoy": _num(_pick(r, "營業收入-去年同月增減(%)", contains=("去年同月增減",))),
                "cum_yoy": _num(_pick(r, "累計營業收入-前期比較增減(%)", contains=("前期比較增減",))),
            }

    def exdiv(rows):
        for r in rows:
            c = _pick(r, "Code", "SecuritiesCompanyCode", contains=("代號",))
            d = roc_to_date(_pick(r, "Date", "ExRightsExDividendDate", contains=("日期", "Date")))
            if not c or not d:
                continue
            kind = str(_pick(r, "Exdividend", "ExRightsExDividend", contains=("權/息", "除權息", "Ex")) or "").strip()
            if kind in ("息", "權", "權息"):
                kind = "除" + kind
            item = {"date": d.isoformat(), "kind": kind or "除權息",
                    "cash": _num(_pick(r, "CashDividend", contains=("現金股利", "現金"))),
                    "stock": _num(_pick(r, "StockDividendRatio", contains=("無償配股", "股票股利")))}
            lst = snap["exdiv"].setdefault(str(c).strip(), [])
            if item not in lst:
                lst.append(item)

    step("twse_pe", pe_twse)
    step("tpex_pe", pe_tpex)
    step("twse_close", close_twse)
    step("tpex_close", close_tpex)
    step("twse_index", index_twse)
    step("twse_rev", rev)
    step("tpex_rev", rev)
    step("twse_exdiv", exdiv)
    step("tpex_exdiv", exdiv)
    if chips:
        if progress:
            progress("籌碼")
        for d in _recent_weekdays(6):
            ch = fetch_chips_day(d, sess)
            if ch:
                snap["chips"] = ch
                snap["chips_date"] = d.isoformat()
                snap["_src"]["chips"] = {"url": TWSE_T86.format(ymd=d.strftime("%Y%m%d")), "n": len(ch),
                                         "at": datetime.now().strftime("%Y-%m-%d %H:%M")}
                break
        else:
            errors.append("籌碼：近幾天都抓不到")
        if progress:
            progress("集保大戶")
        try:
            snap["tdcc"] = fetch_tdcc(sess)
            snap["_src"]["tdcc"] = {"url": TDCC_URL, "n": len(snap["tdcc"]),
                                    "at": datetime.now().strftime("%Y-%m-%d %H:%M")}
        except Exception as e:
            errors.append(f"集保：{e}")
    snap["_diag"] = dict(_DIAG)
    if not (snap["pe"] or snap["close"] or snap["rev"]):
        raise RuntimeError("全部來源都抓不到（網路被擋？）\n" + "\n".join(errors[:8]))
    return snap, errors


# ─────────────────────────── 官方查詢頁（人看的版本） ───────────────────────────

def official_pages(code, sec, d=None):
    """回傳 [(說明, 網址)]：證交所／櫃買／公開資訊觀測站上可以直接看到這筆資料的頁面。"""
    info = stock_info(code) or ("", "", "", "")
    listed = info[1] == "上市"
    try:
        d = date.fromisoformat(d) if isinstance(d, str) else (d or date.today())
    except ValueError:
        d = date.today()
    ymd = d.strftime("%Y%m%d")
    ym1 = d.strftime("%Y%m01")
    out = []
    if sec in ("pe", "val"):
        if listed:
            out.append(("證交所 個股日本益比、殖利率及股價淨值比（當月）",
                        f"https://www.twse.com.tw/rwd/zh/afterTrading/BWIBBU?date={ym1}&stockNo={code}&response=html"))
            out.append(("證交所 全部上市本益比（當日）",
                        f"https://www.twse.com.tw/rwd/zh/afterTrading/BWIBBU_d?date={ymd}&selectType=ALL&response=html"))
        else:
            out.append(("櫃買中心 上櫃股票本益比、殖利率及股價淨值比",
                        "https://www.tpex.org.tw/zh-tw/mainboard/trading/info/pe-ratio.html"))
    elif sec in ("close", "rs", "tech"):
        if listed:
            out.append(("證交所 個股日成交資訊（當月）",
                        f"https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY?date={ym1}&stockNo={code}&response=html"))
            if sec == "rs":
                out.append(("證交所 加權指數歷史（當月）",
                            f"https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK?date={ym1}&response=html"))
        else:
            out.append(("櫃買中心 個股日成交資訊",
                        "https://www.tpex.org.tw/zh-tw/mainboard/trading/info/stock-pricing.html"))
    elif sec == "rev":
        m = d.month - 1 or 12
        y = d.year - (1 if d.month == 1 else 0)
        out.append(("公開資訊觀測站 每月營收彙總表（" + ("上市" if listed else "上櫃") + f" {y - 1911}/{m}）",
                    f"https://mopsov.twse.com.tw/nas/t21/{'sii' if listed else 'otc'}/t21sc03_{y - 1911}_{m}_0.html"))
        out.append(("公開資訊觀測站 個股月營收查詢", "https://mops.twse.com.tw/mops/#/web/t05st10_ifrs"))
    elif sec == "chips":
        if listed:
            out.append(("證交所 三大法人買賣超日報",
                        f"https://www.twse.com.tw/rwd/zh/fund/T86?date={ymd}&selectType=ALLBUT0999&response=html"))
            out.append(("證交所 融資融券餘額",
                        f"https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date={ymd}&selectType=ALL&response=html"))
        else:
            out.append(("櫃買中心 三大法人買賣明細", "https://www.tpex.org.tw/zh-tw/mainboard/trading/major-institutional/detail/day.html"))
    elif sec == "exdiv":
        out.append(("證交所 除權除息預告表", "https://www.twse.com.tw/zh/announcement/ex-right/twt48u.html") if listed else
                   ("櫃買中心 除權除息預告", "https://www.tpex.org.tw/zh-tw/mainboard/trading/exright/forecast.html"))
    elif sec == "tdcc":
        out.append(("集保結算所 集保戶股權分散表", "https://www.tdcc.com.tw/portal/zh/smWeb/qryStock"))
    suffix = ".TW" if listed else ".TWO"
    out.append(("Yahoo 股市（對照用，非官方）", f"https://tw.stock.yahoo.com/quote/{code}{suffix}"))
    return out


# ─────────────────────────── 補抓歷史 ───────────────────────────

def _months_back(n):
    d = date.today().replace(day=1)
    for _ in range(n):
        yield d
        d = (d - timedelta(days=1)).replace(day=1)


def backfill_pe(codes, months=24, progress=None, sleep=2.5):
    """上市股票過去 N 個月每日 [日期, 本益比, 殖利率, 股價淨值比]。"""
    import requests
    sess = requests.Session()
    stocks = load_stock_list()
    out = {}
    todo = [c for c in codes if (stocks.get(c) or ("", ""))[1] == "上市"]
    for k, code in enumerate(todo):
        rows = []
        for d in _months_back(months):
            if progress:
                progress(f"本益比 {code} {d.strftime('%Y-%m')}（{k + 1}/{len(todo)}）")
            try:
                j = _get_json(TWSE_PE_HIST.format(ymd=d.strftime("%Y%m01"), code=code), sess)
                for fields, data in _tables(j):
                    ix_d, ix_pe = _idx(fields, "日期"), _idx(fields, "本益比")
                    ix_y, ix_pb = _idx(fields, "殖利率"), _idx(fields, "淨值比")
                    for r in data:
                        dd = roc_to_date(r[ix_d]) if ix_d is not None else None
                        pe = _num(r[ix_pe]) if ix_pe is not None else None
                        if dd:
                            rows.append([dd.isoformat(), pe,
                                         _num(r[ix_y]) if ix_y is not None else None,
                                         _num(r[ix_pb]) if ix_pb is not None else None])
            except Exception:
                pass
            time.sleep(sleep)
        if rows:
            out[code] = sorted(rows)
    return out


def backfill_prices(codes, months=13, progress=None, sleep=2.5, index=True, vol_out=None):
    """每日收盤價 {code: [[日期, 收盤], ...]}；上市用證交所、上櫃用櫃買，另含加權指數（^TAIEX）。
    vol_out：傳一個 dict 進來，會順便填成交量 {code: [[日期, 張], ...]}。"""
    import requests
    sess = requests.Session()
    stocks = load_stock_list()
    out = {}
    jobs = list(codes) + ([INDEX_KEY] if index else [])
    for k, code in enumerate(jobs):
        mkt = "指數" if code == INDEX_KEY else (stocks.get(code) or ("", ""))[1]
        rows, vrows = [], []
        for d in _months_back(months):
            if progress:
                progress(f"股價 {code} {d.strftime('%Y-%m')}（{k + 1}/{len(jobs)}）")
            try:
                if mkt == "指數":
                    j = _get_json(TWSE_INDEX_HIST.format(ymd=d.strftime("%Y%m01")), sess)
                    key = "收盤指數"
                elif mkt == "上市":
                    j = _get_json(TWSE_PX_HIST.format(ymd=d.strftime("%Y%m01"), code=code), sess)
                    key = "收盤價"
                elif mkt == "上櫃":
                    j = _get_json(TPEX_PX_HIST.format(roc_ym=f"{d.year - 1911}/{d.month:02d}", code=code), sess)
                    key = "收盤"
                else:
                    break
                tabs = _tables(j)
                for fields, data in tabs:
                    ix_d = _idx(fields, "日期") if fields else 0
                    ix_c = _idx(fields, key) if fields else 6
                    if ix_d is None or ix_c is None:
                        continue
                    ix_v, vdiv = None, 1
                    if mkt != "指數":
                        if fields and _idx(fields, "成交股數") is not None:
                            ix_v, vdiv = _idx(fields, "成交股數"), 1000
                        elif fields:
                            ix_v = _idx(fields, "仟股")          # 櫃買：成交仟股 = 張
                        else:
                            ix_v = 1
                    for r in data:
                        dd = roc_to_date(str(r[ix_d]).replace("＊", "").strip())
                        c = _num(r[ix_c]) if ix_c < len(r) else None
                        if dd and c:
                            rows.append([dd.isoformat(), c])
                            v = _num(r[ix_v]) if ix_v is not None and ix_v < len(r) else None
                            if v is not None:
                                vrows.append([dd.isoformat(), round(v / vdiv)])
            except Exception:
                pass
            time.sleep(sleep)
        if rows:
            out[code] = sorted({r[0]: r for r in rows}.values())
        if vrows and vol_out is not None:
            vol_out[code] = sorted({r[0]: r for r in vrows}.values())
    return out


def fetch_monthly_avg(code, years=10, progress=None, sleep=2.5):
    """月均價 [[YYYY-MM, 均價]]（季節性分析用）。上市：證交所個股月成交資訊（一年一次請求）；
    上櫃：櫃買日成交逐月抓（最多 3 年）。"""
    import requests
    sess = requests.Session()
    mkt = (stock_info(code) or ("", ""))[1]
    out = {}
    this_y = date.today().year
    if mkt == "上市":
        for y in range(this_y - years + 1, this_y + 1):
            if progress:
                progress(f"月均價 {code} {y}")
            try:
                j = _get_json(TWSE_MONTH_STAT.format(y=y, code=code), sess)
                for fields, rows in _tables(j):
                    iy, im = _idx(fields, "年度"), _idx(fields, "月份")
                    ia = _idx(fields, "平均價")
                    if im is None or ia is None:
                        continue
                    for r in rows:
                        try:
                            yy = int(str(r[iy]).strip()) + 1911 if iy is not None else y
                            mm = int(str(r[im]).strip())
                        except ValueError:
                            continue
                        v = _num(r[ia])
                        if v:
                            out[f"{yy:04d}-{mm:02d}"] = v
            except Exception:
                pass
            time.sleep(sleep)
    elif mkt == "上櫃":
        for d in _months_back(min(36, years * 12)):
            if progress:
                progress(f"月均價 {code} {d.strftime('%Y-%m')}")
            try:
                j = _get_json(TPEX_PX_HIST.format(roc_ym=f"{d.year - 1911}/{d.month:02d}", code=code), sess)
                vals = []
                for fields, data in _tables(j):
                    ix_c = _idx(fields, "收盤") if fields else 6
                    if ix_c is None:
                        continue
                    vals += [v for v in (_num(r[ix_c]) for r in data if ix_c < len(r)) if v]
                if vals:
                    out[d.strftime("%Y-%m")] = round(sum(vals) / len(vals), 2)
            except Exception:
                pass
            time.sleep(sleep)
    return sorted([k, v] for k, v in out.items())


def backfill_chips(days=20, progress=None, sleep=2.5):
    """近 N 個交易日的三大法人＋融資融券 → {date: {code: {...}}}。"""
    import requests
    sess = requests.Session()
    out = {}
    d = date.today()
    tried = 0
    while len(out) < days and tried < days * 2:
        if d.weekday() < 5:
            tried += 1
            if progress:
                progress(f"籌碼 {d.isoformat()}（{len(out)}/{days}）")
            ch = fetch_chips_day(d, sess)
            if ch:
                out[d.isoformat()] = ch
            time.sleep(sleep)
        d -= timedelta(days=1)
    return out


def fetch_relay(base_url):
    """從 GitHub Actions 轉存的資料夾抓 market.json＋history.json。"""
    base = base_url.rstrip("/") + "/"
    snap = _get_json(base + "market.json")
    try:
        hist = _get_json(base + "history.json")
    except Exception:
        hist = {}
    try:
        snap["_fin"] = _get_json(base + "fin.json")
    except Exception:
        pass
    try:
        snap["_etf"] = _get_json(base + "etf.json")
    except Exception:
        pass
    try:
        snap["_stocks"] = _get_json(base + "stock_list.json")
    except Exception:
        pass
    try:
        snap["_rt"] = _get_json(base + "realtime.json")
    except Exception:
        pass
    return snap, hist


def fetch_relay_realtime(base_url):
    """只讀 GitHub 轉存的盤中報價 realtime.json：{"at","quotes":{code:{...}}}。"""
    return _get_json(base_url.rstrip("/") + "/realtime.json")


def apply_stock_list(d, path=STOCK_LIST_FILE):
    """GitHub／手機帶來的代號表（比本機新、筆數夠多才換）。回傳是否更新。"""
    try:
        stocks = (d or {}).get("stocks") or {}
        if len(stocks) < 1000:
            return False
        cur = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                cur = json.load(f)
        except (OSError, ValueError):
            pass
        if (cur.get("updated") or "") >= (d.get("updated") or "") and len(cur.get("stocks") or {}) >= len(stocks):
            return False
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, separators=(",", ":"))
        global _STOCKS, _NAME_RX, _NAME2CODE
        _STOCKS = _NAME_RX = _NAME2CODE = None
        return True
    except Exception:
        return False


def run_relay_realtime(out_dir, codes_file=None):
    """盤中報價（給 DeskNotes 聯網關閉時讀）：data/realtime.json。"""
    os.makedirs(out_dir, exist_ok=True)
    codes = []
    if codes_file and os.path.isfile(codes_file):
        with open(codes_file, "r", encoding="utf-8") as f:
            codes = re.findall(r"[0-9]{4,6}[A-Z]?", f.read())
    if not codes:
        print("codes.txt 沒有代號")
        return
    rt = fetch_realtime(codes)
    MarketStore._save_json(os.path.join(out_dir, "realtime.json"),
                           {"at": datetime.now().strftime("%Y-%m-%d %H:%M"), "quotes": rt})
    print("盤中報價：", len(rt), "檔")


# ─────────────────────────── 計算 ───────────────────────────

def _pct_rank(values, x):
    vals = [v for v in values if v is not None]
    if len(vals) < 20 or x is None:
        return None
    return sum(1 for v in vals if v < x) / len(vals)


def _ma(closes, n):
    return sum(closes[-n:]) / n if len(closes) >= n else None


def _ret(px, days):
    """px：[[日期, 收盤]]（舊→新）。用交易日數往回找。"""
    if len(px) <= days:
        return None
    a, b = px[-days - 1][1], px[-1][1]
    return b / a - 1 if a else None


# ─────────────────────────── 存檔與查詢 ───────────────────────────

class MarketStore:
    """market_cache.json（最新一份）＋ market_history.json（歷史）。
    歷史只保存「追蹤中的代號」（觀察股＋候選名單＋持股），避免檔案變太大。
    hist = {"pe": {code: [[d, pe, yield, pb]]}, "rev": {code: [[ym, rev, yoy, mom]]},
            "px": {code: [[d, close]]}, "chips": {code: [[d, foreign, trust, dealer, margin, short]]},
            "tdcc": {code: [[d, big1000, big400, holders]]}}"""

    KEYS = ("pe", "rev", "px", "chips", "tdcc", "vol", "pxm")
    LIMIT = {"pe": 1500, "rev": 60, "px": 520, "chips": 120, "tdcc": 60, "vol": 520, "pxm": 240}

    def __init__(self, folder, track=None):
        self.folder = folder
        self.cache_file = os.path.join(folder, "market_cache.json")
        self.hist_file = os.path.join(folder, "market_history.json")
        self.snap = self._load(self.cache_file) or {}
        self.hist = self._load(self.hist_file) or {}
        for k in self.KEYS:
            self.hist.setdefault(k, {})
        self.track = track          # callable → 追蹤的代號集合；None = 全部
        self._rs_cache = None
        self.rt = {}                # 盤中即時報價（不存檔）
        self.rt_at = ""
        self.fin = None             # 財報（market_fin.json，用到才載入）
        self.etf = None             # ETF 成分股（market_etf.json，用到才載入）

    @staticmethod
    def _load(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    @staticmethod
    def _save_json(p, d):
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, p)

    def save(self):
        try:
            self._save_json(self.cache_file, self.snap)
            self._save_json(self.hist_file, self.hist)
        except OSError:
            pass

    @property
    def updated(self):
        return self.snap.get("fetched") or self.snap.get("date")

    def _tracked(self):
        try:
            t = self.track() if callable(self.track) else self.track
        except Exception:
            t = None
        return set(t) if t is not None else None

    def _append(self, kind, code, row):
        lst = self.hist[kind].setdefault(code, [])
        if lst and lst[-1][0] == row[0]:
            lst[-1] = row
        elif not lst or lst[-1][0] < row[0]:
            lst.append(row)
        elif row[0] not in {r[0] for r in lst}:
            lst.append(row)
            lst.sort()
        del lst[:-self.LIMIT[kind]]

    def merge_snapshot(self, snap, origin=None):
        """新的一份覆蓋最新值；追蹤中的代號另外累積歷史。
        origin = {"label": "電腦直接抓／手機檔 xxx／GitHub", "file": 來源檔路徑}，記在 _prov 供「📄 資料來源」查。"""
        self._rs_cache = None
        old = self.snap or {}
        old_exdiv = old.get("exdiv") or {}
        today = date.today().isoformat()
        # 分區合併：手機只抓到上市、或只抓到籌碼時，不會把上櫃／月營收等舊資料洗掉；
        # 比現有舊的檔案只補缺、不覆蓋。
        newer = (snap.get("date") or today) >= (old.get("date") or "")
        merged = dict(old)
        if snap.get("date") and old.get("date") and snap["date"] > old["date"] and (old.get("close") or old.get("chg")):
            merged["prev"] = {"date": old["date"], "close": old.get("close") or {}, "chg": old.get("chg") or {}}
            merged["chg"] = {}          # 新的一天：舊漲跌不要混進來
        for k, v in snap.items():
            if k == "exdiv" or k in ("_prov", "_src", "prev", "_fin", "_etf", "_stocks", "_rt", "_last_origin",
                                     "_origin_log"):
                continue
            if k == "alerts":                        # 風險警示整份換新（舊的處置可能已解除）
                if newer:
                    merged[k] = v
                continue
            if isinstance(v, dict):
                if not v:
                    continue
                cur = old.get(k) if isinstance(old.get(k), dict) else {}
                merged[k] = {**cur, **v} if newer else {**v, **cur}
            elif v not in (None, "", []) and (newer or k not in old):
                merged[k] = v
        self.snap = merged
        self._record_prov(snap, origin or {}, newer, old.get("_prov") or {})
        self.snap["fetched"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        rec = {"at": self.snap["fetched"], "label": (origin or {}).get("label") or "（未標示）",
               "date": snap.get("date") or ""}
        self.snap["_last_origin"] = rec
        log = list(old.get("_origin_log") or [])
        log.append(rec)
        self.snap["_origin_log"] = log[-40:]
        ex = {k: [i for i in v if i.get("date", "") >= today] for k, v in old_exdiv.items()}
        for k, v in (snap.get("exdiv") or {}).items():
            cur = ex.setdefault(k, [])
            for it in v:
                if it not in cur:
                    cur.append(it)
        self.snap["exdiv"] = {k: sorted(v, key=lambda i: i["date"]) for k, v in ex.items() if v}
        track = self._tracked()

        def want(code):
            return track is None or code in track
        d = snap.get("date") or today
        for code, v in (snap.get("pe") or {}).items():
            if want(code) and (v.get("pe") or v.get("pb")):
                self._append("pe", code, [d, v.get("pe"), v.get("yield"), v.get("pb")])
        for code, v in (snap.get("rev") or {}).items():
            if want(code) and v.get("ym") and v.get("rev") is not None:
                self._append("rev", code, [v["ym"], v["rev"], v.get("yoy"), v.get("mom")])
        for code, p in (snap.get("close") or {}).items():
            if want(code):
                self._append("px", code, [d, p])
        for code, v in (snap.get("vol") or {}).items():
            if want(code) and v is not None:
                self._append("vol", code, [d, v])
        if snap.get("index"):
            self._append("px", INDEX_KEY, [d, snap["index"]])
        cd = snap.get("chips_date")
        for code, v in (snap.get("chips") or {}).items():
            if want(code) and cd:
                self._append("chips", code, [cd, v.get("foreign"), v.get("trust"), v.get("dealer"),
                                             v.get("margin"), v.get("short")])
        for code, v in (snap.get("tdcc") or {}).items():
            if want(code) and v.get("date"):
                self._append("tdcc", code, [v["date"], v.get("big1000"), v.get("big400"), v.get("holders")])

    _SECTION_SRC = {"pe": ("twse_pe", "tpex_pe"), "close": ("twse_close", "tpex_close"),
                    "chg": ("twse_close", "tpex_close"),
                    "rev": ("twse_rev", "tpex_rev"), "exdiv": ("twse_exdiv", "tpex_exdiv"),
                    "index": ("twse_index", None), "chips": ("chips", "chips"), "tdcc": ("tdcc", "tdcc")}

    def _record_prov(self, snap, origin, newer, old_prov):
        """每一區（本益比、收盤…）× 市場（L 上市／O 上櫃）記下：哪裡來、資料日、原始網址／原始檔。"""
        prov = dict(old_prov)
        incoming = snap.get("_prov") or {}          # GitHub market.json 自己帶的來源紀錄
        src = snap.get("_src") or {}
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        for sec, (kl, ko) in self._SECTION_SRC.items():
            v = snap.get(sec)
            if not v:
                continue
            if sec == "index":
                mk = {"L"}
            else:
                mk = set()
                for c in v:
                    info = stock_info(c)
                    mk.add("L" if info and info[1] == "上市" else "O" if info and info[1] == "上櫃" else "X")
                    if len(mk) >= 3:
                        break
            for m in mk:
                key = f"{sec}:{m}"
                if not newer and key in prov:
                    continue
                if key in incoming:
                    prov[key] = dict(incoming[key])
                    prov[key]["via"] = origin.get("label") or ""
                    if origin.get("file"):
                        prov[key]["via_file"] = origin["file"]
                    continue
                sk = kl if m == "L" else ko
                rec = {"label": origin.get("label") or "", "file": origin.get("file"), "at": now,
                       "date": snap.get("chips_date") if sec == "chips" else snap.get("date")}
                if sk and sk in src:
                    rec["url"] = src[sk].get("url")
                    if src[sk].get("file"):
                        rec["raw"] = src[sk]["file"]
                prov[key] = rec
        self.snap["_prov"] = prov

    def provenance(self, code, sec):
        """回傳某代號某區資料的來源紀錄 dict（可能是空的）。"""
        info = stock_info(code)
        m = "L" if sec == "index" or (info and info[1] == "上市") else "O" if info and info[1] == "上櫃" else "X"
        prov = self.snap.get("_prov") or {}
        return prov.get(f"{sec}:{m}") or prov.get(f"{sec}:X") or {}

    def raw_dir_today(self):
        """電腦直接抓時原始回應的存放處；只留最近 10 天。"""
        base = os.path.join(self.folder, "market_raw")
        try:
            os.makedirs(base, exist_ok=True)
            days = sorted(d for d in os.listdir(base) if re.match(r"\d{4}-\d{2}-\d{2}$", d))
            import shutil
            for d in days[:-10]:
                shutil.rmtree(os.path.join(base, d), ignore_errors=True)
        except OSError:
            pass
        return os.path.join(base, date.today().isoformat())

    def merge_history(self, hist):
        self._rs_cache = None
        for kind in self.KEYS:
            for code, rows in (hist.get(kind) or {}).items():
                lst = self.hist[kind].setdefault(code, [])
                have = {r[0] for r in lst}
                lst.extend(r for r in rows if r and r[0] not in have)
                lst.sort()
                del lst[:-self.LIMIT[kind]]

    def merge_chip_days(self, days):
        """backfill_chips() 的結果 {date: {code: {...}}} 併入歷史。"""
        track = self._tracked()
        for d, m in sorted(days.items()):
            for code, v in m.items():
                if track is None or code in track:
                    self._append("chips", code, [d, v.get("foreign"), v.get("trust"), v.get("dealer"),
                                                 v.get("margin"), v.get("short")])

    # ----- 指標 -----
    def valuation(self, code):
        """估值綜合位置：本益比、股價淨值比在歷史中的位置，殖利率反過來算（殖利率越高越便宜）。
        回傳 {"score": 0~1（越低越便宜）, "parts": {"pe","pb","yield"}, "years"} 或 {}。"""
        cur = (self.snap.get("pe") or {}).get(code) or {}
        rows = self.hist["pe"].get(code) or []
        if len(rows) < 20:
            return {}
        pe = _pct_rank([r[1] for r in rows if len(r) > 1 and r[1]], cur.get("pe"))
        pb = _pct_rank([r[3] for r in rows if len(r) > 3 and r[3]], cur.get("pb"))
        yl = _pct_rank([r[2] for r in rows if len(r) > 2 and r[2] is not None], cur.get("yield"))
        parts = {}
        if pe is not None:
            parts["pe"] = pe
        if pb is not None:
            parts["pb"] = pb
        if yl is not None:
            parts["yield"] = 1 - yl
        if not parts:
            return {}
        try:
            yrs = max(0.1, (date.today() - date.fromisoformat(rows[0][0])).days / 365)
        except ValueError:
            yrs = None
        return {"score": sum(parts.values()) / len(parts), "parts": parts, "years": yrs}

    def technical(self, code):
        px = self.hist["px"].get(code) or []
        cl = [r[1] for r in px]
        if len(cl) < 20:
            return {}
        last = cl[-1]
        out = {"close": last, "n": len(cl)}
        for n, k in ((20, "ma20"), (60, "ma60"), (240, "ma240")):
            m = _ma(cl, n)
            if m:
                out[k] = m
                out[k + "_above"] = last >= m
        win = cl[-250:]
        hi, lo = max(win), min(win)
        out["hi52"], out["lo52"] = hi, lo
        out["pos52"] = (last - lo) / (hi - lo) if hi > lo else None
        out["full52"] = len(cl) >= 240
        out["dist_hi"] = last / hi - 1 if hi else None
        out["dist_lo"] = last / lo - 1 if lo else None
        out["new_hi"] = len(cl) >= 60 and last >= hi
        out["new_lo"] = len(cl) >= 60 and last <= lo
        m20, m60, m240 = out.get("ma20"), out.get("ma60"), out.get("ma240")
        if m20 and m60:
            if last >= m20 >= m60 and (m240 is None or m60 >= m240):
                out["trend"] = "多頭排列"
            elif last <= m20 <= m60 and (m240 is None or m60 <= m240):
                out["trend"] = "空頭排列"
            else:
                out["trend"] = "整理"
            if len(cl) >= 25:
                prev20 = sum(cl[-25:-5]) / 20
                out["ma20_up"] = m20 >= prev20
        vo = [r[1] for r in (self.hist.get("vol") or {}).get(code) or [] if r[1] is not None]
        if len(vo) >= 21:
            avg20 = sum(vo[-21:-1]) / 20
            out["vol"] = vo[-1]
            out["vol_ratio"] = vo[-1] / avg20 if avg20 else None
            out["vol5_ratio"] = (sum(vo[-5:]) / 5) / avg20 if avg20 else None
        return out

    def monthly(self, code):
        """月均價 {YYYY-MM: 價}：補抓的月均價（pxm）＋每日收盤算出的月均（有每日資料的月份以每日為準）。"""
        out = {r[0]: r[1] for r in (self.hist.get("pxm") or {}).get(code) or [] if r[1]}
        by = {}
        for r in self.hist["px"].get(code) or []:
            if r[1]:
                by.setdefault(r[0][:7], []).append(r[1])
        cur_m = date.today().strftime("%Y-%m")
        for ym, v in by.items():
            if len(v) >= 10 or (ym == cur_m and v):
                out[ym] = sum(v) / len(v)
        return out

    def spark(self, code, days=60):
        """最近 N 個交易日收盤 [(日期, 收盤)]。"""
        px = self.hist["px"].get(code) or []
        return [(r[0], r[1]) for r in px[-days:] if r[1]]

    def rev_streak(self, code):
        """月營收 YoY 連續成長／衰退月數：{"yoy_streak": +N 連增 / -N 連減, "ym", "mom_streak"}。"""
        rows = sorted(self.hist["rev"].get(code) or [], key=lambda r: r[0])
        cur = (self.snap.get("rev") or {}).get(code) or {}
        if cur.get("ym") and cur.get("yoy") is not None and (not rows or rows[-1][0] < cur["ym"]):
            rows.append([cur["ym"], cur.get("rev"), cur.get("yoy"), cur.get("mom")])
        if not rows:
            return {}

        def streak(ix):
            s = 0
            for r in reversed(rows):
                v = r[ix] if len(r) > ix else None
                if v is None or v == 0:
                    break
                if s == 0:
                    s = 1 if v > 0 else -1
                elif (v > 0) == (s > 0):
                    s += 1 if s > 0 else -1
                else:
                    break
            return s
        out = {"ym": rows[-1][0], "yoy_streak": streak(2), "mom_streak": streak(3), "n": len(rows)}
        revs = [r[1] for r in rows[-12:] if r[1] is not None]
        if len(revs) >= 6 and rows[-1][1] is not None and rows[-1][1] >= max(revs):
            out["high_n"] = len(revs)
        return out

    def rs(self, code):
        """相對強弱：個股報酬 − 加權指數報酬（3 個月≈63 交易日、6 個月≈126）。"""
        px = self.hist["px"].get(code) or []
        ix = self.hist["px"].get(INDEX_KEY) or []
        out = {}
        for days, k in ((63, "rs3m"), (126, "rs6m")):
            r = _ret(px, days)
            if r is None:
                continue
            out["ret" + k[2:]] = r
            ri = _ret(ix, days)
            if ri is not None:
                out[k] = r - ri
        return out

    def rs_rank(self, code, universe):
        """在 universe（代號清單）裡的 3 個月相對強弱百分位（1 = 最強）。"""
        key = tuple(sorted(universe))
        if not self._rs_cache or self._rs_cache[0] != key:
            vals = {}
            for c in universe:
                v = self.rs(c).get("rs3m", self.rs(c).get("ret3m"))
                if v is not None:
                    vals[c] = v
            self._rs_cache = (key, vals)
        vals = self._rs_cache[1]
        if code not in vals or len(vals) < 3:
            return None
        x = vals[code]
        return sum(1 for v in vals.values() if v <= x) / len(vals)

    def chips(self, code):
        rows = self.hist["chips"].get(code) or []
        cur = (self.snap.get("chips") or {}).get(code)
        out = {}
        if rows:
            last5 = rows[-5:]
            f5 = [r[1] for r in last5 if r[1] is not None]
            t5 = [r[2] for r in last5 if r[2] is not None]
            if f5:
                out["foreign5"] = sum(f5)
            if t5:
                out["trust5"] = sum(t5)
            d5 = [r[3] for r in last5 if len(r) > 3 and r[3] is not None]
            if f5 or t5 or d5:
                out["inst5"] = sum(f5) + sum(t5) + sum(d5)
            f20 = [r[1] for r in rows[-20:] if r[1] is not None]
            t20 = [r[2] for r in rows[-20:] if r[2] is not None]
            if len(f20) >= 10:
                out["foreign20"] = sum(f20)
            if len(t20) >= 10:
                out["trust20"] = sum(t20)
            out["days"] = len(rows)

            def streak(ix):
                s = 0
                for r in reversed(rows):
                    v = r[ix]
                    if v is None or v == 0:
                        break
                    if s == 0:
                        s = 1 if v > 0 else -1
                    elif (v > 0) == (s > 0):
                        s += 1 if s > 0 else -1
                    else:
                        break
                return s
            out["foreign_streak"] = streak(1)
            out["trust_streak"] = streak(2)
            mg = [r[4] for r in rows if len(r) > 4 and r[4]]
            if len(mg) >= 6 and mg[-6]:
                out["margin_chg5"] = mg[-1] / mg[-6] - 1
            if mg:
                out["margin"] = mg[-1]
            out["date"] = rows[-1][0]
        elif cur:
            out.update({"foreign1": cur.get("foreign"), "trust1": cur.get("trust")})
        td = self.hist["tdcc"].get(code) or []
        if td:
            out["big1000"] = td[-1][1]
            out["tdcc_date"] = td[-1][0]
            if len(td) >= 2 and td[-2][1] is not None and td[-1][1] is not None:
                out["big1000_chg"] = td[-1][1] - td[-2][1]
        elif (self.snap.get("tdcc") or {}).get(code):
            v = self.snap["tdcc"][code]
            out["big1000"] = v.get("big1000")
            out["tdcc_date"] = v.get("date")
        return out

    def info(self, code):
        """整合所有指標；沒資料的鍵不出現。"""
        out = {}
        pe = (self.snap.get("pe") or {}).get(code) or {}
        if pe.get("pe"):
            out["pe"] = pe["pe"]
            rows = [r[1] for r in self.hist["pe"].get(code, []) if len(r) > 1 and r[1]]
            p = _pct_rank(rows, pe["pe"])
            if p is not None:
                out["pe_pct"] = p
                out["pe_n"] = len(rows)
                try:
                    out["pe_years"] = max(0.1, (date.today() - date.fromisoformat(
                        self.hist["pe"][code][0][0])).days / 365)
                except ValueError:
                    pass
        if pe.get("yield") is not None:
            out["yield"] = pe["yield"]
        if pe.get("pb"):
            out["pb"] = pe["pb"]
        val = self.valuation(code)
        if val:
            out["val"] = val
        cl = (self.snap.get("close") or {}).get(code)
        if cl:
            out["close"] = cl
        rv = (self.snap.get("rev") or {}).get(code)
        if rv:
            out["rev"] = rv
        rh = self.hist["rev"].get(code)
        if rh:
            out["rev_hist"] = rh[-13:]
        today = date.today().isoformat()
        nxt = [i for i in (self.snap.get("exdiv") or {}).get(code, []) if i.get("date", "") >= today]
        if nxt:
            out["exdiv"] = nxt[0]
            try:
                out["exdiv_days"] = (date.fromisoformat(nxt[0]["date"]) - date.today()).days
            except ValueError:
                pass
        tech = self.technical(code)
        if tech:
            out["tech"] = tech
        rs = self.rs(code)
        if rs:
            out["rs"] = rs
        ch = self.chips(code)
        if ch:
            out["chips"] = ch
        rs2 = self.rev_streak(code)
        if rs2:
            out["rev_streak"] = rs2
        try:
            et = self.etf_of(code)
        except Exception:
            et = []
        if et:
            out["etf"] = et
        return out

    # ----- 即時報價、今日漲跌 -----
    def set_realtime(self, rt, src="證交所 MIS"):
        self.rt = dict(self.rt or {})
        self.rt.update(rt or {})
        self.rt_at = datetime.now().strftime("%H:%M:%S")
        self.rt_src = src

    def quote(self, code):
        """{"price","chg","pct","src"}；盤中有即時報價用即時，不然用最近一次收盤。"""
        rt = (getattr(self, "rt", None) or {}).get(code)
        if rt and rt.get("price") is not None:
            t = (rt.get("time") or "")[:5]
            return {"price": rt["price"], "chg": rt.get("chg"), "pct": rt.get("pct"),
                    "src": ("即時" + ("（GitHub）" if "GitHub" in (getattr(self, "rt_src", "") or "") else "")
                            + (" " + t if t else "")), "date": rt.get("date"), "rt": True}
        cl = (self.snap.get("close") or {}).get(code)
        ch = (self.snap.get("chg") or {}).get(code)
        d = self.snap.get("date") or ""
        if cl is None:
            px = self.hist["px"].get(code) or []
            if not px:
                return {}
            cl, d = px[-1][1], px[-1][0]
            ch = round(px[-1][1] - px[-2][1], 2) if len(px) > 1 and px[-2][1] else None
        pct = ch / (cl - ch) * 100 if ch is not None and (cl - ch) else None
        return {"price": cl, "chg": ch, "pct": pct, "src": "收盤 " + d[5:].replace("-", "/"), "date": d, "rt": False}

    def day_pcts(self, codes, which="latest"):
        """題材強弱用：回傳 (說明, {code: 漲跌%})。latest=最新（盤中即時優先）；prev=前一個交易日。"""
        out = {}
        rt = getattr(self, "rt", None) or {}
        if which == "latest":
            n_rt = 0
            for c in codes:
                q = self.quote(c)
                if q.get("pct") is not None:
                    out[c] = q["pct"]
                    n_rt += 1 if q.get("rt") else 0
            lab = (f"盤中 {getattr(self, 'rt_at', '')[:5]}" if n_rt else f"收盤 {self.snap.get('date', '')}")
            return lab, out
        # 前一日：有即時報價時，前一日 = 快照那天；否則用快照的 prev
        if rt:
            src, d = self.snap, self.snap.get("date", "")
        else:
            src = self.snap.get("prev") or {}
            d = src.get("date", "")
        cl, ch = src.get("close") or {}, src.get("chg") or {}
        for c in codes:
            a, b = cl.get(c), ch.get(c)
            if a is not None and b is not None and a - b:
                out[c] = b / (a - b) * 100
        return f"收盤 {d}", out

    # ----- 財報（三率、EPS） -----
    def _fin(self):
        if getattr(self, "fin", None) is None:
            self.fin_file = os.path.join(self.folder, "market_fin.json")
            self.fin = self._load(self.fin_file) or {}
            self.fin.setdefault("q", {})
            self.fin.setdefault("done", [])
        return self.fin

    def save_fin(self):
        try:
            self._save_json(os.path.join(self.folder, "market_fin.json"), self._fin())
        except OSError:
            pass

    def merge_fin(self, rows, label=""):
        """rows：[(代號, "2026Q2", [營收, 毛利, 營業利益, 淨利, 累計EPS])]（金額是年初累計）。"""
        f = self._fin()
        n = 0
        for code, yq, vals in rows:
            f["q"].setdefault(code, {})[yq] = vals
            n += 1
        if n:
            f["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            if label:
                f["label"] = label
        return n

    def merge_fin_store(self, other):
        """GitHub 轉存的 fin.json 併進來。"""
        f = self._fin()
        for code, qs in (other.get("q") or {}).items():
            f["q"].setdefault(code, {}).update(qs)
        for code, qs in (other.get("bs") or {}).items():
            f.setdefault("bs", {}).setdefault(code, {}).update(qs)
        for code, rows in (other.get("div") or {}).items():
            cur = f.setdefault("div", {}).setdefault(code, [])
            have = {r[0] for r in cur}
            cur.extend(r for r in rows if r and r[0] not in have)
            cur.sort()
        if other.get("div_updated"):
            f["div_updated"] = other["div_updated"]
        f["updated"] = other.get("updated") or f.get("updated")
        f["label"] = other.get("label") or "GitHub Actions"

    # ----- ETF 成分股 -----
    def _etf(self):
        if self.etf is None:
            self.etf = self._load(os.path.join(self.folder, "market_etf.json")) or {}
            self.etf.setdefault("funds", {})
            self.etf.setdefault("changes", [])
        return self.etf

    def save_etf(self):
        try:
            self._save_json(os.path.join(self.folder, "market_etf.json"), self._etf())
        except OSError:
            pass

    def merge_etf_store(self, other):
        """GitHub 轉存的 etf.json 或手機檔裡的 etf 併進來（每檔 ETF 用資料日期較新的那份）。"""
        e = self._etf()
        for k, f in (other.get("funds") or {}).items():
            cur = e["funds"].get(k)
            if not cur or (f.get("date") or "", f.get("fetched") or "") >= (cur.get("date") or "", cur.get("fetched") or ""):
                if cur and cur.get("hold") and f.get("hold") and cur.get("date") != f.get("date"):
                    e["changes"].extend(etf_diff(k, cur, f))
                e["funds"][k] = f
        have = {(c.get("date"), c.get("etf"), c.get("code"), c.get("kind")) for c in e["changes"]}
        for c in other.get("changes") or []:
            key = (c.get("date"), c.get("etf"), c.get("code"), c.get("kind"))
            if key not in have:
                e["changes"].append(c)
                have.add(key)
        e["changes"] = sorted(e["changes"], key=lambda c: c.get("date") or "")[-400:]
        e["updated"] = max(e.get("updated") or "", other.get("updated") or "")

    def etf_of(self, code):
        """[(ETF 代號, 名稱, 權重%, 資料日)]，權重大的在前。"""
        out = []
        for k, f in (self._etf().get("funds") or {}).items():
            w = (f.get("hold") or {}).get(code)
            if w is not None:
                out.append((k, f.get("name") or k, w, f.get("date") or ""))
        return sorted(out, key=lambda x: -x[2])

    def etf_changes(self, code=None, days=45):
        lim = (date.today() - timedelta(days=days)).isoformat()
        return [c for c in self._etf().get("changes") or []
                if (code is None or c.get("code") == code) and (c.get("date") or "") >= lim]

    def fin_quarters(self, code):
        """單季數字（舊→新）：[{"yq","rev","gm","om","nm","eps_q","eps_cum"}]。"""
        q = (self._fin()["q"].get(code)) or {}
        out = []
        for yq in sorted(q):
            try:
                y, s = int(yq[:4]), int(yq[-1])
            except ValueError:
                continue
            cur = q[yq]
            if s == 1:
                single = list(cur[:4])
                eq = cur[4] if len(cur) > 4 else None
            else:
                prev = q.get(f"{y}Q{s - 1}")
                if not prev:
                    single, eq = [None] * 4, None
                else:
                    single = [a - b if a is not None and b is not None else None for a, b in zip(cur[:4], prev[:4])]
                    eq = (cur[4] - prev[4]) if len(cur) > 4 and len(prev) > 4 and cur[4] is not None \
                        and prev[4] is not None else None
            rev, gp, op, ni = single
            ok = rev not in (None, 0)
            out.append({"yq": yq, "rev": rev,
                        "gm": gp / rev * 100 if ok and gp is not None else None,
                        "om": op / rev * 100 if ok and op is not None else None,
                        "nm": ni / rev * 100 if ok and ni is not None else None,
                        "eps_q": round(eq, 2) if eq is not None else None,
                        "eps_cum": cur[4] if len(cur) > 4 else None})
        return out

    def annual_eps(self, code):
        """全年 EPS {年: EPS}（第 4 季累計 EPS）。"""
        q = (self._fin()["q"].get(code)) or {}
        return {int(yq[:4]): v[4] for yq, v in q.items()
                if yq.endswith("Q4") and len(v) > 4 and v[4] is not None}

    def highlight(self, code):
        """亮點：①全年 EPS 連 3 年成長 ②單季 EPS 連 2 季成長 ③單季營收連 2 季成長。
        各項 True／False／None（資料不足）。回傳 {"year","eps","rev","n_ok","all","detail":{...}}。"""
        out = {"detail": {}}
        ae = self.annual_eps(code)
        if ae:
            ys = sorted(ae)
            last = ys[-1]
            seq = [ae.get(y) for y in range(last - 3, last + 1)]
            out["detail"]["year"] = [(y, ae.get(y)) for y in range(last - 3, last + 1)]
            if None in seq:
                # 不足 4 年：已經看得到下降就算 False
                have = [(y, ae[y]) for y in ys[-4:]]
                bad = any(b[1] <= a[1] for a, b in zip(have, have[1:]) if b[0] == a[0] + 1)
                out["year"] = False if bad else None
            else:
                out["year"] = all(b > a for a, b in zip(seq, seq[1:]))
        else:
            out["year"] = None

        def nxt(yq):
            y, q = int(yq[:4]), int(yq[-1])
            return f"{y + 1}Q1" if q == 4 else f"{y}Q{q + 1}"
        qs = self.fin_quarters(code)
        for key, field in (("eps", "eps_q"), ("rev", "rev")):
            rows = [r for r in qs if r.get(field) is not None]
            tail = rows[-3:]
            ok = len(tail) == 3 and nxt(tail[0]["yq"]) == tail[1]["yq"] and nxt(tail[1]["yq"]) == tail[2]["yq"]
            out["detail"][key] = [(r["yq"], r[field]) for r in tail]
            out[key] = (tail[2][field] > tail[1][field] > tail[0][field]) if ok else None
        vals = [out[k] for k in ("year", "eps", "rev")]
        out["n_ok"] = sum(1 for v in vals if v)
        out["all"] = all(v is True for v in vals)
        out["known"] = sum(1 for v in vals if v is not None)
        return out

    def balance(self, code):
        """最近一季資產負債＋近四季 ROE：{"yq","debt_ratio","bvps","equity","roe"}。"""
        bs = (self._fin().get("bs") or {}).get(code) or {}
        if not bs:
            return {}
        yq = max(bs)
        a, li, eq, bv = (bs[yq] + [None] * 4)[:4]
        out = {"yq": yq, "bvps": bv, "equity": eq}
        if a and li is not None:
            out["debt_ratio"] = li / a * 100
        qs = [r for r in self.fin_quarters(code) if r.get("rev") is not None]
        q = self._fin()["q"].get(code) or {}
        ni4 = []
        for r in qs[-4:]:
            cur = q.get(r["yq"]) or []
            y, s = int(r["yq"][:4]), int(r["yq"][-1])
            prev = q.get(f"{y}Q{s - 1}") if s > 1 else None
            if len(cur) > 3 and cur[3] is not None:
                ni4.append(cur[3] - (prev[3] if prev and len(prev) > 3 and prev[3] is not None else 0)
                           if s > 1 else cur[3])
        if len(ni4) == 4 and eq:
            out["roe"] = sum(ni4) / eq * 100
        return out

    def three_rates(self, code):
        """{"status": 三率三升／三率三降／2升1降…, "yq", "qoq": {gm,om,nm 差幾個百分點}, "yoy": {...}, "rows": [...]}"""
        rows = self.fin_quarters(code)
        full = [r for r in rows if r["gm"] is not None and r["om"] is not None and r["nm"] is not None]
        if not rows:
            return {}
        last = rows[-1]
        out = {"yq": last["yq"], "rows": rows[-6:], "eps_cum": last.get("eps_cum"), "eps_q": last.get("eps_q")}
        if len(full) < 2 or full[-1] is not last:
            out["status"] = "資料不足" if last["gm"] is None or len(full) < 2 else ""
            if last["rev"] is not None and last["gm"] is None and last["om"] is not None:
                out["status"] = "無毛利（金融業）"
            return out
        prev = full[-2]
        qoq = {k: last[k] - prev[k] for k in ("gm", "om", "nm")}
        ups = sum(1 for v in qoq.values() if v > 0.005)
        downs = sum(1 for v in qoq.values() if v < -0.005)
        out["qoq"] = qoq
        out["prev_yq"] = prev["yq"]
        if ups == 3:
            out["status"] = "三率三升"
        elif downs == 3:
            out["status"] = "三率三降"
        else:
            flat = 3 - ups - downs
            out["status"] = (f"{ups}升" if ups else "") + (f"{downs}降" if downs else "") + (f"{flat}平" if flat else "")
        ly = f"{int(last['yq'][:4]) - 1}{last['yq'][4:]}"
        lyr = next((r for r in rows if r["yq"] == ly and r["gm"] is not None), None)
        if lyr:
            out["yoy"] = {k: last[k] - lyr[k] for k in ("gm", "om", "nm")}
            q = self._fin()["q"].get(code) or {}
            if q.get(ly) and len(q[ly]) > 4 and q[ly][4] is not None and last.get("eps_cum") is not None:
                out["eps_cum_ly"] = q[ly][4]
        return out


# ─────────────────────────── 即時報價（證交所 MIS） ───────────────────────────

MIS_HOME = "https://mis.twse.com.tw/stock/index.jsp"
MIS_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={q}&json=1&delay=0&_={ts}"


def fetch_realtime(codes, sess=None):
    """盤中即時（約 5 秒延遲）：{code: {"price","prev","chg","pct","time","date"}}。
    還沒成交時用最佳買價估（est=True）。收盤後回傳當天收盤。"""
    import requests
    s = sess or requests.Session()
    try:
        s.get(MIS_HOME, headers=UA, timeout=10)
    except Exception:
        pass
    keys = []
    for c in dict.fromkeys(str(x).upper() for x in codes):
        info = stock_info(c)
        if not info:
            continue
        ex = "tse" if info[1] == "上市" else "otc" if info[1] == "上櫃" else None
        if ex:
            keys.append(f"{ex}_{c}.tw")
    out = {}
    err = None
    for i in range(0, len(keys), 50):
        url = MIS_URL.format(q="|".join(keys[i:i + 50]), ts=int(time.time() * 1000))
        try:
            r = s.get(url, headers=dict(UA, Referer=MIS_HOME), timeout=15)
            r.raise_for_status()
            arr = r.json().get("msgArray") or []
        except Exception as e:
            err = e
            continue
        for m in arr:
            c = m.get("c")
            y = _num(m.get("y"))
            z = _num(m.get("z"))
            est = z is None
            if z is None:
                z = _num((m.get("b") or "").split("_")[0]) or _num((m.get("a") or "").split("_")[0])
            if not c or z is None or not y:
                continue
            out[c] = {"price": z, "prev": y, "chg": round(z - y, 2), "pct": (z / y - 1) * 100,
                      "time": m.get("t"), "date": m.get("d"), "est": est}
        if i + 50 < len(keys):
            time.sleep(0.4)
    _diag("mis", ok=bool(out), n=len(out), asked=len(keys), err=str(err)[:200] if err else None)
    if not out and err is not None:
        raise RuntimeError(f"即時報價抓不到：{err}")
    return out


# ─────────────────────────── 題材（族群）清單 ───────────────────────────

DEFAULT_THEMES = {
    "被動元件": ["2327", "2492", "3026", "2375", "6173", "6449"],
    "CCL銅箔基板": ["2383", "6274", "6213"],
    "矽光子/光通訊": ["3081", "4979", "3363", "3163", "6442", "4977", "3450"],
    "PCB(伺服器板/HDI)": ["2368", "3037", "2313", "4958", "6269", "5469"],
    "低軌衛星": ["3491", "2314", "6285", "2313", "6271"],
    "磊晶/化合物半導體": ["3105", "8086", "2455", "4991", "3707"],
    "測試介面/設備": ["6515", "6223", "6510", "7769"],
    "網通設備": ["2345", "3596", "6285", "2332", "4906"],
    "玻纖布/銅箔": ["1802", "1815", "8358"],
    "重電/電網": ["1503", "1513", "1519", "1514"],
    "高速連接器/線材": ["3665", "3533", "2392", "6197", "3023"],
    "封裝測試": ["3711", "2449", "6239", "6147"],
    "BMC(信驊)": ["5274"],
    "機殼": ["8210", "3693", "6117", "2059"],
    "滑軌": ["2059", "6584"],
    "檢測分析": ["3587", "6658"],
    "高速傳輸IC": ["5269", "4966", "6756"],
    "先進封裝設備": ["3131", "6187", "3583", "6640", "2467"],
    "廠務工程": ["6139", "2404", "5536", "6196"],
    "機器人": ["2049", "1590", "4562", "2359"],
    "電源/BBU": ["2308", "2301", "6409", "3211", "6781"],
    "散熱": ["3017", "3324", "3653", "2421", "8996"],
    "組裝代工(ODM)": ["2317", "2382", "3231", "6669", "2356", "2324"],
    "ASIC/矽智財": ["3443", "3661", "3035", "6643", "3529", "6533"],
    "ABF載板": ["3037", "8046", "3189"],
    "晶圓代工": ["2330", "2303", "5347", "6770"],
    "記憶體": ["2408", "2344", "2337", "3260", "8299", "4967"],
}


def load_themes(folder):
    """themes.json（使用者可以自己改）；沒有就用內建清單並寫出一份。"""
    p = os.path.join(folder, "themes.json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and d:
            return {k: [str(c) for c in v] for k, v in d.items() if isinstance(v, list)}
    except (OSError, ValueError):
        pass
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_THEMES, f, ensure_ascii=False, indent=1)
    except OSError:
        pass
    return dict(DEFAULT_THEMES)


# ─────────────────────────── 財報：三率、EPS ───────────────────────────

FIN_OPENAPI = {
    "L": [f"https://openapi.twse.com.tw/v1/opendata/t187ap06_L_{k}" for k in ("ci", "mim", "basi", "bd", "fh", "ins")],
    "O": [f"https://www.tpex.org.tw/openapi/v1/mopsfe_t187ap06_O_{k}" for k in ("ci", "mim", "basi", "bd", "fh", "ins")],
}
MOPS_FIN_URLS = ["https://mopsov.twse.com.tw/mops/web/ajax_t163sb04",
                 "https://mops.twse.com.tw/mops/web/ajax_t163sb04"]


def _nk(k):
    return unicodedata.normalize("NFKC", str(k)).replace(" ", "").replace("　", "")


def _fin_from_row(r):
    """一列綜合損益表 → (代號, "2026Q2", [營收, 毛利, 營業利益, 淨利, 累計EPS]) 或 None。"""
    r = {_nk(k): v for k, v in r.items()}
    code = _pick(r, "公司代號", "SecuritiesCompanyCode", contains=("代號",))
    y = _num(_pick(r, "年度", contains=("年度",)))
    s = _num(_pick(r, "季別", contains=("季別",)))
    if not code or y is None or s is None:
        return None
    y = int(y) + 1911 if y < 1000 else int(y)
    rev = _num(_pick(r, "營業收入", "收益", "淨收益", "收入", contains=("營業收入", "淨收益", "收益合計")))
    gp = _num(_pick(r, "營業毛利(毛損)淨額", "營業毛利(毛損)", contains=("營業毛利",)))
    op = _num(_pick(r, "營業利益(損失)", contains=("營業利益",)))
    ni = _num(_pick(r, "本期淨利(淨損)", "本期稅後淨利(淨損)", contains=("本期淨利", "本期稅後淨利")))
    eps = _num(_pick(r, "基本每股盈餘(元)", contains=("基本每股盈餘", "每股盈餘")))
    if rev is None and eps is None:
        return None
    return str(code).strip(), f"{y}Q{int(s)}", [rev, gp, op, ni, eps]


def recent_quarters(n=6, today=None):
    """已經公布的最近 n 季（新→舊）：[(2026, 2), (2026, 1), (2025, 4)…]。"""
    d = today or date.today()
    out = []
    for yy in range(d.year, d.year - max(4, n // 4 + 2), -1):
        for q in (4, 3, 2, 1):
            due = {1: date(yy, 5, 15), 2: date(yy, 8, 14), 3: date(yy, 11, 14), 4: date(yy + 1, 3, 31)}[q]
            if due <= d:
                out.append((yy, q))
    return out[:n]


def _html_tables(html):
    """簡單的 HTML 表格解析 → [(表頭, [列, …])]；表頭取第一個含 th 的列。"""
    from html.parser import HTMLParser

    class P(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tables, self.row, self.cell, self.in_cell, self.is_th = [], None, [], False, False

        def handle_starttag(self, tag, a):
            if tag == "table":
                self.tables.append({"head": None, "rows": []})
            elif tag == "tr":
                self.row, self.row_th = [], False
            elif tag in ("td", "th"):
                self.in_cell, self.cell = True, []
                if tag == "th":
                    self.row_th = True

        def handle_endtag(self, tag):
            if tag in ("td", "th") and self.row is not None:
                self.row.append("".join(self.cell).strip())
                self.in_cell = False
            elif tag == "tr" and self.row is not None and self.tables:
                t = self.tables[-1]
                if self.row_th and t["head"] is None:
                    t["head"] = self.row
                elif not self.row_th and self.row:
                    t["rows"].append(self.row)
                self.row = None

        def handle_data(self, data):
            if self.in_cell:
                self.cell.append(data)

    p = P()
    p.feed(html)
    return [(t["head"] or [], t["rows"]) for t in p.tables if t["rows"]]


def fetch_fin_mops(year, season, typek, sess=None):
    """公開資訊觀測站「綜合損益表 彙總報表」某一季全部公司（typek：sii 上市／otc 上櫃）。"""
    import requests
    s = sess or requests
    data = {"encodeURIComponent": "1", "step": "1", "firstin": "1", "off": "1", "isQuery": "Y",
            "TYPEK": typek, "year": str(year - 1911), "season": f"{season:02d}"}
    last = None
    for url in MOPS_FIN_URLS:
        try:
            r = s.post(url, data=data, headers=dict(UA, Referer=url.rsplit("/", 1)[0] + "/t163sb04"),
                       timeout=60)
            r.raise_for_status()
            r.encoding = r.apparent_encoding or "utf-8"
            html = r.text
            out = []
            for head, rows in _html_tables(html):
                if not any("代號" in h for h in head):
                    continue
                for row in rows:
                    d = dict(zip(head, row))
                    d.setdefault("年度", str(year - 1911))
                    d.setdefault("季別", str(season))
                    v = _fin_from_row(d)
                    if v:
                        out.append(v)
            _diag(f"mops_fin_{typek}", ok=bool(out), url=url, yq=f"{year}Q{season}", n=len(out),
                  head=html[:200] if not out else None)
            if out:
                return out
            last = RuntimeError("沒有表格（可能查詢太頻繁被擋）")
        except Exception as e:
            last = e
            _diag(f"mops_fin_{typek}", ok=False, url=url, yq=f"{year}Q{season}", err=str(e)[:200])
    raise RuntimeError(f"{year}Q{season} {typek}：{last}")


def fetch_fin_openapi(sess=None):
    """證交所／櫃買 OpenAPI 的最新一季綜合損益表（年初累計）。"""
    rows = []
    for mk, urls in FIN_OPENAPI.items():
        for url in urls:
            try:
                data = _get_json(url, sess)
                got = [v for v in (_fin_from_row(r) for r in data if isinstance(r, dict)) if v]
                rows += got
                _diag("fin_" + url.rsplit("/", 1)[-1], ok=bool(got), n=len(got), **_sample(data))
            except Exception as e:
                _diag("fin_" + url.rsplit("/", 1)[-1], ok=False, err=str(e)[:200])
    return rows


MOPS_BS_URLS = ["https://mopsov.twse.com.tw/mops/web/ajax_t163sb05",
                "https://mops.twse.com.tw/mops/web/ajax_t163sb05"]
MOPS_REV_URLS = ["https://mopsov.twse.com.tw/nas/t21/{mk}/t21sc03_{y}_{m}_0.html",
                 "https://mops.twse.com.tw/nas/t21/{mk}/t21sc03_{y}_{m}_0.html"]


def fetch_bs_mops(year, season, typek, sess=None):
    """公開資訊觀測站「資產負債表 彙總報表」：{code: [資產總計, 負債總計, 權益總計, 每股淨值]}（千元）。"""
    import requests
    s = sess or requests
    data = {"encodeURIComponent": "1", "step": "1", "firstin": "1", "off": "1", "isQuery": "Y",
            "TYPEK": typek, "year": str(year - 1911), "season": f"{season:02d}"}
    last = None
    for url in MOPS_BS_URLS:
        try:
            r = s.post(url, data=data, headers=dict(UA, Referer=url.rsplit("/", 1)[0] + "/t163sb05"), timeout=60)
            r.raise_for_status()
            r.encoding = r.apparent_encoding or "utf-8"
            out = {}
            for head, rows in _html_tables(r.text):
                if not any("代號" in h for h in head):
                    continue
                for row in rows:
                    d = {_nk(k): v for k, v in zip(head, row)}
                    code = str(d.get("公司代號") or "").strip()
                    if not code:
                        continue
                    a = _num(_pick(d, "資產總計", "資產總額", contains=("資產總",)))
                    li = _num(_pick(d, "負債總計", "負債總額", contains=("負債總",)))
                    eq = _num(_pick(d, "權益總計", "權益總額", contains=("權益總",)))
                    bv = _num(_pick(d, "每股參考淨值", contains=("每股參考淨值", "每股淨值")))
                    if a is not None or eq is not None:
                        out[code] = [a, li, eq, bv]
            _diag(f"mops_bs_{typek}", ok=bool(out), yq=f"{year}Q{season}", n=len(out))
            if out:
                return out
            last = RuntimeError("沒有表格")
        except Exception as e:
            last = e
            _diag(f"mops_bs_{typek}", ok=False, url=url, err=str(e)[:200])
    raise RuntimeError(f"資產負債 {year}Q{season} {typek}：{last}")


def fetch_rev_month(year, month, mk, sess=None):
    """公開資訊觀測站每月營收彙總（mk：sii 上市／otc 上櫃）→ {code: [年月, 營收(千元), YoY%, MoM%]}。"""
    import requests
    s = sess or requests
    last = None
    for tpl in MOPS_REV_URLS:
        url = tpl.format(mk=mk, y=year - 1911, m=month)
        try:
            r = s.get(url, headers=UA, timeout=40)
            r.raise_for_status()
            raw = r.content
            try:
                txt = raw.decode("big5hkscs")
            except UnicodeDecodeError:
                txt = raw.decode("utf-8", errors="ignore")
            out = {}
            for _head, rows in _html_tables(txt):
                for row in rows:
                    cells = [c.strip() for c in row]
                    if len(cells) < 7 or not re.fullmatch(r"[0-9]{4,6}[A-Z]?", cells[0]):
                        continue
                    rev = _num(cells[2])
                    if rev is None:
                        continue
                    out[cells[0]] = [f"{year}-{month:02d}", rev, _num(cells[6]), _num(cells[5])]
            _diag(f"mops_rev_{mk}", ok=bool(out), ym=f"{year}-{month:02d}", n=len(out))
            if out:
                return out
            last = RuntimeError("沒有資料（可能還沒公布）")
        except Exception as e:
            last = e
    raise RuntimeError(f"月營收 {year}-{month:02d} {mk}：{last}")


def recent_rev_months(n=13, today=None):
    """已公布的月營收月份（新→舊）；每月 10 日前公布上個月。"""
    d = today or date.today()
    y, m = d.year, d.month - (1 if d.day > 10 else 2)
    while m <= 0:
        m += 12
        y -= 1
    out = []
    for _ in range(n):
        out.append((y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return out


# ─────────────────────────── 風險警示、股利歷史 ───────────────────────────

ALERT_URLS = [
    ("注意股", "https://openapi.twse.com.tw/v1/announcement/notice"),
    ("處置股", "https://openapi.twse.com.tw/v1/announcement/punish"),
    ("注意股", "https://www.tpex.org.tw/openapi/v1/tpex_trading_warning_information"),
    ("處置股", "https://www.tpex.org.tw/openapi/v1/tpex_disposal_information"),
]
PLEDGE_URLS = ["https://openapi.twse.com.tw/v1/opendata/t187ap11_L",
               "https://www.tpex.org.tw/openapi/v1/mopsfe_t187ap11_O"]
DIV_TWSE = "https://www.twse.com.tw/rwd/zh/exRight/TWT49U?startDate={y}0101&endDate={y}1231&response=json"
DIV_TPEX = ("https://www.tpex.org.tw/web/stock/exright/dailyquo/exDailyQ_result.php"
            "?l=zh-tw&d={ry}/01/01&ed={ry}/12/31&o=json")


def fetch_alerts(sess=None):
    """注意股、處置股（證交所／櫃買公告）＋董監質押比 > 30%：{code: [{"kind","text","date"}]}。"""
    out = {}

    def add(code, kind, text="", d=""):
        code = str(code or "").strip()
        if not re.fullmatch(r"[0-9]{4,6}[A-Z]?", code):
            return
        lst = out.setdefault(code, [])
        if not any(x["kind"] == kind for x in lst):
            lst.append({"kind": kind, "text": str(text or "")[:120], "date": str(d or "")})

    for kind, url in ALERT_URLS:
        try:
            rows = _get_json(url, sess)
            n = 0
            for r in rows if isinstance(rows, list) else []:
                r = {_nk(k): v for k, v in r.items()}
                code = _pick(r, "Code", "SecuritiesCompanyCode", "證券代號", contains=("代號", "Code"))
                text = _pick(r, "DispositionMeasures", "DispositionPeriod", "TradingInfoForAttention",
                             "ReasonsOfDisposition", contains=("處置", "條件", "原因", "注意", "期間", "Disposition"))
                d = _pick(r, "Date", contains=("日期", "Date"))
                add(code, kind, text, d)
                n += 1
            _diag("alert_" + url.rsplit("/", 1)[-1], ok=n > 0, n=n, **_sample(rows))
        except Exception as e:
            _diag("alert_" + url.rsplit("/", 1)[-1], ok=False, err=str(e)[:200])
    for url in PLEDGE_URLS:
        try:
            rows = _get_json(url, sess)
            agg = {}
            for r in rows if isinstance(rows, list) else []:
                r = {_nk(k): v for k, v in r.items()}
                code = str(_pick(r, "公司代號", contains=("代號",)) or "").strip()
                held = _num(_pick(r, contains=("目前持股", "持股數", "選任時持股")))
                pled = _num(_pick(r, contains=("設質股數", "質權設定", "設質")))
                if code and held:
                    a = agg.setdefault(code, [0.0, 0.0])
                    a[0] += held
                    a[1] += pled or 0
            for code, (h, p) in agg.items():
                if h and p / h > 0.30:
                    add(code, "董監質押高", f"董監質押 {p / h * 100:.0f}%")
            _diag("pledge_" + url.rsplit("/", 1)[-1], ok=bool(agg), n=len(agg), **_sample(rows))
        except Exception as e:
            _diag("pledge_" + url.rsplit("/", 1)[-1], ok=False, err=str(e)[:200])
    return out


# ─────────────────────────── ETF 成分股 ───────────────────────────

ETF_DEFAULT = [
    ("0050", "元大台灣50"), ("006208", "富邦台50"), ("0056", "元大高股息"), ("00878", "國泰永續高股息"),
    ("00919", "群益台灣精選高息"), ("00929", "復華台灣科技優息"), ("00713", "元大台灣高息低波"),
    ("00940", "元大台灣價值高息"), ("00939", "統一台灣高息動能"), ("00934", "中信成長高股息"),
    ("00900", "富邦特選高股息30"), ("00915", "凱基優選高股息30"), ("00918", "大華優利高填息30"),
    ("00881", "國泰台灣科技龍頭"), ("00891", "中信關鍵半導體"), ("00892", "富邦台灣半導體"),
    ("00927", "群益半導體收益"), ("00935", "野村臺灣新科技50"), ("0052", "富邦科技"), ("00692", "富邦公司治理"),
    ("00850", "元大臺灣ESG永續"), ("00905", "FT臺灣Smart"), ("00981A", "主動統一台股增長"),
    ("00980A", "主動野村臺灣優選"), ("00982A", "主動群益台灣強棒"),
]
ETF_SRC = [
    ("MoneyDJ 全部持股", "https://www.moneydj.com/ETF/X/Basic/Basic0007B.xdjhtm?etfid={etf}.TW"),
    ("MoneyDJ 持股", "https://www.moneydj.com/ETF/X/Basic/Basic0007.xdjhtm?etfid={etf}.TW"),
    ("Yahoo 前十大", "https://tw.stock.yahoo.com/quote/{etf}.TW/holding"),
]


def etf_list(raw=None):
    """raw：使用者自訂清單文字（代號，可加名稱）；空的用預設。回傳 [(代號, 名稱)]。"""
    if not raw or not str(raw).strip():
        return list(ETF_DEFAULT)
    names = dict(ETF_DEFAULT)
    out = []
    for m in re.finditer(r"(\d{4,6}[A-Z]?)\s*([^\s,，、;；\d][^,，、;；\n]*)?", str(raw).upper()):
        k = m.group(1)
        nm = (m.group(2) or "").strip() or names.get(k) or ((stock_info(k) or [k])[0])
        if k not in {x[0] for x in out}:
            out.append((k, nm))
    return out


def parse_etf_holdings(html):
    """從持股頁 HTML 抓 {股票代號: 權重%} 與資料日期。看不懂回傳 ({}, None)。"""
    import html as _h
    txt = _h.unescape(html or "")
    hold = {}
    rx = re.compile(r"[(（](\d{4,6}[A-Z]?)\.TWO?[)）]")
    for m in rx.finditer(txt):
        code = m.group(1)
        tail = txt[m.end():m.end() + 600]
        w = re.search(r">\s*(\d{1,3}(?:\.\d+)?)\s*%?\s*<", tail)
        if w and code not in hold:
            v = float(w.group(1))
            if 0 < v <= 100:
                hold[code] = v
    if not hold:                      # Yahoo：/quote/2330.TW … 12.34%
        for m in re.finditer(r"/quote/(\d{4,6}[A-Z]?)\.TWO?[\"'/?]", txt):
            code = m.group(1)
            w = re.search(r"(\d{1,3}\.\d+)\s*%", txt[m.end():m.end() + 800])
            if w and code not in hold and 0 < float(w.group(1)) <= 100:
                hold[code] = float(w.group(1))
    d = None
    dm = re.search(r"(?:資料日期|日期)[：:\s]*(\d{4})[/-](\d{1,2})[/-](\d{1,2})", txt)
    if dm:
        d = f"{int(dm.group(1)):04d}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}"
    return hold, d


def fetch_etf_holdings(etf, sess=None):
    """回傳 {"hold": {code: %}, "date", "src", "url"}；全部來源都失敗丟例外。"""
    import requests
    s = sess or requests
    last = None
    for label, tpl in ETF_SRC:
        url = tpl.format(etf=etf)
        try:
            r = s.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=TIMEOUT)
            r.raise_for_status()
            raw = r.content
            try:
                html = raw.decode("utf-8")
            except UnicodeDecodeError:
                html = raw.decode("big5", errors="ignore")
            hold, d = parse_etf_holdings(html)
            hold.pop(etf, None)
            if len(hold) >= 3:
                _diag("etf_" + etf, ok=True, url=url, parsed=len(hold))
                return {"hold": hold, "date": d or date.today().isoformat(), "src": label, "url": url,
                        "top10_only": label.startswith("Yahoo")}
            last = RuntimeError(f"{label} 看不懂（{len(hold)} 檔）")
            _diag("etf_" + etf, ok=False, url=url, parsed=len(hold), head=html[:200])
        except Exception as e:
            last = e
            _diag("etf_" + etf, ok=False, url=url, err=str(e)[:200])
    raise last or RuntimeError("抓不到")


def etf_diff(etf, old, new, min_pt=0.5):
    """兩份持股比較：新增、剔除、權重變動 ≥ min_pt 個百分點。"""
    out = []
    o, n = old.get("hold") or {}, new.get("hold") or {}
    if new.get("top10_only") or old.get("top10_only"):
        return out                    # 只有前十大時無法判斷剔除
    d = new.get("date") or date.today().isoformat()
    for c in n:
        if c not in o:
            out.append({"date": d, "etf": etf, "code": c, "kind": "add", "w": n[c]})
        elif abs(n[c] - o[c]) >= min_pt:
            out.append({"date": d, "etf": etf, "code": c, "kind": "up" if n[c] > o[c] else "down",
                        "w": n[c], "w_old": o[c]})
    for c in o:
        if c not in n:
            out.append({"date": d, "etf": etf, "code": c, "kind": "remove", "w_old": o[c]})
    return out


def update_etf(store, etfs=None, progress=None, sleep=1.5, force=False, deadline=None, max_age_days=6):
    """更新 ETF 成分股（預設一週一次）。回傳 (更新檔數, 錯誤)。"""
    import requests
    sess = requests.Session()
    e = store._etf()
    etfs = etfs or ETF_DEFAULT
    n, errs = 0, []
    lim = (datetime.now() - timedelta(days=max_age_days)).strftime("%Y-%m-%d %H:%M")
    for k, (etf, nm) in enumerate(etfs):
        if deadline and time.time() > deadline:
            errs.append("時間到，剩下的下次再抓")
            break
        cur = e["funds"].get(etf) or {}
        if not force and (cur.get("fetched") or "") > lim:
            continue
        if progress:
            progress(f"ETF 成分股 {etf} {nm}（{k + 1}/{len(etfs)}）")
        try:
            got = fetch_etf_holdings(etf, sess)
            got["name"] = nm
            got["fetched"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            if cur.get("hold") and cur.get("date") != got["date"]:
                e["changes"].extend(etf_diff(etf, cur, got))
            e["funds"][etf] = got
            n += 1
        except Exception as ex:
            errs.append(f"{etf}：{str(ex)[:80]}")
        time.sleep(sleep)
    e["changes"] = sorted(e["changes"], key=lambda c: c.get("date") or "")[-400:]
    if n:
        e["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    return n, errs


def fetch_dividends(years=5, sess=None, progress=None, sleep=2.5):
    """近幾年除權息結果：{code: [[日期, 權值+息值, 權/息, 除權息前收盤, 參考價], ...]}。"""
    out = {}
    this = date.today().year
    for y in range(this - years + 1, this + 1):
        if progress:
            progress(f"除權息 {y}")
        for url, mk in ((DIV_TWSE.format(y=y), "L"), (DIV_TPEX.format(ry=y - 1911), "O")):
            try:
                j = _get_json(url, sess)
                n = 0
                for fields, rows in _tables(j):
                    f = [_nk(x) for x in fields]
                    ic = _idx(f, "代號")
                    idt = _idx(f, "日期")
                    iv = _idx(f, "權值") if _idx(f, "權值") is not None else _idx(f, "息值")
                    ik = _idx(f, "權/息")
                    ipc = _idx(f, "前收盤")
                    irf = _idx(f, "參考價", exclude=("減除",))
                    if ic is None or idt is None:
                        continue
                    for r in rows:
                        d = roc_to_date(str(r[idt]).replace("年", "/").replace("月", "/").replace("日", ""))
                        code = str(r[ic]).strip()
                        if not d or not code:
                            continue
                        row = [d.isoformat(), _num(r[iv]) if iv is not None else None,
                               re.sub(r"<[^>]+>", "", str(r[ik])).strip() if ik is not None else "",
                               _num(r[ipc]) if ipc is not None else None, _num(r[irf]) if irf is not None else None]
                        lst = out.setdefault(code, [])
                        if not any(x[0] == row[0] for x in lst):
                            lst.append(row)
                            n += 1
                _diag(f"div_{mk}_{y}", ok=n > 0, n=n)
            except Exception as e:
                _diag(f"div_{mk}_{y}", ok=False, err=str(e)[:200])
            time.sleep(sleep)
    for v in out.values():
        v.sort()
    return out


def update_extras(store, progress=None, dividends=True):
    """風險警示（每天）＋股利歷史（7 天一次）。"""
    import requests
    sess = requests.Session()
    if progress:
        progress("風險警示")
    try:
        al = fetch_alerts(sess)
        store.snap["alerts"] = al
        store.snap["alerts_date"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    except Exception:
        pass
    f = store._fin()
    if dividends:
        try:
            last = datetime.strptime(f.get("div_updated") or "2000-01-01", "%Y-%m-%d")
        except ValueError:
            last = datetime(2000, 1, 1)
        if (datetime.now() - last).days >= 7 or not f.get("div"):
            d = fetch_dividends(5, sess, progress)
            if d:
                f["div"] = d
                f["div_updated"] = date.today().isoformat()
    store.save()
    store.save_fin()


def update_fin(store, progress=None, backfill=True, quarters=9, sleep=3.5, balance=True, rev_months=13,
               deadline=None):
    """更新財報：OpenAPI 最新一季＋（backfill）公開資訊觀測站近幾季損益表、資產負債表、
    近 13 個月月營收（月營收只存追蹤中的代號）。回傳 (筆數, 錯誤清單)。"""
    import requests
    sess = requests.Session()
    errors = []
    n = 0
    if progress:
        progress("財報（OpenAPI 最新一季）")
    try:
        n += store.merge_fin(fetch_fin_openapi(sess), "OpenAPI")
    except Exception as e:
        errors.append(f"OpenAPI：{e}")
    if backfill:
        f = store._fin()
        qs = recent_quarters(quarters)
        latest = qs[0] if qs else None
        # 全年 EPS（亮點：連 3 年成長）要 4 個完整年度的第 4 季累計 EPS；近 9 季以外的年度只補損益表
        q4s = [x for x in recent_quarters(20) if x[1] == 4][:4]
        extra = [x for x in q4s if x not in qs]
        for (y, q) in qs + extra:
            for typek, mk in (("sii", "L"), ("otc", "O")):
                key = f"{y}Q{q}:{mk}"
                if key in f["done"] and (y, q) != latest:
                    continue
                if progress:
                    progress(f"財報 {y}Q{q} {'上市' if mk == 'L' else '上櫃'}")
                try:
                    got = fetch_fin_mops(y, q, typek, sess)
                    n += store.merge_fin(got, "公開資訊觀測站")
                    if key not in f["done"]:
                        f["done"].append(key)
                except Exception as e:
                    errors.append(str(e)[:160])
                time.sleep(sleep)
                if (y, q) in extra:
                    continue
                if balance and f"bs:{key}" not in f["done"] or (balance and (y, q) == latest):
                    if deadline and time.time() > deadline:
                        break
                    if progress:
                        progress(f"資產負債 {y}Q{q} {'上市' if mk == 'L' else '上櫃'}")
                    try:
                        got = fetch_bs_mops(y, q, typek, sess)
                        bs = f.setdefault("bs", {})
                        for code, v in got.items():
                            bs.setdefault(code, {})[f"{y}Q{q}"] = v
                        n += len(got)
                        if f"bs:{key}" not in f["done"]:
                            f["done"].append(f"bs:{key}")
                    except Exception as e:
                        errors.append(str(e)[:160])
                    time.sleep(sleep)
        if rev_months:
            track = store._tracked() if hasattr(store, "_tracked") else None
            months = recent_rev_months(rev_months)
            for i, (y, m) in enumerate(months):
                for mk in ("sii", "otc"):
                    key = f"rev:{y}-{m:02d}:{mk}"
                    if key in f["done"] and i > 0:
                        continue
                    if deadline and time.time() > deadline:
                        break
                    if progress:
                        progress(f"月營收 {y}-{m:02d} {'上市' if mk == 'sii' else '上櫃'}")
                    try:
                        got = fetch_rev_month(y, m, mk, sess)
                        add = {c: [v] for c, v in got.items() if track is None or c in track}
                        store.merge_history({"rev": add})
                        n += len(add)
                        if key not in f["done"]:
                            f["done"].append(key)
                    except Exception as e:
                        errors.append(str(e)[:160])
                    time.sleep(sleep / 2)
    store.save_fin()
    return n, errors



# ─────────────────────────── GitHub Actions 轉存 ───────────────────────────

def run_relay(out_dir, codes_file=None, backfill_months=0):
    """抓一份 snapshot，累積歷史（只存 codes.txt 裡的代號；沒有 codes.txt 就不存歷史），
    輸出 market.json＋history.json 到 out_dir。第一次遇到的代號會補抓歷史。"""
    os.makedirs(out_dir, exist_ok=True)
    codes = []
    if codes_file and os.path.isfile(codes_file):
        with open(codes_file, "r", encoding="utf-8") as f:
            codes = re.findall(r"[0-9]{4,6}[A-Z]?", f.read())
    store = MarketStore(out_dir, track=set(codes))
    old_m = MarketStore._load(os.path.join(out_dir, "market.json"))
    old_h = MarketStore._load(os.path.join(out_dir, "history.json"))
    if old_m:
        store.snap = old_m
    if old_h:
        store.hist = old_h
        for k in MarketStore.KEYS:
            store.hist.setdefault(k, {})
    snap, errors = fetch_snapshot(progress=lambda k: print("抓取", k, flush=True))
    store.merge_snapshot(snap, {"label": "GitHub Actions（tw-market-relay）"})
    t0 = time.time()
    budget = float(os.environ.get("RELAY_MINUTES", "45")) * 60      # 一次最多跑多久（GitHub 免費額度友善）
    if codes and backfill_months:
        done = set(store.hist.setdefault("backfilled", []))
        need = [c for c in codes if c not in done]
        if need:
            print("補抓歷史（分批，剩", len(need), "檔）", flush=True)
            if INDEX_KEY not in store.hist["px"] or len(store.hist["px"][INDEX_KEY]) < 200:
                store.merge_history({"px": backfill_prices([], 13, progress=print, index=True)})
            chips_codes = set(store.hist.get("chips_codes") or [])
            if not store.hist.get("chips_backfilled") or any(c not in chips_codes for c in need):
                store.merge_chip_days(backfill_chips(20, progress=print))      # 新加入的代號也補近 20 日法人
                store.hist["chips_backfilled"] = True
                store.hist["chips_codes"] = sorted(chips_codes | set(codes))
            for c in need:
                if time.time() - t0 > budget * 0.6:
                    print("時間到，剩下的下次再補：", len(need) - len(done & set(need)), "檔", flush=True)
                    break
                store.merge_history({"pe": backfill_pe([c], backfill_months, progress=print)})
                vo = {}
                store.merge_history({"px": backfill_prices([c], 13, progress=print, index=False, vol_out=vo)})
                store.merge_history({"vol": vo})
                done.add(c)
                store.hist["backfilled"] = sorted(done)
            store.hist["backfill_left"] = [c for c in codes if c not in done]
        # 舊版沒存成交量：已補過價格的代號，補近 4 個月成交量（量比、量價圖用），分批
        vdone = set(store.hist.setdefault("vol_done", []))
        vneed = [c for c in codes if c in done and c not in vdone and len(store.hist["vol"].get(c) or []) < 60]
        for c in vneed:
            if time.time() - t0 > budget * 0.6:
                break
            vo = {}
            store.merge_history({"px": backfill_prices([c], 4, progress=print, index=False, vol_out=vo)})
            store.merge_history({"vol": vo})
            vdone.add(c)
            store.hist["vol_done"] = sorted(vdone)
        # 月均價（季節性分析用）：每檔補一次近 6 年，之後由每日股價自動延伸
        mdone = set(store.hist.setdefault("pxm_done", []))
        for c in [c for c in codes if c in done and c not in mdone]:
            if time.time() - t0 > budget * 0.7:
                print("月均價時間到，下次再補", flush=True)
                break
            rows = fetch_monthly_avg(c, 6, progress=print)
            if rows:
                store.merge_history({"pxm": {c: rows}})
            mdone.add(c)
            store.hist["pxm_done"] = sorted(mdone)
    # market.json 只留追蹤代號的籌碼／集保／成交量（全市場太大）
    if codes:
        for k in ("chips", "tdcc", "vol"):
            if store.snap.get(k):
                store.snap[k] = {c: v for c, v in store.snap[k].items() if c in set(codes)}
    MarketStore._save_json(os.path.join(out_dir, "market.json"), store.snap)
    MarketStore._save_json(os.path.join(out_dir, "history.json"), store.hist)
    try:                                   # 股票代號表（上市＋上櫃，一週一次）
        sl = os.path.join(out_dir, "stock_list.json")
        old = MarketStore._load(sl) or {}
        if (old.get("updated") or "") < (date.today() - timedelta(days=7)).isoformat():
            print("代號表：", refresh_stock_list(sl), "筆", flush=True)
    except Exception as e:
        print("警告：代號表更新失敗", e)
    try:                                   # 財報（三率、EPS）：OpenAPI 最新一季＋觀測站近 6 季（只補沒抓過的）
        store.fin = MarketStore._load(os.path.join(out_dir, "fin.json")) or {"q": {}, "done": []}
        nf, ferr = update_fin(store, progress=lambda k: print("抓取", k, flush=True), deadline=t0 + budget)
        try:
            update_extras(store, progress=lambda k: print("抓取", k, flush=True))
            MarketStore._save_json(os.path.join(out_dir, "market.json"), store.snap)   # 含風險警示
        except Exception as e:
            print("警告：風險警示／股利失敗", e)
        MarketStore._save_json(os.path.join(out_dir, "fin.json"), store.fin)
        try:                               # ETF 成分股（一週一次）
            store.etf = MarketStore._load(os.path.join(out_dir, "etf.json")) or {"funds": {}, "changes": []}
            ne, eerr = update_etf(store, etf_list(os.environ.get("ETF_LIST")), progress=lambda k: print(k, flush=True),
                                  deadline=t0 + budget)
            MarketStore._save_json(os.path.join(out_dir, "etf.json"), store.etf)
            print("ETF 成分股：", ne, "檔", "；".join(eerr[:3]))
        except Exception as e:
            print("警告：ETF 成分股失敗", e)
        MarketStore._save_json(os.path.join(out_dir, "history.json"), store.hist)   # 月營收歷史
        for junk in ("market_fin.json", "market_cache.json", "market_history.json"):
            try:
                os.remove(os.path.join(out_dir, junk))
            except OSError:
                pass
        print("財報：", nf, "筆", "；".join(ferr[:3]))
    except Exception as e:
        print("警告：財報失敗", e)
    for e in errors:
        print("警告：", e)
    print("完成：", len(snap["pe"]), "檔本益比、", len(snap["rev"]), "檔月營收")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--relay", help="輸出資料夾（GitHub Actions 用）")
    ap.add_argument("--codes", help="追蹤代號清單檔（累積歷史、補抓歷史用）")
    ap.add_argument("--backfill-months", type=int, default=0)
    ap.add_argument("--update-list", action="store_true", help="更新 tw_stock_list.json")
    ap.add_argument("--realtime", action="store_true", help="只抓盤中報價到 realtime.json（配合 --relay）")
    a = ap.parse_args()
    if a.update_list:
        print("代號表筆數：", refresh_stock_list())
    if a.relay and a.realtime:
        run_relay_realtime(a.relay, a.codes)
    elif a.relay:
        run_relay(a.relay, a.codes, a.backfill_months)
    if not (a.relay or a.update_list):
        ap.print_help()
        sys.exit(1)
