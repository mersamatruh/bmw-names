"""
Подпроект «Счета» (mod_invoices.py).

Телеграм-бот встроен прямо в модуль: фоновый поток опрашивает Telegram
(long-polling, стандартная библиотека — без aiogram, без установки на сервере).
Кто-то кидает файл в привязанный чат → бот скачивает → сервер смотрит: счёт ли
это, кто контрагент, сумма, дата (ИИ ядра для PDF и фото) → кладёт файл рядом и
добавляет запись в раздел «Счета» со статусом «на одобрении».

Токен бота и владельцы задаются в самом разделе (под паролем сайта); привязка
чата — командой /here в Telegram. Деплой — кнопкой «Обновить с GitHub», сервер
трогать не нужно.

Данные: invoices.json ({seq, items}); настройки бота: invoices_config.json
({token, bot_username, owner_ids, chat_id, offset}); файлы — папка
invoices_files/ (отдаются под X-Auth, браузер тянет через fetch → blob).
"""

import os
import re
import json
import time
import base64
import threading
import datetime
import urllib.request
import urllib.parse
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Moscow")
MAX_AI_BYTES = 12 * 1024 * 1024
IMG_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
TG_API = "https://api.telegram.org"


def register(ctx):
    app = ctx["app"]
    check_auth = ctx["check_auth"]
    lock = ctx["lock"]
    jsonify = ctx["jsonify"]
    request = ctx["request"]
    log = ctx.get("log")
    DATA = ctx["data_dir"]
    INVOICES_PATH = os.path.join(DATA, "invoices.json")
    CONFIG_PATH = os.path.join(DATA, "invoices_config.json")
    FILES_DIR = os.path.join(DATA, "invoices_files")
    os.makedirs(FILES_DIR, exist_ok=True)

    def warn(msg):
        try:
            (log.warning if log else print)(f"invoices: {msg}")
        except Exception:
            pass

    def now_msk():
        return datetime.datetime.now(TZ).strftime("%d.%m.%Y %H:%M")

    # ---------- Хранилище счетов ----------

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

    # ---------- Настройки бота ----------

    def load_config():
        cfg = {}
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, encoding="utf-8") as f:
                    cfg = json.load(f)
            except Exception:
                pass
        cfg.setdefault("owner_ids", [])
        return cfg

    def save_config(cfg):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)

    def config_set(key, value):
        with lock:
            cfg = load_config()
            cfg[key] = value
            save_config(cfg)

    # ---------- ИИ-разбор ----------

    def analyze(data, mime, filename):
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
            "Если это НЕ счёт на оплату — is_invoice=false и коротко why.\n"
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

    def store_invoice(data, mime, fname, by, chat):
        """Разбирает и сохраняет. Возвращает (item|None, res). None — не счёт."""
        res = analyze(data, mime, fname)
        if res.get("is_invoice") is False:
            return None, res
        with lock:
            store = load()
            store["seq"] += 1
            iid = store["seq"]
            safe = re.sub(r"[^0-9A-Za-zА-Яа-яЁё._-]", "_", fname or "file")[:80] or "file"
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
        return item, res

    # ---------- Telegram (long-polling на urllib) ----------

    def tg(token, method, params=None, timeout=35):
        url = f"{TG_API}/bot{token}/{method}"
        data = urllib.parse.urlencode(params or {}).encode()
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())

    def tg_download(token, file_id):
        info = tg(token, "getFile", {"file_id": file_id}, timeout=20)
        fp = info["result"]["file_path"]
        with urllib.request.urlopen(f"{TG_API}/file/bot{token}/{fp}", timeout=60) as r:
            return r.read()

    def reply(token, chat_id, msg, text):
        try:
            tg(token, "sendMessage", {
                "chat_id": chat_id, "text": text,
                "reply_to_message_id": msg.get("message_id")}, timeout=20)
        except Exception as e:
            warn(f"sendMessage: {e}")

    def handle_update(token, msg):
        if not msg:
            return
        cfg = load_config()
        owners = set(cfg.get("owner_ids") or [])
        bound = cfg.get("chat_id")
        chat = msg.get("chat") or {}
        frm = msg.get("from") or {}
        chat_id = chat.get("id")
        uid = frm.get("id")
        text = (msg.get("text") or "").strip()
        is_owner = uid in owners
        in_bound = bound is not None and str(chat_id) == str(bound)

        if text.startswith("/start") or text.startswith("/help"):
            if is_owner or in_bound:
                reply(token, chat_id, msg,
                      "Бот «Счета». Присылайте сюда файлы счетов (PDF, фото, Word/Excel) — "
                      "добавлю их в раздел «Счета» на сайте на одобрение. PDF и фото разбираю "
                      "автоматически (контрагент, сумма, дата).")
            else:
                reply(token, chat_id, msg, f"Доступа нет. Ваш id: {uid}")
            return
        if text.startswith("/here"):
            if is_owner:
                config_set("chat_id", chat_id)
                reply(token, chat_id, msg,
                      "✅ Этот чат привязан. Присылайте счета файлами.")
            else:
                reply(token, chat_id, msg, "Привязать чат может только владелец (задан на сайте).")
            return
        if text.startswith("/id"):
            reply(token, chat_id, msg, f"Ваш id: {uid}\nID этого чата: {chat_id}")
            return

        if not (is_owner or in_bound):
            return  # в чужих чатах/от посторонних молчим

        doc = msg.get("document")
        photos = msg.get("photo")
        if doc:
            file_id = doc["file_id"]
            fname = doc.get("file_name") or f"file_{msg.get('message_id')}"
            mime = doc.get("mime_type") or "application/octet-stream"
        elif photos:
            file_id = photos[-1]["file_id"]
            fname = f"photo_{msg.get('message_id')}.jpg"
            mime = "image/jpeg"
        else:
            return

        try:
            data = tg_download(token, file_id)
        except Exception as e:
            reply(token, chat_id, msg, f"Не смог скачать файл: {e}")
            return
        name = (frm.get("first_name", "") + " " + frm.get("last_name", "")).strip()
        by = name or (f"@{frm.get('username')}" if frm.get("username") else str(uid))
        chat_title = chat.get("title") or chat.get("first_name") or str(chat_id)
        try:
            item, res = store_invoice(data, mime, fname, by, chat_title)
        except Exception as e:
            reply(token, chat_id, msg, f"Ошибка обработки: {e}")
            return
        if item is None:
            why = res.get("why") or ""
            reply(token, chat_id, msg, "Это не похоже на счёт — не добавил." +
                  (f"\n{why}" if why else ""))
            return
        if item.get("auto"):
            reply(token, chat_id, msg,
                  "🧾 Счёт добавлен на одобрение:\n"
                  f"Контрагент: {item.get('contractor') or '—'}\n"
                  f"Сумма: {item.get('amount') or '—'}\n"
                  f"Дата: {item.get('date') or '—'}\n\n"
                  "Проверить и одобрить — в разделе «Счета» на сайте.")
        else:
            reply(token, chat_id, msg,
                  "📄 Файл добавлен в «Счета». Авторазбор не сработал — "
                  "заполните контрагента, сумму и дату на сайте.")

    def poller():
        while True:
            try:
                cfg = load_config()
                token = cfg.get("token")
                if not token:
                    time.sleep(5)
                    continue
                offset = cfg.get("offset")
                if offset is None:  # свежий токен — пропускаем накопившееся
                    try:
                        ups = tg(token, "getUpdates", {"offset": -1, "timeout": 0}, timeout=15)
                        res = ups.get("result") or []
                        offset = (res[-1]["update_id"] + 1) if res else 0
                    except Exception:
                        offset = 0
                    config_set("offset", offset)
                ups = tg(token, "getUpdates", {
                    "offset": offset, "timeout": 25,
                    "allowed_updates": json.dumps(["message"])}, timeout=35)
                if not ups.get("ok"):
                    time.sleep(5)
                    continue
                for u in ups.get("result", []):
                    offset = u["update_id"] + 1
                    try:
                        handle_update(token, u.get("message") or {})
                    except Exception as e:
                        warn(f"handle: {e}")
                    config_set("offset", offset)
            except Exception as e:
                warn(f"poller: {e}")
                time.sleep(5)

    threading.Thread(target=poller, daemon=True, name="invoices-poller").start()

    # ---------- API ----------

    @app.post("/api/invoice_submit")
    def api_invoice_submit():
        """Ручная загрузка файла из браузера (и запас для внешних источников)."""
        if not check_auth(request):
            return jsonify({"error": "Неверный пароль"}), 401
        f = request.files.get("file")
        if f is None:
            return jsonify({"error": "Файл не получен"}), 400
        data = f.read()
        if not data:
            return jsonify({"error": "Пустой файл"}), 400
        item, res = store_invoice(data, f.mimetype or "application/octet-stream",
                                  f.filename or "file",
                                  (request.form.get("by") or "вручную")[:120],
                                  (request.form.get("chat") or "")[:120])
        if item is None:
            return jsonify({"ok": True, "is_invoice": False, "why": res.get("why", "")})
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
        iid, status = d.get("id"), d.get("status")
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
        with lock:
            store = load()
            it = find(store["items"], d.get("id"))
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
            return app.response_class(f.read(),
                                      mimetype=it.get("mime") or "application/octet-stream")

    @app.get("/api/invoice_config")
    def api_invoice_config():
        if not check_auth(request):
            return jsonify({"error": "Неверный пароль"}), 401
        cfg = load_config()
        return jsonify({
            "has_token": bool(cfg.get("token")),
            "bot_username": cfg.get("bot_username") or "",
            "owner_ids": cfg.get("owner_ids") or [],
            "chat_id": cfg.get("chat_id"),
        })

    @app.post("/api/invoice_config")
    def api_invoice_config_set():
        if not check_auth(request):
            return jsonify({"error": "Неверный пароль"}), 401
        d = request.get_json(force=True) or {}
        with lock:
            cfg = load_config()
            if "owner_ids" in d:
                ids = d["owner_ids"]
                if isinstance(ids, str):
                    ids = [x for x in re.split(r"[,\s]+", ids) if x]
                try:
                    cfg["owner_ids"] = sorted({int(x) for x in ids})
                except (TypeError, ValueError):
                    return jsonify({"error": "ID — только числа через запятую"}), 400
            if "token" in d:
                token = str(d["token"] or "").strip()
                if not token:
                    cfg.pop("token", None)
                    cfg.pop("bot_username", None)
                else:
                    try:
                        me = tg(token, "getMe", {}, timeout=15)
                    except Exception as e:
                        return jsonify({"error": f"Не удалось проверить токен: {e}"}), 400
                    if not me.get("ok"):
                        return jsonify({"error": "Telegram отклонил токен"}), 400
                    cfg["token"] = token
                    cfg["bot_username"] = (me.get("result") or {}).get("username", "")
                    cfg["offset"] = None  # пропустить старые апдейты
            save_config(cfg)
        return jsonify({"ok": True, "bot_username": cfg.get("bot_username", ""),
                        "chat_id": cfg.get("chat_id")})

    # ---------- Оболочка ----------

    SECTION_HTML = """
  <h2 style="margin:0 0 4px">🧾 Счета</h2>
  <p class="hint" style="margin:0 0 12px">Счета, присланные боту в рабочий чат, попадают сюда
    на одобрение. PDF и фото разбираются автоматически, поля можно поправить.</p>

  <details id="invCfg" style="max-width:720px;margin-bottom:14px">
    <summary style="cursor:pointer;font-weight:600">⚙ Настройка бота <span id="invBotStatus" class="hint"></span></summary>
    <div style="border:1px solid var(--line);border-radius:8px;padding:12px;margin-top:8px">
      <div class="hint" style="margin-bottom:8px">1) Создайте бота у @BotFather, выключите ему
        приватность (/setprivacy → Disable). 2) Вставьте токен. 3) Укажите свой Telegram ID
        (узнать — команда <code>/id</code> боту). 4) Добавьте бота в чат и отправьте там
        <code>/here</code>.</div>
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:6px">
        <input id="invToken" type="password" placeholder="Токен бота от @BotFather" style="flex:1;min-width:260px;padding:7px 9px;border:1px solid var(--line);border-radius:6px;font:inherit">
        <button onclick="invSaveToken()">Подключить</button>
      </div>
      <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">
        <input id="invOwners" placeholder="ID владельцев через запятую" style="min-width:220px;padding:7px 9px;border:1px solid var(--line);border-radius:6px;font:inherit">
        <button onclick="invSaveOwners()">Сохранить</button>
        <span class="hint" id="invChatStatus"></span>
      </div>
      <div class="hint" id="invCfgMsg" style="margin-top:6px"></div>
    </div>
  </details>

  <div class="bar" style="margin-bottom:10px">
    <input type="file" id="invFile" style="display:none" onchange="invUpload(this)">
    <button onclick="document.getElementById('invFile').click()">⬆ Загрузить счёт вручную</button>
    <label class="hint"><input type="checkbox" id="invOnlyPending" onchange="renderInvoices()"> только на одобрении</label>
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
var INV_ITEMS = [];
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
function renderInvoices(){
  var box=document.getElementById('invList'); if(!box) return;
  var items=INV_ITEMS;
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
}
async function loadInvoices(){
  try{ var d=await apiGet('/api/invoices'); INV_ITEMS=d.invoices||[]; renderInvoices(); }
  catch(e){ var b=document.getElementById('invList'); if(b) b.innerHTML='<div class="hint">Не удалось загрузить список.</div>'; }
}
async function invUpload(el){
  var f=el.files[0]; el.value=''; if(!f) return;
  var msg=document.getElementById('invCounts'); if(msg) msg.textContent='Загружаю…';
  try{
    var fd=new FormData(); fd.append('file',f); fd.append('by','вручную');
    var res=await fetch('/api/invoice_submit',{method:'POST',headers:{'X-Auth':AUTH},body:fd});
    var r=await res.json();
    if(r.error){ alert(r.error); }
    else if(r.is_invoice===false){ alert('Это не похоже на счёт — не добавил.'); }
    loadInvoices();
  }catch(e){ alert('Не удалось загрузить'); loadInvoices(); }
}
async function invLoadConfig(){
  try{
    var c=await apiGet('/api/invoice_config');
    var st=document.getElementById('invBotStatus');
    if(st) st.textContent = c.has_token ? ('— подключён: @'+(c.bot_username||'?')) : '— не подключён';
    var cs=document.getElementById('invChatStatus');
    if(cs) cs.textContent = c.chat_id ? ('Чат привязан (id '+c.chat_id+')') : 'Чат не привязан — отправьте боту /here в нужном чате';
    var ow=document.getElementById('invOwners');
    if(ow && !ow.value) ow.value = (c.owner_ids||[]).join(', ');
  }catch(e){}
}
async function invSaveToken(){
  var el=document.getElementById('invToken'); var m=document.getElementById('invCfgMsg');
  var t=(el.value||'').trim(); if(!t){ if(m) m.textContent='Вставьте токен.'; return; }
  if(m) m.textContent='Проверяю токен…';
  try{
    var r=await api('/api/invoice_config',{token:t});
    if(r.error){ if(m) m.textContent=r.error; return; }
    el.value=''; if(m) m.textContent='Готово: бот @'+(r.bot_username||'')+' подключён.';
    invLoadConfig();
  }catch(e){ if(m) m.textContent='Не удалось сохранить.'; }
}
async function invSaveOwners(){
  var el=document.getElementById('invOwners'); var m=document.getElementById('invCfgMsg');
  try{
    var r=await api('/api/invoice_config',{owner_ids:el.value||''});
    if(m) m.textContent = r.error ? r.error : 'Владельцы сохранены.';
  }catch(e){ if(m) m.textContent='Не удалось сохранить.'; }
}
window.onShow_invoices = function(){ loadInvoices(); invLoadConfig(); };
"""

    return {
        "id": "invoices",
        "title": "🧾 Счета",
        "section_html": SECTION_HTML,
        "css": CSS,
        "js": JS,
    }
