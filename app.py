#!/usr/bin/env python3
from __future__ import annotations
import json, re, sqlite3, subprocess, uuid
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
DATA, MEDIA = ROOT / "data", ROOT / "data" / "media"
DB = DATA / "library.db"
PLAT = {"facebook":"Facebook","twitter":"推特/X","instagram":"Instagram","tiktok":"TikTok","telegram":"Telegram"}
for p in ("videos","images","clips"):
    (MEDIA/p).mkdir(parents=True, exist_ok=True)

def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c

def init():
    DATA.mkdir(exist_ok=True)
    c = conn()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS works(
      id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, title TEXT, series_name TEXT DEFAULT '',
      episode_no TEXT DEFAULT '', duration_sec INTEGER DEFAULT 60, master_keywords TEXT DEFAULT '',
      status TEXT DEFAULT 'draft', created_at TEXT, updated_at TEXT);
    CREATE TABLE IF NOT EXISTS copies(
      id INTEGER PRIMARY KEY AUTOINCREMENT, work_id INTEGER, platform TEXT, copy_type TEXT DEFAULT 'main',
      title TEXT DEFAULT '', body TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS keywords(
      id INTEGER PRIMARY KEY AUTOINCREMENT, work_id INTEGER, platform TEXT, words TEXT, note TEXT DEFAULT '', created_at TEXT);
    CREATE TABLE IF NOT EXISTS assets(
      id INTEGER PRIMARY KEY AUTOINCREMENT, work_id INTEGER, platform TEXT, asset_type TEXT,
      filename TEXT, rel_path TEXT, size_bytes INTEGER DEFAULT 0, duration_sec REAL DEFAULT 0,
      source_platform TEXT DEFAULT '', highlight_note TEXT DEFAULT '', clip_copy TEXT DEFAULT '', created_at TEXT);
    CREATE TABLE IF NOT EXISTS analytics(
      id INTEGER PRIMARY KEY AUTOINCREMENT, work_id INTEGER, platform TEXT, publish_time TEXT DEFAULT '',
      play_count INTEGER DEFAULT 0, account_count INTEGER DEFAULT 0, used_copy TEXT DEFAULT '',
      used_title TEXT DEFAULT '', account_names TEXT DEFAULT '', recorded_at TEXT);
    CREATE TABLE IF NOT EXISTS found_accounts(
      id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT, handle TEXT, display_name TEXT DEFAULT '',
      note TEXT DEFAULT '', publish_count INTEGER DEFAULT 0, last_seen TEXT,
      UNIQUE(platform,handle));
    CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE IF NOT EXISTS frames(
      id INTEGER PRIMARY KEY AUTOINCREMENT, work_id INTEGER, platform TEXT DEFAULT '',
      copy_title TEXT DEFAULT '', keywords TEXT DEFAULT '', body TEXT DEFAULT '',
      publish_time TEXT DEFAULT '', play_count INTEGER DEFAULT 0,
      repost_count INTEGER DEFAULT 0, account_count INTEGER DEFAULT 0,
      notes TEXT DEFAULT '', created_at TEXT);
    """)
    c.commit()
    cols = [r[1] for r in c.execute("PRAGMA table_info(assets)").fetchall()]
    if "frame_id" not in cols:
        c.execute("ALTER TABLE assets ADD COLUMN frame_id INTEGER")
        c.commit()
    c.close()

def split_handles(s):
    out, seen = [], set()
    for p in re.split(r"[,，;；|/\n]+", s or ""):
        h = p.strip().lstrip("@")
        if h and h.lower() not in seen:
            seen.add(h.lower()); out.append(h)
    return out

def upsert_acc(c, plat, names, extra=0):
    t = now()
    hs = split_handles(names)
    for h in hs:
        row = c.execute("SELECT id FROM found_accounts WHERE platform=? AND handle=?", (plat,h)).fetchone()
        if row:
            c.execute("UPDATE found_accounts SET last_seen=?, publish_count=publish_count+1 WHERE id=?", (t,row["id"]))
        else:
            c.execute("INSERT INTO found_accounts(platform,handle,display_name,last_seen,publish_count) VALUES(?,?,?,?,1)",
                      (plat,h,h,t))
    if extra > len(hs):
        ghost = f"未具名×{extra-len(hs)}"
        row = c.execute("SELECT id FROM found_accounts WHERE platform=? AND handle=?", (plat,ghost)).fetchone()
        if row:
            c.execute("UPDATE found_accounts SET last_seen=?, publish_count=publish_count+? WHERE id=?", (t, extra-len(hs), row["id"]))
        else:
            c.execute("INSERT INTO found_accounts(platform,handle,display_name,note,last_seen,publish_count) VALUES(?,?,?,?,?,?)",
                      (plat,ghost,ghost,"分析栏只填了数量",t,extra-len(hs)))

init()
app = FastAPI()
ADMIN_USER = "admin"
ADMIN_PASS = "Ab123987"
SESSIONS = set()

LOGIN_HTML = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><title>登录 · 短剧切片库</title>
<style>
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#0b0f16;color:#e8eef8;font-family:sans-serif}
.box{width:min(360px,92vw);background:#171e2b;border:1px solid #273044;border-radius:16px;padding:28px}
h1{font-size:20px;margin:0 0 18px}
label{display:block;font-size:12px;color:#8b9bb4;margin:10px 0 6px}
input{width:100%;padding:10px;border-radius:10px;border:1px solid #33445e;background:#0d131d;color:#fff}
button{width:100%;margin-top:16px;padding:10px;border:0;border-radius:10px;background:#0f766e;color:#fff;font-weight:700;cursor:pointer}
.err{color:#fca5a5;font-size:13px;min-height:18px;margin-top:8px}
</style></head><body><div class="box">
<h1>短剧切片库登录</h1>
<label>用户名</label><input id="u" autocomplete="username">
<label>密码</label><input id="p" type="password" autocomplete="current-password">
<div class="err" id="e"></div>
<button onclick="go()">登录</button>
</div>
<script>
async function go(){
  const fd=new FormData();
  fd.append('username',u.value);fd.append('password',p.value);
  const r=await fetch('/api/login',{method:'POST',body:fd});
  if(r.ok) location.href='/';
  else e.textContent='账号或密码错误';
}
p.addEventListener('keydown',ev=>{if(ev.key==='Enter')go()});
</script></body></html>"""

@app.middleware("http")
async def auth_gate(request: Request, call_next):
    path = request.url.path
    if path in ("/login", "/api/login", "/health"):
        return await call_next(request)
    if request.cookies.get("clip_sess") in SESSIONS:
        return await call_next(request)
    if path.startswith("/api/") or path.startswith("/media/"):
        return JSONResponse({"detail": "未登录"}, status_code=401)
    return RedirectResponse("/login", status_code=302)

@app.get("/login", response_class=HTMLResponse)
def login_page():
    return HTMLResponse(LOGIN_HTML)

@app.post("/api/login")
def api_login(username: str = Form(""), password: str = Form("")):
    if username != ADMIN_USER or password != ADMIN_PASS:
        raise HTTPException(401, "账号或密码错误")
    token = uuid.uuid4().hex
    SESSIONS.add(token)
    resp = JSONResponse({"ok": True})
    resp.set_cookie("clip_sess", token, httponly=True, samesite="lax", max_age=60*60*24*30)
    return resp

@app.post("/api/logout")
def api_logout(request: Request):
    SESSIONS.discard(request.cookies.get("clip_sess"))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("clip_sess")
    return resp

app.mount("/media", StaticFiles(directory=str(MEDIA)), name="media")

@app.get("/", response_class=HTMLResponse)
def home():
    p = ROOT/"static"/"index.html"
    if p.exists():
        return HTMLResponse(p.read_text(encoding="utf-8"))
    return HTMLResponse(MINI_HTML)

MINI_HTML = r"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><title>短剧切片库</title>
<style>
body{margin:0;background:#0b0f16;color:#e8eef8;font-family:sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:20px}
.g{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.card{background:#171e2b;border:1px solid #273044;border-radius:12px;padding:14px}
b{font-size:28px;display:block}
input,select,textarea,button{padding:8px;border-radius:8px;border:1px solid #33445e;background:#121826;color:#fff}
button{cursor:pointer}
.list div{padding:8px;border-bottom:1px solid #273044}
</style></head><body><div class="wrap">
<h2>短剧切片数据分析库</h2>
<div class="g" id="g"></div>
<p><input id="title" placeholder="短剧标题"> <button onclick="add()">新建</button></p>
<div id="list" class="card"></div>
</div>
<script>
async function api(u,o){const r=await fetch(u,o);return r.json()}
async function load(){
  const s=await api('/api/stats');
  g.innerHTML=`<div class="card">总素材<b>${s.total_materials}</b></div>
  <div class="card">找到账号<b>${s.found_accounts}</b></div>
  <div class="card">对方发布<b>${s.peer_publishes}</b>次</div>`;
  (s.platform_gauges||[]).forEach(p=>{
    g.innerHTML+=`<div class="card">${p.name}<b>${p.accounts}</b><small>发布${p.publishes}次</small></div>`
  });
  const w=await api('/api/works');
  list.innerHTML=(w.items||[]).map(i=>`<div>#${i.id} ${i.title} · 播放${i.total_plays} · 账号${i.total_accounts}</div>`).join('')||'暂无作品';
}
async function add(){
  const fd=new FormData();fd.append('title',title.value||'未命名短剧');
  await api('/api/works',{method:'POST',body:fd});title.value='';load();
}
load();
</script></body></html>"""

@app.get("/api/stats")
def stats():
    c = conn()
    works = c.execute("SELECT COUNT(*) n FROM works").fetchone()["n"]
    assets = c.execute("SELECT COUNT(*) n FROM assets").fetchone()["n"]
    copies = c.execute("SELECT COUNT(*) n FROM copies").fetchone()["n"]
    kws = c.execute("SELECT COUNT(*) n FROM keywords").fetchone()["n"]
    videos = c.execute("SELECT COUNT(*) n FROM assets WHERE asset_type IN ('video','clip')").fetchone()["n"]
    images = c.execute("SELECT COUNT(*) n FROM assets WHERE asset_type='image'").fetchone()["n"]
    plays = c.execute("SELECT COALESCE(SUM(play_count),0) n FROM analytics").fetchone()["n"]
    pubs = c.execute("SELECT COALESCE(SUM(account_count),0) n FROM analytics").fetchone()["n"]
    batches = c.execute("SELECT COUNT(*) n FROM analytics").fetchone()["n"]
    found = c.execute("SELECT COUNT(*) n FROM found_accounts").fetchone()["n"]
    by = {r["platform"]: r["n"] for r in c.execute("SELECT platform,COUNT(*) n FROM found_accounts GROUP BY platform")}
    pb = {r["platform"]: r["n"] for r in c.execute("SELECT platform,COALESCE(SUM(account_count),0) n FROM analytics GROUP BY platform")}
    c.close()
    gauges = [{"id":k,"name":v,"accounts":by.get(k,0),"publishes":pb.get(k,0)} for k,v in PLAT.items()]
    return {"works":works,"videos":videos,"images":images,"copies":copies,"keywords":kws,"assets":assets,
            "total_materials":assets+copies+kws,"total_plays":plays,"peer_publishes":pubs,
            "publish_batches":batches,"found_accounts":found,"platform_gauges":gauges,"media_gb":0}

@app.get("/api/works")
def works(q: str = "", page: int = 1):
    c = conn(); like = f"%{q}%"
    rows = c.execute("""
      SELECT w.*, 
        (SELECT COUNT(*) FROM assets a WHERE a.work_id=w.id AND a.asset_type='video') video_count,
        (SELECT COUNT(*) FROM assets a WHERE a.work_id=w.id AND a.asset_type='image') image_count,
        (SELECT COUNT(*) FROM assets a WHERE a.work_id=w.id AND a.asset_type='clip') clip_count,
        (SELECT COUNT(*) FROM copies x WHERE x.work_id=w.id) copy_count,
        (SELECT COALESCE(SUM(play_count),0) FROM analytics an WHERE an.work_id=w.id) total_plays,
        (SELECT COALESCE(SUM(account_count),0) FROM analytics an WHERE an.work_id=w.id) total_accounts,
        (SELECT '/media/'||a.rel_path FROM assets a WHERE a.work_id=w.id AND a.asset_type IN ('video','clip') ORDER BY a.id DESC LIMIT 1) video_url,
        (SELECT '/media/'||a.rel_path FROM assets a WHERE a.work_id=w.id AND a.asset_type='image' ORDER BY a.id DESC LIMIT 1) cover_url
      FROM works w
      WHERE w.title LIKE ? OR w.code LIKE ? OR IFNULL(w.episode_no,'') LIKE ? OR IFNULL(w.series_name,'') LIKE ? OR IFNULL(w.master_keywords,'') LIKE ?
      ORDER BY w.id DESC LIMIT 50 OFFSET ?
    """, (like,like,like,like,like,(page-1)*50)).fetchall()
    total = c.execute("SELECT COUNT(*) n FROM works").fetchone()["n"]
    items = []
    for r in rows:
        d = dict(r)
        def urls(typ, framed, with_id=False):
            q = "SELECT id, '/media/'||rel_path AS url FROM assets WHERE work_id=? AND asset_type IN ({}) AND {} ORDER BY id DESC".format(
                ",".join("?"*len(typ)), "IFNULL(frame_id,0)>0" if framed else "IFNULL(frame_id,0)=0")
            rows = c.execute(q, (d["id"],)+tuple(typ)).fetchall()
            if with_id:
                return [{"id":x["id"],"url":x["url"]} for x in rows]
            return [x["url"] for x in rows]
        d["videos"] = urls(("video","clip"), False)
        d["images"] = urls(("image",), False)
        d["frame_videos"] = urls(("video","clip"), True, True)
        d["frame_images"] = urls(("image",), True, True)
        frs = [dict(x) for x in c.execute("SELECT * FROM frames WHERE work_id=? ORDER BY id ASC", (d["id"],))]
        for f in frs:
            f["videos"] = [{"id":x["id"],"url":"/media/"+x["rel_path"]} for x in c.execute("SELECT id,rel_path FROM assets WHERE frame_id=? AND asset_type IN ('video','clip')", (f["id"],))]
            f["images"] = [{"id":x["id"],"url":"/media/"+x["rel_path"]} for x in c.execute("SELECT id,rel_path FROM assets WHERE frame_id=? AND asset_type='image'", (f["id"],))]
        d["frames"] = frs
        d["video_url"] = (d["videos"] or [None])[0]
        d["cover_url"] = (d["images"] or [None])[0]
        items.append(d)
    c.close()
    return {"total":total,"page":page,"page_size":50,"items":items}

@app.post("/api/works")
def add_work(title: str = Form(...), series_name: str = Form(""), episode_no: str = Form(""),
             duration_sec: int = Form(60), master_keywords: str = Form(""), status: str = Form("draft"),
             description: str = Form(""), genre: str = Form(""), language: str = Form("zh")):
    c = conn(); t = now(); code = "SD"+datetime.now().strftime("%y%m%d")+uuid.uuid4().hex[:5].upper()
    cur = c.execute("INSERT INTO works(code,title,series_name,episode_no,duration_sec,master_keywords,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (code,title.strip(),series_name,episode_no,duration_sec,master_keywords,status,t,t))
    c.commit(); wid = cur.lastrowid; c.close()
    return {"ok":True,"id":wid,"code":code}

@app.get("/api/works/{wid}")
def one(wid: int):
    c = conn()
    w = c.execute("SELECT * FROM works WHERE id=?", (wid,)).fetchone()
    if not w: raise HTTPException(404)
    copies = [dict(x) for x in c.execute("SELECT * FROM copies WHERE work_id=? ORDER BY id DESC",(wid,))]
    keywords = [dict(x) for x in c.execute("SELECT * FROM keywords WHERE work_id=? ORDER BY id DESC",(wid,))]
    assets = [dict(x) for x in c.execute("SELECT * FROM assets WHERE work_id=? ORDER BY id DESC",(wid,))]
    analytics = [dict(x) for x in c.execute("SELECT * FROM analytics WHERE work_id=? ORDER BY id DESC",(wid,))]
    frames = [dict(x) for x in c.execute("SELECT * FROM frames WHERE work_id=? ORDER BY id ASC",(wid,))]
    c.close()
    grouped = {p:{"videos":[],"images":[],"clips":[]} for p in PLAT}
    for a in assets:
        a["url"] = "/media/"+a["rel_path"]
        bucket = {"video":"videos","image":"images","clip":"clips"}.get(a["asset_type"])
        if a.get("platform") in grouped and bucket:
            grouped[a["platform"]][bucket].append(a)
    return {"work":dict(w),"copies":copies,"keywords":keywords,"assets":[dict(a) for a in assets],
            "assets_by_platform":grouped,"analytics":analytics,"frames":frames}

@app.put("/api/works/{wid}")
def upd(wid: int, title: str = Form(...), series_name: str = Form(""), episode_no: str = Form(""),
        duration_sec: int = Form(60), master_keywords: str = Form(""), status: str = Form("draft"),
        description: str = Form(""), genre: str = Form(""), language: str = Form("zh")):
    c = conn()
    c.execute("UPDATE works SET title=?,series_name=?,episode_no=?,duration_sec=?,master_keywords=?,status=?,updated_at=? WHERE id=?",
              (title,series_name,episode_no,duration_sec,master_keywords,status,now(),wid))
    c.commit(); c.close(); return {"ok":True}

@app.delete("/api/works/{wid}")
def dw(wid: int):
    c = conn(); c.execute("DELETE FROM works WHERE id=?", (wid,)); c.commit(); c.close(); return {"ok":True}

@app.get("/api/frames")
def list_frames():
    c = conn()
    rows = c.execute("""SELECT f.*, w.title AS work_title, w.code AS work_code
                        FROM frames f JOIN works w ON w.id=f.work_id
                        ORDER BY f.id DESC""").fetchall()
    items=[]
    for r in rows:
        d=dict(r)
        d["videos"]=[x["url"] for x in c.execute("SELECT '/media/'||rel_path AS url FROM assets WHERE frame_id=? AND asset_type IN ('video','clip')", (d["id"],))]
        d["images"]=[x["url"] for x in c.execute("SELECT '/media/'||rel_path AS url FROM assets WHERE frame_id=? AND asset_type='image'", (d["id"],))]
        items.append(d)
    works=[{"id":x["id"],"title":x["title"],"code":x["code"]} for x in c.execute("SELECT id,title,code FROM works ORDER BY id DESC")]
    c.close()
    return {"items":items,"works":works}

@app.post("/api/works/{wid}/frames")
def add_frame(wid: int, platform: str = Form(""), copy_title: str = Form(""), keywords: str = Form(""),
              body: str = Form(""), publish_time: str = Form(""), play_count: int = Form(0),
              repost_count: int = Form(0), account_count: int = Form(0), notes: str = Form("")):
    c = conn()
    cur = c.execute("""INSERT INTO frames(work_id,platform,copy_title,keywords,body,publish_time,play_count,repost_count,account_count,notes,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (wid, platform.strip() or "未填平台", copy_title, keywords, body, publish_time, play_count, repost_count, account_count, notes, now()))
    c.commit(); fid = cur.lastrowid; c.close()
    return {"ok": True, "id": fid}

@app.put("/api/frames/{fid}")
def upd_frame(fid: int, platform: str = Form(""), copy_title: str = Form(""), keywords: str = Form(""),
              body: str = Form(""), publish_time: str = Form(""), play_count: int = Form(0),
              repost_count: int = Form(0), account_count: int = Form(0), notes: str = Form("")):
    c = conn()
    c.execute("""UPDATE frames SET platform=?,copy_title=?,keywords=?,body=?,publish_time=?,play_count=?,repost_count=?,account_count=?,notes=? WHERE id=?""",
              (platform.strip() or "未填平台", copy_title, keywords, body, publish_time, play_count, repost_count, account_count, notes, fid))
    c.commit(); c.close(); return {"ok": True}

@app.delete("/api/works/{wid}/frames")
def del_work_frames(wid: int):
    c = conn()
    ids = [r["id"] for r in c.execute("SELECT id FROM frames WHERE work_id=?", (wid,))]
    if ids:
        c.execute("DELETE FROM assets WHERE frame_id IN (%s)" % ",".join("?"*len(ids)), ids)
        c.execute("DELETE FROM frames WHERE work_id=?", (wid,))
    c.commit(); c.close()
    return {"ok": True}

@app.delete("/api/frames/{fid}")
def del_frame(fid: int):
    c = conn()
    c.execute("UPDATE assets SET frame_id=NULL WHERE frame_id=?", (fid,))
    c.execute("DELETE FROM frames WHERE id=?", (fid,))
    c.commit(); c.close(); return {"ok": True}

@app.post("/api/works/{wid}/assets")
async def up(wid: int, platform: str = Form(""), asset_type: str = Form(...),
             caption: str = Form(""), source_platform: str = Form(""),
             highlight_note: str = Form(""), clip_copy: str = Form(""),
             frame_id: int = Form(0), file: UploadFile = File(...)):
    kind = {"video":"videos","image":"images","clip":"clips"}[asset_type]
    suf = Path(file.filename or "f.bin").suffix or ".bin"
    name = datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8] + suf
    dest = MEDIA/kind/name; size=0
    with dest.open("wb") as f:
        while True:
            b = await file.read(1024*1024)
            if not b: break
            f.write(b); size += len(b)
    c = conn()
    c.execute("""INSERT INTO assets(work_id,platform,asset_type,filename,rel_path,size_bytes,source_platform,highlight_note,clip_copy,created_at,frame_id)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
              (wid,platform or "",asset_type,file.filename,f"{kind}/{name}",size,source_platform,highlight_note,clip_copy,now(), frame_id or None))
    c.commit(); c.close()
    return {"ok":True,"url":f"/media/{kind}/{name}"}

@app.delete("/api/assets/{aid}")
def da(aid: int):
    c = conn(); c.execute("DELETE FROM assets WHERE id=?", (aid,)); c.commit(); c.close(); return {"ok":True}

@app.post("/api/works/{wid}/copies")
def ac(wid: int, platform: str = Form(...), copy_type: str = Form("main"), title: str = Form(""), body: str = Form(...), language: str = Form("zh")):
    c = conn(); c.execute("INSERT INTO copies(work_id,platform,copy_type,title,body,created_at) VALUES(?,?,?,?,?,?)",
                         (wid,platform,copy_type,title,body,now())); c.commit(); c.close(); return {"ok":True}

@app.delete("/api/copies/{i}")
def dc(i: int):
    c = conn(); c.execute("DELETE FROM copies WHERE id=?", (i,)); c.commit(); c.close(); return {"ok":True}

@app.post("/api/works/{wid}/keywords")
def ak(wid: int, platform: str = Form(...), words: str = Form(...), note: str = Form("")):
    c = conn(); c.execute("INSERT INTO keywords(work_id,platform,words,note,created_at) VALUES(?,?,?,?,?)",
                         (wid,platform,words,note,now())); c.commit(); c.close(); return {"ok":True}

@app.delete("/api/keywords/{i}")
def dk(i: int):
    c = conn(); c.execute("DELETE FROM keywords WHERE id=?", (i,)); c.commit(); c.close(); return {"ok":True}

@app.post("/api/works/{wid}/analytics")
def aa(wid: int, platform: str = Form(...), publish_time: str = Form(""), play_count: int = Form(0),
       account_count: int = Form(0), used_copy: str = Form(""), used_title: str = Form(""),
       account_names: str = Form(""), likes: int = Form(0), comments: int = Form(0), shares: int = Form(0), notes: str = Form("")):
    c = conn()
    c.execute("""INSERT INTO analytics(work_id,platform,publish_time,play_count,account_count,used_copy,used_title,account_names,recorded_at)
                 VALUES(?,?,?,?,?,?,?,?,?)""",
              (wid,platform,publish_time,play_count,account_count,used_copy,used_title,account_names,now()))
    upsert_acc(c, platform, account_names, account_count or 0)
    c.commit(); c.close(); return {"ok":True}

@app.delete("/api/analytics/{i}")
def dan(i: int):
    c = conn(); c.execute("DELETE FROM analytics WHERE id=?", (i,)); c.commit(); c.close(); return {"ok":True}

@app.get("/api/accounts")
def la(platform: str = "", q: str = ""):
    c = conn(); rows = c.execute("SELECT * FROM found_accounts ORDER BY publish_count DESC").fetchall(); c.close()
    return {"items":[dict(r) for r in rows],"total":len(rows)}

@app.post("/api/accounts")
def pa(platform: str = Form(...), handle: str = Form(...), display_name: str = Form(""),
       source: str = Form("found"), profile_url: str = Form(""), note: str = Form(""), publish_count: int = Form(0)):
    c = conn()
    try:
        c.execute("INSERT INTO found_accounts(platform,handle,display_name,note,publish_count,last_seen) VALUES(?,?,?,?,?,?)",
                  (platform,handle.strip().lstrip("@"),display_name or handle,note,publish_count,now()))
        c.commit()
    except sqlite3.IntegrityError:
        c.close(); raise HTTPException(400,"已存在")
    c.close(); return {"ok":True}

@app.delete("/api/accounts/{i}")
def dacc(i: int):
    c = conn(); c.execute("DELETE FROM found_accounts WHERE id=?", (i,)); c.commit(); c.close(); return {"ok":True}

def setting(k, default=""):
    c = conn()
    row = c.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    c.close()
    return row["v"] if row else default

@app.get("/api/telegram/config")
def tg_get():
    token = setting("tg_token")
    return {"configured": bool(token), "chat_id": setting("tg_chat")}

@app.post("/api/telegram/config")
def tg_set(token: str = Form(""), chat_id: str = Form("")):
    c = conn()
    c.execute("INSERT INTO settings(k,v) VALUES('tg_token',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (token.strip(),))
    c.execute("INSERT INTO settings(k,v) VALUES('tg_chat',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (chat_id.strip(),))
    c.commit(); c.close()
    return {"ok": True, "configured": bool(token.strip())}

@app.post("/api/works/{wid}/send_telegram")
def tg_send(wid: int):
    token, chat = setting("tg_token"), setting("tg_chat")
    if not token or not chat:
        raise HTTPException(400, "请先填写 Telegram Bot Token 和 Chat ID")
    c = conn()
    w = c.execute("SELECT * FROM works WHERE id=?", (wid,)).fetchone()
    a = c.execute("SELECT * FROM assets WHERE work_id=? AND asset_type='video' ORDER BY id DESC LIMIT 1", (wid,)).fetchone()
    c.close()
    if not a:
        raise HTTPException(400, "这部短剧还没有视频")
    path = MEDIA / a["rel_path"]
    if not path.exists():
        raise HTTPException(400, "视频文件不在服务器上")
    cap = f"{w['title']} {w['code']}"
    r = subprocess.run(
        ["curl", "-sS", f"https://api.telegram.org/bot{token}/sendVideo",
         "-F", f"chat_id={chat}", "-F", f"video=@{path}", "-F", f"caption={cap}",
         "-F", "supports_streaming=true"],
        capture_output=True, text=True, timeout=180
    )
    if r.returncode != 0:
        raise HTTPException(500, r.stderr or "发送失败")
    try:
        data = json.loads(r.stdout)
    except Exception:
        raise HTTPException(500, r.stdout[:300])
    if not data.get("ok"):
        raise HTTPException(400, data.get("description") or r.stdout[:300])
    return {"ok": True}

@app.get("/health")
def health():
    return {"ok":True}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8765)
