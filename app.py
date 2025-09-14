#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, io, sys, types, zipfile, sqlite3, time, statistics, uuid, json, tempfile, hashlib
from datetime import datetime
from contextlib import redirect_stdout
from concurrent.futures import ThreadPoolExecutor

import streamlit as st

# ---- Pygifsicle stub ----
fake = types.ModuleType("pygifsicle")
def optimize(*a, **k): return
fake.optimize = optimize
sys.modules["pygifsicle"] = fake

# ---- Maxfield ----
from maxfield.maxfield import maxfield as run_maxfield

# ---- Config ----
st.set_page_config(page_title="Maxfield Online", page_icon="🗺️", layout="centered")

bg_url = st.secrets.get("BG_URL","").strip()
st.markdown(f"""
<style>
.stApp {{"background: url('{bg_url}') no-repeat center center fixed; background-size: cover;" if bg_url else ""}}
@media (prefers-color-scheme: light) {{
  .stApp .block-container {{background: rgba(255,255,255,0.92); color: #111;}}
  .stApp .block-container a {{color:#005bbb;}}
}}
@media (prefers-color-scheme: dark) {{
  .stApp .block-container {{background: rgba(20,20,20,0.78); color:#eaeaea;}}
  .stApp .block-container a {{color:#8ecaff;}}
}}
.stApp .block-container {{border-radius:12px; padding:1rem 1.2rem 2rem;}}
.mf-chip{{display:inline-block;padding:4px 10px;border-radius:999px;font-size:12px;margin-right:8px;color:#fff;box-shadow:0 2px 6px rgba(0,0,0,.2)}}
.mf-chip.enl{{background:#25c025}} .mf-chip.res{{background:#2b6dff}}
.mf-badge{{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;margin-left:8px;background:#00000022;}}
div[data-baseweb="tab-list"] button{{padding:12px 18px!important;margin:0 6px 8px 0!important;border-radius:999px!important;border:1px solid rgba(0,0,0,.08)!important;box-shadow:0 2px 6px rgba(0,0,0,.06)!important;font-weight:600!important;}}
div[data-baseweb="tab"] p{{font-size:15px!important;}}
/* Post action buttons side-by-side */
.mf-actions{display:flex;gap:12px;justify-content:flex-end;margin-top:-6px;margin-bottom:6px;flex-wrap:wrap}
/* Big primary nav buttons row */
.mf-mainrow{display:flex;gap:12px;flex-wrap:wrap;justify-content:center;margin:12px 0 6px}
.mf-mainrow .stButton>button{padding:10px 14px;border-radius:14px;font-weight:600;border:1px solid #0001;box-shadow:0 1px 4px rgba(0,0,0,.06)}
.mf-mainrow .stButton>button:hover{transform:translateY(-1px)}
</style>
""", unsafe_allow_html=True)

# =================== DB ===================
@st.cache_resource(show_spinner=False)
def get_db():
    os.makedirs("data", exist_ok=True)
    conn = sqlite3.connect(os.path.join("data","app.db"), check_same_thread=False)
    conn.execute("CREATE TABLE IF NOT EXISTS metrics(key TEXT PRIMARY KEY, value INTEGER NOT NULL)")
    for k in ("visits","plans_completed"):
        conn.execute("INSERT OR IGNORE INTO metrics(key,value) VALUES(?,0)", (k,))
    conn.execute("CREATE TABLE IF NOT EXISTS runs(ts INTEGER, n_portais INTEGER, num_cpus INTEGER, gif INTEGER, dur_s REAL)")
    conn.execute("""CREATE TABLE IF NOT EXISTS jobs(
        job_id TEXT PRIMARY KEY, ts INTEGER, uid TEXT, n_portais INTEGER, num_cpus INTEGER, team TEXT,
        output_csv INTEGER, fazer_gif INTEGER, dur_s REAL, out_dir TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS housekeeping(key TEXT PRIMARY KEY, value TEXT)""")

    # Users/sessions
    conn.execute("CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT, username_lc TEXT, pass_hash TEXT, pass_salt TEXT, faction TEXT, email TEXT, avatar_ext TEXT, is_admin INTEGER DEFAULT 0, created_ts INTEGER, updated_ts INTEGER)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username_lc ON users(username_lc)")
    conn.execute("CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER, created_ts INTEGER, last_seen_ts INTEGER)")

    # Forum
    conn.execute("CREATE TABLE IF NOT EXISTS forum_posts(id INTEGER PRIMARY KEY AUTOINCREMENT, cat TEXT, title TEXT, body_md TEXT, author_id INTEGER, author_name TEXT, author_faction TEXT, created_ts INTEGER, updated_ts INTEGER, images_json TEXT, is_pinned INTEGER DEFAULT 0)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_cat ON forum_posts(cat)")
    conn.execute("CREATE TABLE IF NOT EXISTS forum_comments(id INTEGER PRIMARY KEY AUTOINCREMENT, post_id INTEGER, author_id INTEGER, author_name TEXT, author_faction TEXT, body_md TEXT, created_ts INTEGER, deleted_ts INTEGER)")
    return conn

def inc_metric(k, d=1):
    conn = get_db(); conn.execute("UPDATE metrics SET value=value+? WHERE key=?", (d,k)); conn.commit()
def get_metric(k):
    row = get_db().execute("SELECT value FROM metrics WHERE key=?", (k,)).fetchone()
    return int(row[0]) if row else 0

def record_run(n,c,g,d):
    get_db().execute("INSERT INTO runs(ts,n_portais,num_cpus,gif,dur_s) VALUES(?,?,?,?,?)",(int(time.time()),n,c,1 if g else 0,float(d))); get_db().commit()
def add_job_row(job_id,uid,n_portais,num_cpus,team,output_csv,fazer_gif,dur_s,out_dir):
    get_db().execute("""INSERT OR REPLACE INTO jobs(job_id,ts,uid,n_portais,num_cpus,team,output_csv,fazer_gif,dur_s,out_dir)
    VALUES(?,?,?,?,?,?,?,?,?,?)""",(job_id,int(time.time()),uid,n_portais,num_cpus,team,1 if output_csv else 0,1 if fazer_gif else 0,float(dur_s),out_dir)); get_db().commit()
def list_jobs_recent(uid, within_hours=24, limit=50):
    min_ts = int(time.time()) - within_hours*3600
    conn = get_db()
    if uid:
        cur = conn.execute("""SELECT job_id,ts,uid,n_portais,num_cpus,team,output_csv,fazer_gif,dur_s,out_dir
                              FROM jobs WHERE ts>=? AND uid=? ORDER BY ts DESC LIMIT ?""",(min_ts,uid,limit))
    else:
        cur = conn.execute("""SELECT job_id,ts,uid,n_portais,num_cpus,team,output_csv,fazer_gif,dur_s,out_dir
                              FROM jobs WHERE ts>=? ORDER BY ts DESC LIMIT ?""",(min_ts,limit))
    return cur.fetchall()

