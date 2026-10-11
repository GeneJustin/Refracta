"""
dashboard_routes.py  -  Refracta · Dashboard otomatis berbasis AI

Cara pasang di app.py (sebelum blok `if __name__ == "__main__":`):

    from dashboard_routes import register_dashboard
    register_dashboard(app, supabase, login_required)

Dependensi tambahan:  pip install numpy openpyxl   (openpyxl hanya untuk .xlsx)

Alur:
  1. Baca file (CSV/Excel dari PC, atau CSV hasil scan dari history)
  2. Preprocessing otomatis (nama kolom, teks, angka berformat Rp/$/%, tanggal, tipe kolom)
  3. AI (Gemini -> OpenRouter -> heuristik) merancang SPEC dashboard (kolom + agregasi)
  4. Server menghitung semua angka dari data asli (AI tidak pernah mengarang angka)
  5. Prakiraan linear (hanya jika ada kolom tanggal + >= 8 periode)
"""

import csv
import io
import json
import math
import os
import re
import time
import warnings
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests
from flask import jsonify, render_template, request, session

BUCKET = "refracta-files"
MAX_FILE_BYTES = 15 * 1024 * 1024
MAX_ROWS = 300_000
MAX_SOURCES = 5
MAX_DATASETS = 4
MAX_CHARTS = 14

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

AGGS = {"sum", "mean", "median", "max", "min", "count", "nunique"}
AGG_LABEL = {"sum": "Total", "mean": "Rata-rata", "median": "Median",
             "max": "Maksimum", "min": "Minimum"}
NA_TOKENS = {"", "nan", "none", "null", "n/a", "na", "#n/a", "-", "--", "nil"}
FREQ = {"D": "D", "W": "7D", "M": "MS", "Y": "YS"}
TIME_FMT = {"D": "%Y-%m-%d", "W": "%Y-%m-%d", "M": "%Y-%m", "Y": "%Y"}

SPANS = {"bar": 6, "line": 12, "area": 12, "donut": 6, "treemap": 6,
         "scatter": 6, "histogram": 6, "box": 6, "correlation": 6,
         "heatmap": 6, "radar": 6, "table": 12, "forecast": 12}

ID_NAME = re.compile(
    r"(?i)(^|[\s_\-])(id|uuid|guid|kode|code|no|nomor|number|ref|sku)($|[\s_\-])")

ID_MONTHS = {
    "januari": "Jan", "februari": "Feb", "maret": "Mar", "april": "Apr",
    "mei": "May", "juni": "Jun", "juli": "Jul", "agustus": "Aug",
    "september": "Sep", "oktober": "Oct", "november": "Nov",
    "desember": "Dec", "agu": "Aug", "agt": "Aug", "okt": "Oct", "des": "Dec",
}
ID_MONTH_RE = re.compile(r"(?i)\b(" + "|".join(ID_MONTHS) + r")\b")


# =========================================================
# UTIL
# =========================================================

def now_iso():
    return datetime.now(timezone.utc).isoformat()


def clean(o):
    """Ubah numpy/pandas ke tipe JSON aman (NaN -> None)."""
    if o is pd.NaT or o is None:
        return None
    if isinstance(o, dict):
        return {str(k): clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if isinstance(o, np.ndarray):
        return clean(o.tolist())
    if isinstance(o, (np.bool_, bool)):
        return bool(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating, float)):
        f = float(o)
        return None if (math.isnan(f) or math.isinf(f)) else round(f, 6)
    if isinstance(o, (pd.Timestamp, datetime)):
        return o.isoformat()
    return o


def _clamp(v, lo, hi, default):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def _fmt_num(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    a = abs(v)
    for limit, suffix in ((1e12, " T"), (1e9, " M"), (1e6, " jt"), (1e3, " rb")):
        if a >= limit:
            return f"{v / limit:.2f}".rstrip("0").rstrip(".") + suffix
    return f"{v:,.2f}".rstrip("0").rstrip(".")


def _short(v):
    v = float(v)
    if abs(v) >= 1000 or v == int(v):
        return _fmt_num(v)
    return f"{v:.3g}"


# =========================================================
# BACA FILE
# =========================================================

def read_table(raw, filename):
    ext = os.path.splitext(filename or "")[1].lower()

    if ext in (".xlsx", ".xlsm", ".xls"):
        try:
            sheets = pd.read_excel(io.BytesIO(raw), sheet_name=None, nrows=MAX_ROWS)
        except ImportError:
            raise ValueError(
                "Membaca Excel membutuhkan paket openpyxl. "
                "Jalankan: pip install openpyxl")
        except Exception:
            raise ValueError(f"File Excel '{filename}' tidak bisa dibaca.")

        for df in sheets.values():
            if df is not None and not df.dropna(how="all").empty:
                return df
        raise ValueError(f"File '{filename}' tidak berisi data.")

    text = None
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue

    if not text or not text.strip():
        raise ValueError(f"File '{filename}' kosong.")

    try:
        sep = csv.Sniffer().sniff(text[:20000], delimiters=",;\t|").delimiter
    except csv.Error:
        sep = "\t" if ext == ".tsv" else ","

    try:
        return pd.read_csv(
            io.StringIO(text), sep=sep, nrows=MAX_ROWS,
            skipinitialspace=True, on_bad_lines="skip")
    except Exception:
        raise ValueError(f"File '{filename}' tidak bisa dibaca sebagai CSV.")


# =========================================================
# PREPROCESSING
# =========================================================

def _is_texty(s):
    return s.dtype == object or pd.api.types.is_string_dtype(s)


def _clean_text(s):
    missing = s.isna()
    t = (s.astype(str)
         .str.replace(r"\s+", " ", regex=True)
         .str.strip())
    return t.mask(missing | t.str.lower().isin(NA_TOKENS))


def _parse_number(val, dot_thousands=False):
    if val is None:
        return np.nan
    if isinstance(val, (int, float, np.integer, np.floating)) \
            and not isinstance(val, bool):
        return float(val)

    s = str(val).strip().replace("\u00a0", "").replace(" ", "")
    if not s:
        return np.nan

    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]

    s = re.sub(r"(?i)^(rp\.?|idr|usd|eur|us\$)", "", s)
    s = re.sub(r"(?i)(idr|usd|eur)$", "", s)
    s = re.sub(r"[$€£¥%]", "", s)

    if s.startswith("-"):
        neg, s = True, s[1:]
    elif s.startswith("+"):
        s = s[1:]
    if s.endswith("-"):
        neg, s = True, s[:-1]

    if not s or not re.fullmatch(r"[\d.,]+", s) or not re.search(r"\d", s):
        return np.nan

    if "." in s and "," in s:
        if s.rfind(".") > s.rfind(","):
            s = s.replace(",", "")
        else:
            s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        if s.count(",") > 1:
            s = s.replace(",", "")
        else:
            before, after = s.split(",")
            if len(after) == 3 and before not in ("", "0"):
                s = before + after
            else:
                s = before + "." + after
    elif "." in s:
        if s.count(".") > 1:
            s = s.replace(".", "")
        elif dot_thousands and len(s.split(".")[1]) == 3:
            s = s.replace(".", "")

    try:
        v = float(s)
    except ValueError:
        return np.nan
    return -v if neg else v


def _try_numeric(s):
    sample = s.dropna().astype(str).head(400)
    if sample.empty:
        return None
    if sample.str.fullmatch(r"0\d+").mean() > 0.3:      # kode/telepon
        return None

    dot_th = bool(sample.str.contains(
        r"\d\.\d{3}\.\d|\d\.\d{3},\d|^(?i:rp)\.?\s?\d{1,3}\.\d{3}$",
        regex=True).any())

    parsed = sample.map(lambda v: _parse_number(v, dot_th))
    if parsed.notna().mean() < 0.9:
        return None

    full = s.map(lambda v: _parse_number(v, dot_th)).astype(float)
    if full.notna().sum() < 0.9 * s.notna().sum():
        return None
    return full


def _en_months(x):
    return x.str.replace(ID_MONTH_RE, lambda m: ID_MONTHS[m.group(0).lower()],
                         regex=True)


def _to_dt(x, dayfirst):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            return pd.to_datetime(x, errors="coerce", dayfirst=dayfirst,
                                  format="mixed")
        except (TypeError, ValueError):
            return pd.to_datetime(x, errors="coerce", dayfirst=dayfirst)


