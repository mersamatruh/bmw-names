"""
Подпроект «Анализ продаж» (анализатор продаж Porsche) — модуль для ядра.

Пробная версия: онлайн-таблица позиций (артикулы, продажи по каналам,
остатки, заказано/в пути, продажи за год), свой справочник
«артикул → наименование» и расчёт колонки «в заказ» по простой формуле
(дальше её заменит/дополнит ИИ по правилам владельца).

Данные на сервере (ctx["data_dir"]):
  sales.json          — {settings: {period_days, cover_days}, current: id слепка}
  sales_snapshots/    — слепки таблицы <ГГГГ-ММ-ДД_чч-мм-сс>.json
                        {id, rows, period: {from, to}}
                        (как слепки в «Названиях»: открытый слепок
                        автосохраняется, «Новая партия» начинает новый)
  sales_catalog.json  — {артикул: наименование}
  sales_lessons.json  — «методичка заказа»: правки «В заказ» с причинами
                        [{snap, art, name, calc, manual, reason, period,
                          cover, row, at, updated}] — материал для ИИ
"""

import os
import re
import json
import datetime

# Колонки, которые хранятся в строке (наименование, «общие продажи»
# и «в заказ» вычисляются и не хранятся).
TEXT_FIELDS = ("art", "repl", "comment", "order_reason")
NUM_FIELDS = ("profit", "turn_de", "sold_de", "sold_sup", "sold_stock",
              "stock", "ordered", "transit", "year",
              "order_manual",  # «в заказ», исправленное вручную
              "order_ok")      # одобренное (✓) расчётное количество

# Кириллица, похожая на латиницу, — чтобы «А123» с русской раскладки
# не превращался в другой артикул.
_CYR = str.maketrans("АВЕКМНОРСТХУавекмнорстху", "ABEKMHOPCTXYABEKMHOPCTXY")


def norm_art(s):
    """Маска артикула: только A-Z и 0-9, верхний регистр."""
    s = str(s or "").translate(_CYR).upper()
    return re.sub(r"[^A-Z0-9]", "", s)


def parse_num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    s = re.sub(r"[\s\u00a0\u202f]", "", str(v)).replace(",", ".")
    if s == "":
        return None
    try:
        x = float(s)
    except ValueError:
        return None
    return int(x) if x == int(x) else x