def daily_cleanup(retain_hours=24):
    conn = get_db(); today = datetime.now().strftime("%Y-%m-%d")
    row = conn.execute("SELECT value FROM housekeeping WHERE key='last_cleanup'").fetchone()
    if (row or [None])[0] == today: return
    root = os.path.join("data","jobs"); now = time.time()
    if os.path.isdir(root):
        for jid in os.listdir(root):
            d = os.path.join(root, jid)
            try: st_mtime = os.path.getmtime(d)
            except FileNotFoundError: continue
            if now - st_mtime > retain_hours*3600:
                for base,_,files in os.walk(d, topdown=False):
                    for fn in files:
                        try: os.remove(os.path.join(base,fn))
                        except: pass
                    try: os.rmdir(base)
                    except: pass
    min_ts = int(time.time()) - retain_hours*3600
    conn.execute("DELETE FROM jobs WHERE ts < ?", (min_ts,))
    conn.execute("DELETE FROM runs WHERE ts < ?", (min_ts,))
    conn.execute("INSERT OR REPLACE INTO housekeeping(key,value) VALUES('last_cleanup', ?)", (today,))
    conn.commit()
daily_cleanup(24)

if "visit_counted" not in st.session_state:
    inc_metric("visits",1); st.session_state["visit_counted"]=True

# =================== Utils ===================
def contar_portais(texto):
    return sum(1 for ln in texto.splitlines() if (s:=ln.strip()) and not s.startswith("#"))
def clean_invisibles(s):
    for ch in ["\ufeff","\u200b","\u200c","\u200d","\u2060","\xa0"]: s=s.replace(ch," " if ch=="\xa0" else "")
    return s
def extract_points(texto):
    pts=[]; 
    for ln in texto.splitlines():
        s=ln.strip()
        if not s or s.startswith("#"): continue
        try:
            parts=s.split(";"); url=parts[1].strip() if len(parts)>1 else ""
            if "pll=" in url:
                pll=url.split("pll=")[1].split("&")[0]; lat_s,lon_s=pll.split(",")
                pts.append({"name":parts[0].strip() or "Portal","lat":float(lat_s),"lon":float(lon_s)})
        except: pass
    return pts

def qp_get(n,default=""):
    try:
        params = getattr(st,"query_params",None)
        if params is not None: return params.get(n) or default
        qp = st.experimental_get_query_params(); return qp.get(n,[default])[0]
    except: return default
def qp_set(**kwargs):
    try:
        params = getattr(st,"query_params",None)
        if params is not None:
            for k,v in kwargs.items():
                if v is None:
                    try: del params[k]
                    except KeyError: pass
                else: params[k]=v
        else:
            cur=st.experimental_get_query_params()
            for k,v in kwargs.items():
                if v is None: cur.pop(k,None)
                else: cur[k]=[v]
            st.experimental_set_query_params(**cur)
    except: pass

# Anonymous ID
if "uid" not in st.session_state:
    cur_uid = qp_get("uid","") or uuid.uuid4().hex[:8]; qp_set(uid=cur_uid); st.session_state["uid"]=cur_uid
UID = st.session_state["uid"]

# Public params / userscript
PUBLIC_URL = (st.secrets.get("PUBLIC_URL","https://maxfield.fun/").rstrip("/")+"/")
MIN_ZOOM = int(st.secrets.get("MIN_ZOOM",15))
MAX_PORTALS = int(st.secrets.get("MAX_PORTALS",200))
MAX_URL_LEN = int(st.secrets.get("MAX_URL_LEN",6000))
DEST = PUBLIC_URL
EXEMPLO_TXT = "Portal 1; https://intel.ingress.com/intel?pll=-10.912345,-37.065432\nPortal 2; https://intel.ingress.com/intel?pll=-10.913210,-37.061234\n"

IITC_USERSCRIPT = ("""// ==UserScript==
// @name Maxfield — Send Portals
// @match https://intel.ingress.com/*
// ==/UserScript==
function wrapper(){const s={MIN_ZOOM:%d,MAX_PORTALS:%d,MAX_URL_LEN:%d,DEST:'%s'};
const get=()=>{const m=window.map,b=m&&m.getBounds?m.getBounds():null;if(!b)return[];const out=[];
for(const id in window.portals){const p=window.portals[id];if(!p||!p.getLatLng)continue;
const ll=p.getLatLng();if(!b.contains(ll))continue;const n=(p.options?.data?.title||'Portal');
out.push(n+'; https://intel.ingress.com/intel?pll='+ll.lat.toFixed(6)+','+ll.lng.toFixed(6));if(out.length>=s.MAX_PORTALS)break;}return out;};
window.plugin=window.plugin||{};window.plugin.maxfieldSender={send:()=>{const L=get();if(L.length==0){alert('Sem portais visíveis.');return;}
let text=L.join('\\n'); const full=s.DEST+'?list='+encodeURIComponent(text); if(full.length>s.MAX_URL_LEN){navigator.clipboard.writeText(text); alert('Lista copiada. Abra o Maxfield e cole.'); window.open(s.DEST,'_blank'); return;}
navigator.clipboard.writeText(full); window.open(full,'_blank');}};} wrapper();""" % (MIN_ZOOM,MAX_PORTALS,MAX_URL_LEN,DEST))

# =================== Header ===================
st.title("Ingress Maxfield — Gerador de Planos")
colv,colp=st.columns(2)
with colv: st.metric("Acessos (sessões)", f"{get_metric('visits'):,}")
with colp: st.metric("Planos gerados", f"{get_metric('plans_completed'):,}")

st.markdown("""- Envie o **arquivo .txt** ou **cole** o conteúdo.
- Informe **nº de agentes** e **CPUs**.
- **Mapa de fundo (opcional)**: informe Google Maps API key ou deixe em branco.
- Resultados: **imagens**, **CSVs** e (se permitido) **GIF**.
""")
c1,c2,c3,c4 = st.columns(4)
with c1: st.download_button("📄 Baixar modelo (.txt)", EXEMPLO_TXT.encode(), file_name="modelo_portais.txt", mime="text/plain")
with c2: st.download_button("🧩 Baixar plugin IITC", IITC_USERSCRIPT.encode(), file_name="maxfield_iitc.user.js", mime="application/javascript")
with c3:
    TUTORIAL_URL = st.secrets.get("TUTORIAL_URL","https://www.youtube.com/")
    st.link_button("▶️ Tutorial (normal)", TUTORIAL_URL)
with c4:
    TUTORIAL_IITC_URL = st.secrets.get("TUTORIAL_IITC_URL", TUTORIAL_URL)
    st.link_button("▶️ Tutorial (via IITC)", TUTORIAL_IITC_URL)

