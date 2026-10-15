"""
Подпроект «Бот напоминатель» — модуль для ядра (server.py).

Ядро само находит файлы mod_*.py и вызывает register(ctx). Модуль отдаёт
ядру пункт меню, HTML своего раздела, свой CSS/JS и регистрирует свои
/api-маршруты. Данные — reminders.json рядом с остальными (тот же файл,
что и раньше). Телеграм-бот (paybot) читает эти же данные через API.
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
    REMINDERS_PATH = os.path.join(ctx["data_dir"], "reminders.json")

    # ---------- Хранилище ----------

    def load_reminders():
        data = {"seq": 0, "items": [], "marks": []}
        if os.path.exists(REMINDERS_PATH):
            try:
                with open(REMINDERS_PATH, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                pass
        data.setdefault("seq", 0)
        data.setdefault("items", [])
        data.setdefault("marks", [])  # [{rid, period "YYYY-MM", at, by}]
        return data

    def save_reminders(data):
        with open(REMINDERS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # ---------- API ----------

    @app.get("/api/reminders")
    def api_reminders():
        if not check_auth(request):
            return jsonify({"error": "Неверный пароль"}), 401
        return jsonify({"reminders": load_reminders()["items"]})

    @app.post("/api/reminder_add")
    def api_reminder_add():
        if not check_auth(request):
            return jsonify({"error": "Неверный пароль"}), 401
        d = request.get_json(force=True)
        name = str(d.get("name", "")).strip()
        try:
            start = int(d.get("start"))
            end = int(d.get("end"))
        except (TypeError, ValueError):
            return jsonify({"error": "Неверное окно"}), 400
        if not name:
            return jsonify({"error": "Нужно название"}), 400
        if not (1 <= start <= 31 and 1 <= end <= 31) or start > end:
            return jsonify({"error": "Окно: дни 1–31, начало не позже конца"}), 400
        with lock:
            data = load_reminders()
            data["seq"] += 1
            item = {"id": data["seq"], "name": name, "start": start,
                    "end": end, "active": True}
            data["items"].append(item)
            save_reminders(data)
        return jsonify({"ok": True, "reminder": item})

    @app.post("/api/reminder_archive")
    def api_reminder_archive():
        if not check_auth(request):
            return jsonify({"error": "Неверный пароль"}), 401
        d = request.get_json(force=True)
        rid = d.get("id")
        active = bool(d.get("active"))
        found = False
        with lock:
            data = load_reminders()
            for it in data["items"]:
                if it["id"] == rid:
                    it["active"] = active
                    found = True
                    break
            if found:
                save_reminders(data)
        return jsonify({"ok": found})

    @app.post("/api/reminder_period")
    def api_reminder_period():
        if not check_auth(request):
            return jsonify({"error": "Неверный пароль"}), 401
        d = request.get_json(force=True)
        rid = d.get("id")
        try:
            start = int(d.get("start"))
            end = int(d.get("end"))
        except (TypeError, ValueError):
            return jsonify({"error": "Неверное окно"}), 400
        if not (1 <= start <= 31 and 1 <= end <= 31) or start > end:
            return jsonify({"error": "Окно: дни 1–31, начало не позже конца"}), 400
        found = False
        with lock:
            data = load_reminders()
            for it in data["items"]:
                if it["id"] == rid:
                    it["start"] = start
                    it["end"] = end
                    found = True
                    break
            if found:
                save_reminders(data)
        return jsonify({"ok": found})

    @app.get("/api/marks")
    def api_marks():
        if not check_auth(request):
            return jsonify({"error": "Неверный пароль"}), 401
        period = request.args.get("period", "")
        marks = load_reminders()["marks"]
        paid = [m["rid"] for m in marks if m.get("period") == period]
        return jsonify({"period": period, "paid": paid})

    @app.post("/api/mark")
    def api_mark():
        if not check_auth(request):
            return jsonify({"error": "Неверный пароль"}), 401
        d = request.get_json(force=True)
        try:
            rid = int(d.get("rid"))
        except (TypeError, ValueError):
            return jsonify({"error": "Нужен rid"}), 400
        period = str(d.get("period", "")).strip()
        by = str(d.get("by", "")).strip()
        if not re.fullmatch(r"\d{4}-\d{2}", period):
            return jsonify({"error": "Неверный период"}), 400
        with lock:
            data = load_reminders()
            already = any(m["rid"] == rid and m["period"] == period
                          for m in data["marks"])
            if not already:
                data["marks"].append({
                    "rid": rid, "period": period,
                    "at": datetime.datetime.now().isoformat(timespec="seconds"),
                    "by": by,
                })
                save_reminders(data)
        return jsonify({"ok": True, "already": already})

    @app.post("/api/unmark")
    def api_unmark():
        if not check_auth(request):
            return jsonify({"error": "Неверный пароль"}), 401
        d = request.get_json(force=True)
        try:
            rid = int(d.get("rid"))
        except (TypeError, ValueError):
            return jsonify({"error": "Нужен rid"}), 400
        period = str(d.get("period", "")).strip()
        with lock:
            data = load_reminders()
            before = len(data["marks"])
            data["marks"] = [m for m in data["marks"]
                             if not (m["rid"] == rid and m["period"] == period)]
            removed = before - len(data["marks"])
            if removed:
                save_reminders(data)
        return jsonify({"ok": bool(removed)})

    # ---------- Что отдаём ядру для оболочки ----------

    SECTION_HTML = """
  <h2 style="margin:0 0 4px">🔔 Бот напоминатель</h2>
  <p class="hint" style="margin:0 0 14px">Регулярные платежи. Укажи название и окно дней
    месяца, когда напоминать, — бот будет напоминать каждое утро, пока платёж не отмечен.</p>
  <div class="bar">
    <input id="remName" placeholder="Название — напр. Электроэнергия"
           onkeydown="if(event.key==='Enter')addReminder()">
    <input id="remPeriod" placeholder="Окно: 1-14"
           onkeydown="if(event.key==='Enter')addReminder()">
    <button class="primary" onclick="addReminder()">Добавить напоминание</button>
    <span class="hint">Окно — дни месяца. Пример: <b>1-14</b>. Один день — просто <b>10</b>.</span>
  </div>
  <div id="remMsg" class="hint" style="margin-bottom:10px;color:var(--err)"></div>
  <div id="remList"></div>
