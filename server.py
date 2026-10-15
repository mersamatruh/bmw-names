# -*- coding: utf-8 -*-
"""
Веб-интерфейс генератора наименований BMW.

Онлайн-таблица: вставляете строки из Excel (Ctrl+V), жмёте «Сгенерировать»,
проверяете, принимаете или исправляете — правки сразу уходят в базу обучения.

Запуск на сервере (в папке /root/bmw_names, с активным venv):
    pip install flask
    export ANTHROPIC_API_KEY=sk-ant-...
    export APP_PASSWORD=придумайте-пароль
    python server.py
Откройте в браузере: http://IP_СЕРВЕРА:8080
"""
import os, sys, json, re, datetime, threading, time
from flask import Flask, request, jsonify, Response

import generate_names as gn  # переиспользуем логику подбора примеров и промпта
import anthropic

VERSION = "3.0 от 15.10.2026"

APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
PORT = int(os.environ.get("PORT", "8080"))

app = Flask(__name__)
lock = threading.Lock()

print("Загружаю базу эталонов...")
METHODIKA = open(gn.METHODIKA_PATH, encoding="utf-8").read()
REFS = gn.load_reference(gn.DATASET_PATH)
print(f"Готово: {len(REFS)} эталонов.")
print(f"Версия сервера: {VERSION}")

client = anthropic.Anthropic()


def check_auth(req) -> bool:
    if not APP_PASSWORD:
        return True
    return req.headers.get("X-Auth", "") == APP_PASSWORD


@app.post("/api/generate_one")
def generate_one():
    if not check_auth(request):
        return jsonify({"error": "Неверный пароль"}), 401
    d = request.get_json(force=True)
    art = str(d.get("art", "")).strip()
    base = str(d.get("base", "")).strip()
    models = str(d.get("models", "")).strip()
    if not art or not base:
        return jsonify({"error": "Нужны артикул и базовое название"}), 400
    with lock:
        examples = gn.find_similar(REFS, base, models)
    prompt = gn.build_prompt(METHODIKA, examples, art, base, models)
    try:
        msg = client.messages.create(
            model=gn.MODEL,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": gn.MAX_SEARCHES_PER_PART,
            }],
        )
        name, why = gn.parse_result(gn.extract_text(msg))
        return jsonify({"name": name, "why": why})
    except Exception as e:
        return jsonify({"error": str(e)}), 500



COSTS_PATH = "costs.json"
# Цены claude-sonnet-4-6, USD за 1 млн токенов (при смене модели поправить тут)
PRICES = {"in": 3.0, "out": 15.0, "cache_read": 0.30, "cache_write": 3.75,
          "web_search_per_1000": 10.0}


def load_costs():
    if os.path.exists(COSTS_PATH):
        try:
            with open(COSTS_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"total": 0.0, "days": {}}


COSTS = load_costs()


def add_cost(msg):
    """Считает стоимость одного ответа API по токенам из usage."""
    u = getattr(msg, "usage", None)
    if u is None:
        return
    def g(a):
        return getattr(u, a, 0) or 0
    usd = (g("input_tokens") * PRICES["in"]
           + g("output_tokens") * PRICES["out"]
           + g("cache_read_input_tokens") * PRICES["cache_read"]
           + g("cache_creation_input_tokens") * PRICES["cache_write"]) / 1e6
    st = getattr(u, "server_tool_use", None)
    ws = getattr(st, "web_search_requests", 0) if st else 0
    usd += (ws or 0) * PRICES["web_search_per_1000"] / 1000.0
    day = datetime.date.today().isoformat()
    with lock:
        COSTS["total"] = round(COSTS.get("total", 0) + usd, 6)
        days = COSTS.setdefault("days", {})
        days[day] = round(days.get(day, 0) + usd, 6)
        tmp = COSTS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(COSTS, f)
        os.replace(tmp, COSTS_PATH)


@app.get("/api/costs")
def api_costs():
    if not check_auth(request):
        return jsonify({"error": "Неверный пароль"}), 401
    day = datetime.date.today().isoformat()
    return jsonify({"today": round(COSTS.get("days", {}).get(day, 0), 2),
                    "total": round(COSTS.get("total", 0), 2)})


CATALOG_PATH = "catalog.json"


def norm_art_key(v) -> str:
    """Нормализация артикула: верхний регистр, без пробелов/дефисов, без хвоста .0,
    ведущие нули отбрасываются (07149352988 == 7149352988)."""
    s = str(v).strip().upper().replace(" ", "").replace("-", "")
    if s.endswith(".0"):
        s = s[:-2]
    s = s.lstrip("0") or s          # '0000' не превращаем в пустоту
    return s


def load_catalog():
    if not os.path.exists(CATALOG_PATH):
        return {}
    try:
        with open(CATALOG_PATH, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return {}
    # миграция ключей под текущую нормализацию (ведущие нули и т.п.)
    out, changed = {}, False
    for k, v in raw.items():
        nk = norm_art_key(k)
        if nk != k:
            changed = True
        if nk not in out:
            out[nk] = v
    if changed:
        try:
            tmp = CATALOG_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False)
            os.replace(tmp, CATALOG_PATH)
            print(f"Справочник: ключи приведены к новой нормализации "
                  f"({len(raw)} -> {len(out)}).")
        except Exception:
            pass
    return out


CATALOG = load_catalog()
print(f"Справочник артикулов: {len(CATALOG)} позиций.")


def save_catalog():
    tmp = CATALOG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(CATALOG, f, ensure_ascii=False)
    os.replace(tmp, CATALOG_PATH)


@app.post("/api/catalog_upload")
def catalog_upload():
    if not check_auth(request):
        return jsonify({"error": "Неверный пароль"}), 401
    f = request.files.get("file")
    if f is None:
        return jsonify({"error": "Файл не получен"}), 400
    import pandas as pd, io
    fn = (f.filename or "").lower()
    try:
        data = f.read()
        if fn.endswith(".csv") or fn.endswith(".txt"):
            df = pd.read_csv(io.BytesIO(data), sep=None, engine="python",
                             dtype=str, encoding="utf-8-sig")
        else:
            df = pd.read_excel(io.BytesIO(data), dtype=str)
    except Exception as e:
        return jsonify({"error": f"Не удалось прочитать файл: {e}"}), 400

    # найти столбцы по заголовкам, иначе первые 2-3 непустых
    art_col = base_col = models_col = None
    for c in df.columns:
        n = str(c).strip().lower()
        if art_col is None and ("артикул" in n or "article" in n or n in ("art", "номер")):
            art_col = c
        elif base_col is None and ("базов" in n or "наимен" in n or "назван" in n or n == "base"):
            base_col = c
        elif models_col is None and ("примен" in n or "модел" in n or "кузов" in n or n == "models"):
            models_col = c
    if art_col is None or base_col is None:
        cols = [c for c in df.columns if df[c].notna().any()]
        if len(cols) < 2:
            return jsonify({"error": "Нужны минимум 2 столбца: артикул и базовое название"}), 400
        art_col, base_col = cols[0], cols[1]
        if models_col is None and len(cols) > 2:
            models_col = cols[2]

    added = updated = 0
    with lock:
        for _, row in df.iterrows():
            art = norm_art_key(row.get(art_col, ""))
            base = str(row.get(base_col, "") or "").strip()
            if base.lower() == "nan":
                base = ""
            if not art or art == "NAN" or not base:
                continue
            models = ""
            if models_col is not None:
                models = str(row.get(models_col, "") or "").strip()
                if models.lower() == "nan":
                    models = ""
            rec = {"base": base}
            if models:
                rec["models"] = models
            if art in CATALOG:
                updated += 1
            else:
                added += 1
            CATALOG[art] = rec
        save_catalog()
    return jsonify({"ok": True, "added": added, "updated": updated,
                    "total": len(CATALOG)})


@app.post("/api/lookup")
def catalog_lookup():
    if not check_auth(request):
        return jsonify({"error": "Неверный пароль"}), 401
    arts = request.get_json(force=True).get("arts", [])
    out = {}
    for a in arts[:2000]:
        rec = CATALOG.get(norm_art_key(a))
        if rec:
            out[str(a)] = rec
    return jsonify({"found": out, "total": len(CATALOG)})


SNAP_DIR = "snapshots"
os.makedirs(SNAP_DIR, exist_ok=True)
JOBS = {}   # id -> {"rows":[...], "running":bool, "stop":bool}