# =================== Job Manager ===================
@st.cache_resource(show_spinner=False)
def job_manager(): return {"executor": ThreadPoolExecutor(max_workers=1), "jobs": {}}
def prune_jobs(max_jobs=5, max_age_s=3600):
    jm=job_manager(); now=time.time(); to_del=[]
    for jid,rec in list(jm["jobs"].items()):
        age=now-float(rec.get("t0",now))
        if age>max_age_s or (rec.get("done") and age>300): to_del.append(jid)
    alive=[(jid,rec["t0"]) for jid,rec in jm["jobs"].items() if jid not in to_del]
    if len(alive)>max_jobs:
        alive.sort(key=lambda x:x[1]); to_del.extend([jid for jid,_ in alive[:-max_jobs]])
    for jid in to_del:
        try:
            fut=jm["jobs"][jid]["future"]
            if fut and not fut.done(): fut.cancel()
        except: pass
        jm["jobs"].pop(jid,None)

def run_job(kwargs): 
    t0=time.time()
    try:
        res=processar_plano(**kwargs); return {"ok":True,"result":res,"elapsed":time.time()-t0}
    except Exception as e:
        return {"ok":False,"error":str(e),"elapsed":time.time()-t0}

def start_job(kwargs, eta_s, meta):
    prune_jobs(); jm=job_manager(); job_id=uuid.uuid4().hex[:8]
    fut=jm["executor"].submit(run_job, kwargs|{"job_id":job_id,"team":meta.get("team","")})
    jm["jobs"][job_id]={"future":fut,"t0":time.time(),"eta":eta_s,"meta":meta,"done":False,"out":None}
    return job_id
def get_job(job_id): return job_manager()["jobs"].get(job_id)

# =================== Plan processing ===================
@st.cache_data(show_spinner=False, ttl=3600)
def processar_plano(portal_bytes,num_agents,num_cpus,res_colors,google_api_key,google_api_secret,output_csv,fazer_gif,job_id,team):
    jobs_root=os.path.join("data","jobs"); os.makedirs(jobs_root,exist_ok=True)
    outdir=os.path.join(jobs_root,job_id); os.makedirs(outdir,exist_ok=True)
    portal_path=os.path.join(outdir,"portais.txt"); open(portal_path,"wb").write(portal_bytes)

    t0=time.time(); log_buffer=io.StringIO()
    def t(msg): print(f"[{time.strftime('%H:%M:%S')}] {msg}")
    try:
        with redirect_stdout(log_buffer):
            t("INÍCIO processar_plano")
            print(f"[INFO] os.cpu_count()={os.cpu_count()} · cpus_eff={num_cpus} · gif={fazer_gif} · csv={output_csv} · team={team}")
            t("Chamando run_maxfield()…")
            run_maxfield(portal_path,num_agents=int(num_agents),num_cpus=int(num_cpus),res_colors=res_colors,
                         google_api_key=(google_api_key or None),google_api_secret=(google_api_secret or None),
                         output_csv=output_csv,outdir=outdir,verbose=True,skip_step_plots=(not fazer_gif))
            t(f"maxfield() OK em {time.time()-t0:.1f}s"); t1=time.time(); t("Compactando artefatos no ZIP…")
    except Exception as e:
        log_buffer.write(f"\n[ERRO] {e}\n"); raise

    zip_path=os.path.join(outdir,f"maxfield_output_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip")
    with zipfile.ZipFile(zip_path,"w",zipfile.ZIP_DEFLATED) as z:
        for root,_,files in os.walk(outdir):
            for fn in files:
                if fn.endswith(".zip"): continue
                z.write(os.path.join(root,fn), arcname=os.path.relpath(os.path.join(root,fn), outdir))
    with redirect_stdout(log_buffer):
        print(f"[{time.strftime('%H:%M:%S')}] ZIP pronto em {time.time()-t1:.1f}s; total {time.time()-t0:.1f}s")

    log_txt=log_buffer.getvalue(); open(os.path.join(outdir,"maxfield_log.txt"),"w",encoding="utf-8",errors="ignore").write(log_txt or "")
    def rb(p): return open(p,"rb").read() if os.path.exists(p) else None
    return {"zip_bytes":open(zip_path,"rb").read(),
            "pm_bytes":rb(os.path.join(outdir,"portal_map.png")),
            "lm_bytes":rb(os.path.join(outdir,"link_map.png")),
            "gif_bytes":rb(os.path.join(outdir,"plan_movie.gif")),
            "log_txt":log_txt,"outdir":outdir,"job_id":job_id}

def estimate_eta_s(n_portais,num_cpus,gif):
    base_pp=0.35 if not gif else 0.55; base_over=3.0 if not gif else 8.0
    cpu_factor=1.0/max(1.0,(0.6+0.5*min(num_cpus,8)**0.5)); est=(base_over+base_pp*n_portais)*cpu_factor
    cur=get_db().execute("SELECT dur_s,n_portais FROM runs WHERE gif=? ORDER BY ts DESC LIMIT 50",(1 if gif else 0,))
    rows=cur.fetchall()
    if rows:
        pps=[r[0]/max(1,r[1]) for r in rows if r[1]>0]
        if pps: est=(statistics.median(pps)*n_portais)*cpu_factor + (1.5 if not gif else 4.0)
    return max(2.0, est)

# =================== Tabs ===================
ENABLE_FORUM = bool(st.secrets.get("ENABLE_FORUM", True))
tabs = ["🧩 Gerar plano","🕑 Histórico","📊 Métricas"] + (["💬 Fórum (debate e melhorias)"] if ENABLE_FORUM else [])
tab_gen, tab_hist, tab_metrics, *rest = st.tabs(tabs)
tab_forum = rest[0] if rest else None

