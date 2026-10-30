"""
Подпроект «Продажи BMW» — модуль для ядра (server.py).

Сборщик объявлений auto.ru по BMW X3 G01 и X5 G05 (с пробегом) и
«цифровой отпечаток» каждой машины, чтобы узнавать её при перевыкладке:
  • «корзина» — модель/кузов, год, тип двигателя, объём, мощность, коробка;
  • комплектация (список опций auto.ru) — сильный признак внутри корзины;
  • перцептивные хэши фото (dHash, 64 бита) — ловят перевыкладку тех же снимков;
  • спорные пары проверяет ИИ по сохранённым фото (по 2 на объявление).
Пробег и цена каждой машины пишутся в отдельную историю (видны скрутки).

auto.ru не пускает зарубежные адреса (Hetzner → страница входа), поэтому
запросы идут через прокси tinyproxy на российском посреднике (Timeweb).
Данные: bmw_settings.json, bmw_offers.json, bmw_cars.json, bmw_history.json,
папка bmw_photos/ — всё в ctx["data_dir"].
"""

import os
import re
import io
import json
import time
import base64
import random
import datetime
import threading
import itertools
import concurrent.futures
import http.cookiejar
import urllib.parse
import urllib.request


DEFAULT_SETTINGS = {
    "proxy": "http://201.24.54.93:8888",
    "pause": 6,          # секунд между страницами auto.ru
    "auto_hours": 0,     # автосбор раз в N часов; 0 — только по кнопке
    "photos_keep": 2,    # сколько фото объявления сохранять файлами
    "targets": [
        {"model": "X3", "code": "G01", "gens": {}},
        {"model": "X5", "code": "G05", "gens": {}},
    ],
    "last_run": {},
}

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

PHOTO_MATCH_BITS = 6      # хэши ближе этого порога — один и тот же снимок
PHOTO_MATCH_SAME = 2      # столько общих снимков — та же машина без вопросов
EQUIP_SIMILAR = 0.97      # сходство комплектаций для «возможно та же»


def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def today():
    return datetime.date.today().isoformat()


# ---------- Разбор страниц auto.ru ----------

RIS_RE = re.compile(r'<script type="application/json" class="RIS" '
                    r'data-webpack-chunk-id="(\d+)"[^>]*>(.*?)</script>', re.S)


def parse_state(html):
    """Состояние страницы auto.ru: JSON, разрезанный на несколько <script class="RIS">."""
    chunks = {k: v for k, v in RIS_RE.findall(html)}
    if not chunks:
        return None
    keys = sorted(chunks, key=int)
    orders = [keys] + [list(p) for p in itertools.permutations(keys) if list(p) != keys]
    for order in orders[:24]:
        try:
            return json.loads("".join(chunks[k] for k in order))
        except ValueError:
            continue
    return None


def gens_from_state(state, code):
    """{id: название} поколений модели, в названии которых есть код кузова (G05)."""
    out = {}
    for lvl in (state.get("breadcrumbsPublicApi") or {}).get("data") or []:
        if lvl.get("meta_level") != "GENERATION_LEVEL":
            continue
        for e in lvl.get("entities") or []:
            if code.upper() in str(e.get("name", "")).upper():
                out[str(e.get("id"))] = e.get("name")
    return out


def photo_url(sizes, want):
    u = (sizes or {}).get(want) or ""
    return ("https:" + u) if u.startswith("//") else u


def offer_record(o, target):
    """Объявление из состояния страницы → компактная запись для базы."""
    vi = o.get("vehicle_info") or {}
    tp = vi.get("tech_param") or {}
    docs = o.get("documents") or {}
    st = o.get("state") or {}
    seller = o.get("seller") or {}
    region = (((seller.get("location") or {}).get("region_info")) or {}).get("name")
    ai = o.get("additional_info") or {}
    equip = sorted(k for k, v in (vi.get("equipment") or {}).items() if v)
    photos = []
    for im in st.get("image_urls") or []:
        sz = im.get("sizes") or {}
        photos.append({
            "cls": im.get("photo_class") or "",
            "thumb": photo_url(sz, "320x240") or photo_url(sz, "small"),
            "big": photo_url(sz, "1200x900n") or photo_url(sz, "1200x900"),
        })
    try:
        created = datetime.datetime.fromtimestamp(
            int(ai.get("creation_date")) / 1000).date().isoformat()
    except (TypeError, ValueError):
        created = ""
    return {
        "id": o.get("saleId") or f"{o.get('id')}-{o.get('hash')}",
        "url": o.get("url") or "",
        "title": o.get("title") or "",
        "model": target["model"], "code": target["code"],
        "gen": (vi.get("super_gen") or {}).get("name") or "",
        "year": docs.get("year"),
        "engine": tp.get("engine_type") or "",
        "disp": tp.get("displacement"),
        "power": tp.get("power"),
        "trans": tp.get("transmission") or "",
        "tech": tp.get("human_name") or "",
        "compl": (vi.get("complectation") or {}).get("name") or "",
        "equip": equip,
        "km": st.get("mileage"),
        "price": (o.get("price_info") or {}).get("RUR") or (o.get("price_info") or {}).get("price"),
        "region": region or "",
        "seller_type": o.get("seller_type") or "",
        "seller": seller.get("name") or "",
        "owners": docs.get("owners_number"),
        "images_count": st.get("images_count"),
        "created": created,
        "photos": photos,
    }


def basket_key(r):
    return "|".join(str(r.get(k) or "") for k in
                    ("model", "code", "year", "engine", "disp", "power", "trans"))


def hamming(a, b):
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def dhash(data):
    """Перцептивный хэш картинки: 9×8 серых точек, сравниваем соседей → 64 бита."""
    from PIL import Image
    im = Image.open(io.BytesIO(data)).convert("L").resize((9, 8), Image.LANCZOS)
    px = list(im.getdata())
    bits = 0
    for y in range(8):
        for x in range(8):
            bits = (bits << 1) | (1 if px[y * 9 + x] > px[y * 9 + x + 1] else 0)
    return f"{bits:016x}"


def have_pillow():
    try:
        import PIL  # noqa: F401
        return True
    except Exception:
        return False


