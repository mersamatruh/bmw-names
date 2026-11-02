"""
Подпроект «Колеса BMW» — модуль для ядра (server.py).

Пока это каркас: пункт меню, пустой раздел и проверочный /api-маршрут.
Функции раздела добавим, когда владелец опишет, что он должен уметь.
Данные модуля — wheels.json в ctx["data_dir"] (создаётся при первой записи).
"""

import os
import json


def register(ctx):
    app = ctx["app"]
    check_auth = ctx["check_auth"]
    lock = ctx["lock"]
    jsonify = ctx["jsonify"]
    request = ctx["request"]
    WHEELS_PATH = os.path.join(ctx["data_dir"], "wheels.json")

    # ---------- Хранилище ----------

    def load_wheels():
        data = {"items": []}
        if os.path.exists(WHEELS_PATH):
            try:
                with open(WHEELS_PATH, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                pass
        data.setdefault("items", [])
        return data

    def save_wheels(data):
        with lock:
            with open(WHEELS_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

    # ---------- API ----------

    @app.get("/api/wheels_state")
    def api_wheels_state():
        if not check_auth(request):
            return jsonify({"error": "Неверный пароль"}), 401
        return jsonify({"ok": True, "count": len(load_wheels()["items"])})

    # ---------- Что отдаём ядру для оболочки ----------

    SECTION_HTML = """
  <h2 style="margin:0 0 4px">🛞 Колеса BMW</h2>
  <p class="hint" style="margin:0 0 14px">Новый подпроект. Раздел в разработке.</p>
  <div id="whMsg" class="hint"></div>
"""

    CSS = """
  #whMsg{max-width:720px}
"""

    JS = """
window['onShow_wheels'] = function(){
  apiGet('/api/wheels_state').then(function(r){
    var el = document.getElementById('whMsg');
    if(el && r && r.ok) el.textContent = 'Записей пока: ' + r.count;
  }).catch(function(){});
};
"""

    return {
        "id": "wheels",
        "title": "🛞 Колеса BMW",
        "section_html": SECTION_HTML,
        "css": CSS,
        "js": JS,
    }