# ===== TAB: GERAR PLANO =====
with tab_gen:
    st.markdown('<span class="mf-chip enl">Enlightened</span><span class="mf-chip res">Resistance</span>', unsafe_allow_html=True)
    fast_mode = st.toggle("⚡ Modo rápido (desliga GIF e CSV)", value=False, key="fast_mode")

    # state for uploader/text
    if "uploader_key" not in st.session_state: st.session_state["uploader_key"]=0
    if "txt_content" not in st.session_state: st.session_state["txt_content"]= (getattr(st,"query_params",None).get("list") or "") if hasattr(st,"query_params") else st.experimental_get_query_params().get("list",[""])[0]
    if st.session_state.get("_clear_text",False): st.session_state["_clear_text"]=False; st.session_state["txt_content"]=""

    with st.form("plan_form"):
        uploaded = st.file_uploader("Arquivo de portais (.txt)", type=["txt"], key=f"uploader_{st.session_state['uploader_key']}")
        txt_content = st.text_area("Ou cole o conteúdo do arquivo de portais", height=200, key="txt_content")
        with st.expander("🗺️ Pré-visualização dos portais (opcional)"):
            txt_preview = st.session_state["txt_content"] or (uploaded.getvalue().decode("utf-8","ignore") if uploaded else "")
            pts = extract_points(clean_invisibles(txt_preview)); st.write(f"Detectados **{len(pts)}** portais para prévia.")
            if pts:
                import pandas as pd, pydeck as pdk
                df=pd.DataFrame(pts); mid_lat=df["lat"].mean(); mid_lon=df["lon"].mean()
                layer=pdk.Layer("ScatterplotLayer", data=df, get_position='[lon, lat]', get_radius=12, pickable=True)
                st.pydeck_chart(pdk.Deck(map_style=None, initial_view_state=pdk.ViewState(latitude=mid_lat, longitude=mid_lon, zoom=14), layers=[layer]))
            else: st.caption("Cole/importe uma lista com URLs contendo `pll=lat,lon` para ver a prévia.")

        col1,col2=st.columns(2)
        with col1: num_agents=st.number_input("Número de agentes",1,50,1,1,key="num_agents")
        with col2: num_cpus = st.number_input("CPUs a usar (0 = máximo)",0,128,0,1,key="num_cpus")
        team = st.selectbox("Facção (cores)", ["Enlightened (verde)","Resistance (azul)"], key="team_select")
        output_csv = st.checkbox("Gerar CSV", value=(not st.session_state.get("fast_mode",False)), disabled=st.session_state.get("fast_mode",False), key="out_csv")
        gerar_gif_checkbox = st.checkbox("Gerar GIF (passo-a-passo)", value=False, disabled=st.session_state.get("fast_mode",False), key="out_gif")
        st.markdown("**Mapa de fundo (opcional):**")
        gkey = st.text_input("Google Maps API key (opcional)", value="", key="g_key")
        gsec = st.text_input("Google Maps API secret (opcional)", value="", type="password", key="g_secret")
        sem_mapa = st.checkbox("Sem mapa de fundo (mais rápido/robusto)", value=False, key="no_bg_map")
        submitted = st.form_submit_button("Gerar plano", use_container_width=True)

    if submitted:
        if uploaded:
            texto_portais = uploaded.getvalue().decode("utf-8","ignore")
        else:
            if not st.session_state["txt_content"].strip(): st.error("Envie um arquivo .txt ou cole o conteúdo."); st.stop()
            texto_portais = st.session_state["txt_content"]
        texto_portais = clean_invisibles(texto_portais)
        MAX_PORTALS_SERVER=int(st.secrets.get("MAX_PORTALS",200))
        kept=[]; count=0
        for ln in texto_portais.splitlines():
            s=ln.strip()
            if s and not s.startswith("#"):
                count+=1
                if count>MAX_PORTALS_SERVER: continue
            kept.append(ln)
        if count>MAX_PORTALS_SERVER: st.warning(f"Lista com {count} portais; usando apenas os primeiros {MAX_PORTALS_SERVER}.")
        texto_portais="\n".join(kept); portal_bytes=texto_portais.encode()
        res_colors = team.startswith("Resistance"); n_portais = contar_portais(texto_portais)

        fazer_gif = (not st.session_state.get("fast_mode",False)) and bool(gerar_gif_checkbox)
        if n_portais>25 and fazer_gif: st.warning(f"{n_portais} portais. Desativando GIF automaticamente."); fazer_gif=False
        output_csv = (not st.session_state.get("fast_mode",False)) and bool(output_csv)

        if sem_mapa:
            google_api_key=None; google_api_secret=None; os.environ["MAXFIELD_DISABLE_BASEMAP"]="1"
        else:
            os.environ.pop("MAXFIELD_DISABLE_BASEMAP",None)
            google_api_key = (gkey or "").strip() or st.secrets.get("GOOGLE_API_KEY",None)
            google_api_secret = (gsec or "").strip() or st.secrets.get("GOOGLE_API_SECRET",None)

        eff_cpus=int(num_cpus) or min(4, os.cpu_count() or 2)
        kwargs=dict(portal_bytes=portal_bytes,num_agents=int(num_agents),num_cpus=int(eff_cpus),res_colors=res_colors,
                    google_api_key=google_api_key,google_api_secret=google_api_secret,output_csv=output_csv,
                    fazer_gif=fazer_gif, team=team)
        eta_s = estimate_eta_s(n_portais, int(eff_cpus), fazer_gif); meta={"n_portais":n_portais,"num_cpus":int(eff_cpus),"gif":fazer_gif,"team":team}
        st.session_state["_clear_text"]=True; st.session_state["uploader_key"]+=1
        new_id=start_job(kwargs,eta_s,meta); st.session_state["job_id"]=new_id; qp_set(job=new_id)
        st.toast(f"Job {new_id} enfileirado: {n_portais} portais · ETA ~{int(eta_s)}s"); st.rerun()

# ===== Job follow =====
job_id = st.session_state.get("job_id")
if job_id:
    job=get_job(job_id)
    if not job: st.warning("Job não encontrado."); qp_set(job=None)
    else:
        if job.get("done") and job.get("out") is not None:
            out=job["out"]
            if out.get("ok"):
                res=out["result"]; st.session_state["last_result"]=res; inc_metric("plans_completed",1)
                try:
                    record_run(int(job["meta"]["n_portais"]), int(job["meta"]["num_cpus"]), bool(job["meta"]["gif"]), float(out["elapsed"]))
                    add_job_row(out.get("job_id",job_id), UID, int(job["meta"]["n_portais"]), int(job["meta"]["num_cpus"]), str(job["meta"]["team"]), bool(job["meta"].get("output_csv",True)), bool(job["meta"]["gif"]), float(out["elapsed"]), str(res.get("outdir","")))
                except: pass
            else: st.error(f"Erro: {out.get('error','desconhecido')}")
            del st.session_state["job_id"]; qp_set(job=None)
        else:
            fut=job["future"]; t0=job["t0"]; eta_s=job["eta"]; canceled=False
            with st.status(f"⏳ Processando… (job {job_id})", expanded=True) as status:
                bar=st.progress(0); eta_ph=st.empty(); cancel_ph=st.empty()
                if cancel_ph.button("🛑 Cancelar este job", key=f"cancel_{job_id}"):
                    canceled=True
                    try: fut.cancel(); status.update(label="🛑 Cancelando…", state="error", expanded=True)
                    except: pass
                while not fut.done():
                    if canceled: break
                    elapsed=time.time()-t0; pct=min(0.90, elapsed/max(1e-6,eta_s)); bar.progress(int(pct*100))
                    eta_left=max(0, eta_s - elapsed); eta_ph.write(f"**Estimativa:** ~{int(eta_left)}s restantes · **Decorridos:** {int(elapsed)}s")
                    time.sleep(0.3)
            if canceled:
                job["done"]=True; job["out"]={"ok":False,"error":"Job cancelado pelo usuário" if fut.cancelled() else "Cancelamento solicitado"}
                del st.session_state["job_id"]; qp_set(job=None); st.stop()
            out=fut.result(); bar.progress(100); job["done"]=True; job["out"]=out
            if out.get("ok"):
                status.update(label="✅ Concluído", state="complete", expanded=False)
                res=out["result"]; st.session_state["last_result"]=res; inc_metric("plans_completed",1)
                try:
                    record_run(int(job["meta"]["n_portais"]), int(job["meta"]["num_cpus"]), bool(job["meta"]["gif"]), float(out["elapsed"]))
                    add_job_row(out.get("job_id",job_id), UID, int(job["meta"]["n_portais"]), int(job["meta"]["num_cpus"]), str(job["meta"]["team"]), bool(job["meta"].get("output_csv",True)), bool(job["meta"]["gif"]), float(out["elapsed"]), str(res.get("outdir","")))
                except: pass
            else:
                status.update(label="❌ Falhou", state="error", expanded=True); st.error(f"Erro: {out.get('error','desconhecido')}")
            del st.session_state["job_id"]; qp_set(job=None)

