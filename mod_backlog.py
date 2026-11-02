# -*- coding: utf-8 -*-
"""
Подпроект «Беклоги» — модуль для ядра (server.py).

BMW публикует PDF со списком деталей, у которых проблемы с поставкой
(беклог календарной недели). Номера там 7-значные. Модуль:
1) принимает PDF, вытаскивает все 7-значные номера и номер календарной недели;
2) сопоставляет короткие номера с полными (11-значными) из нашей базы:
   полный номер всегда оканчивается этими 7 знаками (первые 4 — префикс);
3) сохраняет результат слепком «Календарная неделя ...» с таблицей
   краткий / полный / название.

Для чтения PDF нужен pypdf в venv сервера:
    /root/bmw_names/venv/bin/pip install pypdf
"""

import os
import re
import json
import datetime


def register(ctx):
    app = ctx["app"]
    check_auth = ctx["check_auth"]
    lock = ctx["lock"]
    jsonify = ctx["jsonify"]
    request = ctx["request"]
    DATA = ctx["data_dir"]
    SNAP_DIR = os.path.join(DATA, "backlog_snapshots")
    os.makedirs(SNAP_DIR, exist_ok=True)

    # ---------- Нормализация (как в ядре) ----------

    def norm_key(v):
        s = str(v).strip().upper().replace(" ", "").replace("-", "")
        if s.endswith(".0"):
            s = s[:-2]
        s = s.lstrip("0") or s
        return s

    # ---------- Индекс: последние 7 знаков -> полные артикулы ----------

    _idx_cache = {"mtimes": None, "last7": None, "names": None}

    def build_index():
        """last7 -> [полные артикулы]; имена: полный -> название.

        Название берём из dataset.jsonl (наименование по методике), если
        его нет — базовое из catalog.json."""
        cat_p = os.path.join(DATA, "catalog.json")
        ds_p = os.path.join(DATA, "dataset.jsonl")
        mtimes = (os.path.getmtime(cat_p) if os.path.exists(cat_p) else 0,
                  os.path.getmtime(ds_p) if os.path.exists(ds_p) else 0)
        if _idx_cache["mtimes"] == mtimes:
            return _idx_cache["last7"], _idx_cache["names"]

        last7, names = {}, {}
        if os.path.exists(cat_p):
            try:
                with open(cat_p, encoding="utf-8") as f:
                    cat = json.load(f)
            except Exception:
                cat = {}
            for k, rec in cat.items():
                k = str(k)
                if len(k) >= 7:
                    last7.setdefault(k[-7:], []).append(k)
                base = (rec or {}).get("base", "") if isinstance(rec, dict) else ""
                if base:
                    names[k] = base
        if os.path.exists(ds_p):
            try:
                with open(ds_p, encoding="utf-8") as f:
                    for line in f:
                        try:
                            r = json.loads(line)
                        except Exception:
                            continue
                        k = norm_key(r.get("input", {}).get("артикул", ""))
                        out = str(r.get("output", "")).strip()
                        if not k:
                            continue
                        if len(k) >= 7 and k[-7:] not in last7:
                            last7.setdefault(k[-7:], []).append(k)
                        elif len(k) >= 7 and k not in last7.get(k[-7:], []):
                            last7[k[-7:]].append(k)
                        if out:
                            names[k] = out  # методика важнее базового
            except Exception:
                pass
        _idx_cache["mtimes"] = mtimes
        _idx_cache["last7"] = last7
        _idx_cache["names"] = names
        return last7, names

    # ---------- Разбор PDF ----------

    def pdf_text(data):
        try:
            import pypdf
        except ImportError:
            try:
                import PyPDF2 as pypdf  # noqa: N813
            except ImportError:
                raise RuntimeError(
                    "На сервере не установлен pypdf. Выполните по SSH:\n"
                    "/root/bmw_names/venv/bin/pip install pypdf\n"
                    "и затем: systemctl restart bmw-names")
        import io
        reader = pypdf.PdfReader(io.BytesIO(data))
        parts = []
        for pg in reader.pages:
            try:
                parts.append(pg.extract_text() or "")
            except Exception:
                parts.append("")
        return "\n".join(parts)

    WEEK_RES = [
        re.compile(r"(?:календарн\w*\s+недел\w*|Kalenderwoche|calendar\s+week)\s*[:№#]?\s*(\d{1,2})(?:\s*[/.\-]\s*(\d{4}))?", re.IGNORECASE),
        re.compile(r"\bKW\s*[:.]?\s*(\d{1,2})(?:\s*[/.\-]\s*(\d{4}))?", re.IGNORECASE),
    ]

    def find_week(text):
        for rx in WEEK_RES:
            m = rx.search(text)
            if m:
                wk = m.group(1)
                yr = m.group(2) or ""
                return f"Календарная неделя {wk}" + (f"/{yr}" if yr else "")
        return ""

    NUM_RE = re.compile(r"(?<![\dA-Z])(\d(?:\s?\d{3}){2})(?![\dA-Z])")

    def find_shorts(text):
        """7-значные номера: '2254254' и '2 254 254'. Порядок файла, без дублей."""
        seen, out = set(), []
        for m in NUM_RE.finditer(text):
            s = re.sub(r"\s", "", m.group(1))
            if len(s) == 7 and s not in seen:
                seen.add(s)
                out.append(s)
        return out

    # ---------- Слепки ----------

    def snap_path(sid):
        sid = re.sub(r"[^0-9A-Za-z_\-]", "", str(sid))
        return os.path.join(SNAP_DIR, sid + ".json")

    def list_snaps():
        out = []
        for fn in sorted(os.listdir(SNAP_DIR), reverse=True):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(SNAP_DIR, fn), encoding="utf-8") as f:
                    s = json.load(f)
                rows = s.get("rows", [])
                out.append({"id": s.get("id", fn[:-5]), "title": s.get("title", ""),
                            "total": len(rows),
                            "found": sum(1 for r in rows if r.get("full"))})
            except Exception:
                continue
        return out

    # ---------- API ----------

    @app.post("/api/backlog_upload")
    def api_backlog_upload():
        if not check_auth(request):
            return jsonify({"error": "Неверный пароль"}), 401
        f = request.files.get("file")
        if f is None:
            return jsonify({"error": "Файл не получен"}), 400
        data = f.read()
        if data[:5] != b"%PDF-":
            return jsonify({"error": "Это не PDF-файл"}), 400
        try:
            text = pdf_text(data)
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 500
        except Exception as e:
            return jsonify({"error": f"Не удалось прочитать PDF: {e}"}), 400
        if not text.strip():
            return jsonify({"error": "В PDF не найден текстовый слой (скан?) — "
                                     "нужен оригинальный PDF от BMW"}), 400

        shorts = find_shorts(text)
        if not shorts:
            return jsonify({"error": "7-значные номера в файле не найдены"}), 400
        title = find_week(text) or "Календарная неделя ?"

        last7, names = build_index()
        rows = []
        for s in shorts:
            cands = last7.get(s, [])
            if len(cands) == 1:
                full = cands[0]
                rows.append({"short": s, "full": full,
                             "name": names.get(full, ""), "st": "ok"})
            elif len(cands) > 1:
                full = cands[0]
                rows.append({"short": s, "full": " / ".join(cands[:4]),
                             "name": names.get(full, ""), "st": "multi"})
            else:
                rows.append({"short": s, "full": "", "name": "", "st": "miss"})

        sid = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        snap = {"id": sid, "title": title, "src": f.filename or "",
                "rows": rows}
        with lock:
            tmp = snap_path(sid) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(snap, fp, ensure_ascii=False)
            os.replace(tmp, snap_path(sid))
        return jsonify({"id": sid, "title": title, "total": len(rows),
                        "found": sum(1 for r in rows if r["st"] == "ok"),
                        "multi": sum(1 for r in rows if r["st"] == "multi"),
                        "miss": sum(1 for r in rows if r["st"] == "miss")})

    @app.get("/api/backlog_list")
    def api_backlog_list():
        if not check_auth(request):
            return jsonify({"error": "Неверный пароль"}), 401
        return jsonify({"snapshots": list_snaps()})

    @app.get("/api/backlog_get")
    def api_backlog_get():
        if not check_auth(request):
            return jsonify({"error": "Неверный пароль"}), 401
        p = snap_path(request.args.get("id", ""))
        if not os.path.exists(p):
            return jsonify({"error": "Слепок не найден"}), 404
        with open(p, encoding="utf-8") as fp:
            return jsonify(json.load(fp))

    @app.post("/api/backlog_delete")
    def api_backlog_delete():
        if not check_auth(request):
            return jsonify({"error": "Неверный пароль"}), 401
        sid = str(request.get_json(force=True).get("id", ""))
        p = snap_path(sid)
        if not os.path.exists(p):
            return jsonify({"error": "Слепок не найден"}), 404
        with lock:
            os.remove(p)
        return jsonify({"ok": True})

    # ---------- UI ----------

    SECTION_HTML = """
  <div class="bar">
    <button class="primary" id="bkUpload">Загрузить PDF беклога</button>
    <input type="file" id="bkFile" accept=".pdf" style="display:none">
    <select id="bkSnaps" style="padding:8px;border:1px solid var(--line);border-radius:6px;font:inherit;max-width:320px">
      <option value="">— беклоги —</option></select>
    <button id="bkOpen">Открыть</button>
    <button id="bkDel">Удалить</button>
    <button id="bkCopy">Скопировать таблицу</button>
    <span class="hint" id="bkHint">PDF от BMW с 7-значными номерами проблемных деталей</span>
  </div>
  <h3 id="bkTitle" style="margin:6px 0 2px"></h3>
  <div class="hint" id="bkStats" style="margin-bottom:8px"></div>
  <div class="bar" id="bkFltBar" style="display:none">
    <input id="bkFlt" type="search" autocomplete="off" placeholder="поиск по номеру или названию…"
           style="padding:7px 9px;border:1px solid var(--line);border-radius:6px;font:inherit;width:320px">
    <label class="hint"><input type="checkbox" id="bkOnlyMiss"> только не найденные</label>
  </div>
  <table id="bkTbl" style="display:none">
    <thead><tr>
      <th style="width:30px">№</th>
      <th style="width:130px">Артикул краткий</th>
      <th style="width:170px">Артикул полный</th>
      <th>Название</th>
    </tr></thead>
    <tbody id="bkBody"></tbody>
  </table>
"""

    CSS = """
#bkTbl td.bk-art{font-family:'Consolas','Menlo',monospace;white-space:nowrap;font-size:13px}
#bkTbl tr.bk-miss td{background:#fdecea}
#bkTbl tr.bk-multi td{background:#fdf3dd}
#bkTbl td{cursor:text}
"""

    JS = r"""
let BK={id:null, rows:[], title:''};

function bkEsc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;');}

async function bkRefresh(sel){
  try{
    const d=await apiGet('/api/backlog_list');
    const s=$('#bkSnaps');
    s.innerHTML='<option value="">— беклоги —</option>'+
      d.snapshots.map(x=>`<option value="${x.id}">${bkEsc(x.title)} · ${x.found}/${x.total}</option>`).join('');
    if(sel) s.value=sel;
  }catch(e){}
}

function bkRender(){
  const q=($('#bkFlt').value||'').trim().toLowerCase();
  const onlyMiss=$('#bkOnlyMiss').checked;
  const tb=$('#bkBody'); tb.innerHTML='';
  let shown=0;
  BK.rows.forEach((r,i)=>{
    if(onlyMiss && r.st==='ok') return;
    if(q && !(r.short+' '+(r.full||'')+' '+(r.name||'')).toLowerCase().includes(q)) return;
    const tr=document.createElement('tr');
    tr.className = r.st==='miss' ? 'bk-miss' : (r.st==='multi' ? 'bk-multi' : '');
    tr.innerHTML=`<td>${i+1}</td>
      <td class="bk-art">${bkEsc(r.short)}</td>
      <td class="bk-art">${bkEsc(r.full||'—')}</td>
      <td>${bkEsc(r.name || (r.st==='miss'?'нет в нашей базе':''))}</td>`;
    tb.appendChild(tr);
    shown++;
  });
  const ok=BK.rows.filter(r=>r.st==='ok').length,
        mu=BK.rows.filter(r=>r.st==='multi').length,
        mi=BK.rows.filter(r=>r.st==='miss').length;
  $('#bkTitle').textContent=BK.title||'';
  $('#bkStats').textContent=BK.rows.length?
    `Всего номеров: ${BK.rows.length} · сопоставлено: ${ok}`+
    (mu?` · несколько вариантов: ${mu}`:'')+` · нет в базе: ${mi}`+
    (shown!==BK.rows.length?` · показано: ${shown}`:'') : '';
  $('#bkTbl').style.display=BK.rows.length?'':'none';
  $('#bkFltBar').style.display=BK.rows.length?'':'none';
}

async function bkOpenSnap(id){
  try{
    const d=await apiGet('/api/backlog_get?id='+encodeURIComponent(id));
    if(d.error){alert(d.error);return;}
    BK={id:d.id, rows:d.rows||[], title:d.title||''};
    $('#bkSnaps').value=d.id;
    bkRender();
  }catch(e){}
}

$('#bkUpload').onclick=()=>$('#bkFile').click();
$('#bkFile').onchange=async e=>{
  const f=e.target.files[0]; e.target.value='';
  if(!f) return;
  $('#bkHint').textContent='Читаю PDF…';
  const fd=new FormData(); fd.append('file',f);
  let d={};
  try{
    const res=await fetch('/api/backlog_upload',{method:'POST',headers:{'X-Auth':AUTH},body:fd});
    d=await res.json();
  }catch(err){ d={error:String(err)}; }
  $('#bkHint').textContent='PDF от BMW с 7-значными номерами проблемных деталей';
  if(d.error){alert('Ошибка: '+d.error);return;}
  await bkRefresh(d.id);
  bkOpenSnap(d.id);
};
$('#bkOpen').onclick=()=>{const id=$('#bkSnaps').value; if(id) bkOpenSnap(id);};
$('#bkDel').onclick=async()=>{
  const id=$('#bkSnaps').value;
  if(!id){alert('Выберите беклог в списке');return;}
  if(!confirm('Удалить этот беклог безвозвратно?')) return;
  const d=await api('/api/backlog_delete',{id});
  if(d.error){alert(d.error);return;}
  if(BK.id===id){BK={id:null,rows:[],title:''};bkRender();}
  bkRefresh();
};
$('#bkCopy').onclick=()=>{
  if(!BK.rows.length){alert('Таблица пуста');return;}
  const lines=BK.rows.map(r=>[r.short,r.full||'',r.name||''].join('\t'));
  const ta=document.createElement('textarea');
  ta.value=lines.join('\n');
  ta.style.position='fixed'; ta.style.opacity='0';
  document.body.appendChild(ta); ta.select();
  const ok=document.execCommand('copy'); ta.remove();
  const b=$('#bkCopy'), t=b.textContent;
  b.textContent=ok?('Скопировано: '+BK.rows.length+' строк'):'Не удалось скопировать';
  setTimeout(()=>{b.textContent=t;},2000);
};
$('#bkFlt').addEventListener('input',bkRender);
$('#bkOnlyMiss').addEventListener('change',bkRender);

window.onShow_backlog = ()=>{ bkRefresh($('#bkSnaps').value); };
"""

    return {
        "id": "backlog",
        "title": "📦 Беклоги",
        "section_html": SECTION_HTML,
        "css": CSS,
        "js": JS,
    }
