"""
Подпроект «Счета» (mod_invoices.py) — модуль для ядра (server.py).

Телеграм-бот invoicebot ловит в чате присланные файлы и шлёт их сюда
(POST /api/invoice_submit, multipart). Сервер смотрит файл: если это счёт
на оплату — извлекает контрагента, сумму и дату через ИИ ядра (ctx client),
кладёт файл рядом и добавляет запись в invoices.json со статусом «на
одобрении». В веб-разделе — таблица счетов (ссылка на файл, контрагент,
сумма, дата, статус) с одобрением/отклонением; поля редактируются руками.

Данные: invoices.json ({seq, items}); файлы — папка invoices_files/ рядом,
отдаются под X-Auth (как фото в mod_bmw: браузер тянет через fetch → blob).
"""

import os
import re
import json
import base64
import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Moscow")
MAX_AI_BYTES = 12 * 1024 * 1024  # крупнее — не шлём в ИИ, заполняется вручную
IMG_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


def register(ctx):
    app = ctx["app"]
    check_auth = ctx["check_auth"]
    lock = ctx["lock"]
    jsonify = ctx["jsonify"]
    request = ctx["request"]
    DATA = ctx["data_dir"]
    INVOICES_PATH = os.path.join(DATA, "invoices.json")
    FILES_DIR = os.path.join(DATA, "invoices_files")
    os.makedirs(FILES_DIR, exist_ok=True)

    def now_msk():
        return datetime.datetime.now(TZ).strftime("%d.%m.%Y %H:%M")

    # ---------- Хранилище ----------

    def load():
        data = {"seq": 0, "items": []}
        if os.path.exists(INVOICES_PATH):
            try:
                with open(INVOICES_PATH, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                pass
        data.setdefault("seq", 0)
        data.setdefault("items", [])
        return data

    def save(data):
        with open(INVOICES_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def find(items, iid):
        return next((x for x in items if x["id"] == iid), None)

    # ---------- ИИ-разбор файла ----------

    def analyze(data, mime, filename):
        """{is_invoice: True/False/None, contractor, amount, date, why}.
        None — тип файла авторазбору не поддаётся (Word/Excel/крупный)."""
        low = (filename or "").lower()
        block = None
        if mime in IMG_MIMES:
            block = {"type": "image", "source": {
                "type": "base64", "media_type": mime,
                "data": base64.b64encode(data).decode()}}
        elif mime == "application/pdf" or low.endswith(".pdf") or data[:5] == b"%PDF-":
            block = {"type": "document", "source": {
                "type": "base64", "media_type": "application/pdf",
                "data": base64.b64encode(data).decode()}}

        client, model = ctx.get("client"), ctx.get("model")
        if block is None or client is None or not model or len(data) > MAX_AI_BYTES:
            return {"is_invoice": None, "contractor": "", "amount": "",
                    "date": "", "why": "авторазбор для этого файла недоступен — "
                                       "заполните поля вручную"}

        prompt = (
            "Это файл, присланный в рабочий чат. Определи, счёт ли это на оплату "
            "(инвойс/счёт-фактура для оплаты поставщику). Если да — извлеки:\n"
            "- contractor: кто выставил счёт (поставщик/продавец, название организации);\n"
            "- amount: итоговая сумма к оплате (с валютой, как в документе);\n"
            "- date: дата счёта в формате ДД.ММ.ГГГГ.\n"
            "Если это НЕ счёт на оплату (прайс, договор, фото не по теме, переписка) — "
            "is_invoice=false и коротко why.\n"
            "Ответь ТОЛЬКО JSON: {\"is_invoice\": true|false, \"contractor\": \"...\", "
            "\"amount\": \"...\", \"date\": \"...\", \"why\": \"кратко по-русски\"}")
        try:
            msg = client.messages.create(
                model=model, max_tokens=500,
                messages=[{"role": "user", "content": [block, {"type": "text", "text": prompt}]}])
            if ctx.get("add_cost"):
                ctx["add_cost"](msg)
            txt = (ctx["extract_text"](msg) if ctx.get("extract_text")
                   else "".join(getattr(x, "text", "") for x in msg.content))
            m = re.search(r"\{.*\}", txt, re.S)
            res = json.loads(m.group(0)) if m else {}
        except Exception as e:
            return {"is_invoice": None, "contractor": "", "amount": "", "date": "",
                    "why": f"ИИ не смог разобрать ({e}) — заполните вручную"}
        return {
            "is_invoice": bool(res.get("is_invoice")),
            "contractor": str(res.get("contractor") or "")[:200],
            "amount": str(res.get("amount") or "")[:60],
            "date": str(res.get("date") or "")[:40],
            "why": str(res.get("why") or "")[:300],
        }

    # ---------- API ----------

    @app.post("/api/invoice_submit")
    def api_invoice_submit():
        """Сюда шлёт файлы телеграм-бот (multipart: file + поля by, chat)."""
        if not check_auth(request):
            return jsonify({"error": "Неверный пароль"}), 401
        f = request.files.get("file")
        if f is None:
            return jsonify({"error": "Файл не получен"}), 400
        data = f.read()
        if not data:
            return jsonify({"error": "Пустой файл"}), 400
        fname = f.filename or "file"
        mime = f.mimetype or "application/octet-stream"
        by = (request.form.get("by") or "")[:120]
        chat = (request.form.get("chat") or "")[:120]

        res = analyze(data, mime, fname)
        if res.get("is_invoice") is False:
            return jsonify({"ok": True, "is_invoice": False, "why": res.get("why", "")})

        with lock:
            store = load()
            store["seq"] += 1
            iid = store["seq"]
            safe = re.sub(r"[^0-9A-Za-zА-Яа-яЁё._-]", "_", fname)[:80] or "file"
            stored = f"{iid}_{safe}"
            with open(os.path.join(FILES_DIR, stored), "wb") as fp:
                fp.write(data)
            item = {
                "id": iid, "file_name": fname, "stored": stored, "mime": mime,
                "contractor": res.get("contractor", ""), "amount": res.get("amount", ""),
                "date": res.get("date", ""), "status": "pending",
                "added_at": now_msk(), "added_by": by, "chat": chat,
                "auto": res.get("is_invoice") is True, "ai_why": res.get("why", ""),
            }
            store["items"].append(item)
            save(store)
        return jsonify({"ok": True, "is_invoice": True, "auto": item["auto"], "invoice": item})

    @app.get("/api/invoices")
    def api_invoices():
        if not check_auth(request):
            return jsonify({"error": "Неверный пароль"}), 401
        items = sorted(load()["items"], key=lambda x: x["id"], reverse=True)
        return jsonify({"invoices": items})

    @app.post("/api/invoice_status")
    def api_invoice_status():
        if not check_auth(request):
            return jsonify({"error": "Неверный пароль"}), 401
        d = request.get_json(force=True) or {}
        iid = d.get("id")
        status = d.get("status")
        if status not in ("pending", "approved", "rejected"):
            return jsonify({"error": "Неверный статус"}), 400
        with lock:
            store = load()
            it = find(store["items"], iid)
            if not it:
                return jsonify({"error": "Счёт не найден"}), 404
            it["status"] = status
            save(store)
        return jsonify({"ok": True})

    @app.post("/api/invoice_edit")
    def api_invoice_edit():
        if not check_auth(request):
            return jsonify({"error": "Неверный пароль"}), 401
        d = request.get_json(force=True) or {}
        iid = d.get("id")
        with lock:
            store = load()
            it = find(store["items"], iid)
            if not it:
                return jsonify({"error": "Счёт не найден"}), 404
            for k in ("contractor", "amount", "date"):
                if k in d:
                    it[k] = str(d.get(k) or "")[:200]
            save(store)
        return jsonify({"ok": True})

    @app.post("/api/invoice_delete")
    def api_invoice_delete():
        if not check_auth(request):
            return jsonify({"error": "Неверный пароль"}), 401
        iid = (request.get_json(force=True) or {}).get("id")
        with lock:
            store = load()
            it = find(store["items"], iid)
            if it:
                try:
                    os.remove(os.path.join(FILES_DIR, it["stored"]))
                except Exception:
                    pass
                store["items"] = [x for x in store["items"] if x["id"] != iid]
                save(store)
        return jsonify({"ok": True})

    @app.get("/api/invoice_file/<int:iid>")
    def api_invoice_file(iid):
        if not check_auth(request):
            return jsonify({"error": "Неверный пароль"}), 401
        it = find(load()["items"], iid)
        if not it:
            return jsonify({"error": "Нет файла"}), 404
        p = os.path.join(FILES_DIR, it["stored"])
        if not os.path.exists(p):
            return jsonify({"error": "Нет файла"}), 404
        with open(p, "rb") as f:
            return app.response_class(f.read(), mimetype=it.get("mime") or
                                      "application/octet-stream")

    # ---------- Оболочка ----------

    SECTION_HTML = """
  <h2 style="margin:0 0 4px">🧾 Счета</h2>
  <p class="hint" style="margin:0 0 12px">Счета, присланные в чат боту, попадают сюда
    на одобрение. Поля можно поправить вручную. Бот живёт отдельно (invoicebot).</p>
  <div class="bar" style="margin-bottom:10px">
    <label class="hint"><input type="checkbox" id="invOnlyPending" onchange="loadInvoices()"> только на одобрении</label>
    <span class="sp" style="flex:1"></span>
    <span class="hint" id="invCounts"></span>
  </div>
  <div id="invList"><div class="hint">Загрузка…</div></div>
"""

    CSS = """
  #invList table{border-collapse:collapse;width:100%;max-width:1100px}
  #invList th,#invList td{border:1px solid var(--line);padding:6px 8px;text-align:left;
                          vertical-align:top;font-size:14px}
  #invList th{background:var(--bg);font-weight:600;white-space:nowrap}
  #invList input.ic{width:100%;box-sizing:border-box;border:1px solid transparent;
                    background:transparent;font:inherit;padding:3px 4px;border-radius:4px}
  #invList input.ic:hover{border-color:var(--line)}
  #invList input.ic:focus{border-color:var(--blue);background:var(--panel);outline:none}
  .inv-badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:12px;white-space:nowrap}
  .inv-pending{background:#fff3d6;color:#8a6d00}
  .inv-approved{background:#dcf5e3;color:#176b32}
  .inv-rejected{background:#f6dede;color:#8a2020}
  .inv-act button{margin:0 2px}
  #invList tr.done td{opacity:.6}
"""

    JS = """
function invBadge(s){
  var t = s==='approved' ? ['Одобрен','inv-approved'] :
          s==='rejected' ? ['Отклонён','inv-rejected'] : ['На одобрении','inv-pending'];
  return '<span class="inv-badge '+t[1]+'">'+t[0]+'</span>';
}
function invEsc(s){return (s+'').replace(/[&<>"]/g,function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
async function invOpenFile(id){
  try{
    var res=await fetch('/api/invoice_file/'+id,{headers:{'X-Auth':AUTH}});
    if(!res.ok){ alert('Файл недоступен'); return; }
    var url=URL.createObjectURL(await res.blob());
    window.open(url,'_blank');
    setTimeout(function(){URL.revokeObjectURL(url);},60000);
  }catch(e){ alert('Не удалось открыть файл'); }
}
async function invEdit(id,field,el){
  try{ var b={id:id}; b[field]=el.value; await api('/api/invoice_edit',b); }catch(e){}
}
async function invStatus(id,status){
  try{ await api('/api/invoice_status',{id:id,status:status}); loadInvoices(); }catch(e){}
}
async function invDelete(id){
  if(!confirm('Удалить счёт и файл безвозвратно?')) return;
  try{ await api('/api/invoice_delete',{id:id}); loadInvoices(); }catch(e){}
}
function invRow(v){
  var cls = v.status==='pending' ? '' : ' class="done"';
  var warn = v.auto ? '' :
    ' <span class="hint" title="'+invEsc(v.ai_why||'авто-разбор не сработал — заполните вручную')+'">⚠</span>';
  var act = '';
  if(v.status!=='approved') act += '<button onclick="invStatus('+v.id+',\\'approved\\')">✓ Одобрить</button>';
  if(v.status!=='rejected') act += '<button onclick="invStatus('+v.id+',\\'rejected\\')">✗ Отклонить</button>';
  if(v.status!=='pending')  act += '<button onclick="invStatus('+v.id+',\\'pending\\')">↩</button>';
  act += '<button onclick="invDelete('+v.id+')" title="удалить">🗑</button>';
  return '<tr'+cls+'>'
    +'<td><button onclick="invOpenFile('+v.id+')" title="'+invEsc(v.file_name)+'">📄 Открыть</button>'+warn+'</td>'
    +'<td><input class="ic" value="'+invEsc(v.contractor||'')+'" onchange="invEdit('+v.id+',\\'contractor\\',this)"></td>'
    +'<td><input class="ic" value="'+invEsc(v.amount||'')+'" onchange="invEdit('+v.id+',\\'amount\\',this)" style="min-width:90px"></td>'
    +'<td><input class="ic" value="'+invEsc(v.date||'')+'" onchange="invEdit('+v.id+',\\'date\\',this)" style="min-width:90px"></td>'
    +'<td class="hint">'+invEsc(v.added_by||'')+'<br>'+invEsc(v.added_at||'')+'</td>'
    +'<td>'+invBadge(v.status)+'</td>'
    +'<td class="inv-act">'+act+'</td></tr>';
}
async function loadInvoices(){
  var box=document.getElementById('invList'); if(!box) return;
  try{
    var d=await apiGet('/api/invoices'); var items=d.invoices||[];
    var np=items.filter(function(x){return x.status==='pending';}).length;
    var na=items.filter(function(x){return x.status==='approved';}).length;
    var nr=items.filter(function(x){return x.status==='rejected';}).length;
    var cc=document.getElementById('invCounts');
    if(cc) cc.textContent='На одобрении: '+np+' · Одобрено: '+na+' · Отклонено: '+nr;
    var only=document.getElementById('invOnlyPending');
    if(only && only.checked) items=items.filter(function(x){return x.status==='pending';});
    if(!items.length){ box.innerHTML='<div class="hint">Счетов пока нет.</div>'; return; }
    var h='<table><thead><tr><th>Файл</th><th>Контрагент</th><th>Сумма</th>'
      +'<th>Дата</th><th>От кого</th><th>Статус</th><th>Действия</th></tr></thead><tbody>';
    items.forEach(function(v){ h+=invRow(v); });
    box.innerHTML=h+'</tbody></table>';
  }catch(e){ box.innerHTML='<div class="hint">Не удалось загрузить список.</div>'; }
}
window.onShow_invoices = loadInvoices;
"""

    return {
        "id": "invoices",
        "title": "🧾 Счета",
        "section_html": SECTION_HTML,
        "css": CSS,
        "js": JS,
    }