def _try_datetime(s):
    sample = s.dropna().astype(str).head(300)
    if sample.empty:
        return None

    pattern = (r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}"
               r"|\d{1,2}\s+[A-Za-z]{3,}\s+\d{2,4}|\d{4}-\d{2}-\d{2}T")
    if sample.str.contains(pattern, regex=True).mean() < 0.9:
        return None

    sample = _en_months(sample)
    best = None
    for dayfirst in (True, False):
        ratio = _to_dt(sample, dayfirst).notna().mean()
        if best is None or ratio > best[0]:
            best = (ratio, dayfirst)

    if best[0] < 0.9:
        return None

    full = _to_dt(_en_months(s.astype(str).where(s.notna())), best[1])
    if getattr(full.dt, "tz", None) is not None:
        full = full.dt.tz_localize(None)
    if full.notna().sum() < 0.9 * s.notna().sum():
        return None
    return full


def _convert_columns(df, notes):
    for c in list(df.columns):
        s = df[c]
        if _is_texty(s):
            s = _clean_text(s)
            df[c] = s
            num = _try_numeric(s)
            if num is not None:
                df[c] = num
                notes.append(f"Kolom '{c}' dikonversi menjadi angka.")
                continue
            dt = _try_datetime(s)
            if dt is not None:
                df[c] = dt
                notes.append(f"Kolom '{c}' dikenali sebagai tanggal.")
        elif pd.api.types.is_datetime64_any_dtype(s):
            if getattr(s.dt, "tz", None) is not None:
                df[c] = s.dt.tz_localize(None)
    return df


def classify(df):
    roles = {}
    for c in df.columns:
        s = df[c]
        nn = int(s.notna().sum())
        nun = int(s.nunique(dropna=True))
        idish = bool(ID_NAME.search(c))

        if pd.api.types.is_datetime64_any_dtype(s):
            roles[c] = "datetime"
        elif pd.api.types.is_bool_dtype(s):
            roles[c] = "category"
        elif pd.api.types.is_numeric_dtype(s):
            vals = s.dropna()
            integer_like = bool(len(vals)) and bool((vals % 1 == 0).all())
            if nun <= 1:
                roles[c] = "constant"
            elif idish and integer_like and nn >= 10 and nun / nn > 0.9:
                roles[c] = "id"
            elif integer_like and nun <= 8:
                roles[c] = "discrete"
            else:
                roles[c] = "numeric"
        else:
            if nun <= 1:
                roles[c] = "constant"
            elif idish and nn >= 10 and nun / nn > 0.9:
                roles[c] = "id"
            elif nn >= 20 and nun / nn > 0.9:
                roles[c] = "text"
            elif nun <= 60 or (nn and nun / nn <= 0.5):
                roles[c] = "category"
            else:
                roles[c] = "text"
    return roles


def preprocess(df, name):
    notes = []
    df = df.copy()
    rows_in = len(df)

    cols, seen = [], {}
    for i, c in enumerate(df.columns):
        c = re.sub(r"\s+", " ", str(c)).strip()
        if not c or c.lower().startswith("unnamed:"):
            c = f"Kolom {i + 1}"
        if c in seen:
            seen[c] += 1
            c = f"{c} ({seen[c]})"
        else:
            seen[c] = 0
        cols.append(c)
    df.columns = cols

    for c in list(df.columns):
        if _is_texty(df[c]):
            df[c] = _clean_text(df[c])

    df = df.dropna(axis=0, how="all").dropna(axis=1, how="all")
    if rows_in > len(df):
        notes.append(f"{rows_in - len(df)} baris kosong dibuang.")

    df = _convert_columns(df, notes).reset_index(drop=True)

    dups = int(df.duplicated().sum()) if len(df) else 0
    if dups:
        notes.append(
            f"Ada {dups} baris identik (tidak dihapus karena bisa jadi "
            f"transaksi berulang yang valid).")

    if len(df):
        for c in df.columns:
            miss = df[c].isna().mean() * 100
            if miss >= 20:
                notes.append(f"Kolom '{c}' kosong {miss:.0f}%.")

    return df, notes


def merge_same_schema(items):
    """Gabungkan file dengan susunan kolom sama menjadi satu dataset."""
    groups, order = {}, []
    for it in items:
        sig = tuple(sorted(c.lower() for c in it["df"].columns))
        if sig not in groups:
            groups[sig] = []
            order.append(sig)
        groups[sig].append(it)

    result = []
    for sig in order:
        members = groups[sig]
        if len(members) == 1:
            result.append(members[0])
            continue

        base_cols = list(members[0]["df"].columns)
        lower_map = {c.lower(): c for c in base_cols}
        frames, notes = [], []
        for m in members:
            f = m["df"].rename(columns=lambda c: lower_map.get(c.lower(), c)).copy()
            f["Sumber File"] = m["name"]
            frames.append(f)
            notes.extend(m["notes"])

        combined = pd.concat(frames, ignore_index=True)
        combined = _convert_columns(combined, [])
        notes.insert(0, f"{len(members)} file dengan kolom sama digabung "
                        f"(kolom 'Sumber File' ditambahkan).")
        result.append({
            "name": f"Gabungan ({len(members)} file)",
            "df": combined,
            "notes": notes,
        })
    return result


# =========================================================
# PROFIL UNTUK AI
# =========================================================

def _r(x):
    try:
        return float(f"{float(x):.4g}")
    except (TypeError, ValueError):
        return None


def build_profile(datasets):
    out = []
    for did, d in datasets.items():
        df, roles = d["df"], d["roles"]
        cols = []
        for c in list(df.columns)[:40]:
            s = df[c]
            info = {
                "name": c,
                "role": roles[c],
                "unique": int(s.nunique(dropna=True)),
                "missing_pct": round(float(s.isna().mean() * 100), 1),
            }
            r = roles[c]
            if r in ("numeric", "discrete"):
                info.update(min=_r(s.min()), max=_r(s.max()),
                            mean=_r(s.mean()), median=_r(s.median()))
            elif r == "datetime":
                info.update(min=str(s.min())[:10], max=str(s.max())[:10])
            else:
                vc = s.dropna().astype(str).value_counts().head(5)
                info["top_values"] = {k[:40]: int(v) for k, v in vc.items()}
            cols.append(info)
        out.append({"id": did, "name": d["name"], "rows": int(len(df)),
                    "columns": cols})
    return out


# =========================================================
# AI -> SPEC
# =========================================================

PROMPT = """You are a senior data analyst and BI designer. Design an informative,
elegant analytics dashboard for the datasets below.
You do NOT compute numbers: the server aggregates data from the columns you
reference. Reply with ONE JSON object only (no markdown, no commentary).

DATASETS (profile):
__PROFILE__

OUTPUT JSON:
{
  "title": "short dashboard title (max 60 chars)",
  "summary": "2-3 sentences: what the data is and the main angle of the dashboard",
  "kpis": [ {"dataset":"d1","label":"...","column":"<column or null>",
             "agg":"sum|mean|median|max|min|count|nunique|top",
             "prefix":"Rp or empty","suffix":""} ],          // 3 to 6
  "charts": [ ... ],                                          // 7 to 12
  "forecast": {"dataset":"d1","date":"<datetime column>",
               "value":"<numeric column or null>","agg":"sum|mean|count"} or null,
  "insights": ["...", "..."]                                  // 3 to 5
}

CHART OBJECT (column names must exist exactly in that dataset):
{"type":"...","dataset":"d1","title":"...","description":"one short sentence on why it matters", ...fields}
Types and fields:
- "bar": x (category/discrete/datetime/text), y (numeric or null), agg, series (optional category), top_n, sort ("desc|asc|none"), horizontal (bool), stacked (bool)
- "line" / "area": x (datetime preferred), y, agg, series (optional)
- "donut": x (category), y, agg, top_n
- "treemap": x, y, agg, top_n
- "scatter": x (numeric), y (numeric), color (optional category)
- "histogram": x (numeric), bins (optional int)
- "box": y (numeric), x (optional category)
- "correlation": no fields (uses all numeric columns)
- "heatmap": x (category), y (category), value (optional numeric), agg
- "radar": x (category), metrics ([3-6 numeric columns]), agg
- "table": columns ([max 8]), sort_by, top_n

RULES:
- Choose chart types that fit the data; use at least 5 different types; never repeat the same chart.
- Never use columns with role "id" or "constant". Role "text" only as x in bar/treemap (counts, top_n).
- agg "count" with y=null counts rows. Use sum for amounts, mean for rates/scores/prices.
- If there are no numeric columns, use counts (y=null, agg "count") of category/text columns.
- If a datetime column exists, include a time trend; set "forecast" only if a numeric measure (or counts) and >= 8 time periods exist, otherwise null.
- Write title, description, summary and insights in Indonesian. Insights must ONLY rely on facts visible in the profile; never invent numbers.
"""