def snap_path(sid):
    sid = re.sub(r"[^0-9A-Za-z_\-]", "", sid)
    return os.path.join(SNAP_DIR, sid + ".json")


def save_job(sid):
    job = JOBS[sid]
    tmp = snap_path(sid) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"id": sid, "rows": job["rows"]}, f, ensure_ascii=False)
    os.replace(tmp, snap_path(sid))


def load_job(sid):
    if sid in JOBS:
        return JOBS[sid]
    p = snap_path(sid)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    for r in data["rows"]:
        if r.get("status") == "run":
            r["status"] = "wait"     # прервано перезапуском сервера
    JOBS[sid] = {"rows": data["rows"], "running": False, "stop": False}
    return JOBS[sid]


def list_snapshots():
    out = []
    for fn in sorted(os.listdir(SNAP_DIR), reverse=True):
        if not fn.endswith(".json"):
            continue
        sid = fn[:-5]
        try:
            job = load_job(sid)
            total = len(job["rows"])
            done = sum(1 for r in job["rows"]
                       if r.get("status") in ("done", "ok", "fixed"))
            out.append({"id": sid, "total": total, "done": done,
                        "running": job["running"]})
        except Exception:
            continue
    return out


def worker(sid):
    job = JOBS[sid]
    try:
        for r in job["rows"]:
            if job["stop"]:
                break
            if r.get("status") not in ("wait", "err"):
                continue
            if str(r.get("art", "")).strip() and not str(r.get("base", "")).strip():
                rec = CATALOG.get(norm_art_key(r["art"]))
                if rec:
                    r["base"] = rec["base"]
                    if not str(r.get("models", "")).strip() and rec.get("models"):
                        r["models"] = rec["models"]
            if not str(r.get("art", "")).strip() or not str(r.get("base", "")).strip():
                r["status"] = "err"
                r["why"] = ("Артикула нет в справочнике — впишите базовое название"
                            if str(r.get("art", "")).strip() else
                            "Заполните артикул и базовое название")
                save_job(sid)
                continue
            # новое: вписанное вручную название пополняет справочник
            k = norm_art_key(r.get("art", ""))
            b = str(r.get("base", "")).strip()
            if k and b and k not in CATALOG:
                with lock:
                    if k not in CATALOG:
                        rec = {"base": b}
                        m = str(r.get("models", "")).strip()
                        if m:
                            rec["models"] = m
                        CATALOG[k] = rec
                        save_catalog()
            r["status"] = "run"
            save_job(sid)
            try:
                with lock:
                    examples = gn.find_similar(REFS, r["base"], r.get("models", ""))
                prompt = gn.build_prompt(METHODIKA, examples,
                                         r["art"], r["base"], r.get("models", ""))
                msg = client.messages.create(
                    model=gn.MODEL, max_tokens=1500,
                    messages=[{"role": "user", "content": prompt}],
                    tools=[{"type": "web_search_20250305", "name": "web_search",
                            "max_uses": gn.MAX_SEARCHES_PER_PART}],
                )
                add_cost(msg)
                name, why = gn.parse_result(gn.extract_text(msg))
                r["name"] = name
                r["gen0"] = name
                r["why"] = why
                r["status"] = "done"
            except Exception as e:
                r["status"] = "err"
                r["why"] = str(e)
            save_job(sid)
    finally:
        job["running"] = False
        save_job(sid)


def clean_rows(raw):
    rows = []
    for r in raw:
        st = str(r.get("status", "wait"))
        if st == "run":
            st = "wait"
        rows.append({
            "art": str(r.get("art", "")).strip(),
            "base": str(r.get("base", "")).strip(),
            "models": str(r.get("models", "")).strip(),
            "name": str(r.get("name", "")).strip(),
            "gen0": str(r.get("gen0", "")).strip(),
            "why": str(r.get("why", "")).strip(),
            "seo": str(r.get("seo", "")),
            "status": st,
        })
    return rows


def start_worker(sid):
    JOBS[sid]["running"] = True
    JOBS[sid]["stop"] = False
    threading.Thread(target=worker, args=(sid,), daemon=True).start()


@app.post("/api/start")
def api_start():
    if not check_auth(request):
        return jsonify({"error": "Неверный пароль"}), 401
    d = request.get_json(force=True)
    sid = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    JOBS[sid] = {"rows": clean_rows(d.get("rows", [])), "running": False, "stop": False}
    if not JOBS[sid]["rows"]:
        return jsonify({"error": "Пустая таблица"}), 400
    save_job(sid)
    start_worker(sid)
    return jsonify({"id": sid})


@app.post("/api/resume")
def api_resume():
    if not check_auth(request):
        return jsonify({"error": "Неверный пароль"}), 401
    d = request.get_json(force=True)
    sid = str(d.get("id", ""))
    job = load_job(sid)
    if job is None:
        return jsonify({"error": "Слепок не найден"}), 404
    if job["running"]:
        return jsonify({"id": sid})
    if d.get("rows"):
        job["rows"] = clean_rows(d["rows"])
    save_job(sid)
    start_worker(sid)
    return jsonify({"id": sid})


@app.get("/api/job")
def api_job():
    if not check_auth(request):
        return jsonify({"error": "Неверный пароль"}), 401
    sid = request.args.get("id", "")
    job = load_job(sid)
    if job is None:
        return jsonify({"error": "Слепок не найден"}), 404
    return jsonify({"id": sid, "rows": job["rows"], "running": job["running"],
                    "seo_running": job.get("seo_running", False)})


@app.post("/api/stop")
def api_stop():
    if not check_auth(request):
        return jsonify({"error": "Неверный пароль"}), 401
    sid = str(request.get_json(force=True).get("id", ""))
    if sid in JOBS:
        JOBS[sid]["stop"] = True
    return jsonify({"ok": True})


@app.get("/api/snapshots")
def api_snapshots():
    if not check_auth(request):
        return jsonify({"error": "Неверный пароль"}), 401
    return jsonify({"snapshots": list_snapshots()})


def build_seo_prompt(art, name, base, models):
    return f"""Ты пишешь карточку товара для интернет-магазина НОВЫХ ОРИГИНАЛЬНЫХ запчастей BMW.
Карточка должна быть полезной для человека и хорошо читаться поисковиками.

Структура (Markdown):
1. **Заголовок** — предметный, без слова «купить»: деталь + серия мотора/кузова + "(оригинал, {art})".
2. Абзац-вступление: что это за деталь и за что отвечает (2-3 предложения, обязательно упомянуть
   "новая оригинальная BMW").
3. Раздел «**Типичные симптомы, при которых требуется замена:**» — 3-5 пунктов списком, живым языком,
   как механик объяснил бы клиенту (по каким жалобам/ошибкам ищут эту деталь). Если деталь простая
   (кронштейн, заглушка, болт, накладка) и симптомов у неё нет — вместо этого раздел
   «**Зачем нужна эта деталь:**» с 2-3 пунктами.
4. Раздел «**Применимость:**» — списком, кузова расшифровать в человеческие модели с годами
   (например F15 -> BMW X5 2013–2018). Не более 6-7 строк, группируй разумно.
5. Последний абзац — ровно эта фраза, без изменений:
   «Проверим применимость по VIN по вашему запросу. Оригинальный артикул: {art}.»

Запрещено: аналоги и кросс-номера, цены, сроки и условия доставки, выдуманные характеристики
и размеры. Если в чём-то не уверен — пиши общими, но честными формулировками. Объём 700–1200 знаков.

Данные детали:
Артикул: {art}
Наименование: {name}
Базовое название: {base}
Применимость (кузова): {models}

Ответь только текстом карточки в Markdown, без пояснений."""


def seo_worker(sid):
    job = JOBS[sid]
    try:
        for r in job["rows"]:
            if job["stop"]:
                break
            if str(r.get("seo", "")).strip():
                continue
            if not str(r.get("name", "")).strip():
                continue
            if r.get("status") not in ("done", "ok", "fixed"):
                continue
            try:
                msg = client.messages.create(
                    model=gn.MODEL, max_tokens=2000,
                    messages=[{"role": "user", "content": build_seo_prompt(
                        r.get("art", ""), r.get("name", ""),
                        r.get("base", ""), r.get("models", ""))}],
                    tools=[{"type": "web_search_20250305", "name": "web_search",
                            "max_uses": 2}],
                )
                add_cost(msg)
                txt = gn.extract_text(msg).strip()
                txt = re.sub(r"^```(markdown)?|```$", "", txt).strip()
                r["seo"] = txt
            except Exception as e:
                r["seo"] = f"[ОШИБКА] {e}"
            save_job(sid)
    finally:
        job["seo_running"] = False
        save_job(sid)


