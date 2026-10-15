"""
Подпроект «Анализ продаж» (анализатор продаж Porsche) — модуль для ядра.

Пробная версия: онлайн-таблица позиций (артикулы, продажи по каналам,
остатки, заказано/в пути, продажи за год), свой справочник
«артикул → наименование» и расчёт колонки «в заказ» по простой формуле
(дальше её заменит/дополнит ИИ по правилам владельца).

Данные на сервере (ctx["data_dir"]):
  sales.json          — {settings: {period_days, cover_days}, rows: [...]}
  sales_catalog.json  — {артикул: наименование}
"""

import os
import re
import json

# Колонки, которые хранятся в строке (наименование, «общие продажи»
# и «в заказ» вычисляются и не хранятся).
TEXT_FIELDS = ("art", "repl", "comment")
NUM_FIELDS = ("profit", "turn_de", "sold_de", "sold_sup", "sold_stock",
              "stock", "ordered", "transit", "year")

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

    def load_sales():
        data = load_json(SALES_PATH, {})
        data.setdefault("settings", {})
        data["settings"].setdefault("period_days", 90)
        data["settings"].setdefault("cover_days", 60)
        data.setdefault("rows", [])
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
        data = load_sales()
        cat = load_json(CATALOG_PATH, {})
        arts = set()
        for r in data["rows"]:
            arts.add(r.get("art"))
            arts.add(r.get("repl"))
        names = {a: cat[a] for a in arts if a and a in cat}
        return jsonify({"settings": data["settings"], "rows": data["rows"],
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
        with lock:
            save_json(SALES_PATH, {"settings": settings, "rows": rows})
        return jsonify({"ok": True, "count": len(rows)})

    @app.post("/api/sales_names")
    def api_sales_names():
        if not check_auth(request):
            return deny()
        d = request.get_json(force=True) or {}
        arts = [norm_art(a) for a in (d.get("arts") or [])][:5000]
        return jsonify({"names": names_for(arts)})

    @app.post("/api/sales_catalog_add")
    def api_sales_catalog_add():
        """Принимает текст «артикул<TAB>наименование» построчно (из Excel/CSV)."""
        if not check_auth(request):
            return deny()
        d = request.get_json(force=True) or {}
        text = str(d.get("text") or "")
        added = updated = skipped = 0
        with lock:
            cat = load_json(CATALOG_PATH, {})
            for line in text.splitlines():
                if not line.strip():
                    continue
                parts = re.split(r"\t|;", line, maxsplit=1)
                if len(parts) < 2:
                    skipped += 1
                    continue
                art, name = norm_art(parts[0]), parts[1].strip().strip('"').strip()
                if not art or not name:
                    skipped += 1
                    continue
                if art in cat:
                    if cat[art] != name:
                        updated += 1
                else:
                    added += 1
                cat[art] = name
            save_json(CATALOG_PATH, cat)
        return jsonify({"ok": True, "added": added, "updated": updated,
                        "skipped": skipped, "catalog_size": len(cat)})

    # ---------- Что отдаём ядру для оболочки ----------

    SECTION_HTML = """
  <h2 style="margin:0 0 4px">📈 Анализ продаж</h2>
  <p class="hint" style="margin:0 0 14px">Анализатор продаж Porsche (пробная версия).
    Вносите позиции или вставляйте блок из Excel (Ctrl+V в любую ячейку). Всё
    сохраняется на сервере автоматически.</p>
  <div class="bar">
    <label class="hint">Период продаж, дней
      <input id="slPeriod" class="slnum" type="number" min="1"></label>
    <label class="hint">Заказывать с запасом на, дней
      <input id="slCover" class="slnum" type="number" min="1"></label>
    <input id="slSearch" placeholder="Поиск по артикулу или названию">
    <button onclick="slAddRow()">+ Строка</button>
    <button onclick="slExport()">⬇️ Скачать CSV</button>
    <button onclick="slToggleCat()">📚 Справочник <span id="slCatCnt" class="hint"></span></button>
    <span id="slSaved" class="hint"></span>
  </div>
  <div id="slCat" class="panel" style="display:none;padding:12px 14px;margin-bottom:12px">
    <div class="hint" style="margin-bottom:6px">Справочник «артикул → наименование».
      Скопируйте из Excel два столбца (артикул и наименование) и вставьте сюда,
      либо выберите файл CSV/TXT. Существующие артикулы обновятся.</div>
    <textarea id="slCatText" rows="6" placeholder="99610610155	Фильтр масляный"></textarea>
    <div class="bar" style="margin:8px 0 0">
      <button class="primary" onclick="slCatAdd()">Добавить в справочник</button>
      <input type="file" id="slCatFile" accept=".csv,.txt,.tsv" onchange="slCatFile(this)">
      <span id="slCatMsg" class="hint"></span>
    </div>
  </div>
  <div id="slWrap"><table id="slTable"><thead></thead><tbody></tbody></table></div>
  <p class="hint" style="margin-top:10px">«В заказ» пока считается формулой: средний
    дневной спрос (60% — текущий период, 40% — последний год) × дней запаса − наличие
    − уже заказано. Наведите на число, чтобы увидеть расчёт. Следующий шаг — правила
    и ИИ.</p>
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
  #slTable td.order{background:#eaf2fd;color:var(--blue-d)}
  #slTable td.nm{padding:6px 8px;font-size:12px;min-width:180px}
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
  {k:'order',      t:'В заказ',                     ty:'calc'},
  {k:'comment',    t:'Комментарий',                 ty:'text'},
  {k:'stock',      t:'Наше наличие',                ty:'num'},
  {k:'ordered',    t:'Кол-во заказано',             ty:'num'},
  {k:'transit',    t:'В пути',                      ty:'num'},
  {k:'year',       t:'Продажи за год (365 дн.)',    ty:'num'}
];
var SL = {rows:[], names:{}, settings:{period_days:90, cover_days:60}, loaded:false,
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
function slName(r){
  return SL.names[r.art] || (r.repl && SL.names[r.repl]) || '';
}

async function slLoad(){
  if(SL.loaded) return;
  try{
    var d=await apiGet('/api/sales');
    SL.rows=d.rows||[]; SL.names=d.names||{}; SL.settings=d.settings||SL.settings;
    SL.loaded=true;
    document.getElementById('slPeriod').value=SL.settings.period_days;
    document.getElementById('slCover').value=SL.settings.cover_days;
    slCatCount(d.catalog_size);
    if(!SL.rows.length) for(var i=0;i<5;i++) SL.rows.push({});
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
  var h='<tr>'; SL_COLS.forEach(function(c){h+='<th>'+c.t+'</th>';}); h+='<th></th></tr>';
  t.tHead.innerHTML=h;
  var b='';
  SL.rows.forEach(function(r,i){ if(slVisible(i)) b+=slRowHtml(r,i); });
  t.tBodies[0].innerHTML=b;
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
    var o=slOrder(r);
    return '<td class="calc order" title="'+slEsc(o.why)+'">'+(o.any?o.q:'')+'</td>';
  }
  var v=r[c.k]; v=(c.ty==='num')?slFmt(v):(v||'');
  return '<td class="'+c.ty+'"><input data-i="'+i+'" data-c="'+ci+'" value="'+slEsc(v)+'"></td>';
}
function slRefreshRow(i){
  var tr=document.querySelector('#slTable tr[data-i="'+i+'"]'); if(!tr) return;
  var r=SL.rows[i];
  SL_COLS.forEach(function(c,ci){
    if(c.ty==='name'||c.ty==='calc'){
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
function slSave(){
  clearTimeout(SL.saveTimer);
  document.getElementById('slSaved').textContent='Сохраняю…';
  SL.saveTimer=setTimeout(async function(){
    try{
      var rows=SL.rows.filter(function(r){
        return Object.keys(r).some(function(k){return r[k]!=null&&r[k]!=='';}); });
      var d=await api('/api/sales_save',{rows:rows, settings:SL.settings});
      document.getElementById('slSaved').textContent=d.ok?('✓ Сохранено ('+d.count+')'):(d.error||'Ошибка');
    }catch(e){ document.getElementById('slSaved').textContent='⚠️ Не сохранилось'; }
  }, 700);
}

/* ввод в ячейку */
document.addEventListener('change', async function(e){
  var el=e.target; if(!el.closest||!el.closest('#slTable')||el.dataset.c==null) return;
  var i=+el.dataset.i, ci=+el.dataset.c;
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
  var el=e.target; if(!el.closest||!el.closest('#slTable')||el.dataset.c==null) return;
  var txt=(e.clipboardData||window.clipboardData).getData('text');
  if(txt.indexOf('\t')<0 && txt.indexOf('\n')<0) return;
  e.preventDefault();
  var lines=txt.replace(/\r/g,'').split('\n');
  if(lines.length && lines[lines.length-1]==='') lines.pop();
  var i0=+el.dataset.i, c0=+el.dataset.c, arts=[];
  lines.forEach(function(line,li){
    var i=i0+li; while(SL.rows.length<=i) SL.rows.push({});
    line.split('\t').forEach(function(val,vi){ slSetCell(i,c0+vi,val); });
    arts.push(SL.rows[i].art, SL.rows[i].repl);
  });
  await slLookup(arts);
  slRender(); slSave();
});
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
function slCatFile(inp){
  var f=inp.files&&inp.files[0]; if(!f) return;
  var rd=new FileReader();
  rd.onload=function(){ document.getElementById('slCatText').value=rd.result; inp.value=''; };
  rd.readAsText(f,'utf-8');
}
function slExport(){
  var rows=[SL_COLS.map(function(c){return c.t;})];
  SL.rows.forEach(function(r){
    if(!r.art && !r.comment) return;
    rows.push(SL_COLS.map(function(c){
      if(c.k==='name') return slName(r);
      if(c.k==='total') return slFmt(slTotal(r));
      if(c.k==='order') return slOrder(r).q;
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
