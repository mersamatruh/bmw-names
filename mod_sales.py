"""
Подпроект «Анализ продаж» (анализатор продаж Porsche) — модуль для ядра.

Пробная версия: онлайн-таблица позиций (артикулы, продажи по каналам,
остатки, заказано/в пути, продажи за год), свой справочник
«артикул → наименование» и расчёт колонки «в заказ» по простой формуле
(дальше её заменит/дополнит ИИ по правилам владельца).

Данные на сервере (ctx["data_dir"]):
  sales.json          — {settings: {period_days, cover_days}, current: id слепка}
  sales_snapshots/    — слепки таблицы <ГГГГ-ММ-ДД_чч-мм-сс>.json {id, rows}
                        (как слепки в «Названиях»: открытый слепок
                        автосохраняется, «Новая партия» начинает новый)
  sales_catalog.json  — {артикул: наименование}
"""

import os
import re
import json
import datetime

# Колонки, которые хранятся в строке (наименование, «общие продажи»
# и «в заказ» вычисляются и не хранятся).
TEXT_FIELDS = ("art", "repl", "comment")
NUM_FIELDS = ("profit", "turn_de", "sold_de", "sold_sup", "sold_stock",
              "stock", "ordered", "transit", "year",
              "order_manual")  # «в заказ», исправленное вручную

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
                out.append({"id": fn[:-5],
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
        cat = load_json(CATALOG_PATH, {})
        arts = set()
        for r in rows:
            arts.add(r.get("art"))
            arts.add(r.get("repl"))
        names = {a: cat[a] for a in arts if a and a in cat}
        return jsonify({"settings": data["settings"], "rows": rows,
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
        sid = d.get("id") or None
        created = False
        with lock:
            data = load_sales()
            data["settings"] = settings
            if sid and snap_path(sid) and os.path.exists(snap_path(sid)):
                save_json(snap_path(sid), {"id": sid, "rows": rows})
            elif rows:                    # первая запись новой партии
                sid, created = new_snap_id(), True
                save_json(snap_path(sid), {"id": sid, "rows": rows})
            else:
                sid = None
            data["current"] = sid
            save_json(SALES_PATH, data)
        return jsonify({"ok": True, "count": len(rows), "id": sid,
                        "created": created})

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
    Вносите позиции или вставляйте столбцы из Excel вместе с заголовком (Ctrl+V в ячейку первой строки) — заголовок отбросится. Блок ячеек выделяется мышкой или Shift+клик: Ctrl+C — копия, Delete — очистка. Всё
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
    <label class="hint">Период продаж, дней
      <input id="slPeriod" class="slnum" type="number" min="1"></label>
    <label class="hint">Заказывать с запасом на, дней
      <input id="slCover" class="slnum" type="number" min="1"></label>
    <input id="slSearch" placeholder="Поиск по артикулу или названию">
    <button onclick="slAddRow()">+ Строка</button>
    <button onclick="slExport()">⬇️ Скачать CSV</button>
    <button onclick="document.getElementById('slCatXls').click()"
            title="Excel-файл: столбец A — артикул, столбец B — наименование">📥 Загрузить справочник (Excel)</button>
    <input type="file" id="slCatXls" accept=".xlsx,.xls,.xlsm,.csv,.txt,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/vnd.ms-excel" style="display:none"
           onchange="slCatUpload(this)">
    <button onclick="slToggleCat()">📚 Справочник <span id="slCatCnt" class="hint"></span></button>
    <label class="hint" title="При вставке из Excel первая строка (название столбца) отбрасывается">
      <input type="checkbox" id="slSkipHead" checked> Убирать заголовок при вставке</label>
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
  <div id="slWrap"><table id="slTable"><thead></thead><tbody></tbody></table></div>
  <p class="hint" style="margin-top:10px">«В заказ» считается формулой: средний
    дневной спрос (60% — текущий период, 40% — последний год) × дней запаса − наличие
    − уже заказано. Наведите на число, чтобы увидеть расчёт. Число можно исправить
    вручную (оно станет жирным); очистите ячейку — вернётся расчёт.</p>
"""

    CSS = """
  #sec-sales{max-width:none;margin:18px 0}
  #slWrap{overflow:auto;max-height:calc(100vh - 230px);border:1px solid var(--line);
          border-radius:8px}
  #slTable{border:none;border-radius:0;min-width:1500px}
  #slTable th{position:sticky;top:0;background:var(--panel);z-index:1;
              white-space:normal;vertical-align:bottom;font-size:11px;line-height:1.25}
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
  #slTable td.sel{background:#cfe0fb !important;box-shadow:inset 0 0 0 1px #8fb3ef}
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
  {k:'comment',    t:'Комментарий',                 ty:'text'},
  {k:'stock',      t:'Наше наличие',                ty:'num'},
  {k:'ordered',    t:'Кол-во заказано',             ty:'num'},
  {k:'transit',    t:'В пути',                      ty:'num'},
  {k:'year',       t:'Продажи за год (365 дн.)',    ty:'num'}
];
var SL = {rows:[], names:{}, settings:{period_days:90, cover_days:60}, loaded:false, id:null,
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
function slOrder(r){
  var P=SL.settings.period_days||90, C=SL.settings.cover_days||60;
  var tot=slTotal(r)||0, yr=r.year||0;
  var rp=tot/P, ry=yr/365, rate;
  if(tot>0&&yr>0) rate=0.6*rp+0.4*ry; else rate=Math.max(rp,ry);
  var need=rate*C, have=(r.stock||0)+(r.ordered||0);
  var q=Math.max(0, Math.ceil(need-have-1e-9));
  var why='Спрос в день: '+slFmt(rp)+' (период) / '+slFmt(ry)+' (год) → '+slFmt(rate)
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
    return '<option value="'+slEsc(x.id)+'">'+slEsc(x.id)+' · '+x.total+' поз.</option>';}).join('');
  if(SL.id) s.value=SL.id;
  document.getElementById('slCurSnap').textContent=SL.id?('Открыт слепок '+SL.id):'Новая партия (слепок создастся при первом сохранении)';
}
async function slFetch(q){
  var d=await apiGet('/api/sales'+(q||''));
  if(d.error){ alert(d.error); return false; }
  SL.rows=d.rows||[]; SL.names=d.names||{}; SL.settings=d.settings||SL.settings;
  SL.id=d.id||null; slSnapList(d.snapshots);
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
    document.getElementById('slPeriod').value=SL.settings.period_days;
    document.getElementById('slCover').value=SL.settings.cover_days;
    slCatCount(d.catalog_size);
    slRender();
  }catch(e){ document.getElementById('slSaved').textContent='Не удалось загрузить таблицу.'; }
}
function slCatCount(n){ document.getElementById('slCatCnt').textContent=n?'('+n+')':''; }