"""

    CSS = """
  #remName{flex:1;min-width:220px;padding:8px 10px;border:1px solid var(--line);
           border-radius:6px;font:inherit}
  #remPeriod{width:130px;padding:8px 10px;border:1px solid var(--line);
             border-radius:6px;font:inherit}
  .remcard{background:var(--panel);border:1px solid var(--line);border-radius:8px;
           padding:11px 14px;margin-bottom:8px;display:flex;align-items:center;
           gap:12px;max-width:720px}
  .remcard .nm{font-weight:600}
  .remcard .pd{color:var(--mut);font-size:13px}
  .remcard .sp{flex:1}
  .remcard.arch{opacity:.65}
"""

    JS = """
function remEsc(s){return (s+'').replace(/[&<>"]/g,function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
function remPeriodStr(s,e){return s===e?(s+' числа'):('с '+s+' по '+e+' число');}
function parseRemPeriod(t){
  t=(t||'').replace(/[—–]/g,'-').trim();
  var parts = t.indexOf('-')>=0 ? t.split('-') : t.split(/\\s+/);
  var nums=[];
  for(var i=0;i<parts.length;i++){var x=parts[i].trim();
    if(x!==''){ if(!/^\\d+$/.test(x)) return null; nums.push(parseInt(x,10)); }}
  var s,e;
  if(nums.length===1){s=e=nums[0];}
  else if(nums.length===2){s=nums[0];e=nums[1];}
  else return null;
  if(!(s>=1&&s<=31&&e>=1&&e<=31)||s>e) return null;
  return [s,e];
}
async function loadReminders(){
  var box=document.getElementById('remList'); if(!box) return;
  box.innerHTML='<div class="hint">Загрузка…</div>';
  try{ var d=await apiGet('/api/reminders'); renderReminders(d.reminders||[]); }
  catch(e){ box.innerHTML='<div class="hint">Не удалось загрузить список.</div>'; }
}
function renderReminders(items){
  var box=document.getElementById('remList'); if(!box) return;
  var act=items.filter(function(r){return r.active;});
  var arch=items.filter(function(r){return !r.active;});
  var html='';
  if(!act.length && !arch.length) html='<div class="hint">Пока нет ни одного напоминания.</div>';
  act.forEach(function(r){
    html+='<div class="remcard"><span class="nm">🔔 '+remEsc(r.name)+'</span>'
      +'<span class="pd">'+remPeriodStr(r.start,r.end)+'</span><span class="sp"></span>'
      +'<button onclick="archiveReminder('+r.id+',false)">📦 В архив</button></div>';
  });
  if(arch.length){
    html+='<div class="hint" style="margin:14px 0 6px">Архив</div>';
    arch.forEach(function(r){
      html+='<div class="remcard arch"><span class="nm">'+remEsc(r.name)+'</span>'
        +'<span class="pd">'+remPeriodStr(r.start,r.end)+'</span><span class="sp"></span>'
        +'<button onclick="archiveReminder('+r.id+',true)">↩️ Вернуть</button></div>';
    });
  }
  box.innerHTML=html;
}
async function addReminder(){
  var nm=document.getElementById('remName'), pd=document.getElementById('remPeriod'),
      msg=document.getElementById('remMsg');
  var name=(nm.value||'').trim();
  var per=parseRemPeriod(pd.value);
  if(!name){ msg.textContent='Впишите название.'; return; }
  if(!per){ msg.textContent='Окно неверное. Пример: 1-14 или одно число 10 (дни 1–31).'; return; }
  msg.textContent='';
  try{
    var r=await api('/api/reminder_add',{name:name,start:per[0],end:per[1]});
    if(r && r.error){ msg.textContent=r.error; return; }
    nm.value=''; pd.value=''; loadReminders();
  }catch(e){ msg.textContent='Не удалось сохранить.'; }
}
async function archiveReminder(id, active){
  try{ await api('/api/reminder_archive',{id:id,active:active}); loadReminders(); }
  catch(e){}
}
/* ядро вызовет это при открытии раздела */
window.onShow_bot = loadReminders;
"""

    return {
        "id": "bot",
        "title": "🔔 Бот напоминатель",
        "section_html": SECTION_HTML,
        "css": CSS,
        "js": JS,
    }
