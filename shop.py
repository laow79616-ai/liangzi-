# -*- coding: utf-8 -*-
"""Short-drama shop bot: USD price, OKX RMB->USD tutorial, genre categories."""
import hashlib, json, secrets, sqlite3, time, urllib.request
from pathlib import Path
from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

ROOT = Path("/opt/video_clip_library")
DB = ROOT / "data" / "shop.db"
CFG = ROOT / "data" / "shop_config.json"
MEDIA = ROOT / "data" / "shop_media"

CATS = [
    ("urban", "现代都市"),
    ("costume", "古装传奇"),
    ("xianxia", "仙侠玄幻"),
    ("scifi", "科幻脑洞"),
    ("mystery", "灵异悬疑"),
    ("other", "其他短剧"),
]
CAT_MAP = dict(CATS)
router = APIRouter()

def db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS products(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cat TEXT, title TEXT, price_usd REAL, work_code TEXT,
        note TEXT, episode_no TEXT, video_rel TEXT, cover_rel TEXT, sku TEXT, on_sale INTEGER DEFAULT 1, created_at TEXT)""")
    cols = [r[1] for r in c.execute("PRAGMA table_info(products)").fetchall()]
    for col,typ in (("episode_no","TEXT"),("video_rel","TEXT"),("cover_rel","TEXT"),("sku","TEXT")):
        if col not in cols:
            c.execute(f"ALTER TABLE products ADD COLUMN {col} {typ}")
    c.execute("""CREATE TABLE IF NOT EXISTS orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_no TEXT UNIQUE, tg_id TEXT, cat TEXT, product_id INTEGER,
        title TEXT, amount_usd REAL, status TEXT, okx_uid TEXT,
        proof TEXT, created_at TEXT, paid_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS wallets(
        tg_id TEXT PRIMARY KEY, balance REAL DEFAULT 0)""")
    c.execute("""CREATE TABLE IF NOT EXISTS chain_tx(
        txid TEXT PRIMARY KEY, amount REAL, order_no TEXT, created_at TEXT)""")
    c.commit()
    return c

def load_cfg():
    if CFG.exists():
        return json.loads(CFG.read_text())
    return {
        "bot_token": "",
        "bot_username": "",
        "okx_uid": "",
        "pay_account": "",
        "admin_pass": "",
        "auth_salt": secrets.token_hex(8),
        "ppt_path": "",
        "qr_path": "",
        "notice_path": "",
        "cs_telegram": "",
        "cs_note": "",
        "enabled": False,
    }

def save_cfg(d):
    CFG.parent.mkdir(parents=True, exist_ok=True)
    CFG.write_text(json.dumps(d, ensure_ascii=False, indent=2))


def render_ppt(src: Path, prefix: str):
    import subprocess
    MEDIA.mkdir(parents=True, exist_ok=True)
    work = MEDIA / "_conv"
    work.mkdir(exist_ok=True)
    try:
        subprocess.run(["soffice","--headless","--convert-to","pdf","--outdir",str(work),str(src)], timeout=90, check=False)
        pdf = next(work.glob("*.pdf"), None)
        if not pdf:
            return
        for old in MEDIA.glob(prefix + "-*.jpg"):
            old.unlink()
        subprocess.run(["pdftoppm","-jpeg","-r","120",str(pdf),str(MEDIA / prefix)], timeout=60, check=False)
    except Exception:
        pass
    for f in work.glob("*"):
        try: f.unlink()
        except Exception: pass

def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


USDT_TRC20 = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

def check_trc20(order_no=None, tg_id=None):
    cfg = load_cfg()
    addr = (cfg.get("pay_trc20") or cfg.get("pay_account") or "").strip()
    if not addr or not addr.startswith("T"):
        return {"ok": False, "msg": "未填写 TRC20 地址", "credited": []}
    url = "https://api.trongrid.io/v1/accounts/%s/transactions/trc20?only_to=true&limit=40&contract_address=%s" % (addr, USDT_TRC20)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "clip-shop"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        return {"ok": False, "msg": str(e), "credited": []}
    incoming = []
    for tx in data.get("data") or []:
        txid = tx.get("transaction_id") or ""
        raw = str(tx.get("value") or "0")
        try:
            dec = int((tx.get("token_info") or {}).get("decimals") or 6)
            amt = int(raw) / (10 ** dec)
        except Exception:
            continue
        if txid and amt > 0:
            incoming.append((txid, amt))
    credited = []
    c = db()
    used = {r["txid"] for r in c.execute("SELECT txid FROM chain_tx").fetchall()}
    if order_no:
        row = c.execute("SELECT * FROM orders WHERE order_no=?", (order_no,)).fetchone()
        rows = [row] if row else []
    else:
        rows = c.execute("SELECT * FROM orders WHERE cat='recharge' AND status IN ('pending','checking') ORDER BY id ASC").fetchall()
    for row in rows:
        if not row or row["status"]=="paid":
            continue
        if tg_id and str(row["tg_id"]) != str(tg_id):
            continue
        target = float(row["amount_usd"])
        hit = None
        for txid, amt in incoming:
            if txid in used:
                continue
            if abs(amt - target) < 0.02:
                hit = (txid, amt)
                break
        if not hit:
            continue
        txid, amt = hit
        used.add(txid)
        c.execute("INSERT OR IGNORE INTO chain_tx(txid,amount,order_no,created_at) VALUES(?,?,?,?)",
                  (txid, amt, row["order_no"], now()))
        wallet_add(c, row["tg_id"] or "admin", float(row["amount_usd"]))
        c.execute("UPDATE orders SET status='paid', paid_at=? WHERE id=?", (now(), row["id"]))
        credited.append({"order_no": row["order_no"], "tg_id": row["tg_id"], "amount": row["amount_usd"], "txid": txid})
        if row["tg_id"] and str(row["tg_id"]).isdigit():
            send(row["tg_id"], "充值已到账 %.2f USD\n点「充值余额」查看" % float(row["amount_usd"]))
    c.commit(); c.close()
    return {"ok": True, "credited": credited}

def wallet_get(c, tg_id):
    tg_id = tg_id or "admin"
    row = c.execute("SELECT balance FROM wallets WHERE tg_id=?", (tg_id,)).fetchone()
    if not row:
        c.execute("INSERT INTO wallets(tg_id,balance) VALUES(?,0)", (tg_id,))
        return 0.0
    return float(row["balance"] or 0)

def wallet_add(c, tg_id, amt):
    tg_id = tg_id or "admin"
    wallet_get(c, tg_id)
    c.execute("UPDATE wallets SET balance=balance+? WHERE tg_id=?", (float(amt), tg_id))

def order_no():
    return "SD" + time.strftime("%Y%m%d-%H%M%S")

def hash_pw(pw, salt):
    return hashlib.sha256((salt + ":" + (pw or "")).encode()).hexdigest()

def token_ok(request: Request):
    cfg = load_cfg()
    if not cfg.get("admin_pass"):
        return True
    return request.cookies.get("shop_auth") == cfg.get("admin_pass")

def deny():
    return JSONResponse({"ok": False, "msg": "需要商店密码"}, status_code=401)

def tg_api(method, payload):
    cfg = load_cfg()
    token = cfg.get("bot_token") or ""
    if not token:
        return {"ok": False, "description": "no token"}
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"ok": False, "description": str(e)}

def kb(rows):
    return {"keyboard": rows, "resize_keyboard": True}

def recharge_kb():
    return kb([
        ["10 USD","50 USD","100 USD"],
        ["200 USD","500 USD","1000 USD"],
        ["2000 USD","5000 USD"],
        ["自定义金额","返回"],
    ])

def send_pay_card(chat_id, no, amt, acc):
    cfg = load_cfg()
    text = (
        f"充值单 {no}\n"
        f"付款金额：{amt:.2f} USD（1 USDT = 1 USD）\n"
        f"收款账号：{acc}\n\n"
        "转账后回复：已付 "+no+"\n"
        "系统按订单号+链上金额入账，避免多人同金额记错人。\n"
        "入账后点「充值余额」查看余额。"
    )
    qr = cfg.get("qr_path") or ""
    if qr and Path(qr).exists():
        token = cfg.get("bot_token") or ""
        try:
            import urllib.request as ur
            url = f"https://api.telegram.org/bot{token}/sendPhoto"
            boundary="----shopqr"
            data=Path(qr).read_bytes()
            body=b""
            def part(name, val, filename=None, mime=None):
                h=f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\""
                if filename:
                    h+=f"; filename=\"{filename}\"\r\nContent-Type: {mime}"
                h+="\r\n\r\n"
                return h.encode()+val+b"\r\n"
            raw=part("chat_id", str(chat_id).encode())
            raw+=part("caption", text.encode(),)
            raw+=part("photo", data, Path(qr).name, "image/jpeg")
            raw+=f"--{boundary}--\r\n".encode()
            req=ur.Request(url, data=raw, headers={"Content-Type":f"multipart/form-data; boundary={boundary}"})
            ur.urlopen(req, timeout=20)
            send(chat_id, " ", recharge_kb())
            return
        except Exception:
            pass
    send(chat_id, text, recharge_kb())

def main_kb():
    names = [n for _, n in CATS]
    rows = [names[i:i+2] for i in range(0, len(names), 2)]
    rows.append(["充值余额", "购买记录"])
    rows.append(["欧易转换教程", "购买须知"])
    rows.append(["主菜单", "联系客服"])
    return kb(rows)

OKX_GUIDE = """【欧易转换教程 · 人民币转 USD】
本店按美元标价。请先在欧易把人民币换成美元/USDT，再转到收款账号。

