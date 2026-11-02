"""
Подпроект «Колеса BMW» — модуль для ядра (server.py). С версии 7.0.

Суть: BMW рассылает Excel «колёсное предложение» (артикул, применяемость
кузовами, размерность, шина, диаметр, диск, цвет, DOT…). Модуль перегоняет
его в Excel по нашему шаблону («шаблон колеса»), с которого сайт
автоматически загружает колёса.

Правила переноса (согласованы с владельцем 02.11.2026):
- Модель авто — только значения из списка MODELS (= Лист2 шаблона); кузова
  из «Применяемости» сопоставляются со строками списка, несколько строк
  соединяются через «, ». Бренд — MINI, если все строки MINI, иначе BMW.
- Параметры диска (J/ET), фото, цена и МЦП — не заполняем.
- Датчики давления — всегда «Да». Ось «Обе», Аварийное/Комплект «Нет».
- Цвет диска — как у BMW, по-английски.
- DOT из комментария → «Дата производства» («DOT уточняется» → пусто).
- Размерность без «XL».

Слепки: wheels_snapshots/<дата-время>.json {id, title, src, rows, at}.
"""

import os
import re
import io
import json
import glob
import datetime


# Лист2 шаблона: допустимые значения «Модель авто» (выпадающий список).
MODELS = [
    "1' E81; E87; E82; E88", "1' F20; F21; F22; F23",
    "1' F40; F52 седан; F70; F44; F74; F78", "2' F87 M2", "2' G42",
    "2' F45 Active Tourer; F46", "2' U06 Active Tourer",
    "3' E90; E91; E92; E93", "3' F30; F31; F32; F36; F33",
    "3' F80 M3; F83 M4; 4' F82",
    "3' G20 седан; G28; G21; G22; G23; G26 Coupe; i4",
    "3' G80 M3; G81; 2' G87 M2; 4' G82 M4 Coupé; G83 Cabrio", "3' F34 GT",
    "5' E60; E61", "5' F07 GT", "5' F10; F11; F18", "5' G30; G38; G31",
    "5' F90 M5", "5' G60; G68; G61", "5' G90 M5; G91 Touring",
    "6' F06 Gran Coupé; F12; F13", "6' G32 GT", "7' E65; E66",
    "7' F01; F02; F04", "7' G11; G12", "7' G70",
    "8' G14 Convertible; G15; G16", "8' F92 M8 Coupé; F93; F91", "X1 E84",
    "X1 F48; F49; X2 F39", "X1 U11; U12; X2 U10", "X3 E83", "X3 F25; X4 F26",
    "X3 G01; G08; X4 G02", "X3 G45; G48", "iX3 NA5", "X4 M F98; X3 M F97",
    "X5 E53", "X5 E70", "X5 F15; X6 F16", "X5 M F85; X6 M F86",
    "X5 G05; X6 G06; X5 G18", "X5 M F95; X6 M F96", "X6 E71; E71 Hybrid",
    "XM G09", "Z4 E89", "Z4 G29", "MINI R56; R55; R57; R58",
    "MINI F56; F55; F57", "MINI Cooper J01", "MINI F65", "MINI Aceman J05",
    "MINI Clubman F54", "MINI Cabrio F67", "MINI Roadster R59",
    "MINI Countryman R60", "MINI Countryman F60", "MINI Countryman U25",
    "MINI Paceman R61", "i8", "i3", "ix20", "X7 G07",
]

# Колонки шаблона (Лист1), порядок важен.
HEADERS = [
    "Артикул", "Связанный артикул", "Альтернативный номер", "Название",
    "Бренд авто", "Модель авто", "Ось", "Тип шин", "Аварийное", "Runflat",
    "Комплект", "Аварийное ", "Датчики давления", "Диаметр", "Размерность",
    "Параметры диска", "Цвет диска", "Стиль диска", "Марка шин",
    "Дата производства", "Изображение (ссылка)", "Комментарий",
]
WIDTHS = {"A": 15, "B": 21, "C": 22, "D": 70, "E": 14, "F": 60, "G": 12,
          "H": 19, "O": 30, "P": 19, "Q": 22, "R": 28, "S": 40, "T": 18,
          "U": 30, "V": 40}