@app.post("/api/seo_start")
def api_seo_start():
    if not check_auth(request):
        return jsonify({"error": "Неверный пароль"}), 401
    sid = str(request.get_json(force=True).get("id", ""))
    job = load_job(sid)
    if job is None:
        return jsonify({"error": "Слепок не найден"}), 404
    if job.get("running") or job.get("seo_running"):
        return jsonify({"error": "Дождитесь окончания текущей генерации"}), 409
    todo = sum(1 for r in job["rows"]
               if not str(r.get("seo", "")).strip()
               and str(r.get("name", "")).strip()
               and r.get("status") in ("done", "ok", "fixed"))
    if not todo:
        return jsonify({"error": "Нет строк для SEO: нужны готовые наименования без описания"}), 400
    job["seo_running"] = True
    job["stop"] = False
    threading.Thread(target=seo_worker, args=(sid,), daemon=True).start()
    return jsonify({"id": sid, "todo": todo})


@app.post("/api/feedback")
def feedback():
    """Принять или исправить наименование: пишем в dataset.jsonl и методичку."""
    if not check_auth(request):
        return jsonify({"error": "Неверный пароль"}), 401
    d = request.get_json(force=True)
    art = str(d.get("art", "")).strip()
    base = str(d.get("base", "")).strip()
    models = str(d.get("models", "")).strip()
    generated = str(d.get("generated", "")).strip()
    correct = str(d.get("correct", "")).strip() or generated
    comment = str(d.get("comment", "")).strip()
    if not art or not correct:
        return jsonify({"error": "Пустая правка"}), 400

    global METHODIKA
    with lock:
        # обновляем/добавляем запись в память и на диск
        found = None
        for r in REFS:
            if str(r["input"]["артикул"]).strip() == art:
                found = r
                break
        if found:
            found["output"] = correct
            if base:
                found["input"]["базовое_название"] = base
                found["_tokens"] = gn.tokenize(base)
            if models:
                found["input"]["применимость"] = models
                found["_models"] = set(m.strip() for m in models.split(","))
        else:
            rec = {
                "input": {"артикул": art, "базовое_название": base,
                          "применимость": models},
                "output": correct,
                "_tokens": gn.tokenize(base),
                "_models": set(m.strip() for m in models.split(",")),
            }
            REFS.append(rec)
        with open(gn.DATASET_PATH, "w", encoding="utf-8") as f:
            for r in REFS:
                clean = {"input": r["input"], "output": r["output"]}
                f.write(json.dumps(clean, ensure_ascii=False) + "\n")

        # пополняем справочник, если артикул ему неизвестен
        k = norm_art_key(art)
        if k and k not in CATALOG:
            rec_c = {"base": base or correct}
            if models:
                rec_c["models"] = models
            CATALOG[k] = rec_c
            save_catalog()

        # ошибка с комментарием -> правило в методичку
        if comment and generated and correct != generated:
            today = datetime.date.today().isoformat()
            text = open(gn.METHODIKA_PATH, encoding="utf-8").read()
            header = "## Работа над ошибками (обязательно учитывать!)"
            if header not in text:
                text = text.rstrip() + f"\n\n{header}\n"
            text = text.rstrip() + (
                f"\n- ({today}) НЕПРАВИЛЬНО: «{generated}» -> "
                f"ПРАВИЛЬНО: «{correct}». Причина: {comment}\n"
            )
            open(gn.METHODIKA_PATH, "w", encoding="utf-8").write(text)
            METHODIKA = text
    # отметить в слепке
    sid = str(d.get("sid", "") or "")
    idx = d.get("idx", None)
    if sid and idx is not None:
        job = load_job(sid)
        if job and 0 <= int(idx) < len(job["rows"]):
            row = job["rows"][int(idx)]
            row["name"] = correct
            row["status"] = "ok" if (generated and correct == generated) else "fixed"
            save_job(sid)
    return jsonify({"ok": True})


@app.post("/api/snap_delete")
def api_snap_delete():
    if not check_auth(request):
        return jsonify({"error": "Неверный пароль"}), 401
    sid = str(request.get_json(force=True).get("id", ""))
    job = load_job(sid)
    if job is None:
        return jsonify({"error": "Слепок не найден"}), 404
    if job.get("running") or job.get("seo_running"):
        return jsonify({"error": "По этому слепку идёт генерация — сначала нажмите «Стоп»"}), 409
    with lock:
        JOBS.pop(sid, None)
        try:
            os.remove(snap_path(sid))
        except FileNotFoundError:
            pass
    return jsonify({"ok": True})


GH_RAW_BASE = "https://raw.githubusercontent.com/mersamatruh/bmw-names/main/"
GH_MANIFEST = GH_RAW_BASE + "files.json"


def _fetch(url):
    import urllib.request as _ur
    sep = "&" if "?" in url else "?"
    req = _ur.Request(url + sep + "nocache=" + str(int(time.time())),
                      headers={"User-Agent": "bmw-names-updater",
                               "Cache-Control": "no-cache"})
    return _ur.urlopen(req, timeout=30).read()


def _safe_name(fn):
    """Разрешаем только простые имена .py-файлов в текущей папке."""
    import re as _re
    return (isinstance(fn, str) and fn.endswith(".py")
            and "/" not in fn and ".." not in fn
            and bool(_re.fullmatch(r"[A-Za-z0-9_.\-]+", fn)))


def _install_code(code: str):
    """Проверяет новый server.py, сохраняет бэкап, ставит и планирует перезапуск.

    Возвращает None при успехе или текст ошибки."""
    try:
        compile(code, "server.py", "exec")
    except SyntaxError as e:
        return f"В файле синтаксическая ошибка, замена отменена: {e}"
    if "генератор наименований" not in code or "app.run" not in code:
        return "Это не похоже на server.py — замена отменена"
    path = os.path.abspath(__file__)
    try:
        import shutil as _sh
        _sh.copy(path, path + ".bak")   # резервная копия прежней версии
    except Exception:
        pass
    with open(path, "w", encoding="utf-8") as fp:
        fp.write(code)

    def _restart():
        os.execv(sys.executable, [sys.executable, path])

    threading.Timer(1.5, _restart).start()
    return None


@app.post("/api/update_github")
def update_github():
    """Скачивает все файлы проекта по манифесту files.json из GitHub и ставит их.

    Если манифеста нет — обновляет только server.py (как раньше)."""
    if not check_auth(request):
        return jsonify({"error": "Неверный пароль"}), 401
    if not APP_PASSWORD:
        return jsonify({"error": "Обновление доступно только при заданном APP_PASSWORD"}), 403

    # 1) список файлов; при любой проблеме — страховка на server.py
    try:
        files = json.loads(_fetch(GH_MANIFEST).decode("utf-8"))
        if not isinstance(files, list):
            raise ValueError("manifest не список")
    except Exception:
        files = ["server.py"]
    files = [f for f in files if _safe_name(f)]
    if "server.py" not in files:
        files.append("server.py")

    # 2) скачать всё заранее (ничего не пишем, пока не проверим)
    downloaded = {}
    for fn in files:
        try:
            downloaded[fn] = _fetch(GH_RAW_BASE + fn).decode("utf-8")
        except Exception as e:
            return jsonify({"error": f"Не удалось скачать {fn}: {e}"}), 502

    # 3) проверка синтаксиса каждого файла
    for fn, code in downloaded.items():
        try:
            compile(code, fn, "exec")
        except SyntaxError as e:
            return jsonify({"error": f"Синтаксическая ошибка в {fn}, обновление отменено: {e}"}), 400

    # 4) server.py должен остаться нашим
    sv = downloaded.get("server.py", "")
    if "генератор наименований" not in sv or "app.run" not in sv:
        return jsonify({"error": "server.py не похож на наш — обновление отменено"}), 400

    # 5) бэкап и запись всех файлов
    import shutil as _sh
    base = os.path.dirname(os.path.abspath(__file__))
    for fn, code in downloaded.items():
        path = os.path.join(base, fn)
        try:
            if os.path.exists(path):
                _sh.copy(path, path + ".bak")
        except Exception:
            pass
        with open(path, "w", encoding="utf-8") as fp:
            fp.write(code)

    # 6) перезапуск
    def _restart():
        os.execv(sys.executable, [sys.executable,
                                  os.path.join(base, "server.py")])
    threading.Timer(1.5, _restart).start()
    return jsonify({"ok": True, "files": sorted(downloaded.keys())})