# ===== Results =====
res = st.session_state.get("last_result")
if res:
    st.success("Plano gerado com sucesso!")
    if res.get("pm_bytes"): st.image(res["pm_bytes"], caption="Portal Map")
    if res.get("lm_bytes"): st.image(res["lm_bytes"], caption="Link Map")
    if res.get("gif_bytes"): st.download_button("Baixar GIF (plan_movie.gif)", data=res["gif_bytes"], file_name="plan_movie.gif", mime="image/gif", key="dl_gif_last")
    st.download_button("Baixar todos os arquivos (.zip)", data=res["zip_bytes"], file_name=f"maxfield_output_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip", mime="application/zip", key="dl_zip_last")
    with st.expander("Ver logs do processamento"):
        log_txt_full=res.get("log_txt") or "(sem logs)"
        if len(log_txt_full)>20000: st.caption("Log truncado (últimos ~20k)."); log_txt=log_txt_full[-20000:]
        else: log_txt=log_txt_full
        st.code(log_txt, language="bash")
    if st.button("🧹 Limpar resultados", key="clear_res"): st.session_state.pop("last_result",None); qp_set(job=None); st.rerun()

# ===== Histórico =====
with tab_hist:
    st.caption(f"Seu ID anônimo: `{UID}` — itens são mantidos por 24h.")
    rows = list_jobs_recent(uid=UID, within_hours=24, limit=50)
    if not rows: st.info("Sem planos recentes.")
    else:
        for (jid, ts, uid, n_port, ncpu, team, out_csv, do_gif, dur_s, out_dir) in rows:
            dt=datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
            with st.container(border=True):
                st.write(f"**Job {jid}** — {dt} · Portais: **{n_port}** · CPUs: {ncpu} · {team} · CSV: {bool(out_csv)} · GIF: {bool(do_gif)} · Duração: {int(dur_s)}s")
                if out_dir and os.path.isdir(out_dir):
                    pm=os.path.join(out_dir,"portal_map.png"); lm=os.path.join(out_dir,"link_map.png"); gif_p=os.path.join(out_dir,"plan_movie.gif")
                    zip_p=None
                    for fn in os.listdir(out_dir):
                        if fn.endswith('.zip'): zip_p=os.path.join(out_dir,fn)
                    cols=st.columns(4)
                    if os.path.exists(pm): 
                        with cols[0]: st.download_button("Portal Map", data=open(pm,"rb").read(), file_name="portal_map.png", mime="image/png", key=f"dl_pm_{jid}")
                    if os.path.exists(lm): 
                        with cols[1]: st.download_button("Link Map", data=open(lm,"rb").read(), file_name="link_map.png", mime="image/png", key=f"dl_lm_{jid}")
                    if os.path.exists(gif_p): 
                        with cols[2]: st.download_button("GIF", data=open(gif_p,"rb").read(), file_name="plan_movie.gif", mime="image/gif", key=f"dl_gif_{jid}")
                    if zip_p and os.path.exists(zip_p): 
                        with cols[3]: st.download_button("ZIP", data=open(zip_p,"rb").read(), file_name=os.path.basename(zip_p), mime="application/zip", key=f"dl_zip_{jid}")
                else: st.caption("_Arquivos expirados._")

# ===== Métricas =====
with tab_metrics:
    cur=get_db().execute("SELECT ts, n_portais, num_cpus, gif, dur_s FROM runs ORDER BY ts DESC LIMIT 100")
    data=cur.fetchall()
    if not data: st.info("Ainda sem dados suficientes.")
    else:
        import pandas as pd
        df=pd.DataFrame(data, columns=["ts","n_portais","num_cpus","gif","dur_s"])
        st.metric("Duração p50 (s)", f"{int(df['dur_s'].quantile(0.50))}")
        st.metric("Duração p90 (s)", f"{int(df['dur_s'].quantile(0.90))}")
        st.metric("Execuções (últimos 100)", f"{len(df)}")
        st.bar_chart(df[["dur_s"]].iloc[::-1], height=180)
        st.caption("Barras (da mais antiga para a mais recente) mostram a duração por execução.")

# =================== Fórum / Login ===================
ADMIN_CODE = st.secrets.get("ADMIN_CODE","")
COMMENTS_ENABLED = bool(st.secrets.get("COMMENTS_ENABLED", True))
MAX_IMG_MB = int(st.secrets.get("MAX_IMG_MB", 2))
MAX_IMGS_PER_POST = int(st.secrets.get("MAX_IMGS_PER_POST", 3))
def _now_ts(): return int(time.time())
def hash_pass(password,salt): return hashlib.sha256(f"{salt}:{password}".encode("utf-8","ignore")).hexdigest()

def save_avatar_file(user_id, avatar_bytes, avatar_ext):
    if not avatar_bytes or not avatar_ext: return None
    safe_ext=avatar_ext.lower().strip(); 
    if not safe_ext.startswith("."): safe_ext="."+safe_ext
    if safe_ext not in (".png",".jpg",".jpeg",".webp"): return None
    av_dir=os.path.join("data","avatars",str(int(user_id))); os.makedirs(av_dir, exist_ok=True)
    av_path=os.path.join(av_dir,f"avatar{safe_ext}")
    try:
        open(av_path,"wb").write(avatar_bytes)
        get_db().execute("UPDATE users SET avatar_ext=? WHERE id=?", (safe_ext,int(user_id))); get_db().commit()
        return safe_ext
    except: return None

def get_user_by_username_or_email(identifier):
    if not identifier: return None
    ident=identifier.strip(); cur=get_db().execute("""
        SELECT id, username, username_lc, pass_hash, faction, email, avatar_ext, is_admin, pass_salt
        FROM users WHERE username_lc=? OR email=? LIMIT 1""",(ident.lower(), ident))
    row=cur.fetchone(); 
    if not row: return None
    return {"id":int(row[0]),"username":row[1] or "","username_lc":row[2] or "","pass_hash":row[3] or "",
            "faction":row[4] or "","email":row[5] or "","avatar_ext":row[6] or None,"is_admin":int(row[7] or 0),
            "pass_salt":row[8] or ""}