def register(ctx):
    app = ctx["app"]
    check_auth = ctx["check_auth"]
    jsonify = ctx["jsonify"]
    request = ctx["request"]
    DATA = ctx["data_dir"]
    SETTINGS_PATH = os.path.join(DATA, "bmw_settings.json")
    OFFERS_PATH = os.path.join(DATA, "bmw_offers.json")
    CARS_PATH = os.path.join(DATA, "bmw_cars.json")
    HISTORY_PATH = os.path.join(DATA, "bmw_history.json")
    PHOTOS_DIR = os.path.join(DATA, "bmw_photos")
    LAST_HTML = os.path.join(DATA, "bmw_last_page.html")

    # свой замок: сборщик работает долго и не должен держать общий замок ядра
    mlock = threading.RLock()

    def load_json(p, default):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default

    def save_json(p, data):
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, p)

    def deny():
        return jsonify({"error": "Неверный пароль"}), 401

    settings = dict(DEFAULT_SETTINGS)
    settings.update(load_json(SETTINGS_PATH, {}))
    offers = load_json(OFFERS_PATH, {})           # id объявления → запись
    cars = load_json(CARS_PATH, {})
    cars.setdefault("seq", 0)
    cars.setdefault("cars", {})                   # cid → {first_seen, note}
    cars.setdefault("pairs", [])                  # кандидаты «возможно та же машина»
    history = load_json(HISTORY_PATH, [])         # [{oid, d, km, price}]

    def save_all():
        save_json(SETTINGS_PATH, settings)
        save_json(OFFERS_PATH, offers)
        save_json(CARS_PATH, cars)
        save_json(HISTORY_PATH, history)

    RUN = {"running": False, "stop": False, "stage": "", "log": []}

    def log(msg):
        line = datetime.datetime.now().strftime("%H:%M:%S ") + msg
        RUN["log"] = (RUN["log"] + [line])[-60:]
        print("[bmw] " + msg)

    # ---------- Сеть (через прокси на посреднике) ----------

    def make_opener():
        handlers = [urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())]
        proxy = (settings.get("proxy") or "").strip()
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        return urllib.request.build_opener(*handlers)

    def get(opener, url, binary=False, timeout=40):
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": ("image/avif,image/webp,*/*" if binary else
                       "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
        })
        with opener.open(req, timeout=timeout) as r:
            data = r.read()
            final = r.geturl()
        if binary:
            return data
        if "captcha" in final or "auth.auto.ru" in final:
            raise RuntimeError("auto.ru перенаправил на капчу/вход: " + final[:120])
        html = data.decode("utf-8", "replace")
        try:
            with open(LAST_HTML, "w", encoding="utf-8") as f:
                f.write(html)
        except Exception:
            pass
        state = parse_state(html)
        if state is None:
            if "captcha" in html.lower():
                raise RuntimeError("auto.ru показал капчу «я не робот»")
            raise RuntimeError("не нашёл данных на странице (формат auto.ru поменялся?)")
        return state

    def listing_url(target, page):
        q = [("catalog_filter", f"mark=BMW,model={target['model']},generation={g}")
             for g in sorted(target.get("gens") or {})]
        q += [("sort", "cr_date-desc"), ("page", str(page))]
        return "https://auto.ru/cars/used/?" + urllib.parse.urlencode(q)

    # ---------- Отпечаток и сопоставление ----------

    # Индексы для быстрого поиска: хэш делим на 8 байтов — при отличии не больше
    # чем в 6 битах хотя бы один байт совпадёт точно (принцип Дирихле).
    IDX = {"built": False, "bands": [], "basket": {}}

    def idx_add(o):
        for h in o.get("hashes") or []:
            if not h:
                continue
            for i in range(8):
                IDX["bands"][i].setdefault(h[i * 2:i * 2 + 2], []).append((h, o["id"]))
        IDX["basket"].setdefault(basket_key(o), []).append(o["id"])

    def idx_build():
        IDX["bands"] = [dict() for _ in range(8)]
        IDX["basket"] = {}
        for o in offers.values():
            idx_add(o)
        IDX["built"] = True

    def similar_photos(h):
        """id объявлений, у которых есть снимок с хэшем близким к h."""
        out = set()
        for i in range(8):
            for hh, oid in IDX["bands"][i].get(h[i * 2:i * 2 + 2], ()):
                if oid not in out and hamming(h, hh) <= PHOTO_MATCH_BITS:
                    out.add(oid)
        return out

    def equip_sim(a, b):
        a, b = set(a or []), set(b or [])
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)

    def new_car():
        cars["seq"] += 1
        cid = str(cars["seq"])
        cars["cars"][cid] = {"first_seen": today()}
        return cid

    def has_pair(a, b):
        for p in cars["pairs"]:
            if {p["a"], p["b"]} == {a, b}:
                return True
        return False

    def add_pair(new_oid, old_oid, reason, score):
        if has_pair(new_oid, old_oid):
            return
        cars["pairs"].append({"pid": f"{new_oid}~{old_oid}", "a": new_oid, "b": old_oid,
                              "reason": reason, "score": round(score, 3),
                              "status": "new", "at": now_iso()})

    def match_offer(rec):
        """Новое объявление → чья это машина. Возвращает (cid, причина)."""
        if not IDX["built"]:
            idx_build()
        hs = [h for h in rec.get("hashes") or [] if h]
        key = basket_key(rec)
        # 1) фото: два и больше общих снимков — та же машина
        per_car = {}     # cid → (сколько снимков rec нашлось у машины, пример объявления)
        for h in hs:
            cids = {}
            for oid in similar_photos(h):
                o = offers.get(oid)
                if o and oid != rec["id"] and o.get("cid"):
                    cids[o["cid"]] = oid
            if len(cids) > 3:
                continue  # снимок есть у многих машин — заставка/баннер дилера, не примета
            for c, oid in cids.items():
                n, _ = per_car.get(c, (0, oid))
                per_car[c] = (n + 1, oid)
        best = max(per_car.items(), key=lambda kv: kv[1][0], default=(None, (0, None)))
        if best[1][0] >= PHOTO_MATCH_SAME:
            return best[0], f"фото: {best[1][0]} общих снимка с {best[1][1]}"
        cid = new_car()
        # 2) кандидаты: один общий снимок, или одна корзина + почти та же комплектация
        paired = set()
        for c, (n, oid) in per_car.items():
            add_pair(rec["id"], oid, "фото: 1 общий снимок", 0.5)
            paired.add(c)
        for oid in IDX["basket"].get(key, []):
            o = offers.get(oid)
            if not o or oid == rec["id"] or not o.get("cid") or o["cid"] in (cid, *paired):
                continue
            sim = equip_sim(rec.get("equip"), o.get("equip"))
            if sim >= EQUIP_SIMILAR and len(rec.get("equip") or []) >= 10:
                km_ok = not (rec.get("km") and o.get("km") and rec["km"] + 1000 < o["km"])
                why = f"корзина и комплектация совпадают на {round(sim * 100)}%"
                if not km_ok:
                    why += ", но пробег меньше прежнего"
                add_pair(rec["id"], o["id"], why, sim)
                paired.add(o["cid"])
        return cid, "новая машина"

    def note_history(rec):
        """Пишем пробег/цену, если они новые для этого объявления."""
        last = None
        for h in reversed(history):
            if h["oid"] == rec["id"]:
                last = h
                break
        if last and last.get("km") == rec.get("km") and last.get("price") == rec.get("price"):
            return
        history.append({"oid": rec["id"], "d": today(),
                        "km": rec.get("km"), "price": rec.get("price")})

    def process_photos(opener, rec):
        """Хэши всех фото объявления из выдачи + первые N снимков файлами."""
        if rec.get("hashes") is not None or not have_pillow():
            return
        if RUN["stop"]:
            return
        photos = rec.get("photos") or []
        keep = int(settings.get("photos_keep") or 0)

        def one_hash(ph):
            try:
                return dhash(get(opener, ph["thumb"], binary=True))
            except Exception:
                return ""

        def one_save(i):
            try:
                fn = f"{rec['id']}_{i + 1}.jpg"
                data = get(opener, photos[i]["big"], binary=True)
                os.makedirs(PHOTOS_DIR, exist_ok=True)
                with open(os.path.join(PHOTOS_DIR, fn), "wb") as f:
                    f.write(data)
                return fn
            except Exception:
                return None

        # фото лежат на CDN auto.ru — качаем в 4 потока, без пауз
        with concurrent.futures.ThreadPoolExecutor(4) as ex:
            hashes = list(ex.map(one_hash, photos))
            saved = list(ex.map(one_save, [i for i in range(min(keep, len(photos)))
                                           if photos[i].get("big")]))
        rec["hashes"] = hashes
        rec["saved"] = [fn for fn in saved if fn]

    # ---------- Сборщик ----------

    def run_target(opener, target, run_stamp):
        if not target.get("gens"):
            RUN["stage"] = f"{target['model']} {target['code']}: ищу поколения"
            st = get(opener, f"https://auto.ru/cars/bmw/{target['model'].lower()}/used/")
            target["gens"] = gens_from_state(st, target["code"])
            log(f"{target['model']} {target['code']}: поколения {target['gens']}")
            if not target["gens"]:
                raise RuntimeError(f"не нашёл поколений {target['code']} у BMW {target['model']}")
            time.sleep(settings["pause"])
        page, pages, seen, new = 1, 1, 0, 0
        complete = False
        while page <= pages and not RUN["stop"]:
            RUN["stage"] = f"{target['model']} {target['code']}: страница {page} из {pages}"
            st = get(opener, listing_url(target, page))
            data = (st.get("listing") or {}).get("data") or {}
            pg = data.get("pagination") or {}
            pages = min(int(pg.get("total_page_count") or 1), 99)
            items = data.get("offers") or []
            for o in items:
                if RUN["stop"]:
                    break
                if o.get("section") != "used":
                    continue
                rec = offer_record(o, target)
                with mlock:
                    old = offers.get(rec["id"])
                    if old:
                        # знакомое объявление: обновляем цифры, фото уже обработаны
                        for k in ("km", "price", "region", "title", "url", "images_count", "seller"):
                            old[k] = rec[k]
                        old["last_seen"] = today()
                        old["run"] = run_stamp
                        if old.get("status") != "active":
                            old["status"] = "active"
                            old.pop("gone", None)
                        if not old.get("hashes") and not old.get("saved"):
                            old["photos"] = rec["photos"]
                        note_history(old)
                        cur = old
                    else:
                        cur = None
                if cur is not None:
                    if cur.get("hashes") is None:
                        process_photos(opener, cur)
                    seen += 1
                    continue
                process_photos(opener, rec)
                with mlock:
                    rec.update({"first_seen": today(), "last_seen": today(),
                                "status": "active", "run": run_stamp})
                    offers[rec["id"]] = rec
                    rec["cid"], rec["why"] = match_offer(rec)
                    idx_add(rec)
                    note_history(rec)
                seen += 1
                new += 1
            with mlock:
                save_all()
            if page == pages:
                complete = True
            page += 1
            if page <= pages:
                time.sleep(settings["pause"] + random.uniform(0, 3))
        # кто не встретился при полном проходе — снят с продажи
        gone = 0
        if complete and not RUN["stop"]:
            with mlock:
                for o in offers.values():
                    if (o.get("model") == target["model"] and o.get("code") == target["code"]
                            and o.get("status") == "active" and o.get("run") != run_stamp):
                        o["status"] = "gone"
                        o["gone"] = today()
                        gone += 1
        log(f"{target['model']} {target['code']}: объявлений {seen}, новых {new}, снято {gone}"
            + ("" if complete else " (проход не полный)"))
        return {"seen": seen, "new": new, "gone": gone, "complete": complete}

    def run_all():
        RUN.update({"running": True, "stop": False, "log": [], "stage": "старт"})
        with mlock:
            idx_build()
        run_stamp = now_iso()
        summary = {"started": run_stamp, "targets": {}, "error": ""}
        try:
            opener = make_opener()
            if not have_pillow():
                log("Pillow не установлен — хэши фото пропускаю")
            for t in settings["targets"]:
                if RUN["stop"]:
                    break
                try:
                    summary["targets"][f"{t['model']} {t['code']}"] = run_target(opener, t, run_stamp)
                except Exception as e:
                    log(f"{t['model']} {t['code']}: ошибка — {e}")
                    summary["error"] = str(e)
                time.sleep(settings["pause"])
        finally:
            summary["finished"] = now_iso()
            summary["stopped"] = RUN["stop"]
            with mlock:
                settings["last_run"] = summary
                save_all()
            RUN.update({"running": False, "stage": ""})

    def start_run():
        if RUN["running"]:
            return False
        threading.Thread(target=run_all, daemon=True).start()
        return True

    def scheduler():
        while True:
            time.sleep(60)
            try:
                hrs = float(settings.get("auto_hours") or 0)
                if hrs <= 0 or RUN["running"]:
                    continue
                last = (settings.get("last_run") or {}).get("started")
                due = True
                if last:
                    t = datetime.datetime.fromisoformat(last)
                    due = (datetime.datetime.now() - t).total_seconds() >= hrs * 3600
                if due:
                    start_run()
            except Exception as e:
                print("[bmw] планировщик:", e)

    threading.Thread(target=scheduler, daemon=True).start()

    # ---------- Сводки для страницы ----------

    def car_rows():
        by = {}
        for o in offers.values():
            if o.get("cid"):
                by.setdefault(o["cid"], []).append(o)
        hist_by = {}
        for h in history:
            hist_by.setdefault(h["oid"], []).append(h)
        rows = []
        for cid, lst in by.items():
            lst.sort(key=lambda o: (o.get("last_seen") or "", o.get("first_seen") or ""))
            cur = lst[-1]
            hs = sorted((h for o in lst for h in hist_by.get(o["id"], [])),
                        key=lambda h: h["d"])
            kms = [h["km"] for h in hs if h.get("km")]
            drop = any(kms[i] + 500 < max(kms[:i]) for i in range(1, len(kms)))
            prices = [h["price"] for h in hs if h.get("price")]
            rows.append({
                "cid": cid, "model": cur["model"], "code": cur["code"],
                "gen": cur.get("gen"), "year": cur.get("year"), "tech": cur.get("tech"),
                "trans": cur.get("trans"), "compl": cur.get("compl"),
                "km": cur.get("km"), "price": cur.get("price"),
                "price0": prices[0] if prices else None,
                "region": cur.get("region"), "url": cur.get("url"),
                "thumb": ((cur.get("photos") or [{}])[0]).get("thumb", ""),
                "offers": len(lst),
                "active": any(o.get("status") == "active" for o in lst),
                "first_seen": min(o.get("first_seen") or "" for o in lst),
                "last_seen": cur.get("last_seen"),
                "km_drop": drop, "seller_type": cur.get("seller_type"),
            })
        return rows

    def offer_brief(o):
        if not o:
            return None
        return {k: o.get(k) for k in ("id", "cid", "url", "title", "gen", "year", "tech", "trans",
                                      "compl", "km", "price", "region", "seller", "seller_type",
                                      "status", "first_seen", "last_seen", "gone", "created",
                                      "saved", "why")} | {
            "thumbs": [p.get("thumb") for p in (o.get("photos") or [])][:6],
            "equip_n": len(o.get("equip") or [])}

    # ---------- API ----------

    @app.get("/api/bmw_state")
    def api_bmw_state():
        if not check_auth(request):
            return deny()
        with mlock:
            act = sum(1 for o in offers.values() if o.get("status") == "active")
            pend = sum(1 for p in cars["pairs"] if p["status"] == "new")
            ncars = len({o.get("cid") for o in offers.values() if o.get("cid")})
            return jsonify({
                "settings": {k: settings.get(k) for k in
                             ("proxy", "pause", "auto_hours", "photos_keep", "targets", "last_run")},
                "run": {k: RUN[k] for k in ("running", "stage", "log")},
                "counts": {"offers": len(offers), "active": act, "cars": ncars,
                           "pairs": pend, "history": len(history)},
                "pillow": have_pillow(),
            })

    @app.post("/api/bmw_settings")
    def api_bmw_settings():
        if not check_auth(request):
            return deny()
        d = request.get_json(force=True) or {}
        with mlock:
            if "proxy" in d:
                settings["proxy"] = str(d["proxy"] or "").strip()
            for k, lo, hi in (("pause", 2, 120), ("auto_hours", 0, 720), ("photos_keep", 0, 10)):
                if k in d:
                    try:
                        settings[k] = max(lo, min(hi, float(d[k])))
                        if k == "photos_keep":
                            settings[k] = int(settings[k])
                    except (TypeError, ValueError):
                        return jsonify({"error": f"Неверное число: {k}"}), 400
            save_json(SETTINGS_PATH, settings)
        return jsonify({"ok": True})

    @app.post("/api/bmw_run")
    def api_bmw_run():
        if not check_auth(request):
            return deny()
        return jsonify({"ok": start_run(), "running": True})

    @app.post("/api/bmw_stop")
    def api_bmw_stop():
        if not check_auth(request):
            return deny()
        RUN["stop"] = True
        return jsonify({"ok": True})

    @app.get("/api/bmw_cars")
    def api_bmw_cars():
        if not check_auth(request):
            return deny()
        with mlock:
            return jsonify({"cars": car_rows()})

    @app.get("/api/bmw_car")
    def api_bmw_car():
        if not check_auth(request):
            return deny()
        cid = request.args.get("cid", "")
        with mlock:
            lst = [offer_brief(o) for o in offers.values() if o.get("cid") == cid]
            ids = {o["id"] for o in lst}
            hs = sorted((h for h in history if h["oid"] in ids), key=lambda h: h["d"])
        lst.sort(key=lambda o: o.get("first_seen") or "")
        return jsonify({"cid": cid, "offers": lst, "history": hs})

    @app.get("/api/bmw_history")
    def api_bmw_history():
        if not check_auth(request):
            return deny()
        with mlock:
            out = []
            for h in history:
                o = offers.get(h["oid"]) or {}
                out.append(dict(h, cid=o.get("cid"), title=o.get("title"),
                                year=o.get("year"), url=o.get("url")))
        return jsonify({"history": out})

    @app.get("/api/bmw_pairs")
    def api_bmw_pairs():
        if not check_auth(request):
            return deny()
        with mlock:
            out = []
            for p in cars["pairs"]:
                if p["status"] != "new":
                    continue
                a, b = offers.get(p["a"]), offers.get(p["b"])
                if not a or not b or a.get("cid") == b.get("cid"):
                    continue
                out.append(dict(p, A=offer_brief(a), B=offer_brief(b)))
        return jsonify({"pairs": out})

    def merge_cars(keep, drop):
        for o in offers.values():
            if o.get("cid") == drop:
                o["cid"] = keep
        if drop in cars["cars"]:
            cars["cars"][keep]["first_seen"] = min(
                cars["cars"].get(keep, {}).get("first_seen") or today(),
                cars["cars"][drop].get("first_seen") or today())
            del cars["cars"][drop]

    @app.post("/api/bmw_pair_decide")
    def api_bmw_pair_decide():
        if not check_auth(request):
            return deny()
        d = request.get_json(force=True) or {}
        pid, same = d.get("pid"), bool(d.get("same"))
        with mlock:
            p = next((x for x in cars["pairs"] if x["pid"] == pid), None)
            if not p:
                return jsonify({"error": "Пара не найдена"}), 404
            p["status"] = "same" if same else "diff"
            p["decided"] = now_iso()
            a, b = offers.get(p["a"]), offers.get(p["b"])
            if same and a and b and a.get("cid") != b.get("cid"):
                cars["cars"].setdefault(b["cid"], {"first_seen": today()})
                merge_cars(b["cid"], a["cid"])
            save_all()
        return jsonify({"ok": True})

    def photo_blocks(o):
        out = []
        for fn in (o.get("saved") or [])[:2]:
            try:
                with open(os.path.join(PHOTOS_DIR, fn), "rb") as f:
                    out.append({"type": "image", "source": {
                        "type": "base64", "media_type": "image/jpeg",
                        "data": base64.b64encode(f.read()).decode()}})
            except Exception:
                pass
        return out

    def describe(o):
        return (f"{o.get('title')}, {o.get('year')} г., {o.get('tech')}, "
                f"комплектация «{o.get('compl')}», пробег {o.get('km')} км, "
                f"цена {o.get('price')} ₽, регион {o.get('region')}, "
                f"продавец {o.get('seller') or '?'} ({o.get('seller_type')}), "
                f"на сайте с {o.get('created') or o.get('first_seen')}, "
                f"опций {len(o.get('equip') or [])}")

    @app.post("/api/bmw_pair_ai")
    def api_bmw_pair_ai():
        """ИИ смотрит по 2 сохранённых фото каждого объявления и решает, одна ли машина."""
        if not check_auth(request):
            return deny()
        client, model = ctx.get("client"), ctx.get("model")
        if client is None or not model:
            return jsonify({"error": "ИИ недоступен на сервере"}), 500
        pid = (request.get_json(force=True) or {}).get("pid")
        with mlock:
            p = next((x for x in cars["pairs"] if x["pid"] == pid), None)
            a = offers.get(p["a"]) if p else None
            b = offers.get(p["b"]) if p else None
        if not a or not b:
            return jsonify({"error": "Пара не найдена"}), 404
        pa, pb = photo_blocks(a), photo_blocks(b)
        if not pa or not pb:
            return jsonify({"error": "Нет сохранённых фото у одного из объявлений"}), 400
        only_a = sorted(set(a.get("equip") or []) - set(b.get("equip") or []))
        only_b = sorted(set(b.get("equip") or []) - set(a.get("equip") or []))
        content = [{"type": "text", "text": "ОБЪЯВЛЕНИЕ A: " + describe(a)}] + pa + [
            {"type": "text", "text": "ОБЪЯВЛЕНИЕ B: " + describe(b)}] + pb + [
            {"type": "text", "text": (
                f"Опции только у A: {', '.join(only_a) or 'нет'}. "
                f"Опции только у B: {', '.join(only_b) or 'нет'}.\n\n"
                "Это два объявления auto.ru. Определи, один и тот же это автомобиль "
                "(перевыложен, другой продавец, новые фото) или разные. Ищи приметы: "
                "диски, плёнка, тонировка, сколы, вмятины, обвес, номер, фон и место съёмки, "
                "цвет салона. Одинаковый цвет и модель сами по себе ничего не доказывают. "
                "Учти: пробег у одной машины не уменьшается (если уменьшился — возможна "
                "скрутка). Ответь ТОЛЬКО JSON: {\"same\": \"yes\"|\"no\"|\"unsure\", "
                "\"confidence\": 0-100, \"why\": \"коротко по-русски, какие приметы\"}")}]
        try:
            msg = client.messages.create(model=model, max_tokens=1000,
                                         messages=[{"role": "user", "content": content}])
            if ctx.get("add_cost"):
                ctx["add_cost"](msg)
            txt = (ctx["extract_text"](msg) if ctx.get("extract_text")
                   else "".join(getattr(x, "text", "") for x in msg.content))
            m = re.search(r"\{.*\}", txt, re.S)
            res = json.loads(m.group(0)) if m else {}
        except Exception as e:
            return jsonify({"error": f"Ошибка ИИ: {e}"}), 500
        verdict = {"same": str(res.get("same") or "unsure"),
                   "confidence": res.get("confidence"),
                   "why": str(res.get("why") or "")[:600], "at": now_iso()}
        with mlock:
            p["ai"] = verdict
            save_json(CARS_PATH, cars)
        return jsonify({"ok": True, "ai": verdict})

    @app.get("/api/bmw_photo/<fn>")
    def api_bmw_photo(fn):
        if not check_auth(request):
            return deny()
        if not re.fullmatch(r"[0-9a-zA-Z_\-]+\.jpg", fn):
            return jsonify({"error": "Нет файла"}), 404
        p = os.path.join(PHOTOS_DIR, fn)
        if not os.path.exists(p):
            return jsonify({"error": "Нет файла"}), 404
        with open(p, "rb") as f:
            return app.response_class(f.read(), mimetype="image/jpeg")

    # ---------- Что отдаём ядру для оболочки ----------

    SECTION_HTML = """
  <h2 style="margin:0 0 4px">🚙 Продажи BMW</h2>
  <p class="hint" style="margin:0 0 12px">Объявления auto.ru: BMW X3 G01 и X5 G05 с пробегом.
    У каждой машины — цифровой отпечаток (корзина: год, двигатель, мощность, коробка +
    комплектация + отпечатки фото), чтобы узнавать её при перевыкладке. Пробег и цена — в истории.</p>
  <div class="bar">
    <button class="primary" id="bmwRunBtn" onclick="bmwRun()">▶ Собрать сейчас</button>
    <button id="bmwStopBtn" onclick="bmwStop()" style="display:none">■ Остановить</button>
    <span id="bmwStage" class="hint"></span>
  </div>
  <div id="bmwWarn" class="bmwwarn" style="display:none"></div>
  <div id="bmwCounts" class="hint" style="margin-bottom:8px"></div>
  <details id="bmwSet" style="margin-bottom:12px">
    <summary class="hint" style="cursor:pointer">⚙ Настройки и журнал</summary>
    <div class="bar" style="margin-top:8px">
      <label class="hint">Прокси <input id="bmwProxy" style="width:230px"></label>
      <label class="hint">Пауза между страницами, с <input id="bmwPause" type="number" style="width:60px"></label>
      <label class="hint">Автосбор раз в N часов (0 — выкл.) <input id="bmwAuto" type="number" style="width:60px"></label>
      <label class="hint">Фото хранить <input id="bmwKeep" type="number" style="width:50px"></label>
      <button onclick="bmwSaveSettings()">Сохранить</button>
    </div>
    <pre id="bmwLog" class="bmwlog"></pre>
  </details>
  <div class="bar bmwtabs">
    <button data-t="cars" class="on" onclick="bmwTab('cars')">Машины</button>
    <button data-t="hist" onclick="bmwTab('hist')">История пробега</button>
    <button data-t="pairs" onclick="bmwTab('pairs')">Совпадения <span id="bmwPairsN"></span></button>
  </div>
  <div id="bmwT_cars">
    <div class="bar">
      <select id="bmwFModel" onchange="bmwRenderCars()"><option value="">Все модели</option>
        <option value="X3">X3 G01</option><option value="X5">X5 G05</option></select>
      <select id="bmwFAct" onchange="bmwRenderCars()"><option value="1">В продаже</option>
        <option value="0">Снятые</option><option value="">Все</option></select>
      <input id="bmwFText" placeholder="Поиск: 30d, M Sport, Москва…" oninput="bmwRenderCars()" style="min-width:220px">
      <label class="hint"><input type="checkbox" id="bmwFDrop" onchange="bmwRenderCars()"> только со скруткой</label>
      <label class="hint"><input type="checkbox" id="bmwFMulti" onchange="bmwRenderCars()"> только перевыложенные</label>
      <span id="bmwCarsN" class="hint"></span>
    </div>
    <div class="bmwscroll"><table id="bmwCars"></table></div>
  </div>
  <div id="bmwT_hist" style="display:none">
    <div class="bar"><label class="hint"><input type="checkbox" id="bmwHDrop" onchange="bmwRenderHist()">
      только машины со скруткой</label></div>
    <div class="bmwscroll"><table id="bmwHist"></table></div>
  </div>
  <div id="bmwT_pairs" style="display:none">
    <p class="hint">Пары объявлений, похожие на одну и ту же машину. Посмотрите сами или спросите ИИ
      (он сравнит по 2 сохранённых фото) и нажмите решение — «та же» объединит их в одну машину.</p>
    <div id="bmwPairs"></div>
  </div>
  <div id="bmwModal" class="bmwmodal" style="display:none" onclick="if(event.target===this)this.style.display='none'">
    <div class="bmwbox"><button style="float:right" onclick="document.getElementById('bmwModal').style.display='none'">✕</button>
      <div id="bmwModalBody"></div></div>
  </div>
"""

    CSS = """
  #sec-bmw input:not([type=checkbox]),#sec-bmw select{padding:6px 8px;border:1px solid var(--line);
       border-radius:6px;font:inherit;background:var(--panel)}
  .bmwwarn{background:var(--warn-bg);color:var(--warn);border-radius:6px;padding:8px 12px;margin-bottom:10px}
  .bmwlog{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:8px;
          max-height:220px;overflow:auto;font:12px/1.4 var(--mono);white-space:pre-wrap}
  .bmwtabs button.on{border-color:var(--blue);color:var(--blue);font-weight:600}
  .bmwscroll{overflow:auto;max-height:70vh}
  #bmwCars td,#bmwHist td{padding:5px 8px;border-top:1px solid var(--line);font-size:13px;vertical-align:middle}
  #bmwCars th,#bmwHist th{position:sticky;top:0;z-index:1;cursor:pointer;padding:6px 8px;white-space:nowrap}
  #bmwCars tr:hover td{background:#f3f7fd}
  #bmwCars img{width:80px;height:60px;object-fit:cover;border-radius:4px;display:block}
  .bmwnum{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
  .bmwbad{color:var(--err);font-weight:600}
  .bmwgone td{opacity:.6}
  .bmwpair{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px;margin-bottom:12px}
  .bmwpair .cols{display:flex;gap:14px;flex-wrap:wrap}
  .bmwpair .col{flex:1;min-width:300px}
  .bmwthumbs{display:flex;gap:4px;flex-wrap:wrap;margin:6px 0}
  .bmwthumbs img{width:120px;height:90px;object-fit:cover;border-radius:4px}
  .bmwai{background:var(--ok-bg);border-radius:6px;padding:6px 10px;margin-top:8px}
  .bmwmodal{position:fixed;inset:0;background:rgba(0,0,0,.35);z-index:50;display:flex;
            align-items:flex-start;justify-content:center;overflow:auto;padding:30px 10px}
  .bmwbox{background:var(--panel);border-radius:10px;padding:16px 18px;max-width:1000px;width:100%}
  .bmwbox table td{padding:4px 8px;border-top:1px solid var(--line);font-size:13px}
"""

    JS = r"""
var BMW={cars:[],hist:[],pairs:[],sort:{k:'last_seen',d:-1},timer:null,tab:'cars'};
function bmwEsc(s){return (s==null?'':s+'').replace(/[&<>"]/g,function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
function bmwNum(n){return n==null||n===''?'':Number(n).toLocaleString('ru-RU');}
function bmwTrans(t){return {AUTOMATIC:'АКПП',MECHANICAL:'МКПП',ROBOT:'робот',VARIATOR:'вариатор'}[t]||t||'';}
async function bmwLoadState(){
  var d; try{ d=await apiGet('/api/bmw_state'); }catch(e){ return; }
  var s=d.settings||{}, r=d.run||{}, c=d.counts||{};
  document.getElementById('bmwRunBtn').disabled=!!r.running;
  document.getElementById('bmwStopBtn').style.display=r.running?'':'none';
  var lr=s.last_run||{};
  document.getElementById('bmwStage').textContent=r.running?('Идёт сбор: '+(r.stage||'…')):
    (lr.finished?('Последний сбор: '+lr.finished.replace('T',' ')+(lr.error?' — ошибка: '+lr.error:'')):'Ещё не собирали');
  document.getElementById('bmwCounts').textContent='Машин: '+bmwNum(c.cars)+' · объявлений: '+bmwNum(c.offers)
    +' (в продаже '+bmwNum(c.active)+') · записей пробега: '+bmwNum(c.history);
  document.getElementById('bmwPairsN').textContent=c.pairs?('('+c.pairs+')'):'';
  var w=document.getElementById('bmwWarn');
  if(!d.pillow){ w.style.display=''; w.innerHTML='Для отпечатков фото на сервере нужна библиотека Pillow. '
    +'Без неё сбор работает, но фото не сравниваются.'; } else w.style.display='none';
  var px=document.getElementById('bmwProxy');
  if(document.activeElement!==px && !BMW.setLoaded){
    px.value=s.proxy||''; document.getElementById('bmwPause').value=s.pause;
    document.getElementById('bmwAuto').value=s.auto_hours; document.getElementById('bmwKeep').value=s.photos_keep;
    BMW.setLoaded=true;
  }
  document.getElementById('bmwLog').textContent=(r.log||[]).join('\n')||'Журнал пуст.';
  clearTimeout(BMW.timer);
  if(r.running){ BMW.timer=setTimeout(bmwLoadState,4000); BMW.wasRunning=true; }
  else if(BMW.wasRunning){ BMW.wasRunning=false; bmwLoadAll(); }
}
async function bmwRun(){ await api('/api/bmw_run',{}); BMW.wasRunning=true; bmwLoadState(); }
async function bmwStop(){ await api('/api/bmw_stop',{}); bmwLoadState(); }
async function bmwSaveSettings(){
  var r=await api('/api/bmw_settings',{proxy:document.getElementById('bmwProxy').value,
    pause:document.getElementById('bmwPause').value, auto_hours:document.getElementById('bmwAuto').value,
    photos_keep:document.getElementById('bmwKeep').value});
  alert(r&&r.error?r.error:'Сохранено'); BMW.setLoaded=false; bmwLoadState();
}
function bmwTab(t){
  BMW.tab=t;
  ['cars','hist','pairs'].forEach(function(x){ document.getElementById('bmwT_'+x).style.display=x===t?'':'none'; });
  document.querySelectorAll('.bmwtabs button').forEach(function(b){ b.classList.toggle('on',b.dataset.t===t); });
}
async function bmwLoadAll(){
  try{
    var a=await apiGet('/api/bmw_cars'); BMW.cars=a.cars||[]; bmwRenderCars();
    var h=await apiGet('/api/bmw_history'); BMW.hist=h.history||[]; bmwRenderHist();
    var p=await apiGet('/api/bmw_pairs'); BMW.pairs=p.pairs||[]; bmwRenderPairs();
  }catch(e){}
}
var BMW_COLS=[['thumb','Фото'],['model','Модель'],['year','Год'],['tech','Двигатель'],['trans','КПП'],
  ['compl','Комплектация'],['km','Пробег'],['price','Цена'],['region','Регион'],['offers','Объявл.'],
  ['first_seen','Впервые'],['last_seen','Последний раз']];
function bmwSortBy(k){ if(BMW.sort.k===k) BMW.sort.d*=-1; else BMW.sort={k:k,d:1}; bmwRenderCars(); }
function bmwRenderCars(){
  var fm=document.getElementById('bmwFModel').value, fa=document.getElementById('bmwFAct').value,
      ft=(document.getElementById('bmwFText').value||'').toLowerCase(),
      fd=document.getElementById('bmwFDrop').checked, fmu=document.getElementById('bmwFMulti').checked;
  var rows=BMW.cars.filter(function(c){
    if(fm && c.model!==fm) return false;
    if(fa==='1' && !c.active) return false;
    if(fa==='0' && c.active) return false;
    if(fd && !c.km_drop) return false;
    if(fmu && c.offers<2) return false;
    if(ft && [c.tech,c.compl,c.region,c.gen,c.year].join(' ').toLowerCase().indexOf(ft)<0) return false;
    return true;
  });
  var k=BMW.sort.k, d=BMW.sort.d;
  rows.sort(function(a,b){ var x=a[k], y=b[k]; if(x==null) x=''; if(y==null) y='';
    return (x>y?1:x<y?-1:0)*d; });
  var h='<tr>'+BMW_COLS.map(function(c){ return '<th onclick="bmwSortBy(\''+c[0]+'\')">'+c[1]
    +(BMW.sort.k===c[0]?(BMW.sort.d>0?' ▲':' ▼'):'')+'</th>'; }).join('')+'</tr>';
  rows.slice(0,1500).forEach(function(c){
    var pr=bmwNum(c.price)+' ₽';
    if(c.price0 && c.price!==c.price0) pr+='<div class="hint">было '+bmwNum(c.price0)+'</div>';
    h+='<tr class="'+(c.active?'':'bmwgone')+'" style="cursor:pointer" onclick="bmwCar(\''+c.cid+'\')">'
      +'<td>'+(c.thumb?'<img loading="lazy" referrerpolicy="no-referrer" src="'+bmwEsc(c.thumb)+'">':'')+'</td>'
      +'<td>'+bmwEsc(c.model+' '+c.code)+'<div class="hint">'+bmwEsc(c.gen)+'</div></td>'
      +'<td>'+bmwEsc(c.year)+'</td><td>'+bmwEsc(c.tech)+'</td><td>'+bmwTrans(c.trans)+'</td>'
      +'<td>'+bmwEsc(c.compl)+'</td>'
      +'<td class="bmwnum'+(c.km_drop?' bmwbad':'')+'">'+bmwNum(c.km)+(c.km_drop?' ⚠':'')+'</td>'
      +'<td class="bmwnum">'+pr+'</td><td>'+bmwEsc(c.region)+'</td>'
      +'<td class="bmwnum">'+c.offers+'</td><td>'+bmwEsc(c.first_seen)+'</td><td>'+bmwEsc(c.last_seen)
      +(c.active?'':' <span class="hint">снято</span>')+'</td></tr>';
  });
  document.getElementById('bmwCars').innerHTML=h;
  document.getElementById('bmwCarsN').textContent='Показано '+Math.min(rows.length,1500)+' из '+rows.length;
}
function bmwRenderHist(){
  var only=document.getElementById('bmwHDrop').checked;
  var drop={}; BMW.cars.forEach(function(c){ if(c.km_drop) drop[c.cid]=1; });
  var rows=BMW.hist.filter(function(h){ return !only || drop[h.cid]; });
  rows.sort(function(a,b){ var x=+a.cid||0, y=+b.cid||0; return x!==y?x-y:(a.d>b.d?1:a.d<b.d?-1:0); });
  var h='<tr><th>Машина №</th><th>Объявление</th><th>Год</th><th>Дата</th><th>Пробег</th><th>Изменение</th><th>Цена</th></tr>';
  var prev={};
  rows.slice(-3000).forEach(function(r){
    var p=prev[r.cid], dk='';
    if(p!=null && r.km!=null){ var x=r.km-p; dk=x===0?'':((x>0?'+':'')+bmwNum(x)); }
    var bad=p!=null && r.km!=null && r.km+500<p;
    if(r.km!=null) prev[r.cid]=Math.max(p||0,r.km);
    h+='<tr><td><a href="#" onclick="bmwCar(\''+r.cid+'\');return false">'+bmwEsc(r.cid)+'</a></td>'
      +'<td><a target="_blank" href="'+bmwEsc(r.url)+'">'+bmwEsc(r.title)+'</a></td><td>'+bmwEsc(r.year)+'</td>'
      +'<td>'+bmwEsc(r.d)+'</td><td class="bmwnum'+(bad?' bmwbad':'')+'">'+bmwNum(r.km)+'</td>'
      +'<td class="bmwnum'+(bad?' bmwbad':'')+'">'+dk+(bad?' ⚠ скрутка?':'')+'</td>'
      +'<td class="bmwnum">'+bmwNum(r.price)+'</td></tr>';
  });
  document.getElementById('bmwHist').innerHTML=h;
}
function bmwOfferHtml(o){
  if(!o) return '';
  var th=(o.thumbs||[]).map(function(u){ return '<img loading="lazy" referrerpolicy="no-referrer" src="'+bmwEsc(u)+'">'; }).join('');
  return '<div><a target="_blank" href="'+bmwEsc(o.url)+'"><b>'+bmwEsc(o.title)+'</b></a> · машина №'+bmwEsc(o.cid)+'</div>'
    +'<div class="bmwthumbs">'+th+'</div>'
    +'<div>'+bmwEsc(o.year)+' г. · '+bmwEsc(o.tech)+' · '+bmwEsc(o.compl)+' · опций '+o.equip_n+'</div>'
    +'<div>Пробег <b>'+bmwNum(o.km)+'</b> км · цена <b>'+bmwNum(o.price)+'</b> ₽ · '+bmwEsc(o.region)+'</div>'
    +'<div class="hint">'+bmwEsc(o.seller||'')+' ('+bmwEsc(o.seller_type)+') · на сайте с '+bmwEsc(o.created||o.first_seen)
    +(o.status==='gone'?' · снято '+bmwEsc(o.gone):'')+'</div>';
}
function bmwRenderPairs(){
  var box=document.getElementById('bmwPairs');
  if(!BMW.pairs.length){ box.innerHTML='<div class="hint">Спорных пар нет.</div>'; return; }
  box.innerHTML=BMW.pairs.map(function(p,i){
    var ai=p.ai?'<div class="bmwai">🤖 ИИ: <b>'+({yes:'та же машина',no:'разные',unsure:'не уверен'}[p.ai.same]||p.ai.same)
      +'</b>'+(p.ai.confidence!=null?' ('+p.ai.confidence+'%)':'')+' — '+bmwEsc(p.ai.why)+'</div>':'';
    return '<div class="bmwpair" id="bmwP'+i+'"><div class="hint" style="margin-bottom:6px">Почему в паре: '+bmwEsc(p.reason)+'</div>'
      +'<div class="cols"><div class="col">'+bmwOfferHtml(p.A)+'</div><div class="col">'+bmwOfferHtml(p.B)+'</div></div>'
      +ai+'<div class="bar" style="margin:10px 0 0">'
      +'<button class="primary" onclick="bmwDecide('+i+',true)">✓ Та же машина</button>'
      +'<button onclick="bmwDecide('+i+',false)">✗ Разные</button>'
      +'<button onclick="bmwAskAi('+i+',this)">🤖 Спросить ИИ</button></div></div>';
  }).join('');
}
async function bmwDecide(i,same){
  var p=BMW.pairs[i]; var r=await api('/api/bmw_pair_decide',{pid:p.pid,same:same});
  if(r&&r.error){ alert(r.error); return; }
  bmwLoadAll(); bmwLoadState();
}
async function bmwAskAi(i,btn){
  var p=BMW.pairs[i]; btn.disabled=true; btn.textContent='🤖 Думает…';
  var r=await api('/api/bmw_pair_ai',{pid:p.pid});
  if(r&&r.error){ alert(r.error); btn.disabled=false; btn.textContent='🤖 Спросить ИИ'; return; }
  p.ai=r.ai; bmwRenderPairs();
}
async function bmwPhoto(fn,img){
  try{ var res=await fetch('/api/bmw_photo/'+fn,{headers:{'X-Auth':AUTH}});
    if(res.ok) img.src=URL.createObjectURL(await res.blob()); }catch(e){}
}
async function bmwCar(cid){
  var d=await apiGet('/api/bmw_car?cid='+encodeURIComponent(cid));
  var h='<h3 style="margin:0 0 8px">Машина №'+bmwEsc(cid)+' — объявлений: '+d.offers.length+'</h3>';
  d.offers.forEach(function(o){
    h+='<div class="bmwpair">'+bmwOfferHtml(o)
      +(o.saved&&o.saved.length?'<div class="hint" style="margin-top:6px">Сохранённые фото:</div><div class="bmwthumbs">'
        +o.saved.map(function(fn){ return '<img data-fn="'+bmwEsc(fn)+'" style="width:240px;height:180px">'; }).join('')+'</div>':'')
      +'<div class="hint">Как узнали: '+bmwEsc(o.why||'')+'</div></div>';
  });
  h+='<h4>История пробега и цены</h4><table><tr><th>Дата</th><th>Пробег</th><th>Цена</th><th>Объявление</th></tr>';
  var mx=0;
  d.history.forEach(function(r){
    var bad=r.km!=null && r.km+500<mx; if(r.km!=null) mx=Math.max(mx,r.km);
    h+='<tr><td>'+bmwEsc(r.d)+'</td><td class="bmwnum'+(bad?' bmwbad':'')+'">'+bmwNum(r.km)+(bad?' ⚠':'')+'</td>'
      +'<td class="bmwnum">'+bmwNum(r.price)+'</td><td class="hint">'+bmwEsc(r.oid)+'</td></tr>';
  });
  h+='</table>';
  document.getElementById('bmwModalBody').innerHTML=h;
  document.getElementById('bmwModal').style.display='flex';
  document.querySelectorAll('#bmwModalBody img[data-fn]').forEach(function(img){ bmwPhoto(img.dataset.fn,img); });
}
window.onShow_bmw=function(){ bmwLoadState(); bmwLoadAll(); };
"""

    return {
        "id": "bmw",
        "title": "🚙 Продажи BMW",
        "section_html": SECTION_HTML,
        "css": CSS,
        "js": JS,
    }