function slVisible(i){
  if(!SL.filter) return true;
  var r=SL.rows[i], f=SL.filter;
  if(((r.art||'')+' '+(r.repl||'')+' '+slName(r)).toUpperCase().indexOf(f)>=0) return true;
  var fa=/[А-ЯЁ]/.test(f)?'':slArt(f);
  return !!fa && ((r.art||'')+' '+(r.repl||'')).indexOf(fa)>=0;
}
function slRender(){
  var t=document.getElementById('slTable');
  var h='<tr>'; SL_COLS.forEach(function(c){h+='<th class="'+c.ty+'">'+c.t+'</th>';}); h+='<th></th></tr>';
  t.tHead.innerHTML=h;
  var b='';
  SL.rows.forEach(function(r,i){ if(slVisible(i)) b+=slRowHtml(r,i); });
  t.tBodies[0].innerHTML=b;
  if(SL.sel) slSelPaint();
}
function slRowHtml(r,i){
  var h='<tr data-i="'+i+'">';
  SL_COLS.forEach(function(c,ci){ h+=slCellHtml(r,i,c,ci); });
  return h+'<td class="del"><button title="Удалить строку" onclick="slDelRow('+i+')">✕</button></td></tr>';
}
function slCellHtml(r,i,c,ci){
  if(c.ty==='name'){
    var n=slName(r);
    if(n) return '<td class="nm">'+slEsc(n)+'</td>';
    return '<td class="nm miss">'+(r.art?'нет в справочнике':'')+'</td>';
  }
  if(c.k==='total') return '<td class="calc">'+slFmt(slTotal(r))+'</td>';
  if(c.k==='order'){
    var o=slOrder(r), man=r.order_manual!=null;
    var tip=man?('Исправлено вручную. Расчёт: '+(o.any?o.q:'—')+'\nОчистите ячейку, чтобы вернуть расчёт.\n\n'+o.why):o.why;
    return '<td class="order'+(man?' manual':'')+'" title="'+slEsc(tip)+'"><input data-i="'+i+'" data-c="'+ci
      +'" value="'+slEsc(slFmt(slOrderVal(r)))+'"></td>';
  }
  var v=r[c.k]; v=(c.ty==='num')?slFmt(v):(v||'');
  return '<td class="'+c.ty+'"><input data-i="'+i+'" data-c="'+ci+'" value="'+slEsc(v)+'"></td>';
}
function slRefreshRow(i){
  var tr=document.querySelector('#slTable tr[data-i="'+i+'"]'); if(!tr) return;
  var r=SL.rows[i];
  SL_COLS.forEach(function(c,ci){
    if(c.ty==='order' && document.activeElement && tr.children[ci].contains(document.activeElement)) return;
    if(c.ty==='name'||c.ty==='calc'||c.ty==='order'){
      var tmp=document.createElement('tbody');
      tmp.innerHTML='<tr>'+slCellHtml(r,i,c,ci)+'</tr>';
      tr.replaceChild(tmp.firstChild.firstChild, tr.children[ci]);
    }
  });
}
function slRefreshCalc(){ SL.rows.forEach(function(r,i){ slRefreshRow(i); }); }