def register(ctx):
    app = ctx["app"]
    check_auth = ctx["check_auth"]
    lock = ctx["lock"]
    jsonify = ctx["jsonify"]
    request = ctx["request"]
    SALES_PATH = os.path.join(ctx["data_dir"], "sales.json")
    CATALOG_PATH = os.path.join(ctx["data_dir"], "sales_catalog.json")
    LESSONS_PATH = os.path.join(ctx["data_dir"], "sales_lessons.json")
    SNAP_DIR = os.path.join(ctx["data_dir"], "sales_snapshots")
    os.makedirs(SNAP_DIR, exist_ok=True)

    # ---------- Хранилище ----------

    def load_json(path, default):
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return default

    def save_json(path, data):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)

    def snap_path(sid):
        sid = re.sub(r"[^0-9A-Za-z_\-]", "", str(sid or ""))
        return os.path.join(SNAP_DIR, sid + ".json") if sid else None

    def new_snap_id():
        sid = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        while os.path.exists(snap_path(sid)):
            sid += "_"
        return sid

    def load_snap(sid):
        p = snap_path(sid)
        if not p or not os.path.exists(p):
            return None
        return load_json(p, {"id": sid, "rows": []})

    def list_snaps():
        out = []
        for fn in sorted(os.listdir(SNAP_DIR), reverse=True):
            if fn.endswith(".json"):
                d = load_json(os.path.join(SNAP_DIR, fn), {})
                rows = d.get("rows") or []
                out.append({"id": fn[:-5], "period": d.get("period") or {},
                            "total": sum(1 for r in rows if r.get("art"))})
        return out

    def load_sales():
        data = load_json(SALES_PATH, {})
        data.setdefault("settings", {})
        data["settings"].setdefault("period_days", 90)
        data["settings"].setdefault("cover_days", 60)
        # переход со старого формата (одна таблица) — она становится слепком
        old_rows = data.pop("rows", None)
        if old_rows:
            sid = new_snap_id()
            save_json(snap_path(sid), {"id": sid, "rows": old_rows})
            data["current"] = sid
            save_json(SALES_PATH, data)
        data.setdefault("current", None)
        return data

    def clean_row(r):
        out = {}
        for k in TEXT_FIELDS:
            v = str(r.get(k) or "").strip()
            out[k] = norm_art(v) if k in ("art", "repl") else v[:1000]
        for k in NUM_FIELDS:
            out[k] = parse_num(r.get(k))
        out["n"] = parse_num(r.get("n"))   # постоянный № строки (порядок вставки)
        return out

    def names_for(arts):
        cat = load_json(CATALOG_PATH, {})
        return {a: cat.get(a, "") for a in arts if a}

    # ---------- API ----------

    def deny():
        return jsonify({"error": "Неверный пароль"}), 401

    @app.get("/api/sales")
    def api_sales():
        if not check_auth(request):
            return deny()
        with lock:
            data = load_sales()
            want = request.args.get("id")
            if want is not None:          # «Открыть» слепок
                if want and load_snap(want) is None:
                    return jsonify({"error": "Слепок не найден"}), 404
                data["current"] = want or None
                save_json(SALES_PATH, data)
        snap = load_snap(data["current"]) if data["current"] else None
        if snap is None:
            data["current"] = None
        rows = (snap or {}).get("rows") or []
        period = (snap or {}).get("period") or {}
        cat = load_json(CATALOG_PATH, {})
        arts = set()
        for r in rows:
            arts.add(r.get("art"))
            arts.add(r.get("repl"))
        names = {a: cat[a] for a in arts if a and a in cat}
        return jsonify({"settings": data["settings"], "rows": rows,
                        "period": period,
                        "lessons_count": len(load_json(LESSONS_PATH, [])),
                        "id": data["current"], "snapshots": list_snaps(),
                        "names": names, "catalog_size": len(cat)})

    @app.post("/api/sales_save")
    def api_sales_save():
        if not check_auth(request):
            return deny()
        d = request.get_json(force=True) or {}
        rows = [clean_row(r) for r in (d.get("rows") or []) if isinstance(r, dict)]
        st = d.get("settings") or {}
        settings = {}
        for k, dflt in (("period_days", 90), ("cover_days", 60)):
            v = parse_num(st.get(k))
            settings[k] = v if isinstance(v, (int, float)) and v > 0 else dflt
        per = d.get("period") or {}
        period = {k: str(per.get(k) or "") for k in ("from", "to")
                  if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(per.get(k) or ""))}
        sid = d.get("id") or None
        created = False
        with lock:
            data = load_sales()
            data["settings"] = settings
            if sid and snap_path(sid) and os.path.exists(snap_path(sid)):
                save_json(snap_path(sid), {"id": sid, "rows": rows, "period": period})
            elif rows or period:          # первая запись новой партии
                sid, created = new_snap_id(), True
                save_json(snap_path(sid), {"id": sid, "rows": rows, "period": period})
            else:
                sid = None
            data["current"] = sid
            save_json(SALES_PATH, data)
        return jsonify({"ok": True, "count": len(rows), "id": sid,
                        "created": created})

    # ---------- Методичка заказа (правки «В заказ» с причинами) ----------

    @app.post("/api/sales_lesson")
    def api_sales_lesson():
        """Сохранить/обновить правку: ключ — слепок + артикул."""
        if not check_auth(request):
            return deny()
        d = request.get_json(force=True) or {}
        snap, art = str(d.get("snap") or ""), norm_art(d.get("art"))
        reason = str(d.get("reason") or "").strip()[:2000]
        approved = bool(d.get("approved"))   # ✓ — расчёт ИИ признан верным, без причины
        if not snap or not art or not (reason or approved):
            return jsonify({"error": "Нужны слепок, артикул и причина"}), 400
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        rec = {"snap": snap, "art": art, "repl": norm_art(d.get("repl")),
               "name": str(d.get("name") or "")[:300],
               "calc": parse_num(d.get("calc")), "manual": parse_num(d.get("manual")),
               "kind": "approved" if approved else "corrected",
               "reason": reason, "period": d.get("period") or {},
               "cover": parse_num(d.get("cover")),
               "row": {k: parse_num((d.get("row") or {}).get(k)) for k in NUM_FIELDS
                       if k not in ("order_manual", "order_ok")}}
        with lock:
            lessons = load_json(LESSONS_PATH, [])
            for old in lessons:
                if old.get("snap") == snap and old.get("art") == art:
                    if old.get("reason") != reason or old.get("kind") != rec["kind"]:
                        old.setdefault("history", []).append(
                            {"kind": old.get("kind") or "corrected",
                             "reason": old.get("reason"), "manual": old.get("manual"),
                             "at": old.get("updated") or old.get("at")})
                    old.update(rec)
                    old["updated"] = now
                    break
            else:
                rec["at"] = rec["updated"] = now
                lessons.append(rec)
            save_json(LESSONS_PATH, lessons)
        return jsonify({"ok": True, "lessons_count": len(lessons)})

    @app.post("/api/sales_lesson_del")
    def api_sales_lesson_del():
        """Правку отменили (вернули расчёт) — убрать её из методички."""
        if not check_auth(request):
            return deny()
        d = request.get_json(force=True) or {}
        snap, art = str(d.get("snap") or ""), norm_art(d.get("art"))
        with lock:
            lessons = [x for x in load_json(LESSONS_PATH, [])
                       if not (x.get("snap") == snap and x.get("art") == art)]
            save_json(LESSONS_PATH, lessons)
        return jsonify({"ok": True, "lessons_count": len(lessons)})

    @app.get("/api/sales_lessons")
    def api_sales_lessons():
        if not check_auth(request):
            return deny()
        return jsonify({"lessons": load_json(LESSONS_PATH, [])})

    @app.post("/api/sales_snap_delete")
    def api_sales_snap_delete():
        if not check_auth(request):
            return deny()
        sid = (request.get_json(force=True) or {}).get("id")
        p = snap_path(sid)
        if not p or not os.path.exists(p):
            return jsonify({"error": "Слепок не найден"}), 404
        with lock:
            os.remove(p)
            data = load_sales()
            if data.get("current") == sid:
                data["current"] = None
                save_json(SALES_PATH, data)
        return jsonify({"ok": True, "snapshots": list_snaps()})

    @app.post("/api/sales_names")
    def api_sales_names():
        if not check_auth(request):
            return deny()
        d = request.get_json(force=True) or {}
        arts = [norm_art(a) for a in (d.get("arts") or [])][:5000]
        return jsonify({"names": names_for(arts)})

    def catalog_merge(pairs):
        """Добавляет пары (артикул, наименование) в справочник."""
        added = updated = skipped = 0
        with lock:
            cat = load_json(CATALOG_PATH, {})
            for art, name in pairs:
                art = norm_art(art)
                name = str(name or "").strip().strip('"').strip()
                if not art or not name or name.lower() == "nan":
                    skipped += 1
                    continue
                if art in cat:
                    if cat[art] != name:
                        updated += 1
                else:
                    added += 1
                cat[art] = name
            save_json(CATALOG_PATH, cat)
        return {"ok": True, "added": added, "updated": updated,
                "skipped": skipped, "catalog_size": len(cat)}

    @app.post("/api/sales_catalog_upload")
    def api_sales_catalog_upload():
        """Файл Excel (xlsx/xls) или CSV: столбцы «артикул», «наименование»."""
        if not check_auth(request):
            return deny()
        f = request.files.get("file")
        if f is None:
            return jsonify({"error": "Файл не получен"}), 400
        import io
        import pandas as pd
        fn = (f.filename or "").lower()
        try:
            data = f.read()
            if fn.endswith((".csv", ".txt", ".tsv")):
                df = pd.read_csv(io.BytesIO(data), sep=None, engine="python",
                                 dtype=str, header=None, encoding="utf-8-sig")
            else:
                df = pd.read_excel(io.BytesIO(data), dtype=str, header=None)
        except Exception as e:
            return jsonify({"error": f"Не удалось прочитать файл: {e}"}), 400
        cols = [c for c in df.columns if df[c].notna().any()]
        if len(cols) < 2:
            return jsonify({"error": "В файле нужны два столбца: артикул и наименование"}), 400
        df = df[cols[:2]].fillna("")
        rows = df.values.tolist()
        # первая строка — заголовок («Артикул», «Наименование»)? пропускаем
        if rows:
            h0, h1 = str(rows[0][0]).lower(), str(rows[0][1]).lower()
            if any(w in h0 for w in ("артикул", "article", "номер", "part")) or \
               any(w in h1 for w in ("наимен", "назван", "name")):
                rows = rows[1:]
        return jsonify(catalog_merge(rows))

    @app.post("/api/sales_catalog_add")
    def api_sales_catalog_add():
        """Принимает текст «артикул<TAB>наименование» построчно (из Excel/CSV)."""
        if not check_auth(request):
            return deny()
        d = request.get_json(force=True) or {}
        text = str(d.get("text") or "")
        pairs, bad = [], 0
        for line in text.splitlines():
            if not line.strip():
                continue
            parts = re.split(r"\t|;", line, maxsplit=1)
            if len(parts) < 2:
                bad += 1
                continue
            pairs.append(parts)
        res = catalog_merge(pairs)
        res["skipped"] += bad
        return jsonify(res)

    # ---------- Что отдаём ядру для оболочки ----------

    SECTION_HTML = """
  <h2 style="margin:0 0 4px">📈 Анализ продаж</h2>
  <p class="hint" style="margin:0 0 14px">Анализатор продаж Porsche (пробная версия).
    Вносите позиции или вставляйте столбцы из Excel вместе с заголовком (Ctrl+V в ячейку первой строки) — заголовок отбросится. Блок ячеек выделяется мышкой или Shift+клик, весь столбец — кликом по заголовку; сортировка — значок ⇅: Ctrl+C — копия, Delete — очистка. Всё
    сохраняется на сервере автоматически.</p>
  <div class="bar">
    <button onclick="slNew()">Новая партия</button>
    <select id="slSnaps" style="padding:8px;border:1px solid var(--line);border-radius:6px;font:inherit;max-width:280px">
      <option value="">— слепки —</option></select>
    <button onclick="slOpen()">Открыть</button>
    <button onclick="slDelSnap()" title="Удалить выбранный слепок с сервера">Удалить</button>
    <span id="slCurSnap" class="hint"></span>
  </div>
  <div class="bar">
    <label class="hint">Период продаж: с
      <input id="slFrom" class="sldate" type="date" min="2000-01-01" max="2099-12-31"> по
      <input id="slTo" class="sldate" type="date" min="2000-01-01" max="2099-12-31"></label>
    <span id="slDays" class="hint"></span>
    <label class="hint">Заказывать с запасом на, дней
      <input id="slCover" class="slnum" type="number" min="1"></label>
    <input id="slSearch" placeholder="Поиск по артикулу или названию">
    <button onclick="slAddRow()">+ Строка</button>
    <button onclick="slResetOrder()" title="Снять фильтры и сортировку, выстроить строки по № от 1">↺ Вернуть исходный порядок</button>
    <button onclick="slExport()">⬇️ Скачать CSV</button>
    <button onclick="document.getElementById('slCatXls').click()"
            title="Excel-файл: столбец A — артикул, столбец B — наименование">📥 Загрузить справочник (Excel)</button>
    <input type="file" id="slCatXls" accept=".xlsx,.xls,.xlsm,.csv,.txt,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/vnd.ms-excel" style="display:none"
           onchange="slCatUpload(this)">
    <button onclick="slToggleCat()">📚 Справочник <span id="slCatCnt" class="hint"></span></button>
    <label class="hint" title="При вставке из Excel первая строка (название столбца) отбрасывается">
      <input type="checkbox" id="slSkipHead" checked> Убирать заголовок при вставке</label>
    <span id="slStCnt" class="slstcnt"></span>
    <span id="slLessons" class="hint" title="Правки «В заказ» с причинами — из них будет учиться ИИ"></span>
    <span id="slSaved" class="hint"></span>
  </div>
  <div id="slCat" class="panel" style="display:none;padding:12px 14px;margin-bottom:12px">
    <div class="hint" style="margin-bottom:6px">Справочник «артикул → наименование».
      Скопируйте из Excel два столбца (артикул и наименование) и вставьте сюда,
      либо загрузите файл Excel (столбец A — артикул, B — наименование).
      Существующие артикулы обновятся.</div>
    <textarea id="slCatText" rows="6" placeholder="99610610155	Фильтр масляный"></textarea>
    <div class="bar" style="margin:8px 0 0">
      <button class="primary" onclick="slCatAdd()">Добавить в справочник</button>
      <button onclick="document.getElementById('slCatXls').click()">📥 Загрузить файл Excel</button>
      <span id="slCatMsg" class="hint"></span>
    </div>
  </div>
  <div id="slDlg" class="sldlg" style="display:none">
    <div class="box">
      <div id="slDlgHd" style="font-weight:700;font-size:16px;margin-bottom:4px">Объясни почему?</div>
      <div id="slDlgSub" class="hint" style="margin-bottom:10px"></div>
      <label class="hint">Количество в заказ
        <input id="slDlgQty" class="slnum" style="width:90px"></label>
      <textarea id="slDlgText" rows="4" placeholder="Причина (обязательно)…"></textarea>
      <div id="slDlgErr" class="hint" style="color:var(--err);min-height:18px"></div>
      <div class="bar" style="margin:6px 0 0">
        <button class="primary" onclick="slDlgOk()">Сохранить (Enter)</button>
        <button onclick="slDlgCancel()">Отмена (Esc)</button>
        <button id="slDlgReset" onclick="slDlgReset()" title="Убрать правку и вернуть расчётное количество">Вернуть расчёт</button>
        <span class="hint">Shift+Enter — новая строка</span>
      </div>
    </div>
  </div>
  <div id="slWrap"><table id="slTable"><thead></thead><tbody></tbody></table></div>
  <p class="hint" style="margin-top:10px">«В заказ» считается формулой: средний
    дневной спрос (60% — выбранный период, 40% — последние 365 дней) × дней запаса
    (по умолчанию 60 = 2 месяца) − наличие − уже заказано. Наведите на число, чтобы
    увидеть расчёт. Статус: «Требует проверки» — число посчитал ИИ; ✓ — количество верное,
    статус «Одобрено» (без комментария); ✎ или правка числа — спросим причину, статус
    «Скорректировано». Одобрения и правки копятся в методичке для обучения ИИ.</p>
"""

    CSS = """
  #sec-sales{max-width:none;margin:18px 0}
  #slWrap{overflow:auto;max-height:calc(100vh - 230px);border:1px solid var(--line);
          border-radius:8px}
  #slTable{border:none;border-radius:0;min-width:1500px}
  #slTable{overflow:visible}  /* overflow:hidden у общей таблицы ломает закрепление шапки */
  #slTable thead{position:sticky;top:0;z-index:2}
  #slTable tr.ttl th{user-select:none}
  #slTable th.csel{background:#dbe7fb}
  #slTable tr.ttl th{position:relative}
  #slTable tr.ttl th{padding-right:26px}
  #slTable th .ar{cursor:pointer;position:absolute;top:3px;right:3px;width:20px;height:26px;
        display:flex;flex-direction:column;align-items:center;justify-content:center;
        border-radius:5px;line-height:1}
  #slTable th .ar i{font-style:normal;font-size:11px;color:#c3cad5;height:11px}
  #slTable th .ar:hover{background:#e3edfb}
  #slTable th .ar:hover i{color:#8aa6d6}
  #slTable th .ar.asc i.u,#slTable th .ar.desc i.d{color:var(--blue);font-size:14px;height:13px}
  #slTable th .ar.asc i.d,#slTable th .ar.desc i.u{color:#dde2ea}
  #slTable th{background:var(--panel);white-space:normal;vertical-align:bottom;
              font-size:11px;line-height:1.25}
  #slTable th.srt{cursor:pointer}
  #slTable tr.flt th{padding:3px 4px;background:#f7f9fb;border-bottom:2px solid var(--line)}
  #slTable tr.flt input,#slTable tr.flt select{width:100%;box-sizing:border-box;padding:3px 5px;
        border:1px solid var(--line);border-radius:4px;font:12px/1.3 inherit;background:#fff;min-width:40px}
  #slTable tr.flt input.on,#slTable tr.flt select.on{border-color:var(--blue);background:#eef4fe}
  #slTable td.nid{text-align:right;padding:6px 6px;color:var(--mut);font-size:12px;
                  background:#f7f9fb;width:34px}
  #slTable td{padding:0;border-right:1px solid var(--line);vertical-align:middle}
  #slTable td input{width:100%;border:none;background:transparent;padding:6px 7px;
                    font:inherit;font-size:13px;box-sizing:border-box}
  #slTable td input:focus{outline:2px solid var(--blue);outline-offset:-2px;background:#fff}
  #slTable td.num input{text-align:right}
  #slTable td.art{min-width:125px}
  #slTable td.art input{font-family:var(--mono)}
  #slTable td.calc{background:#f7f9fb;text-align:right;padding:6px 8px;font-weight:600}
  #slTable td.order{background:#ffe2c2}
  #slTable td.order input{text-align:right;font-weight:600;color:#9a4a00}
  #slTable td.order.manual input{font-weight:800;color:#c2410c;text-decoration:underline dotted}
  #slTable th.order{background:#ffd3a3;color:#7a3a00}
  #slTable td.nm{padding:6px 8px;font-size:12px;min-width:180px}
  #slTable td.st{white-space:nowrap;padding:0 4px}
  #slTable td.st .chip{font-size:11px;padding:2px 7px;margin-right:3px}
  #sec-sales .chip.fixed{background:#e3edfb;color:var(--blue)}
  .slstcnt .chip{font-size:11px;padding:2px 8px}
  #slTable td.st button.stb{border:1px solid var(--line);background:#fff;border-radius:5px;padding:1px 5px;
        margin-left:2px;font-size:13px;cursor:pointer;color:var(--mut)}
  #slTable td.st button.stb:hover{border-color:var(--blue);color:var(--blue)}
  #slTable td.st button.stb.on{background:#eef4fe;border-color:var(--blue);color:var(--blue)}
  #slTable th.status{text-align:center;white-space:nowrap}
  .sldate{padding:5px 6px;border:1px solid var(--line);border-radius:6px;font:inherit;margin:0 2px}
  #slDays.warn{color:#c2410c;font-weight:600}
  .sldlg{position:fixed;inset:0;background:rgba(15,23,42,.35);z-index:50;
         display:flex;align-items:center;justify-content:center}
  .sldlg .box{background:var(--panel);border-radius:10px;padding:18px 20px;width:min(520px,92vw);
              box-shadow:0 10px 40px rgba(0,0,0,.25)}
  .sldlg textarea{width:100%;box-sizing:border-box;margin-top:10px;padding:8px;font:inherit;
                  border:1px solid var(--line);border-radius:6px}
  #slTable tr.cur td{background-color:#eaf3ff}
  #slTable tr.cur td.order{background-color:#ffd4a8}
  #slTable td.order.good{background-color:#cdeed8}
  #slTable td.order.good input{color:#166534}
  #slTable tr.cur td.order.good{background-color:#b6e5c6}
  #slTable td.sel{background:#d6e5fc !important;border-color:#d6e5fc}   /* внутри блока — без сетки, рамка только снаружи */
  #slTable td.sel input{background:transparent}
  #slTable td.nm.miss{color:var(--mut);font-style:italic}
  #slTable td.del{width:28px;text-align:center}
  #slTable td.del button{border:none;background:none;color:var(--mut);padding:4px}
  #slTable td.del button:hover{color:var(--err)}
  .slnum{width:70px;padding:6px 8px;border:1px solid var(--line);border-radius:6px;
         font:inherit;margin-left:4px}
  #slSearch{flex:1;min-width:200px;max-width:320px;padding:7px 10px;
            border:1px solid var(--line);border-radius:6px;font:inherit}
  #slCatText{width:100%;box-sizing:border-box;font-family:var(--mono);font-size:12px;
             border:1px solid var(--line);border-radius:6px;padding:8px}
"""

    JS = r"""
/* колонки таблицы: k — ключ, t — заголовок, ty — тип */
var SL_COLS = [
  {k:'n',          t:'№',                           ty:'nid'},
  {k:'art',        t:'Артикул',                     ty:'art'},
  {k:'repl',       t:'Артикул замены',              ty:'art'},
  {k:'name',       t:'Наименование (справочник)',   ty:'name'},
  {k:'profit',     t:'Прибыль склад за период',     ty:'num'},
  {k:'turn_de',    t:'Оборот Герм.',                ty:'num'},
  {k:'sold_de',    t:'Продано за период Герм.',     ty:'num'},
  {k:'sold_sup',   t:'Продано за период поставщики',ty:'num'},
  {k:'sold_stock', t:'Продано за период склад',     ty:'num'},
  {k:'total',      t:'Общие продажи',               ty:'calc'},
  {k:'order',      t:'В заказ',                     ty:'order'},
  {k:'status',     t:'Статус',                      ty:'status'},
  {k:'comment',    t:'Комментарий',                 ty:'text'},
  {k:'stock',      t:'Наше наличие',                ty:'num'},
  {k:'ordered',    t:'Кол-во заказано',             ty:'num'},
  {k:'transit',    t:'В пути',                      ty:'num'},
  {k:'year',       t:'Продажи за год (365 дн.)',    ty:'num'}
];
var SL = {colf:{}, sort:null, rows:[], names:{}, settings:{period_days:90, cover_days:60}, loaded:false, id:null, period:{},
          saveTimer:null, filter:''};
var SL_CYR = {'А':'A','В':'B','Е':'E','К':'K','М':'M','Н':'H','О':'O','Р':'P','С':'C',
              'Т':'T','Х':'X','У':'Y'};

function slEsc(s){return (s==null?'':s+'').replace(/[&<>"]/g,function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
function slArt(s){
  s=(s||'').toUpperCase().replace(/[А-ЯЁ]/g,function(c){return SL_CYR[c]||c;});
  return s.replace(/[^A-Z0-9]/g,'');
}
function slNum(v){
  if(v==null) return null;
  var s=(v+'').replace(/[\s\u00a0\u202f]/g,'').replace(',','.');
  if(s==='') return null;
  var x=parseFloat(s); return isNaN(x)?null:x;
}
function slFmt(v){
  if(v==null||v==='') return '';
  return (Math.round(v*100)/100).toLocaleString('ru-RU',{maximumFractionDigits:2});
}
function slTotal(r){
  var a=r.sold_de, b=r.sold_sup, c=r.sold_stock;
  if(a==null&&b==null&&c==null) return null;
  return (a||0)+(b||0)+(c||0);
}
/* пробная формула «в заказ» */
function slPeriodDays(){
  var f=SL.period.from, t=SL.period.to;
  if(f && t){ var d=Math.round((Date.parse(t)-Date.parse(f))/864e5)+1; if(d>0) return d; }
  return null;
}
function slShowDays(){
  var el=document.getElementById('slDays'), d=slPeriodDays();
  var bad=SL.period.from && SL.period.to && SL.period.from>SL.period.to;
  el.textContent=d?('= '+d+' дн.'):bad?'дата «с» позже даты «по» — проверьте период'
    :('период не указан, считаю как '+(SL.settings.period_days||90)+' дн.');
  el.classList.toggle('warn', !d);
}
function slOrder(r){
  var P=slPeriodDays()||SL.settings.period_days||90, C=SL.settings.cover_days||60;
  var tot=slTotal(r)||0, yr=r.year||0;
  var rp=tot/P, ry=yr/365, rate;
  if(tot>0&&yr>0) rate=0.6*rp+0.4*ry; else rate=Math.max(rp,ry);
  var need=rate*C, have=(r.stock||0)+(r.ordered||0);
  var q=Math.max(0, Math.ceil(need-have-1e-9));
  var why='Спрос в день: '+slFmt(rp)+' (период '+P+' дн.) / '+slFmt(ry)+' (год) → '+slFmt(rate)
    +'\nНужно на '+C+' дн.: '+slFmt(need)
    +'\nЕсть: наличие '+(r.stock||0)+' + заказано '+(r.ordered||0)+' = '+have
    +'\nВ заказ: '+q;
  return {q:q, why:why, any:(tot>0||yr>0||r.stock!=null||r.ordered!=null)};
}
function slOrderVal(r){
  if(r.order_manual!=null) return r.order_manual;
  var o=slOrder(r); return o.any?o.q:null;
}
function slName(r){
  return SL.names[r.art] || (r.repl && SL.names[r.repl]) || '';
}

function slSnapList(list){
  var s=document.getElementById('slSnaps');
  s.innerHTML='<option value="">— слепки —</option>'+(list||[]).map(function(x){
    var per=(x.period&&x.period.from&&x.period.to)?(' · '+x.period.from.split('-').reverse().join('.')+'–'+x.period.to.split('-').reverse().join('.')):'';
    return '<option value="'+slEsc(x.id)+'">'+slEsc(x.id)+per+' · '+x.total+' поз.</option>';}).join('');
  if(SL.id) s.value=SL.id;
  document.getElementById('slCurSnap').textContent=SL.id?('Открыт слепок '+SL.id):'Новая партия (слепок создастся при первом сохранении)';
}
async function slFetch(q){
  var d=await apiGet('/api/sales'+(q||''));
  if(d.error){ alert(d.error); return false; }
  SL.rows=d.rows||[]; SL.names=d.names||{}; SL.settings=d.settings||SL.settings;
  SL.id=d.id||null; SL.period=d.period||{}; slSnapList(d.snapshots);
  ['from','to'].forEach(function(k){   // битый год (0002…) из старой версии — сбросить
    if(SL.period[k] && !/^20\d\d-/.test(SL.period[k])) SL.period[k]=''; });
  document.getElementById('slFrom').value=SL.period.from||'';
  document.getElementById('slTo').value=SL.period.to||'';
  slShowDays(); slLessonsCount(d.lessons_count);
  if(!SL.rows.length) for(var i=0;i<5;i++) SL.rows.push({});
  return d;
}
async function slFlush(){
  if(!SL.saveTimer) return;
  clearTimeout(SL.saveTimer); SL.saveTimer=null; await slSaveNow();
}
async function slNew(){
  if(!confirm('Начать новую партию? Текущая таблица очистится (слепки сохраняются).')) return;
  await slFlush(); await slFetch('?id='); slRender();
}
async function slOpen(){
  var id=document.getElementById('slSnaps').value; if(!id) return;
  await slFlush(); if(await slFetch('?id='+encodeURIComponent(id))) slRender();
}
async function slDelSnap(){
  var id=document.getElementById('slSnaps').value;
  if(!id){ alert('Выберите слепок в списке'); return; }
  if(!confirm('Удалить слепок '+id+' безвозвратно?\nСправочник не пострадает — он хранится отдельно.')) return;
  var d=await api('/api/sales_snap_delete',{id:id});
  if(d.error){ alert(d.error); return; }
  if(SL.id===id){ SL.id=null; SL.rows=[]; for(var i=0;i<5;i++) SL.rows.push({}); slRender(); }
  slSnapList(d.snapshots);
}
async function slLoad(){
  if(SL.loaded) return;
  try{
    var d=await slFetch(); if(!d) return;
    SL.loaded=true;
    document.getElementById('slCover').value=SL.settings.cover_days;
    slCatCount(d.catalog_size);
    slRender();
  }catch(e){ document.getElementById('slSaved').textContent='Не удалось загрузить таблицу.'; }
}
function slLessonsCount(n){
  var w=(n%10===1&&n%100!==11)?'запись':((n%10>=2&&n%10<=4&&(n%100<10||n%100>=20))?'записи':'записей');
  document.getElementById('slLessons').textContent=n?('📘 Методичка: '+n+' '+w):''; }
function slCatCount(n){ document.getElementById('slCatCnt').textContent=n?'('+n+')':''; }

function slColMatch(r, ci, f){
  var t=(slCellText(r,ci)+'').trim(), m;
  if(f==='пусто') return t==='';
  if(f==='не пусто') return t!=='';
  if((m=f.match(/^(>=|<=|>|<|=)\s*(-?[\d\s.,]+)$/))){
    var v=slNum(t), x=slNum(m[2]); if(v==null||x==null) return false;
    return m[1]==='>'?v>x:m[1]==='<'?v<x:m[1]==='>='?v>=x:m[1]==='<='?v<=x:v===x;
  }
  return t.toUpperCase().indexOf(f.toUpperCase())>=0;
}
function slVisible(i){
  var r=SL.rows[i];
  for(var k in SL.colf){ var f=SL.colf[k]; if(!f) continue;
    var ci=SL_COLS.findIndex(function(c){return c.k===k;});
    if(ci>=0 && !slColMatch(r,ci,f)) return false; }
  if(!SL.filter) return true;
  var f=SL.filter;
  if(((r.art||'')+' '+(r.repl||'')+' '+slName(r)).toUpperCase().indexOf(f)>=0) return true;
  var fa=/[А-ЯЁ]/.test(f)?'':slArt(f);
  return !!fa && ((r.art||'')+' '+(r.repl||'')).indexOf(fa)>=0;
}
function slEnsureN(){
  var mx=0; SL.rows.forEach(function(r){ if(r.n>mx) mx=r.n; });
  SL.rows.forEach(function(r){ if(!(r.n>0)) r.n=++mx; });
}
function slHead(){
  var t=document.getElementById('slTable');
  if(!t.tHead.rows.length){
    var h='<tr class="ttl">'; SL_COLS.forEach(function(c){
      h+='<th class="'+c.ty+' srt" data-k="'+c.k+'" title="Клик — выделить весь столбец (Shift+клик — несколько столбцов); ⇅ — сортировка">'+c.t
        +' <span class="ar" title="Сортировка: ▲ по возрастанию, ▼ по убыванию"><i class="u">▲</i><i class="d">▼</i></span></th>';});
    h+='<th></th></tr><tr class="flt">';
    SL_COLS.forEach(function(c){
      if(c.ty==='status') h+='<th><select data-f="status"><option value="">все</option>'
        +SL_ST.map(function(s){return '<option>'+s+'</option>';}).join('')+'</select></th>';
      else h+='<th><input data-f="'+c.k+'" placeholder="🔍" title="'+(c.ty==='num'||c.ty==='calc'||c.ty==='order'||c.ty==='nid'
        ?'Число: &gt;5, &lt;3, &gt;=10, =0; или «пусто» / «не пусто»':'Текст содержит…; или «пусто» / «не пусто»')+'"></th>';
    });
    h+='<th></th></tr>';
    t.tHead.innerHTML=h;
  }
  t.tHead.querySelectorAll('tr.ttl th[data-k]').forEach(function(th){
    var k=th.dataset.k, ar=th.querySelector('.ar');
    var on=SL.sort&&SL.sort.k===k;
    ar.classList.toggle('asc', !!on && SL.sort.dir>0); ar.classList.toggle('desc', !!on && SL.sort.dir<0); });
  t.tHead.querySelectorAll('tr.flt [data-f]').forEach(function(el){
    var v=SL.colf[el.dataset.f]||''; if(el.value!==v) el.value=v; el.classList.toggle('on', !!v); });
}
function slSortBy(k){
  var ci=SL_COLS.findIndex(function(c){return c.k===k;}); if(ci<0) return;
  var dir=(SL.sort&&SL.sort.k===k)?-SL.sort.dir:1;
  SL.sort={k:k, dir:dir};
  var num=['nid','num','calc','order'].indexOf(SL_COLS[ci].ty)>=0;
  SL.rows.sort(function(a,b){
    var x=slCellText(a,ci), y=slCellText(b,ci);
    if(num){ x=slNum(x); y=slNum(y); }
    var ex=(x==null||x===''), ey=(y==null||y==='');
    if(ex&&ey) return (a.n||0)-(b.n||0); if(ex) return 1; if(ey) return -1;   // пустые — вниз
    var r=num?(x-y):(x+'').localeCompare(y+'','ru');
    return r? r*dir : (a.n||0)-(b.n||0);
  });
  slSelClear(); slRender(); slSave();
}
function slResetOrder(){
  SL.colf={}; SL.sort=null; SL.filter='';
  var s=document.getElementById('slSearch'); if(s) s.value='';
  // пустые строки — в конец, остальные по №, затем № заново от 1
  SL.rows.sort(function(a,b){ return (a.n||1e9)-(b.n||1e9); });
  SL.rows.forEach(function(r,i){ r.n=i+1; });
  slSelClear(); slRender(); slSave();
}
document.addEventListener('click', function(e){
  var th=e.target.closest && e.target.closest('#slTable tr.ttl th[data-k]');
  if(!th) return;
  if(e.target.closest('.ar')){ slSortBy(th.dataset.k); return; }
  slSelCol(SL_COLS.findIndex(function(c){return c.k===th.dataset.k;}), e.shiftKey);
});
/* клик по заголовку — выделить весь столбец (видимые строки), как в Excel */
function slSelCol(c, extend){
  var n=document.querySelectorAll('#slTable tbody tr').length; if(c<0 || !n) return;
  if(document.activeElement && document.activeElement.blur) document.activeElement.blur();
  var a=(extend && SL.sel && SL.sel.col)?SL.sel.a:{r:0,c:c};
  SL.sel={a:a, b:{r:n-1,c:c}, col:true}; slSelPaint();
}
document.addEventListener('input', function(e){
  var el=e.target; if(!el.closest || !el.closest('#slTable tr.flt') || !el.dataset.f) return;
  SL.colf[el.dataset.f]=el.value.trim(); el.classList.toggle('on', !!el.value.trim());
  slSelClear(); slRender();
});
document.addEventListener('change', function(e){
  var el=e.target; if(!el.closest || !el.closest('#slTable tr.flt') || el.tagName!=='SELECT') return;
  SL.colf[el.dataset.f]=el.value; slSelClear(); slRender();
});
function slRender(){
  var t=document.getElementById('slTable');
  slEnsureN(); slHead();
  var b='';
  SL.rows.forEach(function(r,i){ if(slVisible(i)) b+=slRowHtml(r,i); });
  t.tBodies[0].innerHTML=b;
  if(SL.sel) slSelPaint();
  slCurPaint(); slStCount();
}
/* текущая строка (где щёлкнули/стоит курсор) подсвечивается голубым — только для глаз,
   на копирование не влияет */
function slCurPaint(){
  document.querySelectorAll('#slTable tr.cur').forEach(function(tr){ tr.classList.remove('cur'); });
  var i=SL.cur?SL.rows.indexOf(SL.cur):-1; if(i<0) return;
  var tr=document.querySelector('#slTable tbody tr[data-i="'+i+'"]'); if(tr) tr.classList.add('cur');
}
function slSetCur(el){
  var tr=el && el.closest && el.closest('#slTable tbody tr[data-i]'); if(!tr) return;
  var r=SL.rows[+tr.dataset.i]; if(r && r!==SL.cur){ SL.cur=r; slCurPaint(); }
}
document.addEventListener('focusin', function(e){
  slSetCur(e.target);
  // переход стрелками/Enter — выделение переезжает вместе с курсором
  var td=e.target.closest && e.target.closest('#slTable tbody td');
  if(td && !SL.drag && SL.sel && !slSelMulti()){ var pos=slCellPos(td); SL.sel={a:pos, b:pos}; slSelPaint(); }
});
function slRowHtml(r,i){
  var h='<tr data-i="'+i+'">';
  SL_COLS.forEach(function(c,ci){ h+=slCellHtml(r,i,c,ci); });
  return h+'<td class="del"><button title="Удалить строку" onclick="slDelRow('+i+')">✕</button></td></tr>';
}
function slCellHtml(r,i,c,ci){
  if(c.ty==='nid') return '<td class="nid">'+(r.n||'')+'</td>';
  if(c.ty==='name'){
    var n=slName(r);
    if(n) return '<td class="nm">'+slEsc(n)+'</td>';
    return '<td class="nm miss">'+(r.art?'нет в справочнике':'')+'</td>';
  }
  if(c.k==='total') return '<td class="calc">'+slFmt(slTotal(r))+'</td>';
  if(c.ty==='status'){
    var st=slStatus(r); if(!st) return '<td class="st"></td>';
    var tipS=st==='Скорректировано'?'Ваша причина: '+(r.order_reason||'—')
      :st==='Одобрено'?'Расчёт ИИ подтверждён без правок':'Количество посчитано ИИ — проверьте и подтвердите ✓ или исправьте ✎';
    return '<td class="st" title="'+slEsc(tipS)+'"><span class="chip '+SL_ST_CLS[st]+'">'+st+'</span>'
      +'<button class="stb'+(st==='Одобрено'?' on':'')+'" onclick="slApprove('+i+')" title="'
      +(st==='Одобрено'?'Снять одобрение':st==='Скорректировано'?'Отменить правку и одобрить расчёт ИИ':'Количество верное — одобрить')+'">✓</button>'
      +'<button class="stb'+(st==='Скорректировано'?' on':'')+'" onclick="slEditReason('+i+')" title="'
      +(st==='Скорректировано'?'Изменить количество / причину':'Исправить количество с причиной')+'">✎</button></td>';
  }
  if(c.k==='order'){
    var o=slOrder(r), man=r.order_manual!=null;
    var tip=man?('Исправлено вручную. Расчёт: '+(o.any?o.q:'—')+'\nОчистите ячейку, чтобы вернуть расчёт.\n\n'+o.why):o.why;
    var good=man||slStatus(r)==='Одобрено';   // проверено человеком — зелёным
    return '<td class="order'+(man?' manual':'')+(good?' good':'')+'" title="'+slEsc(tip)+'"><input data-i="'+i+'" data-c="'+ci
      +'" value="'+slEsc(slFmt(slOrderVal(r)))+'"></td>';
  }
  var v=r[c.k]; v=(c.ty==='num')?slFmt(v):(v||'');
  return '<td class="'+c.ty+'"><input data-i="'+i+'" data-c="'+ci+'" value="'+slEsc(v)+'"></td>';
}
function slRefreshRow(i, noCount){
  var tr=document.querySelector('#slTable tr[data-i="'+i+'"]'); if(!tr) return;
  var r=SL.rows[i];
  SL_COLS.forEach(function(c,ci){
    if(c.ty==='order' && document.activeElement && tr.children[ci].contains(document.activeElement)) return;
    if(c.ty==='name'||c.ty==='calc'||c.ty==='order'||c.ty==='status'){
      var tmp=document.createElement('tbody');
      tmp.innerHTML='<tr>'+slCellHtml(r,i,c,ci)+'</tr>';
      tr.replaceChild(tmp.firstChild.firstChild, tr.children[ci]);
    }
  });
  if(!noCount) slStCount();
}
function slRefreshCalc(){ SL.rows.forEach(function(r,i){ slRefreshRow(i, true); }); slStCount(); }

function slSetCell(i,ci,raw){
  var c=SL_COLS[ci], r=SL.rows[i];
  if(!c||c.ty==='name'||c.ty==='calc') return false;
  if(c.ty==='status'||c.ty==='nid') return false;
  if(c.ty==='order'){                       // массово — только очистка (правка требует причину)
    if(slNum(raw)==null && r.order_manual!=null) slClearManual(i);
    return false;
  }
  if(c.ty==='art') r[c.k]=slArt(raw);
  else if(c.ty==='num') r[c.k]=slNum(raw);
  else r[c.k]=(raw||'').trim();
  return c.ty==='art';
}
async function slLookup(arts){
  arts=arts.filter(function(a){return a && !(a in SL.names);});
  if(!arts.length) return;
  try{
    var d=await api('/api/sales_names',{arts:arts});
    Object.keys(d.names||{}).forEach(function(a){ SL.names[a]=d.names[a]; });
  }catch(e){}
}
async function slSaveNow(){
  try{
    var rows=SL.rows.filter(function(r){
      return Object.keys(r).some(function(k){return k!=='n'&&r[k]!=null&&r[k]!=='';}); });
    var d=await api('/api/sales_save',{id:SL.id, rows:rows, settings:SL.settings, period:SL.period});
    if(d.ok){
      SL.id=d.id||null;
      if(d.created){ var l=await apiGet('/api/sales?id='+encodeURIComponent(SL.id)); slSnapList(l.snapshots); }
    }
    document.getElementById('slSaved').textContent=d.ok?('✓ Сохранено ('+d.count+')'):(d.error||'Ошибка');
  }catch(e){ document.getElementById('slSaved').textContent='⚠️ Не сохранилось'; }
}
function slSave(){
  clearTimeout(SL.saveTimer);
  document.getElementById('slSaved').textContent='Сохраняю…';
  SL.saveTimer=setTimeout(function(){ SL.saveTimer=null; slSaveNow(); }, 700);
}

/* ввод в ячейку */
document.addEventListener('change', async function(e){
  var el=e.target; if(!el.closest||!el.closest('#slTable')||el.dataset.c==null) return;
  var i=+el.dataset.i, ci=+el.dataset.c;
  if(SL_COLS[ci].ty==='order'){ slOrderEdited(i, el); return; }
  var isArt=slSetCell(i,ci,el.value);
  var c=SL_COLS[ci], v=SL.rows[i][c.k];
  el.value=(c.ty==='num')?slFmt(v):(v||'');
  if(isArt) await slLookup([SL.rows[i].art, SL.rows[i].repl]);
  slRefreshRow(i); slSave();
});
/* маска артикула прямо при наборе */
document.addEventListener('input', function(e){
  var el=e.target; if(!el.closest||!el.closest('#slTable')||el.dataset.c==null) return;
  if(SL_COLS[+el.dataset.c].ty==='art'){
    var p=el.selectionStart, v=slArt(el.value);
    if(v!==el.value){ el.value=v; try{el.setSelectionRange(p-1,p-1);}catch(_){} }
  }
});
/* вставка блока из Excel: столбцы ложатся по порядку колонок таблицы */
document.addEventListener('paste', async function(e){
  var el=e.target, txt=(e.clipboardData||window.clipboardData).getData('text');
  if(el.closest && el.closest('#slTable') && el.dataset.c!=null){
    if(txt.indexOf('\t')<0 && txt.indexOf('\n')<0) return;
    e.preventDefault(); slSelClear(); await slPasteAt(+el.dataset.i, +el.dataset.c, txt);
  } else if(SL.sel && slSecVisible() && !slInHead()){
    e.preventDefault();
    var b=slSelBox(), tr=document.querySelectorAll('#slTable tbody tr')[b.r1];
    if(tr) await slPasteAt(+tr.dataset.i, b.c1, txt);
  }
});
/* ---------- правка «В заказ» с причиной ---------- */
function slOrderEdited(i, el){
  var r=SL.rows[i], v=slNum(el.value), o=slOrder(r);
  el.blur();
  if(v==null){ if(r.order_manual!=null){ slClearManual(i); slSave(); } slRefreshRow(i); return; }
  if(r.order_manual==null && o.any && v===o.q){ slRefreshRow(i); return; }      // совпало с расчётом
  if(r.order_manual!=null && v===r.order_manual){ slRefreshRow(i); return; }    // не изменилось
  if(!r.art){ alert('Сначала укажите артикул в этой строке.'); slRefreshRow(i); return; }
  slAsk(i, v);
}
function slAsk(i, qty){
  var r=SL.rows[i], o=slOrder(r);
  SL.dlg={i:i};
  document.getElementById('slDlgHd').textContent=r.order_manual!=null?'Изменить причину':'Объясни почему?';
  document.getElementById('slDlgSub').textContent=(r.art||'')+(slName(r)?' · '+slName(r):'')
    +' — расчёт: '+(o.any?o.q:'—')+' шт.';
  document.getElementById('slDlgQty').value=slFmt(qty);
  document.getElementById('slDlgText').value=r.order_reason||'';
  document.getElementById('slDlgErr').textContent='';
  document.getElementById('slDlgReset').style.display=r.order_manual!=null?'':'none';
  document.getElementById('slDlg').style.display='flex';
  setTimeout(function(){ document.getElementById('slDlgText').focus(); },30);
}
function slEditReason(i){ var r=SL.rows[i], o=slOrder(r);
  if(!r.art){ alert('Сначала укажите артикул в этой строке.'); return; }
  slAsk(i, r.order_manual!=null?r.order_manual:(o.any?o.q:null)); }
/* статус строки: правка с причиной → «Скорректировано»; ✓ на текущем расчёте → «Одобрено»;
   иначе расчёт ИИ «Требует проверки» (одобрение слетает, если расчёт поменялся) */
var SL_ST=['Требует проверки','Одобрено','Скорректировано'];
var SL_ST_CLS={'Требует проверки':'done','Одобрено':'ok','Скорректировано':'fixed'};
function slStCount(){
  var c={}; SL.rows.forEach(function(r){ var s=slStatus(r); if(s) c[s]=(c[s]||0)+1; });
  var el=document.getElementById('slStCnt'); if(!el) return;
  el.innerHTML=SL_ST.filter(function(s){return c[s];}).map(function(s){
    return '<span class="chip '+SL_ST_CLS[s]+'">'+s+': '+c[s]+'</span>'; }).join(' ');
}
function slStatus(r){
  if(r.order_manual!=null) return 'Скорректировано';
  var o=slOrder(r); if(!o.any) return '';
  return (r.order_ok!=null && r.order_ok===o.q)?'Одобрено':'Требует проверки';
}
function slLessonRow(r){ var row={};
  ['profit','turn_de','sold_de','sold_sup','sold_stock','stock','ordered','transit','year']
    .forEach(function(k){ row[k]=r[k]; }); return row; }
async function slApprove(i){
  var r=SL.rows[i], o=slOrder(r);
  if(!r.art){ alert('Сначала укажите артикул в этой строке.'); return; }
  if(slStatus(r)==='Одобрено'){            // повторный клик — снять одобрение
    r.order_ok=null; slRefreshRow(i); slSave();
    if(SL.id) api('/api/sales_lesson_del',{snap:SL.id, art:r.art})
      .then(function(res){ if(res&&res.ok) slLessonsCount(res.lessons_count); }).catch(function(){});
    return;
  }
  if(!o.any) return;
  r.order_manual=null; r.order_reason=''; r.order_ok=o.q; slRefreshRow(i);
  clearTimeout(SL.saveTimer); SL.saveTimer=null; await slSaveNow();   // нужен id слепка
  try{
    var p=SL.period; var res=await api('/api/sales_lesson',{snap:SL.id, art:r.art, repl:r.repl, name:slName(r),
      approved:true, calc:o.q, manual:o.q, reason:'', cover:SL.settings.cover_days,
      period:{from:p.from||'', to:p.to||'', days:slPeriodDays()}, row:slLessonRow(r)});
    if(res.ok) slLessonsCount(res.lessons_count);
  }catch(e){}
}
function slDlgClose(){ document.getElementById('slDlg').style.display='none'; var d=SL.dlg; SL.dlg=null; if(d) slRefreshRow(d.i); }
function slDlgCancel(){ slDlgClose(); }
function slDlgReset(){ var d=SL.dlg; if(!d) return; slClearManual(d.i); slDlgClose(); slSave(); }
async function slDlgOk(){
  var d=SL.dlg; if(!d) return;
  var reason=document.getElementById('slDlgText').value.trim(),
      qty=slNum(document.getElementById('slDlgQty').value), err=document.getElementById('slDlgErr');
  if(qty==null){ err.textContent='Укажите количество.'; return; }
  if(!reason){ err.textContent='Причина обязательна — напишите, почему именно столько.'; return; }
  var r=SL.rows[d.i], o=slOrder(r);
  r.order_manual=qty; r.order_reason=reason; r.order_ok=null;
  slDlgClose();
  clearTimeout(SL.saveTimer); SL.saveTimer=null; await slSaveNow();   // нужен id слепка
  try{
    var row=slLessonRow(r);
    var p=SL.period; var res=await api('/api/sales_lesson',{snap:SL.id, art:r.art, repl:r.repl, name:slName(r),
      calc:o.any?o.q:null, manual:qty, reason:reason, cover:SL.settings.cover_days,
      period:{from:p.from||'', to:p.to||'', days:slPeriodDays()}, row:row});
    if(res.ok) slLessonsCount(res.lessons_count);
  }catch(e){}
}
function slClearManual(i){
  var r=SL.rows[i]; r.order_manual=null; r.order_reason=''; r.order_ok=null;
  if(r.art && SL.id) api('/api/sales_lesson_del',{snap:SL.id, art:r.art})
    .then(function(res){ if(res&&res.ok) slLessonsCount(res.lessons_count); }).catch(function(){});
}
document.addEventListener('keydown', function(e){
  if(!SL.dlg) return;
  if(e.key==='Escape'){ e.preventDefault(); slDlgCancel(); }
  else if(e.key==='Enter' && !e.shiftKey){ e.preventDefault(); slDlgOk(); }
}, true);

/* колонки, по которым раскладывается вставка из Excel (без служебной ✓/✎) */
var SL_PASTE=SL_COLS.map(function(c,ci){return ci;}).filter(function(ci){return SL_COLS[ci].ty!=='status'&&SL_COLS[ci].ty!=='nid';});
async function slPasteAt(i0, c0, txt){
  var lines=txt.replace(/\r/g,'').split('\n');
  if(lines.length && lines[lines.length-1]==='') lines.pop();
  var head='', sk=document.getElementById('slSkipHead');
  if(sk && sk.checked && lines.length>1){ head=lines.shift().replace(/\t/g,' | ').trim(); }
  var arts=[];
  lines.forEach(function(line,li){
    var i=i0+li; while(SL.rows.length<=i) SL.rows.push({});
    var cells=line.split('\t'), p0=SL_PASTE.indexOf(c0);
    if(p0<0) p0=SL_PASTE.indexOf(c0+1);
    cells.forEach(function(val,vi){
      var ci=SL_PASTE[p0+vi];
      if(ci==null || SL_COLS[ci].ty==='order') return;   // «В заказ» правится только с причиной
      slSetCell(i,ci,val); });
    arts.push(SL.rows[i].art, SL.rows[i].repl);
  });
  await slLookup(arts);
  slRender(); slSave();
  if(head) setTimeout(function(){
    document.getElementById('slSaved').textContent='Вставлено строк: '+lines.length
      +' · убран заголовок «'+head+'»'; }, 1500);
}

/* ---------- выделение блока ячеек, как в Excel ----------
   протянуть мышкой или Shift+клик; Ctrl+C — копия, Delete — очистка,
   Ctrl+V — вставка с левого верхнего угла; Esc — снять выделение */
SL.sel=null; SL.drag=null;
function slInHead(){ var a=document.activeElement; return !!(a&&a.closest&&a.closest('#slTable thead')); }
function slSecVisible(){ var s=document.getElementById('sec-sales'); return s && s.style.display!=='none'; }
function slCellPos(td){
  var tr=td.parentNode, rows=Array.prototype.indexOf.call(tr.parentNode.children, tr);
  return {r:rows, c:Array.prototype.indexOf.call(tr.children, td)};
}
function slSelBox(){ var a=SL.sel.a, b=SL.sel.b;
  return {r1:Math.min(a.r,b.r), r2:Math.max(a.r,b.r), c1:Math.min(a.c,b.c), c2:Math.min(Math.max(a.c,b.c), SL_COLS.length-1)}; }
function slSelPaint(){
  document.querySelectorAll('#slTable td.sel').forEach(function(td){ td.classList.remove('sel'); td.style.boxShadow=''; });
  document.querySelectorAll('#slTable th.csel').forEach(function(th){ th.classList.remove('csel'); });
  if(!SL.sel) return;
  if(SL.sel.col){ var hb=slSelBox(), ths=document.querySelectorAll('#slTable tr.ttl th');
    for(var hc=hb.c1;hc<=hb.c2;hc++) if(ths[hc]) ths[hc].classList.add('csel'); }
  var b=slSelBox(), trs=document.querySelectorAll('#slTable tbody tr');
  for(var r=b.r1;r<=b.r2;r++){ var tr=trs[r]; if(!tr) continue;
    for(var c=b.c1;c<=b.c2;c++){ var td=tr.children[c]; if(!td) continue;
      td.classList.add('sel');
      var sh=[], L='#4a86e8';                 // одна рамка вокруг всего блока
      if(r===b.r1) sh.push('inset 0 2px 0 '+L); if(r===b.r2) sh.push('inset 0 -2px 0 '+L);
      if(c===b.c1) sh.push('inset 2px 0 0 '+L); if(c===b.c2) sh.push('inset -2px 0 0 '+L);
      td.style.boxShadow=sh.join(','); } }
}
function slSelClear(){ if(SL.sel){ SL.sel=null; slSelPaint(); } }
function slSelMulti(){ if(!SL.sel) return false; var b=slSelBox(); return b.r1!==b.r2||b.c1!==b.c2; }
document.addEventListener('mousedown', function(e){
  var td=e.target.closest && e.target.closest('#slTable tbody td');
  if(!td || td.classList.contains('del')){
    if(!(e.target.closest&&e.target.closest('#slTable tbody, #slTable tr.ttl'))) slSelClear(); return; }
  var pos=slCellPos(td); slSetCur(td);
  if(e.shiftKey){
    var a=SL.sel?SL.sel.a:null;
    if(!a){ var f=document.activeElement; var ftd=f&&f.closest&&f.closest('#slTable tbody td'); if(ftd) a=slCellPos(ftd); }
    if(a){ e.preventDefault(); if(document.activeElement) document.activeElement.blur();
      SL.sel={a:a, b:pos}; slSelPaint(); return; }
  }
  SL.drag=pos; SL.sel={a:pos, b:pos}; slSelPaint();
});
document.addEventListener('mouseover', function(e){
  if(!SL.drag || !(e.buttons&1)) return;
  var td=e.target.closest && e.target.closest('#slTable tbody td');
  if(!td || td.classList.contains('del')) return;
  var pos=slCellPos(td);
  if(pos.r===SL.sel.b.r && pos.c===SL.sel.b.c) return;
  SL.sel.b=pos;
  if(slSelMulti()){ if(document.activeElement) document.activeElement.blur();
    if(window.getSelection) window.getSelection().removeAllRanges(); }
  slSelPaint();
});
document.addEventListener('mouseup', function(){ SL.drag=null; });
function slCellText(r,c){
  var col=SL_COLS[c];
  if(col.k==='name') return slName(r);
  if(col.k==='total') return slFmt(slTotal(r));
  if(col.k==='order') return slFmt(slOrderVal(r));
  if(col.ty==='status') return slStatus(r);
  if(col.ty==='nid') return r.n||'';
  return col.ty==='num'?slFmt(r[col.k]):(r[col.k]||'');
}
function slSelText(){
  var b=slSelBox(), trs=document.querySelectorAll('#slTable tbody tr'), out=[];
  for(var r=b.r1;r<=b.r2;r++){ var tr=trs[r]; if(!tr) continue; var row=SL.rows[+tr.dataset.i], line=[];
    for(var c=b.c1;c<=b.c2;c++) line.push((slCellText(row,c)+'').replace(/[\t\n]/g,' '));
    out.push(line.join('\t')); }
  return {text:out.join('\r\n'), n:(b.r2-b.r1+1)*(b.c2-b.c1+1)};
}
/* копировать ли блок: выделено несколько ячеек, или одна, но в ней не выделен кусок текста */
function slCopyWanted(){
  if(!SL.sel || SL.dlg || !slSecVisible() || slInHead() || SL.copying) return false;
  if(slSelMulti()) return true;
  var ae=document.activeElement;
  if(ae && ae.tagName==='INPUT' && ae.selectionStart!==ae.selectionEnd) return false;  // обычная копия куска текста
  return true;
}
function slCopyDone(n){ document.getElementById('slSaved').textContent='Скопировано ячеек: '+n; }
document.addEventListener('copy', function(e){
  if(!slCopyWanted()) return;
  var t=slSelText(); e.clipboardData.setData('text/plain', t.text); e.preventDefault(); slCopyDone(t.n);
});
/* Ctrl+C ловим сами: браузер не всегда шлёт событие copy, когда фокуса нет
   (после выделения мышкой) — тогда копируем через скрытое поле */
document.addEventListener('keydown', function(e){
  if(!(e.ctrlKey||e.metaKey) || e.altKey || e.code!=='KeyC' || !slCopyWanted()) return;
  e.preventDefault();
  var t=slSelText(), ae=document.activeElement, ok=false;
  var ta=document.createElement('textarea'); ta.value=t.text;
  ta.style.cssText='position:fixed;left:-9999px;top:0;opacity:0';
  document.body.appendChild(ta); ta.select();
  SL.copying=true; try{ ok=document.execCommand('copy'); }catch(err){} SL.copying=false;
  document.body.removeChild(ta);
  if(ae && ae.focus && ae!==document.body) ae.focus({preventScroll:true});
  if(!ok && navigator.clipboard) navigator.clipboard.writeText(t.text).then(function(){ slCopyDone(t.n); });
  else if(ok) slCopyDone(t.n);
});
document.addEventListener('keydown', function(e){
  if(!SL.sel || SL.dlg || !slSecVisible() || slInHead()) return;
  var ae=document.activeElement, inInput=ae && ae.tagName==='INPUT';
  if(e.key==='Escape'){ slSelClear(); return; }
  if((e.key==='Delete'||e.key==='Backspace') && (slSelMulti() || !inInput)){
    e.preventDefault(); if(inInput) ae.blur();
    var b=slSelBox(), trs=document.querySelectorAll('#slTable tbody tr');
    for(var r=b.r1;r<=b.r2;r++){ var tr=trs[r]; if(!tr) continue;
      for(var c=b.c1;c<=b.c2;c++) slSetCell(+tr.dataset.i, c, ''); }
    slRender(); slSelPaint(); slSave();
  }
});
(function(){ var sk=document.getElementById('slSkipHead'); if(!sk) return;
  try{ if(localStorage.getItem('slSkipHead')==='0') sk.checked=false; }catch(e){}
  sk.addEventListener('change', function(){
    try{ localStorage.setItem('slSkipHead', sk.checked?'1':'0'); }catch(e){} }); })();
/* стрелки вверх/вниз и Enter — по строкам */
document.addEventListener('keydown', function(e){
  var el=e.target; if(!el.closest||!el.closest('#slTable')||el.dataset.c==null) return;
  var d=(e.key==='ArrowDown'||e.key==='Enter')?1:(e.key==='ArrowUp'?-1:0); if(!d) return;
  e.preventDefault(); el.blur();
  var tr=el.closest('tr'), sib=d>0?tr.nextElementSibling:tr.previousElementSibling;
  if(!sib && d>0){ slAddRow(); return; }
  var nx=sib&&sib.querySelector('input[data-c="'+el.dataset.c+'"]'); if(nx){nx.focus();nx.select();}
});

function slAddRow(){
  SL.rows.push({}); slRender();
  var tr=document.querySelector('#slTable tr[data-i="'+(SL.rows.length-1)+'"]');
  if(tr){ var inp=tr.querySelector('input'); if(inp) inp.focus(); }
}
function slDelRow(i){
  var r=SL.rows[i];
  if((r.art||r.comment) && !confirm('Удалить строку '+(r.art||'')+'?')) return;
  SL.rows.splice(i,1); slRender(); slSave();
}
function slToggleCat(){ var p=document.getElementById('slCat');
  p.style.display=p.style.display==='none'?'':'none'; }
async function slCatAdd(){
  var ta=document.getElementById('slCatText'), msg=document.getElementById('slCatMsg');
  if(!ta.value.trim()){ msg.textContent='Вставьте строки: артикул и наименование.'; return; }
  msg.textContent='Загружаю…';
  try{
    var d=await api('/api/sales_catalog_add',{text:ta.value});
    if(d.error){ msg.textContent=d.error; return; }
    msg.textContent='Добавлено '+d.added+', обновлено '+d.updated
      +(d.skipped?', пропущено '+d.skipped:'')+'. Всего в справочнике: '+d.catalog_size+'.';
    ta.value=''; slCatCount(d.catalog_size);
    SL.names={}; await slLookup([].concat.apply([],SL.rows.map(function(r){return [r.art,r.repl];})));
    slRender();
  }catch(e){ msg.textContent='Не удалось загрузить.'; }
}
async function slCatUpload(inp){
  var f=inp.files&&inp.files[0]; if(!f) return; inp.value='';
  var st=document.getElementById('slSaved'); st.textContent='Загружаю справочник…';
  try{
    var fd=new FormData(); fd.append('file',f);
    var res=await fetch('/api/sales_catalog_upload',{method:'POST',headers:{'X-Auth':AUTH},body:fd});
    if(res.status===401){ st.textContent='Нужен пароль'; return; }
    var d=await res.json();
    if(d.error){ st.textContent=''; alert('Ошибка: '+d.error); return; }
    slCatCount(d.catalog_size); st.textContent='';
    alert('Справочник загружен: добавлено '+d.added+', обновлено '+d.updated
      +(d.skipped?', пропущено пустых '+d.skipped:'')+'. Всего позиций: '+d.catalog_size+'.');
    SL.names={}; await slLookup([].concat.apply([],SL.rows.map(function(r){return [r.art,r.repl];})));
    slRender();
  }catch(e){ st.textContent='⚠️ Не удалось загрузить справочник'; }
}
function slExport(){
  var rows=[SL_COLS.map(function(c){return c.t;}).concat(['Причина правки'])];
  SL.rows.forEach(function(r){
    if(!r.art && !r.comment) return;
    rows.push(SL_COLS.map(function(c,ci){ return slCellText(r,ci); }).concat([r.order_reason||'']));
  });
  var csv=rows.map(function(row){return row.map(function(v){
    v=(v==null?'':v+''); return /[;"\n]/.test(v)?'"'+v.replace(/"/g,'""')+'"':v;}).join(';');}).join('\r\n');
  var a=document.createElement('a');
  a.href=URL.createObjectURL(new Blob(['﻿'+csv],{type:'text/csv;charset=utf-8'}));
  a.download='analiz_prodazh.csv'; a.click();
}
(function(){ var el=document.getElementById('slCover'); if(!el) return;
  el.addEventListener('change', function(){
    var v=slNum(el.value); if(!(v>0)){ el.value=SL.settings.cover_days; return; }
    SL.settings.cover_days=v; slRefreshCalc(); slSave();
  }); })();
['slFrom','slTo'].forEach(function(id){
  var el=document.getElementById(id); if(!el) return;
  // Chrome шлёт change на каждую цифру года (2 → 0002, 20 → 0020…), поэтому
  // недописанный год пропускаем, а не сохраняем; и никаких alert — он сбивал ввод
  el.addEventListener('change', function(){
    var v=el.value||'';
    if(v && !/^(20\d\d)-\d\d-\d\d$/.test(v)) return;
    SL.period[id==='slFrom'?'from':'to']=v;
    slShowDays(); slRefreshCalc(); slSave();
  });
  el.addEventListener('blur', function(){   // ушли из поля с недописанным годом — вернуть сохранённое
    var k=id==='slFrom'?'from':'to';
    if((el.value||'')!==(SL.period[k]||'')) el.value=SL.period[k]||'';
  });
});
(function(){ var s=document.getElementById('slSearch'); if(!s) return;
  s.addEventListener('input', function(){ SL.filter=s.value.trim().toUpperCase();
    slRender(); }); })();

/* ядро вызовет это при открытии раздела */
window.onShow_sales = slLoad;
"""

    return {
        "id": "sales",
        "title": "📈 Анализ продаж",
        "section_html": SECTION_HTML,
        "css": CSS,
        "js": JS,
    }