@app.post("/api/update_code")
def update_code():
    """Самообновление: принимает новый server.py, проверяет и перезапускается."""
    if not check_auth(request):
        return jsonify({"error": "Неверный пароль"}), 401
    if not APP_PASSWORD:
        return jsonify({"error": "Обновление кода доступно только при заданном APP_PASSWORD"}), 403
    f = request.files.get("file")
    if f is None:
        return jsonify({"error": "Файл не получен"}), 400
    data = f.read()
    if data[:4] == b"PK\x03\x04":          # zip-архив: достаём server.py изнутри
        import io as _io, zipfile as _zf
        try:
            with _zf.ZipFile(_io.BytesIO(data)) as z:
                names = [n for n in z.namelist() if n.lower().endswith(".py")
                         and not n.endswith("/")]
                pick = next((n for n in names if n.split("/")[-1] == "server.py"),
                            names[0] if len(names) == 1 else None)
                if pick is None:
                    return jsonify({"error": "В архиве не найден server.py"}), 400
                data = z.read(pick)
        except _zf.BadZipFile:
            return jsonify({"error": "Архив повреждён"}), 400
    try:
        code = data.decode("utf-8")
    except Exception:
        return jsonify({"error": "Файл должен быть текстом в UTF-8"}), 400
    err = _install_code(code)
    if err:
        return jsonify({"error": err}), 400
    return jsonify({"ok": True})