def _extract_json(text):
    text = re.sub(r"```(?:json)?", "", text or "", flags=re.IGNORECASE)
    dec = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = dec.raw_decode(text[m.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and ("charts" in obj or "kpis" in obj):
            return obj
    return None


def call_llm_for_spec(profile):
    from_env = os.getenv
    prompt = PROMPT.replace("__PROFILE__", json.dumps(profile, ensure_ascii=False))
    errors = []

    keys = [k for k in (from_env("GEMINI_API_KEY"), from_env("GEMINI_API_KEY2"),
                        from_env("GEMINI_API_KEY3")) if k]
    model = from_env("GEMINI_MODEL", "gemini-3.8-flash")

    if keys:
        try:
            from google import genai
        except ImportError:
            genai = None
            errors.append("google-genai tidak terpasang")

        if genai:
            for i, key in enumerate(keys, start=1):
                try:
                    client = genai.Client(api_key=key)
                    response = None
                    for attempt in range(3):
                        try:
                            response = client.models.generate_content(
                                model=model, contents=[prompt],
                                config={"response_mime_type": "application/json",
                                        "temperature": 0.3})
                            break
                        except Exception as e:
                            msg = str(e)
                            temp = any(t in msg for t in ("503", "UNAVAILABLE", "429"))
                            if temp and attempt < 2:
                                time.sleep(2 * (attempt + 1))
                                continue
                            raise
                    obj = _extract_json(response.text)
                    if obj:
                        print(f"Dashboard spec: Gemini key {i} OK")
                        return obj, "gemini"
                    errors.append(f"gemini {i}: JSON tidak valid")
                except Exception as e:
                    errors.append(f"gemini {i}: {e}")
                    print("Dashboard Gemini error:", repr(e))

    or_key = from_env("OPENROUTER_API_KEY")
    if or_key:
        for attempt in range(2):
            try:
                resp = requests.post(
                    OPENROUTER_URL,
                    headers={"Authorization": f"Bearer {or_key}",
                             "Content-Type": "application/json",
                             "HTTP-Referer": "http://localhost:5000",
                             "X-Title": "Refracta"},
                    json={"model": "openrouter/free",
                          "messages": [{"role": "user", "content": prompt}],
                          "temperature": 0.3, "max_tokens": 4000,
                          "response_format": {"type": "json_object"},
                          "reasoning": {"exclude": True}},
                    timeout=90)
                if resp.status_code != 200:
                    raise Exception(f"HTTP {resp.status_code}")
                content = resp.json()["choices"][0]["message"]["content"]
                if isinstance(content, list):
                    content = "".join(p.get("text", "") for p in content
                                      if isinstance(p, dict))
                obj = _extract_json(content)
                if obj:
                    print("Dashboard spec: OpenRouter OK")
                    return obj, "openrouter"
                errors.append("openrouter: JSON tidak valid")
            except Exception as e:
                errors.append(f"openrouter: {e}")
                print("Dashboard OpenRouter error:", repr(e))

    print("Dashboard AI gagal, pakai heuristik:", errors[-3:])
    return None, "heuristic"


def normalize_spec(raw):
    spec = {"title": "", "summary": "", "kpis": [], "charts": [],
            "forecast": None, "insights": []}
    if not isinstance(raw, dict):
        return spec

    spec["title"] = str(raw.get("title") or "")[:80]
    spec["summary"] = str(raw.get("summary") or "")[:600]
    spec["kpis"] = [k for k in (raw.get("kpis") or []) if isinstance(k, dict)][:8]
    spec["charts"] = [c for c in (raw.get("charts") or []) if isinstance(c, dict)][:MAX_CHARTS]
    spec["forecast"] = raw.get("forecast") if isinstance(raw.get("forecast"), dict) else None
    spec["insights"] = [str(i)[:300] for i in (raw.get("insights") or [])
                        if isinstance(i, str) and i.strip()][:5]
    return spec


AVG_NAME = re.compile(
    r"(?i)harga|price|rate|persen|percent|score|skor|rating|ratio|rasio|suhu|usia|umur|age|%")


def _default_agg(col):
    """Kolom harga/skor/rasio dirata-ratakan, kolom jumlah/nominal dijumlahkan."""
    return "mean" if AVG_NAME.search(col) else "sum"


def heuristic_spec(datasets):
    kpis, charts = [], []
    for did, d in datasets.items():
        df, roles = d["df"], d["roles"]
        nums = [c for c, r in roles.items() if r == "numeric"]
        # utamakan kolom yang wajar dijumlahkan (omzet, qty) sebagai ukuran utama
        nums.sort(key=lambda c: _default_agg(c) != "sum")
        discs = [c for c, r in roles.items() if r == "discrete"]
        cats = [c for c, r in roles.items() if r == "category"]
        texts = [c for c, r in roles.items() if r == "text"]
        dates = [c for c, r in roles.items() if r == "datetime"]

        main_cat = cats[0] if cats else (discs[0] if discs else None)
        main_num = nums[0] if nums else None
        agg = _default_agg(main_num) if main_num else "count"
        local = []

        kpis.append({"dataset": did, "label": "Total baris", "agg": "count"})
        for n in nums[:3]:
            a = _default_agg(n)
            kpis.append({"dataset": did, "column": n, "agg": a,
                         "label": f"{AGG_LABEL[a]} {n}"})
        if main_cat:
            kpis.append({"dataset": did, "label": f"{main_cat} terbanyak",
                         "column": main_cat, "agg": "top"})

        if main_cat:
            local.append({"type": "donut", "dataset": did, "x": main_cat,
                          "y": main_num, "agg": agg,
                          "title": f"Komposisi per {main_cat}"})
            local.append({"type": "bar", "dataset": did, "x": main_cat,
                          "y": main_num, "agg": agg, "horizontal": True,
                          "title": f"Peringkat {main_cat}"})
        if dates:
            local.append({"type": "line", "dataset": did, "x": dates[0],
                          "y": main_num, "agg": agg, "title": "Tren dari waktu ke waktu"})
        if texts:
            local.append({"type": "bar", "dataset": did, "x": texts[0], "y": None,
                          "agg": "count", "horizontal": True, "top_n": 10,
                          "title": f"{texts[0]} paling sering muncul"})
            local.append({"type": "treemap", "dataset": did, "x": texts[0],
                          "y": None, "agg": "count", "top_n": 20,
                          "title": f"Peta {texts[0]}"})
        elif main_cat and len(cats) > 0:
            local.append({"type": "treemap", "dataset": did, "x": main_cat,
                          "y": main_num, "agg": agg, "title": f"Peta {main_cat}"})
        for n in nums[:2]:
            local.append({"type": "histogram", "dataset": did, "x": n,
                          "title": f"Sebaran {n}"})
        if len(nums) >= 2:
            local.append({"type": "scatter", "dataset": did, "x": nums[0],
                          "y": nums[1], "title": f"{nums[0]} vs {nums[1]}"})
        if len(nums) >= 3:
            local.append({"type": "correlation", "dataset": did,
                          "title": "Korelasi antar variabel"})
        if main_cat and main_num:
            local.append({"type": "box", "dataset": did, "x": main_cat, "y": main_num,
                          "title": f"Sebaran {main_num} per {main_cat}"})
        if len(cats) >= 2:
            local.append({"type": "heatmap", "dataset": did, "x": cats[0], "y": cats[1],
                          "title": f"{cats[0]} × {cats[1]}"})
        if len(nums) >= 3 and cats:
            local.append({"type": "radar", "dataset": did, "x": cats[0],
                          "metrics": nums[:5], "agg": "mean",
                          "title": f"Profil {cats[0]}"})
        charts.extend(local[:9])

    return {"title": "", "summary": "", "kpis": kpis[:8],
            "charts": charts[:12], "forecast": None, "insights": []}


# =========================================================
# BUILDER CHART
# =========================================================

def _resolve(df, name):
    if name is None:
        return None
    name = str(name).strip()
    if name in df.columns:
        return name
    low = {c.lower(): c for c in df.columns}
    return low.get(name.lower())


def _norm_cs(cs):
    cs = dict(cs)
    for k in ("y", "series", "color", "value", "column", "x", "date"):
        v = cs.get(k)
        if v is None or str(v).strip().lower() in ("", "none", "null", "count", "jumlah"):
            cs[k] = None
    return cs


def _pick_dataset(datasets, cs):
    pref = str(cs.get("dataset") or "").strip()
    order = list(datasets.values())
    if pref in datasets:
        d = datasets[pref]
        order.remove(d)
        order.insert(0, d)
    elif pref:
        for d in order:
            if d["name"].lower() == pref.lower():
                order.remove(d)
                order.insert(0, d)
                break

    keys = [cs.get(k) for k in ("x", "y", "series", "color", "date", "value", "column")
            if cs.get(k)]
    if isinstance(cs.get("metrics"), list):
        keys += [m for m in cs["metrics"] if m]
    if isinstance(cs.get("columns"), list):
        keys += [m for m in cs["columns"] if m]

    for d in order:
        if all(_resolve(d["df"], k) for k in keys):
            return d
    raise ValueError("Kolom tidak ditemukan.")


def _is_measure(roles, c):
    return c is not None and roles.get(c) in ("numeric", "discrete")


def _cat_ok(roles, c):
    return roles.get(c) in ("category", "discrete", "text")


def _resolve_y(df, roles, cs):
    agg = str(cs.get("agg") or "sum").lower()
    if agg not in AGGS:
        agg = "sum"
    y = _resolve(df, cs.get("y"))
    if y is None or agg == "count":
        return None, "count"
    if agg == "nunique":
        return y, "nunique"
    if not _is_measure(roles, y):
        return None, "count"
    return y, agg


def _measure_label(y, agg):
    if y is None or agg == "count":
        return "Jumlah"
    if agg == "nunique":
        return f"Jumlah unik {y}"
    return f"{AGG_LABEL.get(agg, agg)} {y}"


def _aggregate(df, keys, y, agg):
    g = df.groupby(keys, dropna=True)
    if y is None or agg == "count":
        return g.size()
    if agg == "nunique":
        return g[y].nunique()
    return getattr(g[y], agg)()


def _time_bucket(s, prefer=None):
    valid = s.dropna()
    if valid.empty:
        raise ValueError("tanggal kosong")
    span = (valid.max() - valid.min()).days
    if prefer:
        code = prefer
    elif span <= 60:
        code = "D"
    elif span <= 240:
        code = "W"
    elif span <= 1500:
        code = "M"
    else:
        code = "Y"

    if code == "D":
        key = s.dt.normalize()
    elif code == "W":
        key = (s - pd.to_timedelta(s.dt.dayofweek, unit="D")).dt.normalize()
    elif code == "M":
        key = s.dt.to_period("M").dt.to_timestamp()
    else:
        key = s.dt.to_period("Y").dt.to_timestamp()
    return key, code


def _fill_time(tbl, code, additive):
    if len(tbl) >= 2:
        full = pd.date_range(tbl.index.min(), tbl.index.max(), freq=FREQ[code])
        if len(full) <= 800:
            tbl = tbl.reindex(full)
            if additive:
                tbl = tbl.fillna(0)
    return tbl


def _time_labels(index, code):
    return [ts.strftime(TIME_FMT[code]) for ts in index]


def _category_chart(df, roles, cs, ctx, kind):
    x = _resolve(df, cs.get("x"))
    if not x:
        raise ValueError("x tidak ada")
    xr = roles.get(x)
    if kind in ("line", "area"):
        if xr not in ("datetime", "discrete", "category"):
            raise ValueError("x tidak cocok untuk line")
    elif xr not in ("datetime", "category", "discrete", "text"):
        raise ValueError("x tidak cocok untuk bar")

    is_time = xr == "datetime"
    y, agg = _resolve_y(df, roles, cs)
    series = _resolve(df, cs.get("series"))
    if series and (series == x or roles.get(series) not in ("category", "discrete")):
        series = None

    label = _measure_label(y, agg)
    additive = agg in ("sum", "count", "nunique")
    top_n = _clamp(cs.get("top_n"), 3, 30, 12)

    work, code = df, None
    xk = x
    if is_time:
        key, code = _time_bucket(df[x])
        work = df.assign(_xk=key)
        xk = "_xk"

    work = work[work[xk].notna()]
    if work.empty:
        raise ValueError("data kosong")

    keys = [xk] + ([series] if series else [])
    res = _aggregate(work, keys, y, agg)

    if series:
        tbl = res.unstack(series)
        if additive:
            tbl = tbl.fillna(0)
        tot = tbl.sum().sort_values(ascending=False)
        if len(tot) > 6:
            keep = list(tot.index[:6])
            rest = tbl.drop(columns=keep)
            tbl = tbl[keep].copy()
            if additive:
                tbl["Lainnya"] = rest.sum(axis=1)
        main = tbl.sum(axis=1)
    else:
        tbl = res.to_frame(name=label)
        main = res

    if is_time:
        tbl = _fill_time(tbl.sort_index(), code, additive)
        categories = _time_labels(tbl.index, code)
    else:
        appear = [v for v in pd.unique(work[x]) if v in tbl.index]
        sort = str(cs.get("sort") or "").lower()
        if kind in ("line", "area"):
            idx = sorted(tbl.index) if xr == "discrete" else appear[:40]
        else:
            idx = list(main.sort_values(ascending=False).index[:top_n])
            if xr == "discrete" and sort in ("", "none"):
                idx = sorted(idx)
            elif sort == "asc":
                idx = list(main.loc[idx].sort_values(ascending=True).index)
            elif sort == "none":
                idx = [v for v in appear if v in set(idx)]
        tbl = tbl.loc[idx]
        categories = [str(v) for v in tbl.index]

    out_series = [{"name": str(c), "data": tbl[c].tolist()} for c in tbl.columns]

    # insight deterministik
    if not series and not tbl.empty:
        vals = tbl.iloc[:, 0]
        if is_time and vals.notna().any():
            if ctx.get("peak", 0) < 2:
                i = int(np.nanargmax(vals.values))
                ctx["facts"].append(
                    f"Puncak {label.lower()} terjadi pada {categories[i]} "
                    f"({_fmt_num(vals.iloc[i])}).")
                ctx["peak"] = ctx.get("peak", 0) + 1
        elif kind == "bar" and agg in ("sum", "count") and ctx.get("share", 0) < 2:
            total = float(main.sum())
            if total > 0:
                what = "jumlah data" if label == "Jumlah" else label.lower()
                ctx["facts"].append(
                    f"'{main.idxmax()}' menyumbang {main.max() / total * 100:.0f}% "
                    f"dari {what} pada '{x}'.")
                ctx["share"] = ctx.get("share", 0) + 1

    return {
        "categories": categories,
        "series": out_series,
        "stacked": bool(cs.get("stacked")) and len(out_series) > 1,
        "horizontal": bool(cs.get("horizontal")) and not is_time,
        "xLabel": x,
        "yLabel": label,
        "title": f"{label} per {x}" if not is_time else f"{label} dari waktu ke waktu",
    }


def sort_is_desc(cs):
    return str(cs.get("sort") or "desc").lower() == "desc"


def build_bar(df, roles, cs, ctx):
    return _category_chart(df, roles, cs, ctx, "bar")


def build_line(df, roles, cs, ctx):
    return _category_chart(df, roles, cs, ctx, "line")


def build_area(df, roles, cs, ctx):
    return _category_chart(df, roles, cs, ctx, "area")


def _share_data(df, roles, cs, top_n_default, max_n):
    x = _resolve(df, cs.get("x"))
    if not x or not _cat_ok(roles, x):
        raise ValueError("x tidak cocok")
    y, agg = _resolve_y(df, roles, cs)
    res = _aggregate(df[df[x].notna()], [x], y, agg).dropna()
    res = res[res > 0].sort_values(ascending=False)
    if res.empty:
        raise ValueError("tidak ada nilai positif")
    top_n = _clamp(cs.get("top_n"), 3, max_n, top_n_default)
    additive = agg in ("sum", "count")
    if len(res) > top_n:
        head, rest = res.iloc[:top_n].copy(), res.iloc[top_n:]
        if additive:
            head["Lainnya"] = rest.sum()
        res = head
    data = [{"name": str(k), "value": float(v)} for k, v in res.items()]
    return x, y, agg, data, (float(res.sum()) if additive else None)


def build_donut(df, roles, cs, ctx):
    x, y, agg, data, total = _share_data(df, roles, cs, 7, 12)
    label = _measure_label(y, agg)
    return {"data": data, "total": total, "measure": label,
            "title": f"Komposisi {label.lower()} per {x}"}


def build_treemap(df, roles, cs, ctx):
    x, y, agg, data, total = _share_data(df, roles, cs, 20, 30)
    label = _measure_label(y, agg)
    return {"data": data, "total": total, "measure": label,
            "title": f"Peta {label.lower()} per {x}"}


def build_scatter(df, roles, cs, ctx):
    x, y = _resolve(df, cs.get("x")), _resolve(df, cs.get("y"))
    if not (_is_measure(roles, x) and _is_measure(roles, y)) or x == y:
        raise ValueError("scatter butuh dua kolom numerik")
    color = _resolve(df, cs.get("color"))
    if color and (roles.get(color) not in ("category", "discrete")
                  or df[color].nunique() > 6):
        color = None

    w = df[[x, y] + ([color] if color else [])].dropna()
    if len(w) < 5 or w[x].nunique() < 2 or w[y].nunique() < 2:
        raise ValueError("data scatter kurang")

    r = float(w[x].corr(w[y]))
    slope, intercept = np.polyfit(w[x].astype(float), w[y].astype(float), 1)
    x0, x1 = float(w[x].min()), float(w[x].max())

    if len(w) > 800:
        w = w.sample(800, random_state=0)

    if color:
        series = [{"name": str(g), "data": sub[[x, y]].values.tolist()}
                  for g, sub in w.groupby(color)]
    else:
        series = [{"name": f"{x} vs {y}", "data": w[[x, y]].values.tolist()}]

    if abs(r) >= 0.6 and ctx.get("corr", 0) < 1:
        arah = "positif" if r > 0 else "negatif"
        ctx["facts"].append(f"'{x}' dan '{y}' berkorelasi {arah} kuat (r = {r:.2f}).")
        ctx["corr"] = ctx.get("corr", 0) + 1

    return {"series": series, "xLabel": x, "yLabel": y, "r": r,
            "trend": [[x0, slope * x0 + intercept], [x1, slope * x1 + intercept]],
            "title": f"{x} vs {y}"}


def build_histogram(df, roles, cs, ctx):
    x = _resolve(df, cs.get("x"))
    if not _is_measure(roles, x):
        raise ValueError("histogram butuh kolom numerik")
    vals = df[x].dropna().astype(float)
    if len(vals) < 5 or vals.nunique() < 2:
        raise ValueError("data histogram kurang")

    bins = cs.get("bins")
    if isinstance(bins, (int, float)) and 3 <= int(bins) <= 40:
        edges = np.histogram_bin_edges(vals, bins=int(bins))
    else:
        edges = np.histogram_bin_edges(vals, bins="auto")
        if len(edges) - 1 > 40:
            edges = np.histogram_bin_edges(vals, bins=40)
        if len(edges) - 1 < 5:
            edges = np.histogram_bin_edges(vals, bins=min(10, max(5, vals.nunique())))

    counts, edges = np.histogram(vals, bins=edges)
    labels = [f"{_short(edges[i])} – {_short(edges[i + 1])}" for i in range(len(counts))]
    return {"categories": labels, "data": counts.tolist(), "xLabel": x,
            "stats": {"mean": vals.mean(), "median": vals.median(),
                      "std": vals.std(), "min": vals.min(), "max": vals.max(),
                      "n": len(vals)},
            "title": f"Sebaran {x}"}


def build_box(df, roles, cs, ctx):
    y = _resolve(df, cs.get("y"))
    if not _is_measure(roles, y):
        raise ValueError("box butuh y numerik")
    x = _resolve(df, cs.get("x"))
    if x and not _cat_ok(roles, x):
        x = None

    if x:
        top = df[x].dropna().astype(str).value_counts().index[:8]
        groups = [(g, df.loc[df[x].astype(str) == g, y].dropna().astype(float))
                  for g in top]
    else:
        groups = [("Semua data", df[y].dropna().astype(float))]

    names, boxes, outliers = [], [], []
    for g, v in groups:
        if len(v) < 5:
            continue
        q1, med, q3 = v.quantile([.25, .5, .75])
        iqr = q3 - q1
        lo_f, hi_f = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        inside = v[(v >= lo_f) & (v <= hi_f)]
        idx = len(names)
        names.append(str(g)[:30])
        boxes.append([inside.min(), q1, med, q3, inside.max()])
        out = v[(v < lo_f) | (v > hi_f)].head(30)
        outliers.extend([[idx, float(o)] for o in out])

    if not boxes:
        raise ValueError("data box kurang")
    return {"categories": names, "boxes": boxes, "outliers": outliers[:150],
            "yLabel": y, "title": f"Sebaran {y}" + (f" per {x}" if x else "")}


def build_correlation(df, roles, cs, ctx):
    cols = [c for c, r in roles.items()
            if r in ("numeric", "discrete") and df[c].nunique() > 2]
    cols = sorted(cols, key=lambda c: -int(df[c].notna().sum()))[:10]
    if len(cols) < 2:
        raise ValueError("kolom numerik kurang")
    corr = df[cols].corr().fillna(0)

    data = [[j, i, round(float(corr.iloc[i, j]), 2)]
            for i in range(len(cols)) for j in range(len(cols))]

    best = None
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            v = float(corr.iloc[i, j])
            if best is None or abs(v) > abs(best[2]):
                best = (cols[i], cols[j], v)
    if best and abs(best[2]) >= 0.5 and ctx.get("corr", 0) < 1:
        arah = "positif" if best[2] > 0 else "negatif"
        ctx["facts"].append(
            f"Hubungan {arah} terkuat: '{best[0]}' dan '{best[1]}' (r = {best[2]:.2f}).")
        ctx["corr"] = ctx.get("corr", 0) + 1

    return {"labels": cols, "data": data, "title": "Korelasi antar variabel"}


def build_heatmap(df, roles, cs, ctx):
    x, y = _resolve(df, cs.get("x")), _resolve(df, cs.get("y"))
    if not (x and y) or x == y or not (_cat_ok(roles, x) and _cat_ok(roles, y)):
        raise ValueError("heatmap butuh dua kolom kategori")

    xs = df[x].dropna().astype(str).value_counts().index[:10]
    ys = df[y].dropna().astype(str).value_counts().index[:10]
    w = df.assign(_x=df[x].astype(str), _y=df[y].astype(str))
    w = w[w["_x"].isin(xs) & w["_y"].isin(ys)]

    value = _resolve(df, cs.get("value"))
    agg = str(cs.get("agg") or "sum").lower()
    if not _is_measure(roles, value) or agg not in AGGS:
        value, agg = None, "count"
    tbl = _aggregate(w, ["_x", "_y"], value, agg).unstack("_y") \
        .reindex(index=list(xs), columns=list(ys))
    tbl = tbl.fillna(0) if agg in ("sum", "count", "nunique") else tbl

    data, vmax = [], 0.0
    for yi, yy in enumerate(ys):
        for xi, xx in enumerate(xs):
            v = tbl.loc[xx, yy]
            if pd.notna(v):
                vmax = max(vmax, float(v))
                data.append([xi, yi, float(v)])

    return {"xLabels": [s[:24] for s in xs], "yLabels": [s[:24] for s in ys],
            "data": data, "max": vmax,
            "measure": _measure_label(value, agg),
            "title": f"{x} × {y}"}


def build_radar(df, roles, cs, ctx):
    x = _resolve(df, cs.get("x"))
    if not x or not _cat_ok(roles, x):
        raise ValueError("radar butuh kolom kategori")

    metrics = [m for m in (_resolve(df, m) for m in (cs.get("metrics") or [])) if m]
    metrics = [m for m in dict.fromkeys(metrics) if _is_measure(roles, m)][:6]
    if len(metrics) < 3:
        raise ValueError("radar butuh >= 3 metrik")

    agg = str(cs.get("agg") or "mean").lower()
    if agg not in ("sum", "mean", "median", "max", "min"):
        agg = "mean"

    top = df[x].dropna().astype(str).value_counts().index[:4]
    w = df[df[x].astype(str).isin(top)].assign(_g=lambda d: d[x].astype(str))
    tbl = getattr(w.groupby("_g")[metrics], agg)().reindex(list(top)).fillna(0)

    indicators = [{"name": m, "max": max(float(tbl[m].max()) * 1.15, 1e-9)}
                  for m in metrics]
    series = [{"name": g, "value": [float(tbl.loc[g, m]) for m in metrics]}
              for g in tbl.index]
    return {"indicators": indicators, "series": series,
            "title": f"{AGG_LABEL.get(agg, agg)} metrik per {x}"}


def _cell(v):
    if v is None or (isinstance(v, float) and math.isnan(v)) or v is pd.NaT:
        return ""
    if isinstance(v, pd.Timestamp):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, (float, np.floating)):
        return round(float(v), 2)
    if isinstance(v, (np.integer,)):
        return int(v)
    return str(v)[:60]


def build_table(df, roles, cs, ctx):
    cols = [c for c in (_resolve(df, c) for c in (cs.get("columns") or [])) if c]
    cols = list(dict.fromkeys(cols))[:8] or list(df.columns)[:8]
    sort_by = _resolve(df, cs.get("sort_by"))
    top_n = _clamp(cs.get("top_n"), 3, 20, 10)

    w = df
    if sort_by and sort_by in df.columns:
        w = df.sort_values(sort_by, ascending=False, na_position="last")
    w = w[cols].head(top_n)
    return {"columns": cols,
            "rows": [[_cell(v) for v in row] for row in w.itertuples(index=False)],
            "title": "Data teratas" if sort_by else "Cuplikan data"}


BUILDERS = {
    "bar": build_bar, "line": build_line, "area": build_area,
    "donut": build_donut, "treemap": build_treemap, "scatter": build_scatter,
    "histogram": build_histogram, "box": build_box,
    "correlation": build_correlation, "heatmap": build_heatmap,
    "radar": build_radar, "table": build_table,
}
ALIASES = {"column": "bar", "pie": "donut", "doughnut": "donut", "hist": "histogram",
           "boxplot": "box", "corr": "correlation", "stacked_bar": "bar",
           "hbar": "bar", "combo": "bar"}


def build_chart(datasets, cs, ctx):
    cs = _norm_cs(cs)
    t = str(cs.get("type") or "").lower().strip()
    if t == "hbar":
        cs["horizontal"] = True
    if t == "stacked_bar":
        cs["stacked"] = True
    t = ALIASES.get(t, t)
    if t not in BUILDERS:
        raise ValueError(f"tipe '{t}' tidak didukung")

    d = _pick_dataset(datasets, cs)
    chart = BUILDERS[t](d["df"], d["roles"], cs, ctx)

    title = str(cs.get("title") or "").strip()
    chart.update({
        "type": t,
        "title": title[:90] or chart.get("title") or t,
        "description": str(cs.get("description") or "")[:220],
        "dataset": d["name"],
        "span": SPANS.get(t, 6),
        "_key": json.dumps([t, cs.get("x"), cs.get("y"), cs.get("series"),
                            cs.get("agg"), d["name"]], default=str),
    })
    return chart


def build_kpis(datasets, specs):
    out = []
    for k in specs[:8]:
        try:
            k = _norm_cs(k)
            d = _pick_dataset(datasets, k)
            df, roles = d["df"], d["roles"]
            agg = str(k.get("agg") or "count").lower()
            col = _resolve(df, k.get("column"))
            label = str(k.get("label") or "")[:40]
            hint, value = None, None

            if col is None or agg == "count":
                value = int(len(df)) if col is None else int(df[col].notna().sum())
                label = label or "Total baris"
            elif agg == "nunique":
                value = int(df[col].nunique())
                label = label or f"Jumlah unik {col}"
            elif agg == "top":
                vc = df[col].dropna().astype(str).value_counts()
                if vc.empty:
                    continue
                value = vc.index[0][:40]
                hint = f"{vc.iloc[0] / vc.sum() * 100:.0f}% dari data"
                label = label or f"{col} terbanyak"
            elif agg in AGGS and _is_measure(roles, col):
                value = float(getattr(df[col], agg)())
                label = label or f"{AGG_LABEL.get(agg, agg)} {col}"
            else:
                continue

            out.append({
                "label": label, "value": value, "hint": hint,
                "prefix": str(k.get("prefix") or "")[:4],
                "suffix": str(k.get("suffix") or "")[:6],
            })
        except Exception as e:
            print("KPI skipped:", repr(e))
    return out


# =========================================================
# PRAKIRAAN (REGRESI LINEAR)
# =========================================================

def _forecast_candidate(d, fs):
    df, roles = d["df"], d["roles"]
    dates = [c for c, r in roles.items() if r == "datetime"]
    if not dates:
        return None

    date = _resolve(df, fs.get("date")) if fs else None
    if date not in dates:
        date = max(dates, key=lambda c: df[c].nunique())

    value = _resolve(df, fs.get("value")) if fs else None
    agg = str((fs or {}).get("agg") or "sum").lower()
    if value is not None and _is_measure(roles, value) and agg in ("sum", "mean"):
        return date, value, agg

    nums = [c for c, r in roles.items() if r == "numeric"]
    if nums:
        return date, nums[0], "sum"
    return date, None, "count"


def build_forecast(datasets, fs):
    for d in datasets.values():
        cand = _forecast_candidate(d, fs)
        if not cand:
            continue
        date, value, agg = cand
        df = d["df"]
        w = df[df[date].notna()]
        if len(w) < 8:
            continue

        auto = _time_bucket(w[date])[1]
        codes = ["Y", "M", "W", "D"]
        series, code = None, None
        for c in codes[codes.index(auto):]:
            key, code = _time_bucket(w[date], c)
            g = w.assign(_k=key).groupby("_k")
            s = g.size() if value is None else getattr(g[value], agg)()
            s = s.sort_index()
            if len(s) >= 8:
                series = s
                break
        if series is None:
            continue

        additive = agg in ("sum", "count")
        full = pd.date_range(series.index.min(), series.index.max(), freq=FREQ[code])
        if len(full) > 800:
            continue
        series = series.reindex(full)
        series = series.fillna(0) if additive else series.interpolate().bfill().ffill()

        y = series.values.astype(float)
        n = len(y)
        if np.nanstd(y) == 0 or np.isnan(y).any():
            continue

        x = np.arange(n, dtype=float)
        slope, intercept = np.polyfit(x, y, 1)
        fitted = slope * x + intercept
        ss_res = float(((y - fitted) ** 2).sum())
        ss_tot = float(((y - y.mean()) ** 2).sum())
        r2 = 0.0 if ss_tot == 0 else max(0.0, 1 - ss_res / ss_tot)
        sigma = math.sqrt(ss_res / max(n - 2, 1))

        h = min(12, max(3, n // 4))
        fx = np.arange(n, n + h, dtype=float)
        fy = slope * fx + intercept
        sxx = float(((x - x.mean()) ** 2).sum()) or 1.0
        band = 1.96 * sigma * np.sqrt(1 + 1 / n + (fx - x.mean()) ** 2 / sxx)
        lower, upper = fy - band, fy + band
        if (y >= 0).all():
            fy, lower, upper = (np.maximum(a, 0) for a in (fy, lower, upper))

        last = series.index[-1]
        if code == "D":
            fidx = [last + pd.Timedelta(days=i) for i in range(1, h + 1)]
        elif code == "W":
            fidx = [last + pd.Timedelta(days=7 * i) for i in range(1, h + 1)]
        elif code == "M":
            fidx = [last + pd.DateOffset(months=i) for i in range(1, h + 1)]
        else:
            fidx = [last + pd.DateOffset(years=i) for i in range(1, h + 1)]

        labels = _time_labels(series.index, code) + _time_labels(fidx, code)
        pad = [None] * (n - 1)
        label = _measure_label(value, agg)
        mean_y = float(np.mean(np.abs(y))) or 1.0
        pct = slope / mean_y * 100
        conf = "Tinggi" if r2 >= 0.6 else "Sedang" if r2 >= 0.3 else "Rendah"
        unit = {"D": "hari", "W": "minggu", "M": "bulan", "Y": "tahun"}[code]

        return {
            "type": "forecast",
            "title": f"Prakiraan {label.lower()}",
            "description": f"Regresi linear sederhana per {unit} dengan interval keyakinan 95%.",
            "dataset": d["name"],
            "span": 12,
            "labels": labels,
            "actual": y.tolist() + [None] * h,
            "fitted": fitted.tolist() + [None] * h,
            "forecast": pad + [float(fitted[-1])] + fy.tolist(),
            "lower": pad + [float(fitted[-1])] + lower.tolist(),
            "upper": pad + [float(fitted[-1])] + upper.tolist(),
            "stats": {
                "model": "Regresi linear",
                "r2": r2, "confidence": conf,
                "direction": "naik" if slope > 0 else "turun" if slope < 0 else "datar",
                "pct_per_period": pct, "unit": unit,
                "next_label": labels[n], "next_value": float(fy[0]),
                "periods": h, "n": n, "measure": label,
            },
        }
    return None


# =========================================================
# RAKIT DASHBOARD
# =========================================================

def _pack_spans(charts):
    fill, last = 0, None
    for i, c in enumerate(charts):
        if c["span"] == 12:
            if fill == 6 and last is not None:
                charts[last]["span"] = 12
            fill, last = 0, None
        elif fill == 0:
            fill, last = 6, i
        else:
            fill, last = 0, None
    if fill == 6 and last is not None:
        charts[last]["span"] = 12


def build_dashboard(datasets, spec, provider):
    ctx = {"facts": []}
    charts, seen = [], set()

    def add(cs):
        snap = (len(ctx["facts"]), dict((k, v) for k, v in ctx.items() if k != "facts"))
        try:
            ch = build_chart(datasets, cs, ctx)
        except Exception as e:
            print("Chart dilewati:", cs.get("type"), repr(e))
            return
        key = ch.pop("_key")
        if key in seen:
            # chart kembar: batalkan insight yang ikut terbentuk
            del ctx["facts"][snap[0]:]
            ctx.update(snap[1])
            return
        seen.add(key)
        charts.append(ch)

    for cs in spec["charts"]:
        add(cs)

    if len(charts) < 5:
        for cs in heuristic_spec(datasets)["charts"]:
            if len(charts) >= 7:
                break
            add(cs)

    forecast = None
    try:
        forecast = build_forecast(datasets, spec.get("forecast"))
    except Exception as e:
        print("Forecast dilewati:", repr(e))

    first = next(iter(datasets.values()))
    if not any(c["type"] == "table" for c in charts):
        try:
            tb = build_chart(first_ds_map(datasets, first), {"type": "table"}, ctx)
            tb.pop("_key", None)
            charts.append(tb)
        except Exception as e:
            print("Tabel dilewati:", repr(e))

    body = [c for c in charts if c["type"] != "table"]
    tables = [c for c in charts if c["type"] == "table"]
    ordered = body + ([forecast] if forecast else []) + tables
    _pack_spans(ordered)

    kpis = build_kpis(datasets, spec["kpis"])
    if len(kpis) < 3:
        have = {k["label"] for k in kpis}
        for k in build_kpis(datasets, heuristic_spec(datasets)["kpis"]):
            if k["label"] not in have and len(kpis) < 6:
                kpis.append(k)
                have.add(k["label"])

    insights = [{"text": t, "source": "data"} for t in ctx["facts"][:4]]
    insights += [{"text": t, "source": "ai"} for t in spec["insights"][:4]]
    if forecast:
        st = forecast["stats"]
        insights.append({
            "text": (f"Tren {st['measure'].lower()} cenderung {st['direction']} "
                     f"(≈ {st['pct_per_period']:+.1f}% per {st['unit']}, "
                     f"keandalan model: {st['confidence'].lower()})."),
            "source": "data"})

    quality = []
    multi = len(datasets) > 1
    for d in datasets.values():
        prefix = f"[{d['name']}] " if multi else ""
        quality.append(f"{prefix}{len(d['df']):,} baris × {d['df'].shape[1]} kolom dianalisis.")
        quality.extend(prefix + n for n in d["notes"][:8])

    names = ", ".join(d["name"] for d in datasets.values())
    return {
        "title": spec["title"] or f"Dashboard {names}"[:80],
        "summary": spec["summary"] or
                   f"Ringkasan otomatis dari {names}: {len(first['df']):,} baris data.",
        "kpis": kpis,
        "insights": insights[:8],
        "charts": ordered,
        "quality": quality[:14],
        "datasets": [{"name": d["name"], "rows": int(len(d["df"])),
                      "columns": int(d["df"].shape[1])} for d in datasets.values()],
        "provider": provider,
        "generated_at": now_iso(),
    }


def first_ds_map(datasets, first):
    for k, v in datasets.items():
        if v is first:
            return {k: v}
    return datasets


def make_datasets(items):
    items = merge_same_schema(items)[:MAX_DATASETS]
    datasets = {}
    for i, it in enumerate(items, start=1):
        datasets[f"d{i}"] = {
            "name": it["name"], "df": it["df"],
            "roles": classify(it["df"]), "notes": it["notes"],
        }
    return datasets


def register_dashboard(app, supabase, login_required):

    def owned_file(uid, fid):
        r = (supabase.table("files").select("*")
             .eq("user_id", uid).eq("id", fid).limit(1).execute())
        print("done")
        return r.data[0] if r.data else None

    def owned_dashboard(uid, did):
        r = (supabase.table("dashboards").select("*")
             .eq("user_id", uid).eq("id", did).limit(1).execute())
        print("done dboard")
        return r.data[0] if r.data else None

    @app.route("/dashboard")
    @login_required
    def dashboard_page():
        return render_template("dashboard.html")

    @app.route("/api/dashboard/sources")
    @login_required
    def dashboard_sources():
        try:
            rows = (supabase.table("files")
                    .select("id,name,csv_name,csv_path,row_count,created_at")
                    .eq("user_id", session["user_id"])
                    .order("created_at", desc=True)
                    .limit(300).execute().data or [])
        except Exception as e:
            print("Dashboard sources error:", repr(e))
            return jsonify({"success": False,
                            "error": "Gagal memuat riwayat file."}), 500

        return jsonify({"success": True, "items": [
            {"file_id": r["id"], "name": r.get("name"),
             "csv_name": r.get("csv_name") or r.get("name"),
             "row_count": r.get("row_count"), "created_at": r.get("created_at")}
            for r in rows if r.get("csv_path")]})

    @app.route("/api/dashboard/generate", methods=["POST"])
    @login_required
    def dashboard_generate():
        uid = session["user_id"]
        files = [f for f in request.files.getlist("files") if f and f.filename]
        file_ids = [x for x in request.form.getlist("file_ids") if x]

        if not files and not file_ids:
            return jsonify({"success": False,
                            "error": "Pilih minimal satu file."}), 400
        if len(files) + len(file_ids) > MAX_SOURCES:
            return jsonify({"success": False,
                            "error": f"Maksimal {MAX_SOURCES} file sekaligus."}), 400

        loaded = []
        try:
            for f in files:
                raw = f.read()
                if len(raw) > MAX_FILE_BYTES:
                    raise ValueError(f"'{f.filename}' lebih dari 15 MB.")
                loaded.append((f.filename, read_table(raw, f.filename)))

            for fid in file_ids:
                row = owned_file(uid, fid)
                if not row or not row.get("csv_path"):
                    raise ValueError("Salah satu file riwayat tidak ditemukan.")
                raw = supabase.storage.from_(BUCKET).download(row["csv_path"])
                loaded.append((row.get("csv_name") or row.get("name") or "file.csv",
                               read_table(raw, row.get("csv_name") or "file.csv")))
        except ValueError as e:
            return jsonify({"success": False, "error": str(e)}), 400
        except Exception as e:
            print("Dashboard load error:", repr(e))
            return jsonify({"success": False,
                            "error": "Gagal membaca file."}), 500

        try:
            items = []
            for name, df in loaded:
                df, notes = preprocess(df, name)
                if df.empty or df.shape[1] == 0:
                    continue
                items.append({"name": name, "df": df, "notes": notes})
            if not items:
                return jsonify({"success": False,
                                "error": "Data kosong setelah dibersihkan."}), 400

            datasets = make_datasets(items)
            profile = build_profile(datasets)

            raw_spec, provider = call_llm_for_spec(profile)
            spec = normalize_spec(raw_spec)
            if not spec["charts"]:
                spec = normalize_spec(heuristic_spec(datasets))
                provider = "heuristic"

            dashboard = build_dashboard(datasets, spec, provider)
            return jsonify({"success": True, "dashboard": clean(dashboard)})

        except Exception as e:
            print("Dashboard generate error:", repr(e))
            return jsonify({"success": False,
                            "error": "Gagal membuat dashboard dari data ini."}), 500

    # ---- simpan ----
    @app.route("/api/dashboards", methods=["POST"])
    @login_required
    def dashboard_save():
        uid = session["user_id"]
        try:
            payload = json.loads(request.form.get("payload") or "{}")
        except json.JSONDecodeError:
            return jsonify({"success": False, "error": "Payload tidak valid."}), 400

        dash = payload.get("dashboard")
        if not isinstance(dash, dict) or not isinstance(dash.get("charts"), list):
            return jsonify({"success": False, "error": "Data dashboard tidak valid."}), 400
        if len(json.dumps(dash, default=str)) > 6_000_000:
            return jsonify({"success": False, "error": "Dashboard terlalu besar."}), 400

        name = str(payload.get("name") or dash.get("title") or "Dashboard").strip()[:100] \
            or "Dashboard"
        summary = str(dash.get("summary") or "")[:500]
        dash_id = None

        try:
            created = supabase.table("dashboards").insert({
                "user_id": uid, "name": name, "description": summary}).execute()
            if not created.data:
                raise Exception("Dashboard tidak tersimpan.")
            dash_id = created.data[0]["id"]

            comps, order = [], 0

            def add(ctype, title, config, span=12):
                nonlocal order
                comps.append({"dashboard_id": dash_id, "type": ctype, "title": title,
                              "config": config, "position": {"order": order, "span": span}})
                order += 1

            add("meta", "Meta", {k: dash.get(k) for k in
                                 ("title", "summary", "datasets", "provider", "generated_at")})
            add("kpis", "KPI", {"items": dash.get("kpis") or []})
            add("insights", "Insight", {"items": dash.get("insights") or []})
            add("quality", "Kualitas data", {"items": dash.get("quality") or []})
            for ch in dash["charts"][:30]:
                if isinstance(ch, dict) and ch.get("type"):
                    add(str(ch["type"])[:30], str(ch.get("title") or "")[:120], ch,
                        int(ch.get("span") or 6))

            supabase.table("dashboard_components").insert(comps).execute()

        except Exception as e:
            print("Dashboard save error:", repr(e))
            if dash_id:
                try:
                    supabase.table("dashboards").delete().eq("id", dash_id).execute()
                except Exception:
                    pass
            return jsonify({"success": False, "error": "Gagal menyimpan dashboard."}), 500

        # sumber data (best effort: kegagalan di sini tidak membatalkan simpan)
        source_ids = []
        try:
            for fid in payload.get("file_ids") or []:
                if owned_file(uid, str(fid)):
                    source_ids.append(str(fid))

            for f in request.files.getlist("files"):
                if not f or not f.filename:
                    continue
                raw = f.read()
                if len(raw) > MAX_FILE_BYTES:
                    continue
                ext = os.path.splitext(f.filename)[1].lower() or ".csv"
                path = f"{uid}/dashboards/data_{os.urandom(8).hex()}{ext}"
                supabase.storage.from_(BUCKET).upload(
                    path, raw,
                    {"content-type": f.mimetype or "application/octet-stream",
                     "upsert": "true"})
                row = supabase.table("files").insert({
                    "user_id": uid, "name": f.filename, "file_type": "dataset",
                    "mime_type": f.mimetype, "storage_path": path,
                    "source": "dashboard"}).execute()
                if row.data:
                    source_ids.append(row.data[0]["id"])

            if source_ids:
                supabase.table("dashboard_sources").insert(
                    [{"dashboard_id": dash_id, "file_id": s} for s in source_ids]
                ).execute()
        except Exception as e:
            print("Dashboard sources warning:", repr(e))

        return jsonify({"success": True, "dashboard_id": dash_id, "name": name}), 201

    # ---- daftar tersimpan ----
    @app.route("/api/dashboards", methods=["GET"])
    @login_required
    def dashboard_list():
        try:
            rows = (supabase.table("dashboards")
                    .select("id,name,description,created_at,updated_at")
                    .eq("user_id", session["user_id"])
                    .order("created_at", desc=True)
                    .limit(100).execute().data or [])
            return jsonify({"success": True, "items": rows})
        except Exception as e:
            print("Dashboard list error:", repr(e))
            return jsonify({"success": False,
                            "error": "Gagal memuat dashboard tersimpan."}), 500

    # ---- buka ----
    @app.route("/api/dashboards/<dash_id>", methods=["GET"])
    @login_required
    def dashboard_get(dash_id):
        try:
            row = owned_dashboard(session["user_id"], dash_id)
            if not row:
                return jsonify({"success": False, "error": "Dashboard tidak ditemukan."}), 404

            comps = (supabase.table("dashboard_components").select("*")
                     .eq("dashboard_id", dash_id).execute().data or [])
            comps.sort(key=lambda c: (c.get("position") or {}).get("order", 0))

            dash = {"title": row["name"], "summary": row.get("description") or "",
                    "kpis": [], "insights": [], "quality": [], "charts": [],
                    "datasets": [], "provider": None,
                    "generated_at": row.get("created_at")}
            for c in comps:
                cfg = c.get("config") or {}
                t = c.get("type")
                if t == "meta":
                    dash.update({k: v for k, v in cfg.items() if v is not None})
                elif t in ("kpis", "insights", "quality"):
                    dash[t] = cfg.get("items") or []
                else:
                    cfg.setdefault("type", t)
                    cfg.setdefault("span", (c.get("position") or {}).get("span", 6))
                    dash["charts"].append(cfg)
            dash["title"] = row["name"]

            return jsonify({"success": True, "dashboard_id": dash_id, "dashboard": dash})
        except Exception as e:
            print("Dashboard get error:", repr(e))
            return jsonify({"success": False, "error": "Gagal membuka dashboard."}), 500

    @app.route("/api/dashboards/<dash_id>", methods=["PATCH"])
    @login_required
    def dashboard_rename(dash_id):
        data = request.get_json(silent=True) or {}
        name = str(data.get("name") or "").strip()[:100]
        if not name:
            return jsonify({"success": False, "error": "Nama tidak boleh kosong."}), 400
        try:
            if not owned_dashboard(session["user_id"], dash_id):
                return jsonify({"success": False, "error": "Dashboard tidak ditemukan."}), 404
            (supabase.table("dashboards")
             .update({"name": name, "updated_at": now_iso()})
             .eq("id", dash_id).eq("user_id", session["user_id"]).execute())
            return jsonify({"success": True, "name": name})
        except Exception as e:
            print("Dashboard rename error:", repr(e))
            return jsonify({"success": False, "error": "Gagal mengganti nama."}), 500

    # ---- hapus ----
    @app.route("/api/dashboards/<dash_id>", methods=["DELETE"])
    @login_required
    def dashboard_delete(dash_id):
        uid = session["user_id"]
        try:
            if not owned_dashboard(uid, dash_id):
                return jsonify({"success": False, "error": "Dashboard tidak ditemukan."}), 404
            supabase.table("dashboard_components").delete().eq("dashboard_id", dash_id).execute()
            supabase.table("dashboard_sources").delete().eq("dashboard_id", dash_id).execute()
            supabase.table("dashboards").delete().eq("id", dash_id).eq("user_id", uid).execute()
            return jsonify({"success": True})
        except Exception as e:
            print("Dashboard delete error:", repr(e))
            return jsonify({"success": False, "error": "Gagal menghapus dashboard."}), 500