function slSetCell(i,ci,raw){
  var c=SL_COLS[ci], r=SL.rows[i];
  if(!c||c.ty==='name'||c.ty==='calc') return false;
  if(c.ty==='order'){ r.order_manual=slNum(raw); return false; }
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
      return Object.keys(r).some(function(k){return r[k]!=null&&r[k]!=='';}); });
    var d=await api('/api/sales_save',{id:SL.id, rows:rows, settings:SL.settings});
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
  var isArt=slSetCell(i,ci,el.value);
  var c=SL_COLS[ci], v=SL.rows[i][c.k];
  if(c.ty==='order'){ el.blur(); slRefreshRow(i); slSave(); return; }
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
  } else if(SL.sel && slSecVisible()){
    e.preventDefault();
    var b=slSelBox(), tr=document.querySelectorAll('#slTable tbody tr')[b.r1];
    if(tr) await slPasteAt(+tr.dataset.i, b.c1, txt);
  }
});
async function slPasteAt(i0, c0, txt){
  var lines=txt.replace(/\r/g,'').split('\n');
  if(lines.length && lines[lines.length-1]==='') lines.pop();
  var head='', sk=document.getElementById('slSkipHead');
  if(sk && sk.checked && lines.length>1){ head=lines.shift().replace(/\t/g,' | ').trim(); }
  var arts=[];
  lines.forEach(function(line,li){
    var i=i0+li; while(SL.rows.length<=i) SL.rows.push({});
    var cells=line.split('\t');
    cells.forEach(function(val,vi){
      if(cells.length>1 && SL_COLS[c0+vi] && SL_COLS[c0+vi].ty==='order') return;
      slSetCell(i,c0+vi,val); });
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
function slSecVisible(){ var s=document.getElementById('sec-sales'); return s && s.style.display!=='none'; }
function slCellPos(td){
  var tr=td.parentNode, rows=Array.prototype.indexOf.call(tr.parentNode.children, tr);
  return {r:rows, c:Array.prototype.indexOf.call(tr.children, td)};
}
function slSelBox(){ var a=SL.sel.a, b=SL.sel.b;
  return {r1:Math.min(a.r,b.r), r2:Math.max(a.r,b.r), c1:Math.min(a.c,b.c), c2:Math.min(Math.max(a.c,b.c), SL_COLS.length-1)}; }
function slSelPaint(){
  document.querySelectorAll('#slTable td.sel').forEach(function(td){ td.classList.remove('sel'); });
  if(!SL.sel) return;
  var b=slSelBox(), trs=document.querySelectorAll('#slTable tbody tr');
  for(var r=b.r1;r<=b.r2;r++){ var tr=trs[r]; if(!tr) continue;
    for(var c=b.c1;c<=b.c2;c++) if(tr.children[c]) tr.children[c].classList.add('sel'); }
}
function slSelClear(){ if(SL.sel){ SL.sel=null; slSelPaint(); } }
function slSelMulti(){ if(!SL.sel) return false; var b=slSelBox(); return b.r1!==b.r2||b.c1!==b.c2; }
document.addEventListener('mousedown', function(e){
  var td=e.target.closest && e.target.closest('#slTable tbody td');
  if(!td || td.classList.contains('del')){ if(!(e.target.closest&&e.target.closest('#slTable'))) slSelClear(); return; }
  var pos=slCellPos(td);
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
  return col.ty==='num'?slFmt(r[col.k]):(r[col.k]||'');
}
document.addEventListener('copy', function(e){
  if(!SL.sel || !slSecVisible()) return;
  var ae=document.activeElement;
  if(!slSelMulti() && ae && ae.tagName==='INPUT') return;   // обычная копия текста в ячейке
  var b=slSelBox(), trs=document.querySelectorAll('#slTable tbody tr'), out=[];
  for(var r=b.r1;r<=b.r2;r++){ var tr=trs[r]; if(!tr) continue; var row=SL.rows[+tr.dataset.i], line=[];
    for(var c=b.c1;c<=b.c2;c++) line.push((slCellText(row,c)+'').replace(/[\t\n]/g,' '));
    out.push(line.join('\t')); }
  e.clipboardData.setData('text/plain', out.join('\r\n')); e.preventDefault();
  document.getElementById('slSaved').textContent='Скопировано ячеек: '+((b.r2-b.r1+1)*(b.c2-b.c1+1));
});
document.addEventListener('keydown', function(e){
  if(!SL.sel || !slSecVisible()) return;
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
  var rows=[SL_COLS.map(function(c){return c.t;})];
  SL.rows.forEach(function(r){
    if(!r.art && !r.comment) return;
    rows.push(SL_COLS.map(function(c){
      if(c.k==='name') return slName(r);
      if(c.k==='total') return slFmt(slTotal(r));
      if(c.k==='order') return slFmt(slOrderVal(r));
      return c.ty==='num'?slFmt(r[c.k]):(r[c.k]||'');
    }));
  });
  var csv=rows.map(function(row){return row.map(function(v){
    v=(v==null?'':v+''); return /[;"\n]/.test(v)?'"'+v.replace(/"/g,'""')+'"':v;}).join(';');}).join('\r\n');
  var a=document.createElement('a');
  a.href=URL.createObjectURL(new Blob(['﻿'+csv],{type:'text/csv;charset=utf-8'}));
  a.download='analiz_prodazh.csv'; a.click();
}
['slPeriod','slCover'].forEach(function(id){
  var el=document.getElementById(id); if(!el) return;
  el.addEventListener('change', function(){
    var v=slNum(el.value); if(!(v>0)){ el.value=id==='slPeriod'?SL.settings.period_days:SL.settings.cover_days; return; }
    if(id==='slPeriod') SL.settings.period_days=v; else SL.settings.cover_days=v;
    slRefreshCalc(); slSave();
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