BODY_RE = re.compile(r"\b([A-Z]{1,2}\d{2,3})\b")


def _body_index():
    """кузов (G30) → строка списка моделей (первая, где он встречается)."""
    idx = {}
    for m in MODELS:
        for b in BODY_RE.findall(m):
            idx.setdefault(b, m)
    return idx


BODY_INDEX = _body_index()


def _s(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def map_models(appl):
    """«G20, G21, G42» → (строка «Модель авто», бренд, не найденные кузова)."""
    found, miss = [], []
    for tok in re.split(r"[,;/\s]+", appl.upper()):
        tok = tok.strip()
        if not tok:
            continue
        m = BODY_INDEX.get(tok)
        if m:
            if m not in found:
                found.append(m)
        elif tok not in miss:
            miss.append(tok)
    brand = ""
    if found:
        brand = "MINI" if all(m.startswith("MINI") for m in found) else "BMW"
    return ", ".join(found), brand, miss


def style_num(style):
    m = re.search(r"(\d{3,4}\s?M?)\s*$", style)
    return m.group(1).replace(" ", "") if m else style


def tyre_type(t):
    t = t.lower()
    if "шип" in t and "без" not in t:
        return "Зима (с шипами)"
    if "без" in t:
        return "Зима (без шипов)"
    if "лет" in t:
        return "Лето"
    return ""


def clean_size(s):
    return re.sub(r"\s+", " ", re.sub(r"\bXL\b", "", s)).strip()


def parse_dot(comment):
    """«DOT 4523-2525» → («4523-2525», ''); прочий текст → ('', текст)."""
    m = re.match(r"^\s*DOT\s*(.*)$", comment, re.I)
    if not m:
        return "", comment
    rest = m.group(1).strip()
    return (rest if re.search(r"\d", rest) else ""), ""


def find_cols(header):
    """Номера колонок файла BMW по словам в заголовках."""
    keys = {
        "art": ["артикул"], "appl": ["применяем"], "size": ["размерн"],
        "tyre": ["шина", "модель"], "diam": ["диаметр"],
        "style": ["описание диска", "дизайн"], "color": ["цвет"],
        "comment": ["коммент"], "ttype": ["тип шин"], "rf": ["run-flat", "runflat", "rsc"],
    }
    cols = {}
    for i, h in enumerate(header):
        h = _s(h).lower()
        if not h:
            continue
        for k, words in keys.items():
            if k not in cols and any(w in h for w in words):
                cols[k] = i
                break
    return cols


def convert(data):
    """Excel BMW (bytes) → список строк в нашем формате."""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    ws = wb.worksheets[0]
    rows_in = list(ws.iter_rows(values_only=True))
    hi = next((i for i, r in enumerate(rows_in[:20])
               if any(_s(v).lower() == "артикул" for v in r)), None)
    if hi is None:
        raise ValueError("Не найдена строка заголовков с колонкой «Артикул»")
    cols = find_cols(rows_in[hi])
    need = ["art", "appl", "size", "diam", "style"]
    lost = [k for k in need if k not in cols]
    if lost:
        raise ValueError("В файле не найдены колонки: " + ", ".join(lost))

    def g(r, k):
        i = cols.get(k)
        return _s(r[i]) if i is not None and i < len(r) else ""

    out = []
    for r in rows_in[hi + 1:]:
        art = g(r, "art").replace(" ", "")
        if not art:
            continue
        warn = []
        model, brand, miss = map_models(g(r, "appl"))
        if miss:
            warn.append("нет в списке моделей: " + ", ".join(miss))
        if not model:
            warn.append("модель не определена")
        tt = tyre_type(g(r, "ttype"))
        tyre = g(r, "tyre")
        rf = g(r, "rf").lower() in ("да", "yes", "+") or bool(
            re.search(r"RSC|RFT|ROF|r-f|SSR", tyre, re.I))
        diam = g(r, "diam")
        try:
            diam = int(float(diam.replace(",", ".")))
        except ValueError:
            m = re.search(r"R(\d{2})", g(r, "size"))
            diam = int(m.group(1)) if m else ""
        style = g(r, "style")
        kind = "Летнее" if tt == "Лето" else "Зимнее"
        name = f"{kind} колесо в сборе для {{comp}} -  размер R{diam}, стиль {style_num(style)}"
        dot, comment = parse_dot(g(r, "comment"))
        out.append({
            "art": art, "name": name, "brand": brand, "model": model,
            "axle": "Обе", "ttype": tt, "rf": "Да" if rf else "Нет",
            "diam": diam, "size": clean_size(g(r, "size")),
            "color": g(r, "color"), "style": style, "tyre": tyre,
            "dot": dot, "comment": comment, "warn": "; ".join(warn),
        })
    if not out:
        raise ValueError("В файле не найдено ни одной строки с артикулом")
    return out


def build_xlsx(rows):
    """Строки → Excel по шаблону (Лист1 + Лист2 + выпадающие списки)."""
    import openpyxl
    from openpyxl.styles import Font
    from openpyxl.worksheet.datavalidation import DataValidation
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Лист1"
    ws.append(HEADERS)
    for c in ws[1]:
        c.font = Font(bold=True)
    for r in rows:
        ws.append([
            r["art"], "", "", r["name"], r["brand"], r["model"], r["axle"],
            r["ttype"], "Нет", r["rf"], "Нет", "Нет", "Да", r["diam"],
            r["size"], "", r["color"], r["style"], r["tyre"], r["dot"], "",
            r["comment"],
        ])
    for i in range(2, ws.max_row + 1):
        ws.cell(i, 1).number_format = "@"
    for col, w in WIDTHS.items():
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"
    last = max(ws.max_row, 2) + 50

    ws2 = wb.create_sheet("Лист2")
    for m in MODELS:
        ws2.append([m, m.replace("; ", ", ")])

    for sqref, f in [
        (f"G2:G{last}", '"Передняя,Задняя,Обе"'),
        (f"N2:N{last}", '"16,17,18,19,20,21,22,23"'),
        (f"I2:M{last}", '"Да,Нет"'),
        (f"H2:H{last}", '"Лето,Зима (без шипов),Зима (с шипами)"'),
        (f"E2:E{last}", '"BMW,MINI"'),
        (f"F2:F{last}", "Лист2!$A:$A"),
    ]:
        dv = DataValidation(type="list", formula1=f, allow_blank=True)
        dv.add(sqref)
        ws.add_data_validation(dv)
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


def register(ctx):
    app = ctx["app"]
    check_auth = ctx["check_auth"]
    lock = ctx["lock"]
    jsonify = ctx["jsonify"]
    request = ctx["request"]
    SNAP_DIR = os.path.join(ctx["data_dir"], "wheels_snapshots")

    def snap_path(sid):
        if not re.fullmatch(r"[0-9_-]{8,40}", sid or ""):
            return None
        return os.path.join(SNAP_DIR, sid + ".json")

    def load_snap(sid):
        p = snap_path(sid)
        if not p or not os.path.exists(p):
            return None
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    # ---------- API ----------

    @app.post("/api/wheels_upload")
    def api_wheels_upload():
        if not check_auth(request):
            return jsonify({"error": "Неверный пароль"}), 401
        f = request.files.get("file")
        if f is None:
            return jsonify({"error": "Файл не получен"}), 400
        data = f.read()
        if data[:2] != b"PK":
            return jsonify({"error": "Нужен файл Excel .xlsx"}), 400
        try:
            rows = convert(data)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": f"Не удалось прочитать Excel: {e}"}), 400
        now = datetime.datetime.utcnow() + datetime.timedelta(hours=3)
        sid = now.strftime("%Y-%m-%d_%H-%M-%S")
        src = os.path.basename(f.filename or "файл BMW")
        snap = {"id": sid, "title": os.path.splitext(src)[0], "src": src,
                "at": now.strftime("%d.%m.%Y %H:%M"), "rows": rows}
        with lock:
            os.makedirs(SNAP_DIR, exist_ok=True)
            with open(snap_path(sid), "w", encoding="utf-8") as fo:
                json.dump(snap, fo, ensure_ascii=False, indent=1)
        return jsonify({"ok": True, "id": sid})

    @app.get("/api/wheels_list")
    def api_wheels_list():
        if not check_auth(request):
            return jsonify({"error": "Неверный пароль"}), 401
        out = []
        for p in sorted(glob.glob(os.path.join(SNAP_DIR, "*.json")), reverse=True):
            try:
                with open(p, encoding="utf-8") as f:
                    s = json.load(f)
                out.append({"id": s["id"], "title": s.get("title", ""),
                            "at": s.get("at", ""), "total": len(s.get("rows", [])),
                            "warn": sum(1 for r in s.get("rows", []) if r.get("warn"))})
            except Exception:
                continue
        return jsonify({"snapshots": out})

    @app.get("/api/wheels_get")
    def api_wheels_get():
        if not check_auth(request):
            return jsonify({"error": "Неверный пароль"}), 401
        s = load_snap(request.args.get("id", ""))
        if not s:
            return jsonify({"error": "Слепок не найден"}), 404
        return jsonify(s)

    @app.get("/api/wheels_xlsx")
    def api_wheels_xlsx():
        if not check_auth(request):
            return jsonify({"error": "Неверный пароль"}), 401
        s = load_snap(request.args.get("id", ""))
        if not s:
            return jsonify({"error": "Слепок не найден"}), 404
        from flask import Response
        return Response(build_xlsx(s["rows"]), mimetype=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))

    @app.post("/api/wheels_delete")
    def api_wheels_delete():
        if not check_auth(request):
            return jsonify({"error": "Неверный пароль"}), 401
        p = snap_path(str((request.get_json(force=True) or {}).get("id", "")))
        if not p or not os.path.exists(p):
            return jsonify({"error": "Слепок не найден"}), 404
        with lock:
            os.remove(p)
        return jsonify({"ok": True})

    # ---------- Что отдаём ядру для оболочки ----------

    SECTION_HTML = """
  <h2 style="margin:0 0 4px">🛞 Колеса BMW</h2>
  <p class="hint" style="margin:0 0 14px">Загрузи Excel-предложение от BMW — получишь
    Excel по нашему шаблону для загрузки колёс на сайт.</p>
  <div class="bar">
    <button class="primary" id="whUpload">Загрузить файл BMW (.xlsx)</button>
    <input type="file" id="whFile" accept=".xlsx" style="display:none">
    <select id="whSnaps"></select>
    <button id="whOpen">Открыть</button>
    <button id="whDel">Удалить</button>
    <button id="whXlsx" class="primary" style="display:none">⬇ Скачать Excel по шаблону</button>
  </div>
  <div id="whStats" class="hint" style="margin-bottom:8px"></div>
  <div style="overflow:auto">
  <table id="whTbl" style="display:none">
    <thead><tr>
      <th>№</th><th>Артикул</th><th>Название</th><th>Бренд</th><th>Модель авто</th>
      <th>Тип шин</th><th>Runflat</th><th>Диаметр</th><th>Размерность</th>
      <th>Цвет диска</th><th>Стиль диска</th><th>Марка шин</th><th>Дата пр-ва</th>
    </tr></thead>
    <tbody id="whBody"></tbody>
  </table>
  </div>
"""

    CSS = """
#whTbl td{font-size:13px}
#whTbl td.wh-art{font-family:'Consolas','Menlo',monospace;white-space:nowrap}
#whTbl tr.wh-warn td{background:#fdecea}
#whTbl .wh-w{color:var(--err);font-size:12px}
"""

    JS = r"""
let WH={id:null, rows:[], title:''};
function whEsc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;');}

async function whRefresh(sel){
  try{
    const d=await apiGet('/api/wheels_list');
    const s=$('#whSnaps');
    s.innerHTML='<option value="">— загруженные файлы —</option>'+
      (d.snapshots||[]).map(x=>`<option value="${x.id}">${whEsc(x.title)} · ${x.at} · ${x.total} шт.</option>`).join('');
    if(sel) s.value=sel;
  }catch(e){}
}

function whRender(){
  const tb=$('#whBody'); tb.innerHTML='';
  WH.rows.forEach((r,i)=>{
    const tr=document.createElement('tr');
    if(r.warn) tr.className='wh-warn';
    tr.innerHTML=`<td>${i+1}</td><td class="wh-art">${whEsc(r.art)}</td>
      <td>${whEsc(r.name)}</td><td>${whEsc(r.brand)}</td>
      <td>${whEsc(r.model)}${r.warn?'<div class="wh-w">⚠ '+whEsc(r.warn)+'</div>':''}</td>
      <td>${whEsc(r.ttype)}</td><td>${whEsc(r.rf)}</td><td>${whEsc(r.diam)}</td>
      <td>${whEsc(r.size)}</td><td>${whEsc(r.color)}</td><td>${whEsc(r.style)}</td>
      <td>${whEsc(r.tyre)}</td><td>${whEsc(r.dot)}</td>`;
    tb.appendChild(tr);
  });
  const w=WH.rows.filter(r=>r.warn).length;
  $('#whStats').textContent=WH.rows.length?
    `${WH.title} · колёс: ${WH.rows.length}`+(w?` · требуют внимания (красные): ${w}`:' · все модели сопоставлены'):'';
  $('#whTbl').style.display=WH.rows.length?'':'none';
  $('#whXlsx').style.display=WH.rows.length?'':'none';
}

async function whOpenSnap(id){
  try{
    const d=await apiGet('/api/wheels_get?id='+encodeURIComponent(id));
    if(d.error){alert(d.error);return;}
    WH={id:d.id, rows:d.rows||[], title:d.title||''};
    $('#whSnaps').value=d.id;
    whRender();
  }catch(e){}
}

$('#whUpload').onclick=()=>$('#whFile').click();
$('#whFile').onchange=async e=>{
  const f=e.target.files[0]; e.target.value='';
  if(!f) return;
  $('#whStats').textContent='Читаю файл…';
  const fd=new FormData(); fd.append('file',f);
  let d={};
  try{
    const res=await fetch('/api/wheels_upload',{method:'POST',headers:{'X-Auth':AUTH},body:fd});
    d=await res.json();
  }catch(err){ d={error:String(err)}; }
  if(d.error){$('#whStats').textContent='';alert('Ошибка: '+d.error);return;}
  await whRefresh(d.id);
  whOpenSnap(d.id);
};
$('#whOpen').onclick=()=>{const id=$('#whSnaps').value; if(id) whOpenSnap(id);};
$('#whDel').onclick=async()=>{
  const id=$('#whSnaps').value;
  if(!id){alert('Выберите файл в списке');return;}
  if(!confirm('Удалить этот файл из списка?')) return;
  const d=await api('/api/wheels_delete',{id});
  if(d.error){alert(d.error);return;}
  if(WH.id===id){WH={id:null,rows:[],title:''};whRender();}
  whRefresh();
};
$('#whXlsx').onclick=async()=>{
  if(!WH.id) return;
  try{
    const res=await fetch('/api/wheels_xlsx?id='+encodeURIComponent(WH.id),{headers:{'X-Auth':AUTH}});
    if(!res.ok){alert('Не удалось скачать');return;}
    const a=document.createElement('a');
    a.href=URL.createObjectURL(await res.blob());
    a.download='Колеса '+(WH.title||WH.id)+' — шаблон.xlsx';
    document.body.appendChild(a); a.click(); a.remove();
  }catch(e){alert('Ошибка: '+e);}
};

window.onShow_wheels = ()=>{ whRefresh($('#whSnaps').value); };
"""

    return {
        "id": "wheels",
        "title": "🛞 Колеса BMW",
        "section_html": SECTION_HTML,
        "css": CSS,
        "js": JS,
    }