PAGE = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Каталог BMW — генератор наименований</title>
<style>
  :root{
    --bg:#f4f6f8; --panel:#ffffff; --ink:#171a1f; --mut:#6b7280;
    --line:#dfe3e8; --blue:#1c69d4; --blue-d:#155ab8;
    --ok:#177245; --ok-bg:#e7f4ec; --warn:#8a5a00; --warn-bg:#fdf3dd;
    --err:#b3261e; --err-bg:#fdecea; --mono:'Consolas','Menlo',monospace;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
       font:14px/1.45 -apple-system,'Segoe UI',Roboto,Arial,sans-serif}
  header{background:var(--panel);border-bottom:2px solid var(--blue);
         padding:14px 22px;display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}
  header h1{margin:0;font-size:17px;font-weight:700;letter-spacing:.2px}
  header .sub{color:var(--mut);font-size:13px}
  main{max-width:1280px;margin:18px auto;padding:0 16px}
  .bar{display:flex;gap:10px;align-items:center;margin-bottom:12px;flex-wrap:wrap}
  button{border:1px solid var(--line);background:var(--panel);color:var(--ink);
         padding:8px 14px;border-radius:6px;cursor:pointer;font:inherit}
  button:hover{border-color:var(--blue)}
  button.primary{background:var(--blue);border-color:var(--blue);color:#fff;font-weight:600}
  button.primary:hover{background:var(--blue-d)}
  button:disabled{opacity:.5;cursor:default}
  .hint{color:var(--mut);font-size:13px}
  table{width:100%;border-collapse:collapse;background:var(--panel);
        border:1px solid var(--line);border-radius:8px;overflow:hidden}
  th{background:#eef1f5;text-align:left;font-size:12px;text-transform:uppercase;
     letter-spacing:.4px;color:var(--mut);padding:8px 10px;border-bottom:1px solid var(--line)}
  td{padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}
  tr:last-child td{border-bottom:none}
  td.art{font-family:var(--mono);white-space:nowrap;font-size:13px}
  td[contenteditable]{outline:none;min-width:120px}
  td[contenteditable]:focus{background:#eef5fe;box-shadow:inset 0 0 0 1px var(--blue)}
  td.selcell{background:#dce9fb;box-shadow:inset 0 0 0 1px var(--blue)}
  td[data-f]{cursor:cell}
  td[data-f="models"]{max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  td[data-f="models"]:focus{white-space:normal;overflow:visible;max-width:none}
  td[data-f="base"]{max-width:420px}
  td[data-f="art"]{max-width:160px;overflow:hidden;text-overflow:ellipsis}
  td.shortcell{color:var(--blue);cursor:cell;user-select:none;white-space:nowrap}
  td.seocell{max-width:220px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;
    color:var(--mut);cursor:pointer;font-size:12px}
  td.seocell.open{white-space:pre-wrap;overflow:visible;max-width:380px;color:var(--ink);
    cursor:default}
  .seocopy{margin-top:6px;padding:4px 10px;font-size:12px}
  .fltrow td{background:#f7f9fb;padding:4px 6px;border-bottom:2px solid var(--line)}
  .fltrow input,.fltrow select{width:100%;box-sizing:border-box;padding:4px 7px;
    border:1px solid var(--line);border-radius:4px;font:12px/1.3 inherit;background:#fff}
  .fltrow input:focus,.fltrow select:focus{outline:none;border-color:var(--blue)}
  #fltClear{padding:3px 8px;font-size:13px}
  #fltInfo{font-size:11px;white-space:nowrap}
  td.name{font-weight:600}
  .nameedit{display:block;width:100%;box-sizing:border-box;padding:7px 9px;
    border:2px solid var(--blue);border-radius:6px;font:inherit;font-weight:600;
    line-height:1.45;background:#fff;color:var(--ink);outline:none;
    resize:none;overflow:hidden;min-height:34px}
  .chip{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600}
  .chip.wait{background:#eef1f5;color:var(--mut)}
  .chip.run{background:#e3edfb;color:var(--blue)}
  .chip.done{background:var(--warn-bg);color:var(--warn)}
  .chip.ok{background:var(--ok-bg);color:var(--ok)}
  .chip.err{background:var(--err-bg);color:var(--err)}
  .why{color:var(--mut);font-size:12px;margin-top:3px}
  .acts{white-space:nowrap}
  .acts button{padding:5px 9px;font-size:13px;margin-right:4px}
  #login{position:fixed;inset:0;background:rgba(23,26,31,.55);display:flex;
         align-items:center;justify-content:center}
  #login .box{background:var(--panel);padding:26px;border-radius:10px;width:320px}
  #login input{width:100%;padding:9px;border:1px solid var(--line);border-radius:6px;
               font:inherit;margin:10px 0}
  .empty td{color:var(--mut);text-align:center;padding:28px}
  @media (max-width:800px){ .hide-m{display:none} }
  /* --- боковое меню разделов (v2.0) --- */
  .layout{display:flex;align-items:flex-start}
  .side{width:214px;flex:none;background:var(--panel);border-right:1px solid var(--line);
        min-height:calc(100vh - 51px);padding:14px 10px}
  .side .navitem{display:block;width:100%;text-align:left;padding:10px 12px;margin-bottom:4px;
        border:1px solid transparent;border-radius:8px;background:none;color:var(--ink);
        font:inherit;cursor:pointer}
  .side .navitem:hover{background:var(--bg)}
  .side .navitem.active{background:#eaf1fc;border-color:var(--blue);color:var(--blue);font-weight:600}
  .content{flex:1;min-width:0}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;
         padding:22px;max-width:720px}
  @media (max-width:800px){
    .layout{flex-direction:column}
    .side{width:auto;min-height:0;border-right:none;border-bottom:1px solid var(--line);
          display:flex;gap:8px;padding:8px}
    .side .navitem{margin-bottom:0}
  }
  /* --- CSS подпроектов (модулей) --- */
  {{ module_css }}
</style>
</head>
<body>
<header>
  <h1>Каталог BMW · генератор наименований</h1>
  <span class="sub">эталонов в базе: <b id="refcount">—</b> · справочник: <b id="catcount">—</b> · расход: <b id="spend">—</b> · версия {{ version }}</span>
</header>
<div class="layout">
  <nav class="side">
    <button class="navitem active" data-sec="names">📝 Названия</button>
    {{ module_nav }}
  </nav>
  <div class="content">
<main id="sec-names">
  <div class="bar">
    <button class="primary" id="btnGen">Сгенерировать названия</button>
    <button id="btnAdd" style="display:none">Добавить строку</button>
    <button id="btnStop" style="display:none">Стоп</button>
    <button id="btnSeo" title="Сгенерировать SEO-описания для готовых строк без описания">Сгенерировать SEO</button>
    <button id="btnCopyRes" title="Скопировать артикулы и наименования для вставки в Excel">Скопировать результат</button>
    <button id="btnCsv" style="display:none">Скачать CSV</button>
    <button id="btnClear">Новая партия</button>
    <select id="snaps" style="padding:8px;border:1px solid var(--line);border-radius:6px;font:inherit;max-width:260px">
      <option value="">— слепки —</option></select>
    <button id="btnOpen">Открыть</button>
    <button id="btnDelSnap" title="Удалить выбранный слепок с сервера">Удалить</button>
    <button id="btnCat" title="Загрузить справочник артикул → базовое название (xlsx/csv)">Справочник…</button>
    <input type="file" id="catFile" accept=".xlsx,.xls,.csv,.txt" style="display:none">
    <button id="btnGh" title="Скачать и установить свежую версию из GitHub">Обновить с GitHub</button>
    <span class="hint">Ctrl+V — вставка (можно только артикулы: названия подтянутся из справочника) · Ctrl+C — копия · двойной клик — правка</span>
  </div>
  <table id="tbl">
    <thead><tr>
      <th style="width:30px">№</th><th>Артикул</th><th title="Последние 7 символов — для ЕТК">Кратк.</th><th>Базовое название</th>
      <th class="hide-m">Применимость</th><th>Наименование</th>
      <th style="width:220px">SEO-описание</th><th style="width:90px">Статус</th><th style="width:150px">Действия</th>
    </tr>
    <tr class="fltrow">
      <td></td>
      <td><input data-flt="art" placeholder="фильтр…"></td>
      <td><input data-flt="short" placeholder="фильтр…"></td>
      <td><input data-flt="base" placeholder="фильтр…"></td>
      <td class="hide-m"><input data-flt="models" placeholder="фильтр…"></td>
      <td><input data-flt="name" placeholder="фильтр…"></td>
      <td></td>
      <td><select data-flt="status">
        <option value="">— все —</option>
        <option value="wait">Ожидает</option>
        <option value="run">Генерация</option>
        <option value="done">Проверить</option>
        <option value="ok">Принято</option>
        <option value="fixed">Исправлено</option>
        <option value="err">Ошибка</option>
      </select></td>
      <td class="acts"><button id="fltClear" title="Сбросить фильтры">×</button>
        <span id="fltInfo" class="hint"></span></td>
    </tr></thead>
    <tbody id="tb"></tbody>
  </table>
</main>
{{ module_sections }}
  </div><!-- .content -->
</div><!-- .layout -->

<div id="login" style="display:none">
  <div class="box">
    <b>Вход</b>
    <input id="pw" type="password" placeholder="Пароль" autofocus>
    <button class="primary" style="width:100%" onclick="doLogin()">Войти</button>
  </div>
</div>

<script>
const $=s=>document.querySelector(s);
let AUTH = localStorage.getItem('auth')||'';
const NEED_PW = {{ need_pw }};
if(NEED_PW && !AUTH) $('#login').style.display='flex';
function doLogin(){ AUTH=$('#pw').value; localStorage.setItem('auth',AUTH);
                    $('#login').style.display='none'; init(); }

function emptyRow(){
  return {art:'',base:'',models:'',name:'',why:'',status:'wait'};
}
let rows=[emptyRow()];

function chip(st){
  const m={wait:['Ожидает','wait'],run:['Генерация…','run'],done:['Проверить','done'],
           ok:['Принято','ok'],fixed:['Исправлено','ok'],err:['Ошибка','err']};
  const [t,c]=m[st]||m.wait;
  return `<span class="chip ${c}">${t}</span>`;
}
function esc(s){return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;');}
function shortOf(art){return String(art||'').replace(/[\s-]/g,'').slice(-7);}
function cellVal(r,col){return col==='short' ? shortOf(r.art) : (r[col]||'');}

function rowHTML(r,i){
  return `<td>${i+1}</td>
      <td class="art" contenteditable data-f="art" data-i="${i}">${esc(r.art)}</td>
      <td class="art shortcell" data-f="short" data-i="${i}" title="Последние 7 символов артикула">${esc(shortOf(r.art))}</td>
      <td contenteditable data-f="base" data-i="${i}">${esc(r.base)}</td>
      <td class="hide-m" contenteditable data-f="models" data-i="${i}">${esc(r.models)}</td>
      <td class="name" data-f="name" data-i="${i}" title="Двойной клик — редактировать">${esc(r.name)}
        ${r.why&&r.status!=='err'?`<div class="why">${esc(r.why)}</div>`:''}</td>
      <td class="seocell${r._seoOpen?' open':''}" data-si="${i}" title="${r.seo?(r._seoOpen?'Клик по тексту — свернуть':'Клик — развернуть'):''}">${
        r.seo ? (r._seoOpen
          ? esc(r.seo)+`<br><button class="seocopy" onclick="copySeo(${i});event.stopPropagation()">Копировать</button>`
          : esc(r.seo.replace(/\s+/g,' ')).slice(0,60)+(r.seo.length>60?'…':''))
        : ''}</td>
      <td>${chip(r.status)}${r.status==='err'&&r.why?`<div class="why">${esc(r.why)}</div>`:''}</td>
      <td class="acts">${(r.status==='done'||r.status==='ok'||r.status==='fixed')?
        `<button onclick="approve(${i})" title="Название верное">✓</button>
         <button onclick="fix(${i})" title="Добавить причину ошибки в правила">✎</button>
         <button onclick="regen(${i})" title="Сгенерировать заново">↻</button>`:''}</td>`;
}
function render(){
  const tb=$('#tb'); tb.innerHTML='';
  if(!rows.length){tb.innerHTML='<tr class="empty"><td colspan="9">Вставьте строки из Excel или нажмите «Добавить строку»</td></tr>';return;}
  rows.forEach((r,i)=>{
    const tr=document.createElement('tr');
    tr.innerHTML=rowHTML(r,i);
    tb.appendChild(tr);
  });
  applySel();
  applyFilters();
}
function updateRow(i){
  const tb=$('#tb');
  const tr=tb && tb.children[i];
  if(!tr || tr.classList.contains('empty')){ render(); return; }
  tr.innerHTML=rowHTML(rows[i],i);
  applySel();
  applyFilters();
}

// ---- Фильтры ----
const filters={art:'',short:'',base:'',models:'',name:'',status:''};
function rowVisible(r){
  if(r._pin) return true;   // строку только что правили — не прячем
  if(filters.status && r.status!==filters.status) return false;
  if(filters.art && !(r.art||'').toLowerCase().includes(filters.art)) return false;
  if(filters.short && !shortOf(r.art).toLowerCase().includes(filters.short)) return false;
  if(filters.base && !(r.base||'').toLowerCase().includes(filters.base)) return false;
  if(filters.models && !(r.models||'').toLowerCase().includes(filters.models)) return false;
  if(filters.name && !(r.name||'').toLowerCase().includes(filters.name)) return false;
  return true;
}
function applyFilters(){
  const tb=$('#tb'); if(!tb) return;
  const active=Object.values(filters).some(v=>v);
  let shown=0, total=0;
  rows.forEach((r,i)=>{
    const tr=tb.children[i];
    if(!tr || tr.classList.contains('empty')) return;
    total++;
    const v=!active || rowVisible(r);
    tr.style.display=v?'':'none';
    if(v) shown++;
  });
  const info=$('#fltInfo');
  if(info) info.textContent=active?(shown+' из '+total):'';
}
document.querySelectorAll('[data-flt]').forEach(el=>{
  const ev=el.tagName==='SELECT'?'change':'input';
  el.addEventListener(ev,()=>{
    filters[el.dataset.flt]=el.value.trim().toLowerCase();
    rows.forEach(r=>{delete r._pin;});
    applyFilters();
  });
});
document.getElementById('fltClear').onclick=()=>{
  document.querySelectorAll('[data-flt]').forEach(el=>{el.value='';});
  Object.keys(filters).forEach(k=>filters[k]='');
  rows.forEach(r=>{delete r._pin;});
  applyFilters();
};
document.addEventListener('input',e=>{
  const f=e.target.dataset.f, i=e.target.dataset.i;
  if(f!==undefined && i!==undefined){
    rows[i][f]=e.target.innerText.replace(/\n+$/,'').trim();
    rows[i]._dirty=true;                       // правки руками не затираются опросом
    if((f==='base'||f==='art'||f==='models') && rows[i].status==='err'){
      rows[i].status='wait'; rows[i].why=''; rows[i]._pin=true;
      const tr=e.target.closest('tr');
      if(tr){const c=tr.children; c[c.length-2].innerHTML=chip('wait');}
      applyFilters();
    }
  }
});

// ---- Выделение ячеек как в Excel ----
let sel=null, editing=null, dragging=false;
function cellRC(td){return {r:+td.dataset.i, c:COLS.indexOf(td.dataset.f)};}
function selRect(){
  if(!sel) return null;
  return {r1:Math.min(sel.a.r,sel.f.r), r2:Math.max(sel.a.r,sel.f.r),
          c1:Math.min(sel.a.c,sel.f.c), c2:Math.max(sel.a.c,sel.f.c)};
}
function applySel(){
  const R=selRect();
  document.querySelectorAll('#tb td[data-f]').forEach(td=>{
    const {r,c}=cellRC(td);
    td.classList.toggle('selcell', !!R && r>=R.r1&&r<=R.r2&&c>=R.c1&&c<=R.c2);
  });
}
document.addEventListener('mousedown',e=>{
  const td=e.target.closest && e.target.closest('#tb td[data-f]');
  if(!td){ if(!e.target.closest('#tb')){sel=null;applySel();} return; }
  const rc=cellRC(td);
  if(editing && editing.r===rc.r && editing.c===rc.c) return; // редактируем — обычный клик
  e.preventDefault();
  if(editing){document.activeElement.blur();editing=null;}
  if(e.shiftKey && sel){                     // Shift+клик: расширить диапазон, как в Excel
    sel.f=rc; dragging=true; applySel(); return;
  }
  sel={a:rc,f:rc}; dragging=true; applySel();
});
document.addEventListener('mouseover',e=>{
  if(!dragging) return;
  const td=e.target.closest && e.target.closest('#tb td[data-f]');
  if(td){sel.f=cellRC(td);applySel();}
});
document.addEventListener('mouseup',()=>{dragging=false;});
document.addEventListener('dblclick',e=>{
  // двойной клик внутри открытого редактора — это выделение слова, не трогаем
  if(e.target.classList && e.target.classList.contains('nameedit')) return;
  const td=e.target.closest && e.target.closest('#tb td[data-f]');
  if(!td) return;
  const rc=cellRC(td);
  if(editing && editing.r===rc.r && editing.c===rc.c) return;  // уже редактируем её
  if(td.dataset.f==='short') return;
  if(td.dataset.f==='name'){ openNameEditor(rc.r); return; }
  editing=rc; sel={a:rc,f:rc}; applySel();
  td.focus();
  const rng=document.createRange(); rng.selectNodeContents(td); rng.collapse(false);
  const s=getSelection(); s.removeAllRanges(); s.addRange(rng);
});

// ---- Редактор наименования: обычное поле ввода, сохранение по клику вне ----
function openNameEditor(i){
  const td=document.querySelector(`#tb td[data-f="name"][data-i="${i}"]`);
  if(!td || !rows[i]) return;
  if(td.querySelector('.nameedit')) return;   // редактор уже открыт — не пересоздаём
  editing={r:i,c:COLS.indexOf('name')}; sel={a:editing,f:editing};
  const inp=document.createElement('textarea');
  inp.className='nameedit'; inp.rows=1; inp.value=rows[i].name||'';
  const w=td.clientWidth;                     // зафиксировать ширину ячейки
  td.innerHTML=''; td.appendChild(inp);
  inp.style.width=Math.max(w-2,120)+'px';
  const grow=()=>{inp.style.height='auto';inp.style.height=(inp.scrollHeight+2)+'px';};
  grow();
  inp.addEventListener('input',grow);
  inp.focus(); inp.setSelectionRange(inp.value.length, inp.value.length);
  let closed=false;
  const commit=save=>{
    if(closed) return; closed=true; editing=null;
    if(save) saveName(i, inp.value.replace(/\s+/g,' ').trim()).then(flushPending);
    else { render(); flushPending(); }
  };
  inp.addEventListener('blur',()=>commit(true));
  inp.addEventListener('keydown',e=>{
    e.stopPropagation();
    if(e.key==='Enter'){e.preventDefault();inp.blur();}
    else if(e.key==='Escape'){e.preventDefault();commit(false);}
  });
  ['mousedown','mouseup','mouseover','click','dblclick'].forEach(ev=>
    inp.addEventListener(ev, e=>e.stopPropagation()));
}
async function saveName(i, v){
  const r=rows[i];
  if(r.status==='err' && !(r.base||'').trim() && v){
    // артикул без базового: введённое руками наименование — финальное.
    // Пишем в оба поля, фиксируем, в генерацию и под справочник не отдаём.
    r.base=v; r.name=v; r.gen0=''; r.status='fixed'; r.why=''; r._dirty=true; r._pin=true;
    render();
    try{
      await api('/api/feedback',{sid:jobId||'',idx:i,art:r.art,base:r.base,
        models:r.models,generated:'',correct:v});
    }catch(e){}
    return;
  }
  const changed = v!==r.name;
  r.name=v;
  if(changed) r._pin=true;   // правленная строка не прячется фильтром
  // авто-сохранение правки в базу обучения: без диалогов
  if(changed && jobId && v && r.gen0 && v!==r.gen0 &&
     (r.status==='done'||r.status==='ok'||r.status==='fixed')){
    r.status='fixed';
    render();
    try{
      await api('/api/feedback',{sid:jobId,idx:i,art:r.art,base:r.base,models:r.models,
        generated:r.gen0,correct:v});
    }catch(e){}
  } else {
    render();
  }
}
document.addEventListener('focusout',e=>{
  if(e.target.dataset && e.target.dataset.f!==undefined){
    editing=null;
    setTimeout(flushPending, 50);
  }
});
function selText(){
  const R=selRect(); if(!R) return null;
  const active=Object.values(filters).some(v=>v);
  const lines=[];
  for(let r=R.r1;r<=R.r2;r++){
    if(active && rows[r] && !rowVisible(rows[r])) continue;  // скрытые фильтром — не копируем
    const vals=[];
    for(let c=R.c1;c<=R.c2;c++) vals.push(rows[r]?cellVal(rows[r],COLS[c]):'');
    lines.push(vals.join('\t'));
  }
  return lines.join('\n');
}
// Копирование через системное событие — работает при любой раскладке (Ctrl+C / Cmd+C)
document.addEventListener('copy',e=>{
  if(editing || !sel) return;             // в режиме правки — обычное копирование текста
  const t=selText(); if(t===null) return;
  e.preventDefault();
  e.clipboardData.setData('text/plain',t);
});
document.addEventListener('keydown',e=>{
  if(editing){ if(e.key==='Escape'){document.activeElement.blur();editing=null;} return; }
  if(!sel) return;
  if(e.key==='Delete'||e.key==='Backspace'){
    e.preventDefault();
    const R=selRect();
    for(let r=R.r1;r<=R.r2;r++)for(let c=R.c1;c<=R.c2;c++)
      if(rows[r] && COLS[c]!=='short') rows[r][COLS[c]]='';
    render(); return;
  }
  const mv={ArrowUp:[-1,0],ArrowDown:[1,0],ArrowLeft:[0,-1],ArrowRight:[0,1]}[e.key];
  if(mv){
    e.preventDefault();
    const nr=Math.max(0,Math.min(rows.length-1,sel.f.r+mv[0]));
    const nc=Math.max(0,Math.min(COLS.length-1,sel.f.c+mv[1]));
    if(e.shiftKey){sel.f={r:nr,c:nc};} else {sel={a:{r:nr,c:nc},f:{r:nr,c:nc}};}
    applySel(); return;
  }
  if(e.key==='Enter'||e.key==='F2'){
    e.preventDefault();
    if(COLS[sel.a.c]==='short') return;
    if(COLS[sel.a.c]==='name'){ openNameEditor(sel.a.r); return; }
    const td=document.querySelector(`#tb td[data-i="${sel.a.r}"][data-f="${COLS[sel.a.c]}"]`);
    if(td){editing=sel.a;td.focus();}
  }
});

// Вставка из Excel: заполняем начиная с ячейки, куда вставили (как в Excel)
const COLS=['art','short','base','models','name'];
function cleanVal(col, v){
  // убрать управляющие и невидимые символы (из Excel/PDF часто тянутся)
  v=String(v).replace(/[\u0000-\u001f\u007f\u00a0\u00ad\u200b-\u200f\u2028\u2029\ufeff]/g,' ');
  if(col==='art') return v.toUpperCase().replace(/[^A-Z0-9]/g,'');  // артикул: строго A-Z 0-9
  return v.replace(/\s+/g,' ').trim();
}
const WRITE=['art','base','models','name'];   // 'short' вычисляется, в него не пишем
document.addEventListener('paste',e=>{
  if(e.target && e.target.classList && e.target.classList.contains('nameedit')) return;
  const t=(e.clipboardData||window.clipboardData).getData('text');
  if(!t) return;
  const lines=t.replace(/\r/g,'').split('\n').filter((x,idx,arr)=>x.trim()||idx<arr.length-1);
  const multi = t.includes('\t') || lines.length>1;
  if(!multi){
    // одиночное значение
    const edCell=e.target.closest && e.target.closest('#tb td[data-f]');
    if(edCell && editing){
      // правка внутри ячейки: вставить как чистый текст, без форматирования
      e.preventDefault();
      document.execCommand('insertText', false, cleanVal(edCell.dataset.f, t));
      return;
    }
    if(!sel) return;
    // ячейка просто выделена мышью: вставляем прямо в неё (и во весь диапазон)
    e.preventDefault();
    const R=selRect(); if(!R) return;
    for(let r=R.r1;r<=R.r2;r++){
      while(rows.length<=r) rows.push(emptyRow());
      for(let c=R.c1;c<=R.c2;c++){
        const col=COLS[c];
        if(col==='short') continue;
        rows[r][col]=cleanVal(col, t);
        rows[r]._dirty=true; rows[r]._pin=true;
        if(rows[r].status==='err' && col!=='name'){rows[r].status='wait'; rows[r].why='';}
        if(rows[r].status==='done' && (col==='art'||col==='base')) rows[r].status='wait';
      }
    }
    render(); fillFromCatalog();
    return;
  }
  e.preventDefault();
  // куда вставили: если в ячейку таблицы — стартуем с её строки и столбца
  let startRow=rows.length, startCol=0;
  const cell=e.target.closest ? e.target.closest('[data-f]') : null;
  const asWrite=f=>{ if(f==='short') f='art'; const k=WRITE.indexOf(f); return k<0?0:k; };
  if(cell){
    startRow=parseInt(cell.dataset.i);
    startCol=asWrite(cell.dataset.f);
  } else if(sel){
    startRow=sel.a.r; startCol=asWrite(COLS[sel.a.c]);
  } else {
    rows=rows.filter(r=>r.art||r.base);    // вставка вне таблицы: добавляем в конец
    startRow=rows.length;
  }
  lines.forEach((line,li)=>{
    const ri=startRow+li;
    while(rows.length<=ri) rows.push(emptyRow());
    line.split('\t').forEach((val,ci)=>{
      const col=WRITE[startCol+ci];
      if(col) rows[ri][col]=cleanVal(col, val);
    });
    if(rows[ri].status==='done'&&startCol<=1) rows[ri].status='wait';
  });
  render();
  fillFromCatalog();
});

async function api(path,body){
  const res=await fetch(path,{method:'POST',
    headers:{'Content-Type':'application/json','X-Auth':AUTH},
    body:JSON.stringify(body)});
  if(res.status===401){localStorage.removeItem('auth');AUTH='';
    $('#login').style.display='flex';throw new Error('Нужен пароль');}
  return res.json();
}

async function apiGet(path){
  const res=await fetch(path,{headers:{'X-Auth':AUTH}});
  if(res.status===401){localStorage.removeItem('auth');AUTH='';
    $('#login').style.display='flex';throw new Error('Нужен пароль');}
  return res.json();
}

let jobId=null, pollTimer=null, pendingSrv=null;

function setRunning(on){
  $('#btnGen').disabled=on;
  $('#btnGen').textContent=on?'Идёт генерация…':'Сгенерировать названия';
  $('#btnStop').style.display=on?'':'none';
}
function stopPoll(){ if(pollTimer){clearInterval(pollTimer);pollTimer=null;} }
function rowKey(r){
  return [r.art,r.base,r.models,r.name,r.why,r.status,r.seo||''].join('\u0001');
}
function mergeRows(srv){
  // структура изменилась (другое число строк) — только целиком, и не под редактором
  if(srv.length!==rows.length){
    if(editing){ pendingSrv=srv; return; }
    rows=srv; render(); return;
  }
  srv.forEach((sr,i)=>{
    if(editing && editing.r===i){ pendingSrv=srv; return; }  // эту строку правят — не трогаем
    const lr=rows[i];
    let next;
    if(lr && (lr.status==='ok'||lr.status==='fixed') &&
       (sr.status==='done'||sr.status==='ok'||sr.status==='fixed')) return;
    if(lr && lr._dirty){
      const keepWait = lr.status==='wait' && sr.status==='err';
      next={...sr, art:lr.art, base:lr.base, models:lr.models,
            status: keepWait?'wait':sr.status,
            why: keepWait?'':sr.why, _dirty:true};
    } else {
      next=sr;
    }
    if(lr && lr._seoOpen) next._seoOpen=true;
    if(!lr || rowKey(next)!==rowKey(lr)){   // перерисовываем ТОЛЬКО изменившиеся строки
      rows[i]=next;
      updateRow(i);
    } else {
      rows[i]=next;
    }
  });
}
function flushPending(){
  if(pendingSrv && !editing){ const p=pendingSrv; pendingSrv=null; mergeRows(p); }
}
function startPoll(){
  stopPoll(); setRunning(true);
  pollTimer=setInterval(async()=>{
    try{
      const d=await apiGet('/api/job?id='+encodeURIComponent(jobId));
      if(d.error){stopPoll();setRunning(false);return;}
      mergeRows(d.rows);
      updateCosts();
      $('#btnSeo').disabled=!!d.seo_running;
      $('#btnSeo').textContent=d.seo_running?'Идёт SEO…':'Сгенерировать SEO';
      if(!d.running && !d.seo_running){stopPoll();setRunning(false);refreshSnaps(jobId);}
    }catch(e){}
  },2000);
}
async function updateCosts(){
  try{
    const d=await apiGet('/api/costs');
    $('#spend').textContent='$'+d.today+' сегодня / $'+d.total+' всего';
  }catch(e){}
}
async function refreshSnaps(selId){
  updateCosts();
  try{
    const d=await apiGet('/api/snapshots');
    const s=$('#snaps');
    s.innerHTML='<option value=\"\">— слепки —</option>'+
      d.snapshots.map(x=>`<option value=\"${x.id}\">${x.id} · ${x.done}/${x.total}${x.running?' · идёт':''}</option>`).join('');
    if(selId) s.value=selId;
  }catch(e){}
}
async function openSnap(id){
  const d=await apiGet('/api/job?id='+encodeURIComponent(id));
  if(d.error){alert(d.error);return;}
  jobId=id; rows=d.rows; render();
  if(d.running) startPoll(); else {stopPoll();setRunning(false);}
  $('#snaps').value=id;
}

$('#btnGen').onclick=async()=>{
  await fillFromCatalog();
  const payload=rows.filter(r=>r.art||r.base);
  if(!payload.length){alert('Таблица пуста');return;}
  setRunning(true);
  try{
    const d= jobId ? await api('/api/resume',{id:jobId,rows:payload})
                   : await api('/api/start',{rows:payload});
    if(d.error){alert(d.error);setRunning(false);return;}
    rows.forEach(r=>{delete r._dirty;});
    jobId=d.id; refreshSnaps(jobId); startPoll();
  }catch(e){setRunning(false);}
};
$('#btnStop').onclick=async()=>{ if(jobId) await api('/api/stop',{id:jobId}); };
$('#btnSeo').onclick=async()=>{
  if(!jobId){alert('Сначала сгенерируйте названия — SEO делается по готовому слепку');return;}
  const d=await api('/api/seo_start',{id:jobId});
  if(d.error){alert(d.error);return;}
  $('#btnSeo').disabled=true; $('#btnSeo').textContent='Идёт SEO…';
  startPoll();
};
$('#btnOpen').onclick=()=>{ const id=$('#snaps').value; if(id) openSnap(id); };
$('#btnDelSnap').onclick=async()=>{
  const id=$('#snaps').value;
  if(!id){alert('Выберите слепок в списке');return;}
  if(!confirm('Удалить слепок '+id+' безвозвратно?\nПринятые правки и справочник не пострадают — они хранятся отдельно.')) return;
  const d=await api('/api/snap_delete',{id});
  if(d.error){alert(d.error);return;}
  if(jobId===id){jobId=null;stopPoll();setRunning(false);rows=[emptyRow()];render();}
  refreshSnaps();
};

document.addEventListener('click',e=>{
  if(e.target.classList && e.target.classList.contains('seocopy')) return;
  const td=e.target.closest && e.target.closest('td.seocell');
  if(!td) return;
  const i=+td.dataset.si;
  if(!rows[i] || !rows[i].seo) return;
  if(td.classList.contains('open') && getSelection().toString()) return; // выделяют текст
  rows[i]._seoOpen=!rows[i]._seoOpen;
  updateRow(i);
});
window.copySeo=i=>{
  const ta=document.createElement('textarea');
  ta.value=rows[i].seo||'';
  ta.style.position='fixed'; ta.style.opacity='0';
  document.body.appendChild(ta); ta.select();
  document.execCommand('copy'); ta.remove();
};

window.regen=async i=>{
  rows[i].status='wait'; rows[i].name=''; rows[i].gen0=''; rows[i].why='';
  render(); $('#btnGen').onclick();
};
window.approve=async i=>{
  const r=rows[i];
  await api('/api/feedback',{sid:jobId,idx:i,art:r.art,base:r.base,models:r.models,
    generated:r.name,correct:r.name});
  r.status='ok';render();
};
window.fix=async i=>{
  const r=rows[i];
  const comment=prompt('Причина ошибки — попадёт в правила методички (правка названия уже сохранена автоматически):','');
  if(comment===null || !comment.trim()) return;
  await api('/api/feedback',{sid:jobId,idx:i,art:r.art,base:r.base,models:r.models,
    generated:r.gen0||'',correct:r.name,comment:comment.trim()});
  r.status='fixed';render();
};

async function uploadCatalog(file){
  const fd=new FormData(); fd.append('file',file);
  const res=await fetch('/api/catalog_upload',{method:'POST',headers:{'X-Auth':AUTH},body:fd});
  if(res.status===401){localStorage.removeItem('auth');AUTH='';$('#login').style.display='flex';return;}
  const d=await res.json();
  if(d.error){alert('Ошибка: '+d.error);return;}
  $('#catcount').textContent=d.total;
  alert(`Справочник загружен: добавлено ${d.added}, обновлено ${d.updated}. Всего позиций: ${d.total}.`);
}
$('#btnCat').onclick=()=>$('#catFile').click();
$('#btnGh').onclick=async()=>{
  if(!confirm('Скачать свежую версию с GitHub и перезапустить сервер? Текущая сохранится как server.py.bak')) return;
  const d=await api('/api/update_github',{});
  if(d.error){alert('Ошибка: '+d.error);return;}
  alert('Обновление установлено. Сервер перезапускается, страница обновится через 6 секунд.');
  setTimeout(()=>location.reload(),6000);
};

$('#catFile').onchange=e=>{ if(e.target.files[0]){uploadCatalog(e.target.files[0]); e.target.value='';} };

async function fillFromCatalog(){
  const need=rows.filter(r=>r.art && !r.base).map(r=>r.art);
  if(!need.length) return;
  try{
    const d=await api('/api/lookup',{arts:need});
    $('#catcount').textContent=d.total;
    let hit=0;
    rows.forEach(r=>{
      const rec=d.found[r.art];
      if(rec && !r.base){ r.base=rec.base; if(!r.models&&rec.models) r.models=rec.models; hit++; }
    });
    if(hit) render();
  }catch(e){}
}

async function init(){
  try{
    try{const c=await api('/api/lookup',{arts:[]});$('#catcount').textContent=c.total;}catch(e){}
    const d=await apiGet('/api/snapshots');
    refreshSnaps();
    const run=d.snapshots.find(x=>x.running);
    if(run) openSnap(run.id);
  }catch(e){}
}

$('#btnAdd').onclick=()=>{rows.push(emptyRow());render();};
$('#btnClear').onclick=()=>{if(confirm('Начать новую партию? Текущая таблица очистится (слепки сохраняются).')){rows=[emptyRow()];jobId=null;stopPoll();setRunning(false);render();refreshSnaps();}};
$('#btnCopyRes').onclick=()=>{
  const lines=rows.filter(r=>(r.art||'').trim())
                  .map(r=>(r.art||'')+'\t'+(r.name||''));
  if(!lines.length){alert('В таблице нет данных');return;}
  const ta=document.createElement('textarea');
  ta.value=lines.join('\n');
  ta.style.position='fixed'; ta.style.opacity='0';
  document.body.appendChild(ta); ta.select();
  const ok=document.execCommand('copy'); ta.remove();
  const b=$('#btnCopyRes'), t=b.textContent;
  b.textContent=ok?('Скопировано: '+lines.length+' строк'):'Не удалось скопировать';
  setTimeout(()=>{b.textContent=t;},2000);
};
$('#btnCsv').onclick=()=>{
  const head='Артикул;Краткий;Базовое;Применимость;Наименование;SEO-описание;Статус\n';
  const body=rows.map(r=>[r.art,shortOf(r.art),r.base,r.models,r.name,r.seo||'',r.status]
    .map(x=>'"'+String(x||'').replace(/"/g,'""')+'"').join(';')).join('\n');
  const blob=new Blob(["\ufeff"+head+body],{type:'text/csv;charset=utf-8'});
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);
  a.download='naimenovaniya.csv';a.click();
};

$('#refcount').textContent='{{ refcount }}';
render();
if(!NEED_PW || AUTH) init();
// доступно подпроектам (модулям)
window.$=$; window.api=api; window.apiGet=apiGet;
</script>
<script>
/* JS подпроектов (модулей) */
{{ module_js }}
</script>
<script>
/* переключение разделов левого меню (универсальное) */
(function(){
  var items = document.querySelectorAll('.side .navitem');
  function show(sec){
    items.forEach(function(b){
      var el = document.getElementById('sec-'+b.dataset.sec);
      if(el) el.style.display = (b.dataset.sec===sec ? '' : 'none');
      b.classList.toggle('active', b.dataset.sec===sec);
    });
    try{ localStorage.setItem('section', sec); }catch(e){}
    var cb = window['onShow_'+sec];
    if(typeof cb==='function'){ try{ cb(); }catch(e){} }
  }
  items.forEach(function(b){ b.addEventListener('click', function(){ show(b.dataset.sec); }); });
  var saved=null;
  try{ saved = localStorage.getItem('section'); }catch(e){}
  if(saved && document.getElementById('sec-'+saved)) show(saved);
})();
</script>
</body>
</html>"""


# ---------- Подпроекты (модули) ----------
# Ядро само находит файлы mod_*.py рядом с server.py и подключает их.
# Каждый модуль определяет register(ctx) и возвращает
# {id, title, section_html, css, js}; свои /api-маршруты регистрирует сам.
HERE = os.path.dirname(os.path.abspath(__file__))
MODULES = []


def _load_modules():
    import importlib.util
    import glob
    ctx = {
        "app": app, "check_auth": check_auth, "lock": lock,
        "jsonify": jsonify, "request": request, "data_dir": HERE,
    }
    for path in sorted(glob.glob(os.path.join(HERE, "mod_*.py"))):
        name = os.path.splitext(os.path.basename(path))[0]
        try:
            spec = importlib.util.spec_from_file_location(name, path)
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            info = m.register(ctx)
            if info:
                MODULES.append(info)
                print(f"Модуль подключён: {name} → «{info.get('title', '')}»")
        except Exception as e:
            print(f"Модуль пропущен ({name}): {e}")


_load_modules()


@app.get("/")
def index():
    nav = "".join(
        '<button class="navitem" data-sec="{id}">{title}</button>'.format(
            id=m["id"], title=m["title"]) for m in MODULES)
    sections = "".join(
        '<main id="sec-{id}" style="display:none">{html}</main>'.format(
            id=m["id"], html=m.get("section_html", "")) for m in MODULES)
    css = "".join(m.get("css", "") for m in MODULES)
    js = "".join(m.get("js", "") for m in MODULES)
    page = PAGE.replace("{{ module_nav }}", nav)
    page = page.replace("{{ module_sections }}", sections)
    page = page.replace("{{ module_css }}", css)
    page = page.replace("{{ module_js }}", js)
    page = page.replace("{{ need_pw }}", "true" if APP_PASSWORD else "false")
    page = page.replace("{{ refcount }}", str(len(REFS)))
    page = page.replace("{{ version }}", VERSION)
    return Response(page, mimetype="text/html")


if __name__ == "__main__":
    if not APP_PASSWORD:
        print("ВНИМАНИЕ: пароль не задан (APP_PASSWORD). Доступ будет открыт всем!")
    app.run(host="0.0.0.0", port=PORT)