收款账号：{acc}

建议步骤：
1. 打开欧易 App，完成实名
2. 用 C2C/买币 把人民币买成 USDT（按页面显示的美元计价）
3. 资产 → 提币 → 提取数字货币 → USDT
4. 若收款方也是欧易用户：选「OKX 用户」，填写收款账号
5. 金额按订单 USD（1 USDT 记 1 USD）
6. 备注填写订单号
7. 完成安全验证后提交

详细图解请查看后台上传的《欧易转换PPT》。
转完后回到机器人发送：已付 订单号
"""

def send_video(chat_id, video_path, caption=""):
    cfg = load_cfg()
    token = cfg.get("bot_token") or ""
    vp = Path(video_path) if video_path else None
    if not token or not vp or not vp.exists():
        return False
    try:
        data = vp.read_bytes()
        if len(data) > 49 * 1024 * 1024:
            return False
        boundary = "----shopvid"
        def field(n, v):
            return (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{n}\"\r\n\r\n").encode() + v + b"\r\n"
        raw = field("chat_id", str(chat_id).encode())
        raw += field("caption", (caption or "").encode())
        raw += field("supports_streaming", b"true")
        raw += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"video\"; filename=\"{vp.name}\"\r\nContent-Type: video/mp4\r\n\r\n").encode() + data + b"\r\n"
        raw += f"--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendVideo",
            data=raw,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        urllib.request.urlopen(req, timeout=120)
        return True
    except Exception:
        return False

def send_photo(chat_id, photo_path, caption="", markup=None):
    cfg = load_cfg()
    token = cfg.get("bot_token") or ""
    if not token or not photo_path or not Path(photo_path).exists():
        return send(chat_id, caption, markup)
    try:
        import mimetypes
        mime = mimetypes.guess_type(photo_path)[0] or "image/jpeg"
        data = Path(photo_path).read_bytes()
        name = Path(photo_path).name
        boundary = "----shopcover"
        def field(n, v):
            return (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{n}\"\r\n\r\n").encode()+v+b"\r\n"
        raw = field("chat_id", str(chat_id).encode())
        raw += field("caption", (caption or "").encode())
        if markup:
            raw += field("reply_markup", json.dumps(markup).encode())
        raw += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"{name}\"\r\nContent-Type: {mime}\r\n\r\n").encode()+data+b"\r\n"
        raw += f"--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendPhoto",
            data=raw,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        urllib.request.urlopen(req, timeout=6)
        return {"ok": True}
    except Exception as e:
        return send(chat_id, caption, markup)

def send(chat_id, text, markup=None):
    payload = {"chat_id": chat_id, "text": text}
    if markup:
        payload["reply_markup"] = markup
    return tg_api("sendMessage", payload)

@router.get("/shop", response_class=HTMLResponse)
def shop_page():
    p = ROOT / "static" / "shop.html"
    if p.exists():
        return HTMLResponse(p.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>shop.html missing</h1>")

@router.get("/shop/media/{name}")
def shop_media(name: str):
    fp = MEDIA / name
    if not fp.exists() or not fp.is_file():
        return JSONResponse({"ok": False}, 404)
    return FileResponse(fp)

@router.post("/api/shop/login")
def shop_login(password: str = Form(...)):
    cfg = load_cfg()
    pw = (password or "").strip()
    if not pw:
        return JSONResponse({"ok": False, "msg": "请输入密码"})
    if not cfg.get("admin_pass"):
        if not cfg.get("auth_salt"):
            cfg["auth_salt"] = secrets.token_hex(8)
        cfg["admin_pass"] = hash_pw(pw, cfg["auth_salt"])
        save_cfg(cfg)
    elif hash_pw(pw, cfg.get("auth_salt", "")) != cfg.get("admin_pass"):
        return JSONResponse({"ok": False, "msg": "密码错误"}, 403)
    resp = JSONResponse({"ok": True})
    resp.set_cookie("shop_auth", cfg["admin_pass"], httponly=True, max_age=86400 * 7, samesite="lax")
    return resp

@router.get("/api/shop/config")
def api_cfg(request: Request):
    if not token_ok(request):
        return deny()
    d = load_cfg()
    if d.get("bot_token"):
        d["bot_token"] = d["bot_token"][:8] + "****"
    d.pop("admin_pass", None)
    d["has_password"] = bool(load_cfg().get("admin_pass"))
    d["ppt_url"] = "/shop/media/" + Path(d["ppt_path"]).name if d.get("ppt_path") else ""
    d["qr_url"] = "/shop/media/" + Path(d["qr_path"]).name if d.get("qr_path") else ""
    d["notice_url"] = "/shop/media/" + Path(d["notice_path"]).name if d.get("notice_path") else ""
    return d

@router.post("/api/shop/config")
async def api_cfg_save(
    request: Request,
    bot_token: str = Form(""),
    bot_username: str = Form(""),
    okx_uid: str = Form(""),
    pay_account: str = Form(""),
    enabled: str = Form("0"),
    shop_password: str = Form(""),
    ppt: UploadFile | None = File(None),
    qr: UploadFile | None = File(None),
    notice: UploadFile | None = File(None),
    cover: UploadFile | None = File(None),
    cs_telegram: str = Form(""),
    cs_note: str = Form(""),
):
    old = load_cfg()
    if old.get("admin_pass") and not token_ok(request):
        return deny()
    token = bot_token.strip()
    if not token or "****" in token:
        token = old.get("bot_token", "")
    salt = old.get("auth_salt") or secrets.token_hex(8)
    pw = old.get("admin_pass", "")
    if shop_password.strip():
        pw = hash_pw(shop_password.strip(), salt)
    MEDIA.mkdir(parents=True, exist_ok=True)
    ppt_path = old.get("ppt_path") or ""
    qr_path = old.get("qr_path") or ""
    notice_path = old.get("notice_path") or ""
    cover_path = old.get("cover_path") or ""
    if ppt and ppt.filename:
        dest = MEDIA / ("okx_guide" + Path(ppt.filename).suffix.lower())
        dest.write_bytes(await ppt.read())
        ppt_path = str(dest)
        render_ppt(dest, "okx_slide")
    if qr and qr.filename:
        dest = MEDIA / ("okx_qr" + Path(qr.filename).suffix.lower())
        dest.write_bytes(await qr.read())
        qr_path = str(dest)
    if cover and cover.filename:
        dest = MEDIA / ("bot_cover" + Path(cover.filename).suffix.lower())
        dest.write_bytes(await cover.read())
        cover_path = str(dest)
    if notice and notice.filename:
        dest = MEDIA / ("purchase_notice" + Path(notice.filename).suffix.lower())
        dest.write_bytes(await notice.read())
        notice_path = str(dest)
        render_ppt(dest, "notice_slide")
    save_cfg({
        "bot_token": token,
        "bot_username": bot_username.strip().lstrip("@"),
        "okx_uid": okx_uid.strip(),
        "pay_account": pay_account.strip() or okx_uid.strip(),
        "admin_pass": pw,
        "auth_salt": salt,
        "ppt_path": ppt_path,
        "qr_path": qr_path,
        "notice_path": notice_path,
        "cover_path": cover_path,
        "cs_telegram": cs_telegram.strip() or old.get("cs_telegram",""),
        "cs_note": cs_note if cs_note is not None else old.get("cs_note",""),
        "enabled": enabled in ("1", "true", "on"),
    })
    return {"ok": True, "need_login": bool(pw)}


@router.post("/api/shop/okx-images")
async def api_okx_images(request: Request, files: list[UploadFile] | None = File(None)):
    if not token_ok(request):
        return deny()
    form = await request.form()
    blobs = files or []
    if not blobs:
        blobs = form.getlist("files") or form.getlist("file")
    MEDIA.mkdir(parents=True, exist_ok=True)
    n = len(list(MEDIA.glob("okx_slide-*.*")))
    saved = []
    for f in blobs:
        if not f.filename:
            continue
        n += 1
        ext = Path(f.filename).suffix.lower() or ".jpg"
        if ext not in (".jpg",".jpeg",".png",".webp",".gif"):
            ext = ".jpg"
        dest = MEDIA / f"okx_slide-{n}{ext}"
        dest.write_bytes(await f.read())
        saved.append(dest.name)
    return {"ok": True, "saved": saved}

@router.post("/api/shop/notice-images")
async def api_notice_images(request: Request, files: list[UploadFile] = File(...)):
    if not token_ok(request):
        return deny()
    MEDIA.mkdir(parents=True, exist_ok=True)
    n = len(list(MEDIA.glob("notice_slide-*.jpg"))) + len(list(MEDIA.glob("notice_slide-*.png")))
    saved = []
    for f in files or []:
        if not f.filename:
            continue
        n += 1
        ext = Path(f.filename).suffix.lower() or ".jpg"
        if ext not in (".jpg",".jpeg",".png",".webp",".gif"):
            ext = ".jpg"
        dest = MEDIA / f"notice_slide-{n}{ext}"
        dest.write_bytes(await f.read())
        saved.append(dest.name)
    return {"ok": True, "saved": saved}

@router.get("/api/shop/slides")
def api_slides(kind: str = "okx"):
    prefix = "okx_slide" if kind != "notice" else "notice_slide"
    items = []
    if MEDIA.exists():
        files = sorted(MEDIA.glob(prefix + "-*.jpg")) + sorted(MEDIA.glob(prefix + "-*.jpeg")) + sorted(MEDIA.glob(prefix + "-*.png"))
        items = ["/shop/media/" + f.name for f in files]
    return {"items": items}

@router.get("/api/shop/products")
def api_products(cat: str = "", q: str = ""):
    c = db()
    like = f"%{q.strip()}%"
    if cat and q:
        rows = c.execute("SELECT * FROM products WHERE cat=? AND (title LIKE ? OR IFNULL(work_code,'') LIKE ? OR IFNULL(note,'') LIKE ?) ORDER BY id DESC", (cat,like,like,like)).fetchall()
    elif cat:
        rows = c.execute("SELECT * FROM products WHERE cat=? ORDER BY id DESC", (cat,)).fetchall()
    elif q:
        rows = c.execute("SELECT * FROM products WHERE title LIKE ? OR IFNULL(work_code,'') LIKE ? OR IFNULL(note,'') LIKE ? ORDER BY id DESC", (like,like,like)).fetchall()
    else:
        rows = c.execute("SELECT * FROM products ORDER BY id DESC").fetchall()
    items=[]
    for x in rows:
        d=dict(x)
        d["video_url"] = ("/shop/media/"+Path(d["video_rel"]).name) if d.get("video_rel") else ""
        d["cover_url"] = ("/shop/media/"+Path(d["cover_rel"]).name) if d.get("cover_rel") else ""
        if not d.get("episode_no"):
            d["episode_no"]=d.get("work_code") or ""
        if not d.get("sku"):
            d["sku"]=str(d.get("id") or "")
        items.append(d)
    c.close()
    return {"items": items, "cats": CATS}

@router.post("/api/shop/products")
async def api_add_product(request: Request, cat: str = Form(...), title: str = Form(...),
                    price_usd: float = Form(...), work_code: str = Form(""), note: str = Form(""),
                    episode_no: str = Form(""), sku: str = Form(""), video: UploadFile | None = File(None),
                    cover: UploadFile | None = File(None)):
    if not token_ok(request):
        return deny()
    ep = (episode_no or work_code or "").strip()
    video_rel = ""
    MEDIA.mkdir(parents=True, exist_ok=True)
    if video and video.filename:
        ext = Path(video.filename).suffix.lower() or ".mp4"
        dest = MEDIA / f"ep_{cat}_{ep}_{int(time.time())}{ext}"
        dest.write_bytes(await video.read())
        video_rel = str(dest)
    cover_rel=""
    if cover and cover.filename:
        ext = Path(cover.filename).suffix.lower() or ".jpg"
        dest = MEDIA / f"epcover_{cat}_{ep}_{int(time.time())}{ext}"
        dest.write_bytes(await cover.read())
        cover_rel = str(dest)
    sku_v=(sku or "").replace("#","").strip()
    c = db()
    if not sku_v:
        sku_v=str((c.execute("SELECT IFNULL(MAX(id),0)+1 FROM products").fetchone()[0]))
    c.execute(
        "INSERT INTO products(cat,title,price_usd,work_code,note,episode_no,video_rel,cover_rel,sku,on_sale,created_at) VALUES(?,?,?,?,?,?,?,?,?,1,?)",
        (cat, title.strip(), float(price_usd), ep, note.strip(), ep, video_rel, cover_rel, sku_v, now()),
    )
    c.commit(); c.close()
    return {"ok": True}

@router.delete("/api/shop/products/{pid}")
def api_del_product(request: Request, pid: int):
    if not token_ok(request):
        return deny()
    c = db(); c.execute("DELETE FROM products WHERE id=?", (pid,)); c.commit(); c.close()
    return {"ok": True}

@router.get("/api/shop/wallet")
def api_wallet(tg_id: str = "admin"):
    c=db(); bal=wallet_get(c,tg_id); c.commit(); c.close()
    return {"tg_id": tg_id or "admin", "balance": bal}

@router.post("/api/shop/recharge")
def api_recharge(request: Request, amount_usd: float = Form(...), tg_id: str = Form("admin")):
    if not token_ok(request):
        return deny()
    cfg=load_cfg()
    c=db()
    no=order_no()
    c.execute(
        "INSERT INTO orders(order_no,tg_id,cat,product_id,title,amount_usd,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (no, tg_id or "admin", "recharge", 0, "余额充值", float(amount_usd), "pending", now()),
    )
    oid=c.execute("SELECT last_insert_rowid()").fetchone()[0]
    c.commit(); c.close()
    return {"ok":True,"order_id":oid,"order_no":no,"amount_usd":float(amount_usd),
            "pay_account":cfg.get("pay_account") or "","qr_url":("/shop/media/"+Path(cfg["qr_path"]).name) if cfg.get("qr_path") else ""}

@router.post("/api/shop/buy")
def api_buy(request: Request, product_id: int = Form(...), tg_id: str = Form("")):
    if not token_ok(request):
        return deny()
    cfg = load_cfg()
    c = db()
    p = c.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not p:
        c.close()
        return JSONResponse({"ok": False, "msg": "没有这一集"}, 404)
    tg_id = tg_id or "admin"
    price = float(p["price_usd"] or 0)
    bal = wallet_get(c, tg_id)
    video = ("/shop/media/"+Path(p["video_rel"]).name) if p["video_rel"] else ""
    if bal + 1e-9 < price:
        c.commit(); c.close()
        return {"ok": False, "need_recharge": True, "balance": bal, "amount_usd": price,
                "msg": "余额不足，请先充值", "pay_account": cfg.get("pay_account") or "",
                "qr_url": ("/shop/media/"+Path(cfg["qr_path"]).name) if cfg.get("qr_path") else ""}
    wallet_add(c, tg_id, -price)
    no = order_no()
    c.execute(
        "INSERT INTO orders(order_no,tg_id,cat,product_id,title,amount_usd,status,paid_at,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (no, tg_id, p["cat"], p["id"], p["title"], price, "paid", now(), now()),
    )
    oid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    new_bal = wallet_get(c, tg_id)
    c.commit(); c.close()
    return {"ok": True, "paid": True, "order_id": oid, "order_no": no,
            "title": p["title"], "episode_no": p["episode_no"] or p["work_code"] or "",
            "amount_usd": price, "balance": new_bal, "video_url": video}


@router.get("/api/shop/orders")
def api_orders(request: Request, status: str = ""):
    if not token_ok(request):
        return deny()
    c = db()
    if status:
        rows = c.execute("SELECT * FROM orders WHERE status=? ORDER BY id DESC LIMIT 200", (status,)).fetchall()
    else:
        rows = c.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 200").fetchall()
    c.close()
    return {"items": [dict(x) for x in rows]}

@router.post("/api/shop/orders/{oid}/paid")
def api_mark_paid(request: Request, oid: int):
    if not token_ok(request):
        return deny()
    c = db()
    row = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    c.execute("UPDATE orders SET status='paid', paid_at=? WHERE id=?", (now(), oid))
    if row and row["cat"]=="recharge" and row["status"]!="paid":
        wallet_add(c, row["tg_id"] or "admin", row["amount_usd"])
    row = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    c.commit(); c.close()
    video_url = ""
    if row:
        p = c.execute("SELECT video_rel FROM products WHERE id=?", (row["product_id"],)).fetchone() if False else None
    c2 = db()
    if row:
        p = c2.execute("SELECT video_rel FROM products WHERE id=?", (row["product_id"],)).fetchone()
        if p and p["video_rel"]:
            video_url = "/shop/media/" + Path(p["video_rel"]).name
        if row["tg_id"]:
            send(row["tg_id"], f"订单 {row['order_no']} 已确认到账（{row['amount_usd']} USD）。" + (f"\n观看：https://liangzi.icu{video_url}" if video_url else ""))
    c2.close()
    return {"ok": True, "video_url": video_url}

@router.post("/tg/webhook")
async def tg_hook(req: Request):
    cfg = load_cfg()
    body = await req.json()
    msg = body.get("message") or body.get("edited_message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text = (msg.get("text") or "").strip()
    if not chat_id:
        return {"ok": True}
    uid = str(chat_id)
    acc = cfg.get("pay_account") or cfg.get("okx_uid") or "（后台未填收款账号）"

    if text.startswith("/start") or text in ("主菜单", "菜单"):
        c=db(); bal=wallet_get(c, uid)
        spent_row=c.execute("SELECT IFNULL(SUM(amount_usd),0) AS s FROM orders WHERE tg_id=? AND status='paid' AND IFNULL(cat,'')!='recharge'",(uid,)).fetchone()
        cnt_row=c.execute("SELECT COUNT(*) AS n FROM orders WHERE tg_id=? AND status='paid' AND IFNULL(cat,'')!='recharge'",(uid,)).fetchone()
        spent=float(spent_row["s"] if spent_row else 0)
        cnt=int(cnt_row["n"] if cnt_row else 0)
        c.commit(); c.close()
        name=(msg.get("from") or {}).get("first_name") or ""
        cap=(
            f"下午好，{name}\n"
            f"ID：{uid}\n\n"
            f"💰 USDT：{bal:.2f}\n"
            f"💳 消费金额：{spent:.2f}\n"
            f"📦 购买数量：{cnt}\n"
            "----------------\n"
            "短剧商店 · 先充值再点剧购买"
        )
        cover=cfg.get("cover_path") or ""
        try:
            if cover:
                send_photo(chat_id, cover, cap, main_kb())
            else:
                send(chat_id, cap, main_kb())
        except Exception:
            send(chat_id, cap, main_kb())
        return {"ok": True}
    if text in ("联系客服",):
        handle = (cfg.get("cs_telegram") or cfg.get("bot_username") or "").lstrip("@")
        send(chat_id, ("客服 Telegram：@" + handle) if handle else "后台尚未填写客服账号。", main_kb())
        return {"ok": True}
    if text in ("搜索短剧",):
        send(chat_id, "请直接发送要搜索的关键词。", main_kb())
        return {"ok": True}
    if text in ("购买须知",):
        send(chat_id, "购买须知：按美元标价，欧易转换后转至收款账号，备注订单号。到账后回复 已付 订单号。", main_kb())
        return {"ok": True}
    if text in ("充值余额",):
        try:
            check_trc20(tg_id=uid)
        except Exception:
            pass
        c=db(); bal=wallet_get(c, uid); c.commit(); c.close()
        send(chat_id, f"用户 ID：{uid}\n账户余额：{bal:.2f} USD\n请选择充值金额：", recharge_kb())
        return {"ok": True}
    if text in ("返回","主菜单"):
        send(chat_id, "已返回主菜单。", main_kb())
        return {"ok": True}
    if text in ("自定义金额",):
        send(chat_id, "请发送：充值 35.5", recharge_kb())
        return {"ok": True}
    amt=None
    if text.endswith(" USD") and text.replace(" USD","").replace(".","").isdigit():
        amt=float(text.replace(" USD","").strip())
    if text.startswith("充值"):
        raw=text.replace("充值","").strip().replace("USD","").replace("USDT","")
        try: amt=float(raw)
        except Exception: amt=None
    if amt and amt>0:
        c=db(); no=order_no()
        c.execute("INSERT INTO orders(order_no,tg_id,cat,product_id,title,amount_usd,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                  (no, uid, "recharge", 0, "余额充值", amt, "pending", now()))
        c.commit(); c.close()
        send_pay_card(chat_id, no, amt, acc)
        return {"ok": True}
    if text in ("购买记录", "我的订单"):
        text = "我的订单"
    if text in ("付款教程", "欧易转换教程"):
        slides=[]
        if MEDIA.exists():
            slides=sorted(list(MEDIA.glob("okx_slide-*.jpg"))+list(MEDIA.glob("okx_slide-*.jpeg"))+list(MEDIA.glob("okx_slide-*.png"))+list(MEDIA.glob("okx_slide-*.webp")))
        send(chat_id, "欧易转换示意图：", main_kb())
        if slides:
            for fp in slides:
                send_photo(chat_id, str(fp), "")
        else:
            send(chat_id, OKX_GUIDE.format(acc=acc), main_kb())
        return {"ok": True}
    if text == "我的订单":
        c = db()
        rows = c.execute("SELECT * FROM orders WHERE tg_id=? ORDER BY id DESC LIMIT 10", (uid,)).fetchall()
        c.close()
        if not rows:
            send(chat_id, "暂无订单。", main_kb())
        else:
            lines = [f"{r['order_no']}  {r['title']}  ${r['amount_usd']}  {r['status']}" for r in rows]
            send(chat_id, "最近订单：\n" + "\n".join(lines), main_kb())
        return {"ok": True}

    cat = None
    for k, name in CATS:
        if text == name:
            cat = k
            break
    if cat:
        c = db()
        items = c.execute("SELECT * FROM products WHERE cat=? AND on_sale=1 ORDER BY id DESC", (cat,)).fetchall()
        c.close()
        if not items:
            send(chat_id, f"「{CAT_MAP[cat]}」暂无上架商品。后台添加后再来。", main_kb())
            return {"ok": True}
        send(chat_id, f"「{CAT_MAP[cat]}」先充值，再回复 #编号 购买", main_kb())
        for p in items:
            sku=p["sku"] or p["id"]
            ep=p["episode_no"] or p["work_code"] or "-"
            cap=f"#{sku}  第{ep}集 {p['title']}\n{float(p['price_usd']):.2f} USD"
            if p["cover_rel"] and Path(p["cover_rel"]).exists():
                send_photo(chat_id, p["cover_rel"], cap, main_kb())
            else:
                send(chat_id, cap, main_kb())
        return {"ok": True}

    if text.startswith("#") or text.isdigit():
        try:
            code = text.replace("#", "").split()[0]
            pid = int(code)
        except Exception:
            send(chat_id, "请回复编号，例如 #7", main_kb())
            return {"ok": True}
        c = db()
        p = c.execute("SELECT * FROM products WHERE on_sale=1 AND (id=? OR sku=?)", (str(pid), str(pid))).fetchone()
        if not p:
            p = c.execute("SELECT * FROM products WHERE on_sale=1 AND sku=?", (code,)).fetchone()
        if not p:
            c.close()
            send(chat_id, "没有这个商品。", main_kb())
            return {"ok": True}
        price=float(p["price_usd"] or 0)
        bal=wallet_get(c, uid)
        if bal + 1e-9 < price:
            c.commit(); c.close()
            send(chat_id, f"余额 {bal:.2f} USD，本集 {price:.2f} USD，不够。\n请先点「充值余额」。", main_kb())
            return {"ok": True}
        wallet_add(c, uid, -price)
        no=order_no()
        c.execute(
            "INSERT INTO orders(order_no,tg_id,cat,product_id,title,amount_usd,status,paid_at,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (no, uid, p["cat"], p["id"], p["title"], price, "paid", now(), now()),
        )
        video=("/shop/media/"+__import__("pathlib").Path(p["video_rel"]).name) if p["video_rel"] else ""
        new_bal=wallet_get(c, uid)
        c.commit(); c.close()
        cap=f"已用余额购买  第{p['episode_no'] or p['work_code'] or ''}集 {p['title']}\n扣 {price:.2f} USD  剩余 {new_bal:.2f} USD"
        sent=False
        if p["video_rel"]:
            sent=send_video(chat_id, p["video_rel"], cap)
        if not sent:
            send(chat_id, cap + "\n视频发送失败，请联系客服。", main_kb())
        else:
            send(chat_id, "可在本对话直接点开播放。", main_kb())
        return {"ok": True}

    if text.startswith("已付"):
        no = text.replace("已付", "").strip()
        try:
            info=check_trc20(order_no=no, tg_id=uid)
        except Exception as e:
            info={"credited":[], "msg":str(e)}
        c=db(); bal=wallet_get(c, uid); row=c.execute("SELECT status FROM orders WHERE order_no=? AND tg_id=?",(no,uid)).fetchone(); c.close()
        if info.get("credited"):
            send(chat_id, f"已到账。当前余额 {bal:.2f} USD", main_kb())
        elif row and row["status"]=="paid":
            send(chat_id, f"该单已入账。当前余额 {bal:.2f} USD", main_kb())
        else:
            send(chat_id, f"还没对上链上入账。请确认金额与订单一致后再发：已付 {no}\n当前余额 {bal:.2f} USD", main_kb())
        return {"ok": True}

    send(chat_id, "请点下方分类，或发送 /start", main_kb())
    return {"ok": True}

@router.post("/api/shop/set-webhook")
def set_hook(request: Request, public_url: str = Form("https://liangzi.icu")):
    if not token_ok(request):
        return deny()
    cfg = load_cfg()
    if not cfg.get("bot_token"):
        return JSONResponse({"ok": False, "msg": "先保存 Bot Token"}, 400)
    url = public_url.rstrip("/") + "/tg/webhook"
    r = tg_api("setWebhook", {"url": url})
    return {"ok": bool(r.get("ok")), "webhook": url, "telegram": r}