def create_user(username,password,faction,email,is_admin_bool,avatar_bytes,avatar_ext):
    if not username or not password: raise ValueError("username e password são obrigatórios")
    uname=username.strip(); uname_lc=uname.lower(); fac=(faction or "").strip(); mail=(email or "").strip() or None
    is_admin=1 if is_admin_bool else 0; ts=_now_ts(); salt=uuid.uuid4().hex[:8]; p_hash=hash_pass(password,salt)
    conn=get_db()
    if conn.execute("SELECT 1 FROM users WHERE username_lc=?",(uname_lc,)).fetchone(): raise ValueError("Este nome de usuário já está em uso.")
    conn.execute("""INSERT INTO users(username,username_lc,pass_hash,pass_salt,faction,email,avatar_ext,is_admin,created_ts,updated_ts)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",(uname,uname_lc,p_hash,salt,fac,mail,None,int(is_admin),ts,ts)); conn.commit()
    user_id=int(conn.execute("SELECT id FROM users WHERE username_lc=?", (uname_lc,)).fetchone()[0])
    if avatar_bytes and avatar_ext: save_avatar_file(user_id, avatar_bytes, avatar_ext)
    return user_id

def check_password(user_row,password):
    if not user_row or not password: return False
    ph, psalt = get_db().execute("SELECT pass_hash, pass_salt FROM users WHERE id=?", (int(user_row["id"]),)).fetchone()
    return hash_pass(password, psalt or "") == ph

def create_session(user_id):
    token=uuid.uuid4().hex; ts=_now_ts()
    get_db().execute("INSERT OR REPLACE INTO sessions(token,user_id,created_ts,last_seen_ts) VALUES(?,?,?,?)",(token,int(user_id),ts,ts)); get_db().commit()
    return token
def get_user_by_token(token):
    row=get_db().execute("""SELECT u.id,u.username,u.username_lc,u.faction,u.email,u.avatar_ext,u.is_admin
                            FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=? LIMIT 1""",(token,)).fetchone()
    if not row: return None
    get_db().execute("UPDATE sessions SET last_seen_ts=? WHERE token=?", (_now_ts(), token)); get_db().commit()
    return {"id":int(row[0]),"username":row[1] or "","username_lc":row[2] or "","faction":row[3] or "",
            "email":row[4] or "","avatar_ext":row[5] or None,"is_admin":int(row[6] or 0)}
def signout_current():
    token=qp_get("token",""); if_token = bool(token)
    if if_token: get_db().execute("DELETE FROM sessions WHERE token=?", (token,)); get_db().commit()
    st.session_state.pop("user",None); qp_set(token=None); st.toast("Você saiu."); st.experimental_rerun()
def current_user():
    if "user" in st.session_state and st.session_state["user"]: return st.session_state["user"]
    token=qp_get("token",""); 
    if token:
        u=get_user_by_token(token)
        if u: st.session_state["user"]=u; return u
    return None

def forum_count_comments(post_id): return int(get_db().execute("SELECT COUNT(*) FROM forum_comments WHERE post_id=? AND (deleted_ts IS NULL)", (int(post_id),)).fetchone()[0])
def forum_create_post(cat,title,body_md,images,author):
    ts=_now_ts(); conn=get_db()
    conn.execute("""INSERT INTO forum_posts(cat,title,body_md,author_id,author_name,author_faction,created_ts,updated_ts,images_json,is_pinned)
                    VALUES(?,?,?,?,?,?,?,?,?,0)""",(cat,title.strip(),body_md.strip(),int(author["id"]),author["username"],author["faction"],ts,ts,"[]"))
    conn.commit()
    post_id=int(conn.execute("SELECT id FROM forum_posts WHERE author_id=? ORDER BY id DESC LIMIT 1",(int(author["id"]),)).fetchone()[0])
    saved=[]
    if images:
        root=os.path.join("data","posts",str(post_id)); os.makedirs(root,exist_ok=True)
        for i,f in enumerate(images[:MAX_IMGS_PER_POST], start=1):
            data=f.getvalue()
            if len(data)>MAX_IMG_MB*1024*1024: continue
            name=f.name.lower(); ext=".png"
            for e in (".png",".jpg",".jpeg",".webp"):
                if name.endswith(e): ext=e; break
            p=os.path.join(root,f"img{i}{ext}"); open(p,"wb").write(data); saved.append(os.path.basename(p))
    conn.execute("UPDATE forum_posts SET images_json=? WHERE id=?", (json.dumps(saved), post_id)); conn.commit()
    return post_id
def forum_list_posts(cat):
    return get_db().execute("""SELECT id,title,author_name,author_faction,created_ts,images_json,author_id FROM forum_posts
                               WHERE cat=? ORDER BY is_pinned DESC, created_ts DESC""",(cat,)).fetchall()
def forum_get_post(post_id):
    return get_db().execute("""SELECT id,cat,title,body_md,author_id,author_name,author_faction,created_ts,images_json FROM forum_posts WHERE id=? LIMIT 1""",(int(post_id),)).fetchone()
def forum_add_comment(post_id,author,body_md):
    ts=_now_ts(); get_db().execute("""INSERT INTO forum_comments(post_id,author_id,author_name,author_faction,body_md,created_ts,deleted_ts)
                                     VALUES(?,?,?,?,?,?,NULL)""",(int(post_id),int(author["id"]),author["username"],author["faction"],body_md.strip(),ts)); get_db().commit()
def forum_list_comments(post_id):
    return get_db().execute("""SELECT id,author_id,author_name,author_faction,body_md,created_ts,deleted_ts FROM forum_comments
                               WHERE post_id=? ORDER BY created_ts ASC""",(int(post_id),)).fetchall()
def forum_delete_comment(comment_id):
    get_db().execute("UPDATE forum_comments SET deleted_ts=? WHERE id=?", (_now_ts(), int(comment_id))); get_db().commit()
def user_avatar_bytes(user_id, avatar_ext):
    if not avatar_ext: return None
    p=os.path.join("data","avatars",str(user_id), f"avatar{avatar_ext}")
    try: return open(p,"rb").read() if os.path.exists(p) else None
    except: return None

# ---- Fórum UI ----
if tab_forum is not None:
    with tab_forum:
        auto = st.toggle("🔄 Auto-atualizar a cada 20s", value=False, key="forum_auto")
        if auto: st.markdown("<script>setTimeout(()=>location.reload(),20000)</script>", unsafe_allow_html=True)

        u = current_user()

        # Banner visitante + login/signup
        if not u:
            st.info("Você está navegando como **visitante**. Entre para comentar/postar.")
            with st.container(border=True):
                colL, colR = st.columns(2)
                with colL:
                    st.markdown("#### Entrar")
                    li_user = st.text_input("Usuário ou e-mail", key="li_user")
                    li_pass = st.text_input("Senha", type="password", key="li_pass")
                    if st.button("Entrar", key="li_btn"):
                        usr = get_user_by_username_or_email(li_user)
                        if not usr or not check_password(usr, li_pass): st.error("Usuário ou senha inválidos.")
                        else:
                            token = create_session(usr["id"]); st.session_state["user"]=usr; qp_set(token=token); st.toast("Login ok!"); st.experimental_rerun()
                with colR:
                    st.markdown("#### Criar conta")
                    su_user = st.text_input("Nome de usuário (único)", key="su_user")
                    su_faction = st.selectbox("Facção", ["Enlightened","Resistance"], key="su_faction")
                    su_email = st.text_input("E-mail (opcional)", key="su_email")
                    su_pass = st.text_input("Senha", type="password", key="su_pass")
                    su_pass2 = st.text_input("Confirmar senha", type="password", key="su_pass2")
                    su_avatar = st.file_uploader("Avatar (opcional)", type=["png","jpg","jpeg","webp"], key="su_avatar")
                    su_admin_code = st.text_input("Código de admin (se houver)", type="password", key="su_admin_code")
                    if st.button("Criar conta", key="su_btn"):
                        if not su_user or not su_pass: st.error("Preencha usuário e senha.")
                        elif su_pass != su_pass2: st.error("As senhas não conferem.")
                        else:
                            is_admin = bool(ADMIN_CODE) and (su_admin_code.strip() == ADMIN_CODE.strip())
                            av_bytes, av_ext = (su_avatar.getvalue(), os.path.splitext(su_avatar.name)[1].lower()) if su_avatar else (None, None)
                            try:
                                uid_new = create_user(su_user, su_pass, su_faction, (su_email or "").strip() or None, is_admin, av_bytes, av_ext)
                                usr = get_user_by_username_or_email(su_user); token = create_session(usr["id"]); st.session_state["user"]=usr; qp_set(token=token)
                                st.success("Conta criada! Você já está logado."); st.experimental_rerun()
                            except ValueError as ve: st.error(str(ve))
                            except Exception as e: st.error(f"Erro ao criar conta: {e}")
        else:
            with st.container(border=True):
                colA, colB, colC = st.columns([0.6,0.25,0.15])
                with colA:
                    st.write(f"Logado como **{u['username']}** ({u['faction']}){' · 🛡️ Admin' if u['is_admin'] else ''}")
                with colB:
                    # Trocar avatar — seguro (nonce) e sem loop
                    use_pop = hasattr(st, "popover")
                    container = st.popover("Trocar avatar") if use_pop else st.expander("Trocar avatar", expanded=False)
                    with container:
                        if "avatar_nonce" not in st.session_state: st.session_state["avatar_nonce"]=0
                        up_key = f"avatar_upload_{st.session_state['avatar_nonce']}"
                        up_file = st.file_uploader("Carregar nova foto", type=["png","jpg","jpeg","webp"], key=up_key)
                        c1, c2 = st.columns([0.3,0.7])
                        save_clicked   = c1.button("Salvar", disabled=(up_file is None), key=f"save_av_{st.session_state['avatar_nonce']}")
                        cancel_clicked = c2.button("Cancelar", key=f"cancel_av_{st.session_state['avatar_nonce']}")
                        if save_clicked and up_file is not None:
                            name=(up_file.name or "").lower(); ext=os.path.splitext(name)[1] or ".png"
                            ok_ext = save_avatar_file(int(u["id"]), up_file.getvalue(), ext)
                            if ok_ext:
                                refreshed = get_user_by_token(qp_get("token","")); 
                                if refreshed: st.session_state["user"]=refreshed
                                st.toast("Avatar atualizado!")
                            else: st.error("Formato inválido. Use PNG/JPG/WEBP.")
                            st.session_state["avatar_nonce"] += 1; st.rerun()
                        if cancel_clicked:
                            st.session_state["avatar_nonce"] += 1; st.rerun()
                with colC:
                    if st.button("Sair", key="logout_btn"): signout_current()
                av_bytes = user_avatar_bytes(u["id"], u.get("avatar_ext"))
                if av_bytes: st.image(av_bytes, width=64)

        st.markdown("---")
        st.subheader("Tópicos")
        cat_tabs = st.tabs(["📢 Atualizações","💡 Sugestões","🧱 Críticas","❓ Dúvidas"])
        CATS = ["Atualizações","Sugestões","Críticas","Dúvidas"]

        for ci, ct in enumerate(cat_tabs):
            with ct:
                cat=CATS[ci]
                can_create = (cat=="Atualizações" and u and u["is_admin"]==1) or (cat!="Atualizações" and u)

                exp_key=f"new_topic_open_{cat}"
                if exp_key not in st.session_state: st.session_state[exp_key]=False
                nonce_key=f"nt_uploader_nonce_{cat}"
                if nonce_key not in st.session_state: st.session_state[nonce_key]=0

                if can_create:
                    if not st.session_state[exp_key]:
                        if st.button("➕ Novo tópico", key=f"open_new_topic_{cat}"): st.session_state[exp_key]=True; st.experimental_rerun()
                    else:
                        with st.container(border=True):
                            st.markdown("### ➕ Novo tópico")
                            nt_title = st.text_input("Título", key=f"nt_title_{cat}")
                            nt_body = st.text_area("Conteúdo (Markdown)", key=f"nt_body_{cat}", height=140)
                            nt_imgs = st.file_uploader(f"Imagens (até {MAX_IMGS_PER_POST} × {MAX_IMG_MB}MB)",
                                                       type=["png","jpg","jpeg","webp"], accept_multiple_files=True,
                                                       key=f"nt_imgs_{cat}_{st.session_state[nonce_key]}")
                            col_btns = st.columns([0.2,0.2,0.6])
                            post_clicked   = col_btns[0].button("Postar",   key=f"nt_post_{cat}")
                            cancel_clicked = col_btns[1].button("Cancelar", key=f"nt_cancel_{cat}")
                            if cancel_clicked:
                                for _k in (f"nt_title_{cat}", f"nt_body_{cat}"): st.session_state.pop(_k, None)
                                st.session_state[nonce_key]+=1; st.session_state[exp_key]=False; st.toast("Criação cancelada."); st.experimental_rerun()
                            if post_clicked:
                                if not nt_title.strip(): st.error("Informe um título.")
                                else:
                                    forum_create_post(cat, nt_title, nt_body, nt_imgs, u)
                                    for _k in (f"nt_title_{cat}", f"nt_body_{cat}"): st.session_state.pop(_k, None)
                                    st.session_state[nonce_key]+=1; st.session_state[exp_key]=False; st.toast("Postagem enviada!"); st.experimental_rerun()
                else:
                    if not u: st.caption("_Entre para criar um novo tópico._")
                    elif cat == "Atualizações" and u and u['is_admin'] != 1: st.caption("_Apenas admin pode publicar em Atualizações._")

                posts = forum_list_posts(cat)
                if not posts: st.info("Nenhum tópico ainda.")
                else:
                    for (pid, title, author_name, author_faction, cts, images_json, author_id) in posts:
                        cnt = forum_count_comments(pid)
                        av_ext = (get_db().execute("SELECT avatar_ext FROM users WHERE id=?", (int(author_id),)).fetchone() or [None])[0]
                        av_bytes = user_avatar_bytes(author_id, av_ext)
                        with st.container(border=True):
                            head_cols = st.columns([0.1,0.6,0.3])
                            with head_cols[0]:
                                if av_bytes: st.image(av_bytes, width=48)
                            with head_cols[1]:
                                dt = datetime.fromtimestamp(cts).strftime("%Y-%m-%d %H:%M")
                                st.markdown(f"**{title}**  <span class='mf-badge'>{cnt} comentários</span><br><small>por {author_name} · {author_faction} · {dt}</small>", unsafe_allow_html=True)
                            with head_cols[2]:
                                if u and (u.get("is_admin",0)==1 or int(u["id"])==int(author_id)):
                                    colE, colD = st.columns(2)
                                    if colE.button("Editar", key=f"edit_post_{pid}"):
                                        st.session_state[f"edit_post_open_{pid}"]=True; st.rerun()
                                    if colD.button("Apagar", key=f"del_post_{pid}"):
                                        get_db().execute("DELETE FROM forum_posts WHERE id=?", (int(pid),))
                                        get_db().execute("DELETE FROM forum_comments WHERE post_id=?", (int(pid),))
                                        get_db().commit(); st.success("Tópico removido."); st.experimental_rerun()

                            post = forum_get_post(pid)
                            if post:
                                _id,_cat,_title,_body_md,_aid,_aname,_afac,_cts,_imgs = post
                                if _body_md: st.markdown(_body_md)
                                try: imgs = json.loads(_imgs or "[]")
                                except: imgs=[]
                                if imgs:
                                    st.caption("Imagens:"); ig_cols = st.columns(min(3,len(imgs)))
                                    root = os.path.join("data","posts",str(pid))
                                    for i,name in enumerate(imgs):
                                        p=os.path.join(root,name)
                                        if os.path.exists(p):
                                            with ig_cols[i % len(ig_cols)]: st.image(open(p,"rb").read())

                            # Editar tópico
                            edit_open_key=f"edit_post_open_{pid}"
                            if st.session_state.get(edit_open_key, False):
                                st.markdown("---")
                                st.markdown("**Editar tópico**")
                                new_title = st.text_input("Título", value=title, key=f"ed_title_{pid}")
                                new_body  = st.text_area("Conteúdo (Markdown)", value=(forum_get_post(pid)[3] or ""), key=f"ed_body_{pid}", height=160)
                                cS, cC = st.columns(2)
                                if cS.button("Salvar", key=f"ed_save_{pid}"):
                                    get_db().execute("UPDATE forum_posts SET title=?, body_md=?, updated_ts=? WHERE id=?",(new_title.strip(), new_body.strip(), _now_ts(), int(pid))); get_db().commit()
                                    st.session_state.pop(edit_open_key, None); st.toast("Tópico atualizado!"); st.experimental_rerun()
                                if cC.button("Cancelar", key=f"ed_cancel_{pid}"):
                                    st.session_state.pop(edit_open_key, None); st.experimental_rerun()

                            # Comentários
                            if COMMENTS_ENABLED:
                                st.markdown("**Comentários:**")
                                comms = forum_list_comments(pid)
                                if not comms: st.caption("Seja o primeiro a comentar.")
                                else:
                                    for (cid, caid, caname, cafac, cbody, ctime, cdel) in comms:
                                        if cdel: st.caption("_comentário removido_"); continue
                                        row_cols = st.columns([0.1,0.9])
                                        with row_cols[0]:
                                            cav_ext = (get_db().execute("SELECT avatar_ext FROM users WHERE id=?", (int(caid),)).fetchone() or [None])[0]
                                            cav_bytes = user_avatar_bytes(caid, cav_ext)
                                            if cav_bytes: st.image(cav_bytes, width=40)
                                        with row_cols[1]:
                                            line = f"**{caname}** · {cafac} · {datetime.fromtimestamp(ctime).strftime('%Y-%m-%d %H:%M')}"
                                            st.markdown(line)
                                            if cbody: st.markdown(cbody)
                                            if u and (u.get("is_admin",0)==1 or int(u["id"])==int(caid)):
                                                ccols = st.columns(2)
                                                if ccols[0].button("Editar", key=f"editc_{cid}"):
                                                    st.session_state[f"editc_open_{cid}"]=True; st.rerun()
                                                if ccols[1].button("Apagar", key=f"delc_{cid}"):
                                                    forum_delete_comment(cid); st.success("Comentário apagado."); st.experimental_rerun()
                                            if st.session_state.get(f"editc_open_{cid}", False):
                                                ne = st.text_area("Editar comentário", value=(cbody or ""), key=f"editc_txt_{cid}", height=100)
                                                sc, cc = st.columns(2)
                                                if sc.button("Salvar", key=f"editc_save_{cid}"):
                                                    get_db().execute("UPDATE forum_comments SET body_md=?, created_ts=? WHERE id=?", (ne.strip(), ctime, int(cid))); get_db().commit()
                                                    st.session_state.pop(f"editc_open_{cid}", None); st.toast("Comentário atualizado."); st.experimental_rerun()
                                                if cc.button("Cancelar", key=f"editc_cancel_{cid}"):
                                                    st.session_state.pop(f"editc_open_{cid}", None); st.experimental_rerun()

                                if u:
                                    nonce_key_c=f"comment_nonce_{pid}"
                                    if nonce_key_c not in st.session_state: st.session_state[nonce_key_c]=0
                                    nc = st.text_area("Escreva um comentário", key=f"nc_{pid}_{st.session_state[nonce_key_c]}", height=100)
                                    if st.button("Comentar", key=f"btn_nc_{pid}"):
                                        if not (nc or "").strip(): st.error("O comentário está vazio.")
                                        else:
                                            forum_add_comment(pid, u, nc); st.session_state[nonce_key_c]+=1; st.toast("Comentário enviado!"); st.experimental_rerun()
                                else: st.caption("_Entre para comentar._")
                            else: st.caption("_Comentários desabilitados._")

# ===== Rodapé =====
st.markdown("---")
left,right = st.columns(2)
PIX_PHONE_DISPLAY = "+55 79 99834-5186"
WHATS_NUMBER_DIGITS = "5579998345186"
WHATS_URL = f"https://wa.me/{WHATS_NUMBER_DIGITS}"
TELEGRAM_USER = st.secrets.get("TELEGRAM_USER","@HiperionBR")
TELEGRAM_URL = f"https://t.me/{TELEGRAM_USER.lstrip('@')}"
with left:
    st.subheader("💙 Apoie este projeto")
    pix_qr_url = st.secrets.get("PIX_QR_URL","")
    if pix_qr_url: st.image(pix_qr_url, caption="Use o QR Code para doar via PIX", width=220)
    st.markdown(f"Ou copie a chave PIX (celular): **{PIX_PHONE_DISPLAY}**")
    st.markdown(f"[📲 Entrar em contato no WhatsApp]({WHATS_URL})", unsafe_allow_html=True)
    st.markdown(f"[✈️ Falar no Telegram]({TELEGRAM_URL})", unsafe_allow_html=True)
with right:
    st.subheader("📰 Informes")
    news_md = st.secrets.get("NEWS_MD","").strip()
    st.markdown(news_md if news_md else "- Bem-vindo ao **Maxfield Online**!\n- Feedbacks e ideias são bem-vindos.\n")
