# app.py
import os
import io
import sys
import types
import zipfile
import tempfile
import sqlite3
import time
import statistics
import uuid
import json
from datetime import datetime
from contextlib import redirect_stdout
from concurrent.futures import ThreadPoolExecutor

import streamlit as st

# ---------- Pygifsicle stub (evita depender do gifsicle) ----------
fake = types.ModuleType("pygifsicle")
def optimize(*args, **kwargs):
    return
fake.optimize = optimize
sys.modules["pygifsicle"] = fake
# ------------------------------------------------------------------

# Maxfield
from maxfield.maxfield import maxfield as run_maxfield

# ---------- Config do Streamlit ----------
st.set_page_config(
    page_title="Maxfield Online",
    page_icon="🗺️",
    layout="centered",
)

# ===== Fundo + cartão responsivo (claro/escuro automático) + Abas grandes =====
bg_url = st.secrets.get("BG_URL", "").strip()
st.markdown(
    f"""
    <style>
    .stApp {{
      {"background: url('" + bg_url + "') no-repeat center center fixed; background-size: cover;" if bg_url else ""}
    }}

    @media (prefers-color-scheme: light) {{
      .stApp .block-container {{
        background: rgba(255,255,255,0.92);
        color: #111;
      }}
      .stApp .block-container a {{ color: #005bbb; }}
    }}
    @media (prefers-color-scheme: dark) {{
      .stApp .block-container {{
        background: rgba(20,20,20,0.78);
        color: #eaeaea;
      }}
      .stApp .block-container a {{ color: #8ecaff; }}
    }}

    .stApp .block-container {{
      border-radius: 12px;
      padding: 1rem 1.2rem 2rem 1.2rem;
    }}

    /* Chips de facção */
    .mf-chip {{
      display:inline-block;padding:4px 10px;border-radius:999px;font-size:12px;margin-right:8px;
      color:#fff; box-shadow:0 2px 6px rgba(0,0,0,.2)
    }}
    .mf-chip.enl {{ background:#25c025; }}
    .mf-chip.res {{ background:#2b6dff; }}

    /* Mini badge de contagem */
    .mf-badge {{
      display:inline-block; padding:2px 8px; border-radius:999px; font-size:11px; margin-left:8px;
      background:#00000022;
    }}

    /* Abas grandes (pill buttons) */
    div[data-baseweb="tab-list"] button {{
      padding: 12px 18px !important;
      margin: 0 6px 8px 0 !important;
      border-radius: 999px !important;
      border: 1px solid rgba(0,0,0,.08) !important;
      box-shadow: 0 2px 6px rgba(0,0,0,.06) !important;
      font-weight: 600 !important;
    }}
    div[data-baseweb="tab"] p {{
      font-size: 15px !important;
    }}

    /* --- FIXES de UI: impedir quebra de texto em botões (inclui Browse files) --- */
    .stButton button, .stDownloadButton button, button, div[data-baseweb="button"] {{
      white-space: nowrap !important;
    }}
    button p {{
      white-space: nowrap !important;
    }}
    .stFileUploader button {{
      white-space: nowrap !important;
    }}
    /* Botões de ação do fórum com largura total da coluna */
    .mf-action button {{
      width: 100% !important;
    }}
    </style>
    """,
    unsafe_allow_html=True
)

# ---------- Persistência simples (SQLite) ----------
@st.cache_resource(show_spinner=False)
def get_db():
    os.makedirs("data", exist_ok=True)
    db_path = os.path.join("data", "app.db")
    conn = sqlite3.connect(db_path, check_same_thread=False)
    # métricas e runs
    conn.execute("""
        CREATE TABLE IF NOT EXISTS metrics (
            key   TEXT PRIMARY KEY,
            value INTEGER NOT NULL
        )
    """)
    for k in ("visits", "plans_completed"):
        conn.execute("INSERT OR IGNORE INTO metrics(key, value) VALUES (?, 0)", (k,))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS runs(
            ts INTEGER, n_portais INTEGER, num_cpus INTEGER, gif INTEGER, dur_s REAL
        )
    """)
    # jobs + housekeeping
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs(
            job_id TEXT PRIMARY KEY,
            ts INTEGER,
            uid TEXT,
            n_portais INTEGER,
            num_cpus INTEGER,
            team TEXT,
            output_csv INTEGER,
            fazer_gif INTEGER,
            dur_s REAL,
            out_dir TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS housekeeping(
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    # --- Fórum + usuários + sessões ---
    conn.execute("CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT)")
    def colset(table): return {r[1] for r in conn.execute(f"PRAGMA table_info({table});")}
    def ensure_col(table, col, decl):
        if col not in colset(table):
            try: conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            except Exception: pass

    for col, decl in [
        ("username","TEXT"),("username_lc","TEXT"),
        ("pass_hash","TEXT"),("pass_salt","TEXT"),
        ("faction","TEXT"),("email","TEXT"),
        ("avatar_ext","TEXT"),("is_admin","INTEGER DEFAULT 0"),
        ("created_ts","INTEGER"),("updated_ts","INTEGER"),
        # legados
        ("uid","TEXT"),("name","TEXT"),("avatar_path","TEXT"),
    ]: ensure_col("users", col, decl)
    try:
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username_lc ON users(username_lc)")
    except Exception: pass

    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions(
            token TEXT PRIMARY KEY,
            user_id INTEGER,
            created_ts INTEGER,
            last_seen_ts INTEGER
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)")

    conn.execute("CREATE TABLE IF NOT EXISTS forum_posts(id INTEGER PRIMARY KEY AUTOINCREMENT)")
    for col, decl in [
        ("cat","TEXT"),("title","TEXT"),("body_md","TEXT"),
        ("author_id","INTEGER"),("author_name","TEXT"),("author_faction","TEXT"),
        ("created_ts","INTEGER"),("updated_ts","INTEGER"),
        ("images_json","TEXT"),("is_pinned","INTEGER DEFAULT 0"),
        # legados
        ("ts","INTEGER"),("uid","TEXT"),("body","TEXT"),("category","TEXT"),
    ]: ensure_col("forum_posts", col, decl)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_cat ON forum_posts(cat)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_author ON forum_posts(author_id)")

    conn.execute("CREATE TABLE IF NOT EXISTS forum_comments(id INTEGER PRIMARY KEY AUTOINCREMENT)")
    for col, decl in [
        ("post_id","INTEGER"),("author_id","INTEGER"),
        ("author_name","TEXT"),("author_faction","TEXT"),
        ("body_md","TEXT"),("created_ts","INTEGER"),("deleted_ts","INTEGER"),
        # legados
        ("ts","INTEGER"),("uid","TEXT"),("body","TEXT"),
    ]: ensure_col("forum_comments", col, decl)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_comments_postid ON forum_comments(post_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_comments_author ON forum_comments(author_id)")

    conn.commit()
    return conn

def inc_metric(key: str, delta: int = 1):
    conn = get_db()
    conn.execute("UPDATE metrics SET value = value + ? WHERE key = ?", (delta, key))
    conn.commit()

def get_metric(key: str) -> int:
    cur = get_db().execute("SELECT value FROM metrics WHERE key=?", (key,))
    row = cur.fetchone()
    return int(row[0]) if row else 0

# histórico de durações p/ ETA
def record_run(n_portais:int, num_cpus:int, gif:bool, dur_s:float):
    conn = get_db()
    conn.execute("INSERT INTO runs(ts,n_portais,num_cpus,gif,dur_s) VALUES (?,?,?,?,?)",
                 (int(time.time()), n_portais, num_cpus, 1 if gif else 0, float(dur_s)))
    conn.commit()

def add_job_row(job_id:str, uid:str, n_portais:int, num_cpus:int, team:str,
                output_csv:bool, fazer_gif:bool, dur_s:float, out_dir:str):
    conn = get_db()
    conn.execute("""
        INSERT OR REPLACE INTO jobs(job_id,ts,uid,n_portais,num_cpus,team,output_csv,fazer_gif,dur_s,out_dir)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (job_id, int(time.time()), uid, n_portais, num_cpus, team, 1 if output_csv else 0, 1 if fazer_gif else 0, float(dur_s), out_dir))
    conn.commit()

def list_jobs_recent(uid:str|None, within_hours:int=24, limit:int=50):
    conn = get_db()
    min_ts = int(time.time()) - within_hours*3600
    if uid:
        cur = conn.execute(
            "SELECT job_id,ts,uid,n_portais,num_cpus,team,output_csv,fazer_gif,dur_s,out_dir "
            "FROM jobs WHERE ts>=? AND uid=? ORDER BY ts DESC LIMIT ?",
            (min_ts, uid, limit)
        )
    else:
        cur = conn.execute(
            "SELECT job_id,ts,uid,n_portais,num_cpus,team,output_csv,fazer_gif,dur_s,out_dir "
            "FROM jobs WHERE ts>=? ORDER BY ts DESC LIMIT ?",
            (min_ts, limit)
        )
    return cur.fetchall()

def estimate_eta_s(n_portais:int, num_cpus:int, gif:bool) -> float:
    base_pp = 0.35 if not gif else 0.55
    base_overhead = 3.0 if not gif else 8.0
    cpu_factor = 1.0 / max(1.0, (0.6 + 0.5*min(num_cpus, 8)**0.5))
    est = (base_overhead + base_pp*n_portais) * cpu_factor

    cur = get_db().execute("""
        SELECT dur_s, n_portais FROM runs
        WHERE gif=? ORDER BY ts DESC LIMIT 50
    """, (1 if gif else 0,))
    rows = cur.fetchall()
    if rows:
        pps = [r[0]/max(1, r[1]) for r in rows if r[1] > 0]
        if pps:
            pp_med = statistics.median(pps)
            est = (pp_med * n_portais) * cpu_factor + (1.5 if not gif else 4.0)
    return max(2.0, est)

# ---------- Housekeeping diário ----------
def daily_cleanup(retain_hours:int=24):
    conn = get_db()
    today = datetime.now().strftime("%Y-%m-%d")
    cur = conn.execute("SELECT value FROM housekeeping WHERE key='last_cleanup'")
    row = cur.fetchone()
    last = row[0] if row else None
    if last == today:
        return

    root = os.path.join("data", "jobs")
    now = time.time()
    if os.path.isdir(root):
        for jid in os.listdir(root):
            d = os.path.join(root, jid)
            try:
                st_mtime = os.path.getmtime(d)
            except FileNotFoundError:
                continue
            if now - st_mtime > retain_hours*3600:
                try:
                    for base, _, files in os.walk(d, topdown=False):
                        for fn in files:
                            try: os.remove(os.path.join(base, fn))
                            except: pass
                        try: os.rmdir(base)
                        except: pass
                except: pass

    min_ts = int(time.time()) - retain_hours*3600
    conn.execute("DELETE FROM jobs WHERE ts < ?", (min_ts,))
    conn.execute("DELETE FROM runs WHERE ts < ?", (min_ts,))
    conn.execute("INSERT OR REPLACE INTO housekeeping(key,value) VALUES('last_cleanup', ?)", (today,))
    conn.commit()

daily_cleanup(retain_hours=24)

# Conta visita 1x por sessão
if "visit_counted" not in st.session_state:
    inc_metric("visits", 1)
    st.session_state["visit_counted"] = True

# ---------- Utilitários ----------
def contar_portais(texto: str) -> int:
    cnt = 0
    for ln in texto.splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        cnt += 1
    return cnt

def clean_invisibles(s: str) -> str:
    bad = ["\ufeff", "\u200b", "\u200c", "\u200d", "\u2060", "\xa0"]
    for ch in bad:
        s = s.replace(ch, " " if ch == "\xa0" else "")
    return s

def extract_points(texto: str):
    pts = []
    for ln in texto.splitlines():
        s = ln.strip()
        if not s or s.startswith("#"): continue
        try:
            parts = s.split(";")
            name = parts[0].strip()
            url = parts[1].strip() if len(parts) > 1 else ""
            if "pll=" in url:
                pll = url.split("pll=")[1].split("&")[0]
                lat_s, lon_s = pll.split(",")
                lat = float(lat_s); lon = float(lon_s)
                pts.append({"name": name or "Portal", "lat": lat, "lon": lon})
        except Exception:
            continue
    return pts

# ---------- Helpers de QueryString ----------
def qp_get(name: str, default: str = "") -> str:
    try:
        params = getattr(st, "query_params", None)
        if params is not None:
            return params.get(name) or default
        else:
            qp = st.experimental_get_query_params()
            return qp.get(name, [default])[0]
    except Exception:
        return default

def qp_set(**kwargs):
    try:
        params = getattr(st, "query_params", None)
        if params is not None:
            for k, v in kwargs.items():
                if v is None:
                    try: del params[k]
                    except KeyError: pass
                else:
                    params[k] = v
        else:
            cur = st.experimental_get_query_params()
            for k, v in kwargs.items():
                if v is None:
                    cur.pop(k, None)
                else:
                    cur[k] = [v]
            st.experimental_set_query_params(**cur)
    except Exception:
        pass

# ---------- Identificador anônimo ----------
if "uid" not in st.session_state:
    cur_uid = qp_get("uid", "")
    if not cur_uid:
        cur_uid = uuid.uuid4().hex[:8]
        qp_set(uid=cur_uid)
    st.session_state["uid"] = cur_uid
UID = st.session_state["uid"]

# ---------- Parâmetros públicos userscript ----------
PUBLIC_URL = (st.secrets.get("PUBLIC_URL", "https://maxfield.fun/").rstrip("/") + "/")
MIN_ZOOM = int(st.secrets.get("MIN_ZOOM", 15))
MAX_PORTALS = int(st.secrets.get("MAX_PORTALS", 200))
MAX_URL_LEN = int(st.secrets.get("MAX_URL_LEN", 6000))

DEST = PUBLIC_URL

# ---------- Exemplo de entrada (.txt) ----------
EXEMPLO_TXT = """# Exemplo de arquivo de portais (uma linha por portal)
# Formato: Nome do Portal; URL do Intel (com pll=LAT,LON)
Portal 1; https://intel.ingress.com/intel?pll=-10.912345,-37.065432
Portal 2; https://intel.ingress.com/intel?pll=-10.913210,-37.061234
Portal 3; https://intel.ingress.com/intel?pll=-10.910987,-37.060001
"""

# ---------- Userscript IITC (polido) ----------
IITC_USERSCRIPT_TEMPLATE = """// ==UserScript==
// @id             maxfield-send-portals@HiperionBR
// @name           Maxfield — Send Portals (mobile-safe + toolbox button)
// @category       Misc
// @version        0.7.0
// @description    Envia os portais visíveis do IITC para maxfield.fun. Botões no toolbox; contador ao vivo; copy txt; mobile friendly.
// @namespace      https://maxfield.fun/
// @match          https://intel.ingress.com/*
// @grant          none
// ==/UserScript==

function wrapper(plugin_info) {
  if (typeof window.plugin !== 'function') window.plugin = function(){};
  window.plugin.maxfieldSender = {};
  const self = window.plugin.maxfieldSender;

  self.MIN_ZOOM    = 15;
  self.MAX_PORTALS = 200;
  self.MAX_URL_LEN = 6000;
  self.DEST        = '__DEST__';

  const isMobile = /IITC|Android|Mobile/i.test(navigator.userAgent) || !!window.isApp;

  self.openExternal = function(url){
    try {
      if (window.isApp && window.android) {
        if (typeof android.openUrl === 'function')       { android.openUrl(url);       return; }
        if (typeof android.openExternal === 'function')   { android.openExternal(url);  return; }
        if (typeof android.openInBrowser === 'function')  { android.openInBrowser(url); return; }
      }
    } catch(e) {}
    try { window.open(url, '_blank'); } catch(e) { location.href = url; }
  };

  self.visiblePortals = function(){
    const map = window.map;
    const bounds = map && map.getBounds ? map.getBounds() : null;
    if (!bounds) return [];
    const out = [];
    for (const id in window.portals) {
      const p = window.portals[id];
      if (!p || !p.getLatLng) continue;
      const ll = p.getLatLng();
      if (!bounds.contains(ll)) continue;
      const lat = ll.lat.toFixed(6);
      const lng = ll.lng.toFixed(6);
      const name = (p.options?.data?.title || 'Portal');
      out.push(`${name}; https://intel.ingress.com/intel?pll=${lat},${lng}`);
      if (out.length >= self.MAX_PORTALS) break;
    }
    return out;
  };

  self.copy = async function(text){
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch(e) {
      try {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.left = '-9999px';
        document.body.appendChild(ta);
        ta.focus(); ta.select();
        const ok = document.execCommand('copy');
        document.body.removeChild(ta);
        return ok;
      } catch(_) { return false; }
    }
  };

  selfupdateCounter = function(n){
    let el = document.getElementById('mf-portals-counter');
    if (!el) {
      el = document.createElement('div');
      el.id = 'mf-portals-counter';
      el.style.cssText = 'position:fixed;left:10px;bottom:10px;z-index:99999;padding:6px 10px;background:#111;color:#fff;border-radius:6px;font:12px/1.3 sans-serif;opacity:.85';
      (document.body || document.documentElement).appendChild(el);
    }
    el.textContent = 'Portais visíveis: ' + n + (n>=self.MAX_PORTALS ? ' (limite)' : '');
  };

  self.send = async function(){
    const map = window.map;
    const zoom = map && map.getZoom ? map.getZoom : 0;
    if (map && typeof map.getZoom === 'function') {{
      if (map.getZoom() < self.MIN_ZOOM) {{
        alert('Zoom insuficiente (mínimo ' + self.MIN_ZOOM + ').\\n\\nDica: aproxime com o botão + até enquadrar apenas a área desejada, e tente novamente.');
        return;
      }}
    }}

    let lines = self.visiblePortals();
    selfupdateCounter(lines.length);
    if (!lines.length) {
      alert('Nenhum portal visível nesta área.\\n\\nMova o mapa e/ou aumente o zoom até os marcadores aparecerem e tente novamente.');
      return;
    }
    if (lines.length > self.MAX_PORTALS) {
      alert('Foram detectados ' + lines.length + ' portais visíveis.\\nPor estabilidade, enviaremos somente ' + self.MAX_PORTALS + '.\\n\\nDica: aproxime mais e envie em partes para capturar todos.');
      lines = lines.slice(0, self.MAX_PORTALS);
    }

    const text = lines.join('\\n');
    const full = self.DEST + '?list=' + encodeURIComponent(text);

    if (full.length > self.MAX_URL_LEN) {
      await self.copy(text);
      alert('A URL ficou muito grande para abrir diretamente.\\n\\n✅ A LISTA DE PORTAIS FOI COPIADA para a área de transferência.\\n\\nComo proceder:\\n1) Abriremos o Maxfield agora.\\n2) No site, COLE a lista no campo de texto.\\n3) Clique em “Gerar plano”.\\n\\nDica: no mobile/IITC, se abrir dentro do app, escolha “abrir no navegador” (Chrome/Firefox).');
      self.openExternal(self.DEST);
      return;
    }

    await self.copy(full);
    self.openExternal(full);

    if (isMobile) {
      setTimeout(() => {
        alert('Abrimos o Maxfield em uma nova aba.\\n\\nSe ele abrir DENTRO do IITC, toque em “abrir no navegador” (Chrome/Firefox).\\nO link já foi copiado — se precisar, basta colar na barra de endereços.');
      }, 600);
    }
  };

  self.copyListOnly = async function(){
    const lines = self.visiblePortals();
    selfupdateCounter(lines.length);
    if (!lines.length) {{
      alert('Nenhum portal visível para copiar.');
      return;
    }}
    const text = lines.slice(0, self.MAX_PORTALS).join('\\n');
    await self.copy(text);
    alert('Lista copiada! Agora cole no campo de texto do Maxfield.');
  };

  self.addToolbarButtons = function(){
    if (document.getElementById('mf-send-btn-toolbar')) return true;
    const toolbox = document.getElementById('toolbox');
    if (!toolbox) return false;

    const a = document.createElement('a');
    a.id = 'mf-send-btn-toolbar';
    a.className = 'button';
    a.textContent = 'Send to Maxfield';
    a.href = '#';
    a.style.marginLeft = '6px';
    a.addEventListener('click', function(e){ e.preventDefault(); self.send(); });

    const b = document.createElement('a');
    b.id = 'mf-copy-btn-toolbar';
    b.className = 'button';
    b.textContent = 'Copiar lista (txt)';
    b.href = '#';
    b.style.marginLeft = '6px';
    b.addEventListener('click', function(e){ e.preventDefault(); self.copyListOnly(); });

    toolbox.appendChild(a);
    toolbox.appendChild(b);
    return true;
  };

  self.addFloatingButtons = function(){
    if (document.getElementById('mf-send-btn-float')) return;
    const box = document.createElement('div');
    box.id = 'mf-send-btn-float';
    box.style.cssText = 'position:fixed;right:10px;bottom:10px;z-index:99999;display:flex;gap:8px';
    const mk = (label, cb) => {{
      const btn = document.createElement('a');
      btn.textContent = label;
      btn.style.cssText = 'padding:6px 10px;background:#2b8;color:#fff;border-radius:4px;font:12px/1.3 sans-serif;cursor:pointer;box-shadow:0 2px 6px rgba(0,0,0,.25)';
      btn.addEventListener('click', function(e){{ e.preventDefault(); cb(); }});
      return btn;
    }};
    box.appendChild(mk('Send to Maxfield', self.send));
    box.appendChild(mk('Copiar lista (txt)', self.copyListOnly));
    (document.body || document.documentElement).appendChild(box);
  };

  self.mountButtonsRobust = function(){
    if (self.addToolbarButtons()) return;
    const start = Date.now();
    const intv = setInterval(() => {
      if (self.addToolbarButtons()) { clearInterval(intv); return; }
      if (Date.now() - start > 10000) { clearInterval(intv); self.addFloatingButtons(); }
    }, 300);
  };

  const setup = function(){ self.mountButtonsRobust(); };
  setup.info = plugin_info;

  if (!window.bootPlugins) window.bootPlugins = [];
  window.bootPlugins.push(setup);

  if (window.iitcLoaded) setup(); else window.addHook('iitcLoaded', setup);
}

const script = document.createElement('script');
const info = {};
if (typeof GM_info !== 'undefined' && GM_info && GM_info.script) {
  info.script = { version: GM_info.script.version, name: GM_info.script.name, description: GM_info.script.description };
}
script.appendChild(document.createTextNode('(' + wrapper + ')(' + JSON.stringify(info) + ');'));
(document.body || document.documentElement).appendChild(script);
"""

IITC_USERSCRIPT = (IITC_USERSCRIPT_TEMPLATE
    .replace("__DEST__", DEST)
    .replace("self.MIN_ZOOM    = 15;",   f"self.MIN_ZOOM    = {MIN_ZOOM};")
    .replace("self.MAX_PORTALS = 200;",  f"self.MAX_PORTALS = {MAX_PORTALS};")
    .replace("self.MAX_URL_LEN = 6000;", f"self.MAX_URL_LEN = {MAX_URL_LEN};")
)

# ---------- Título + KPIs ----------
st.title("Ingress Maxfield — Gerador de Planos")

colv, colp = st.columns(2)
with colv:
    st.metric("Acessos (sessões)", f"{get_metric('visits'):,}")
with colp:
    st.metric("Planos gerados", f"{get_metric('plans_completed'):,}")

# ---------- Ajuda + botões ----------
st.markdown(
    """
- Envie o **arquivo .txt de portais** ou **cole o conteúdo** do arquivo de portais.  
- Informe **nº de agentes** e **CPUs**.  
- **Mapa de fundo (opcional)**: informe uma **Google Maps API key**. **Ou deixe em branco para usar a nossa**.  
- Resultados: **imagens**, **CSVs** e (se permitido) **GIF** com o passo-a-passo.  
- Dica: você pode enviar a lista direto do IITC com o userscript abaixo.
    """
)

b1, b2, b3, b4 = st.columns(4)
with b1:
    st.download_button("📄 Baixar modelo (.txt)", EXEMPLO_TXT.encode("utf-8"),
                       file_name="modelo_portais.txt", mime="text/plain")
with b2:
    st.download_button("🧩 Baixar plugin IITC", IITC_USERSCRIPT.encode("utf-8"),
                       file_name="maxfield_iitc.user.js", mime="application/javascript")
with b3:
    TUTORIAL_URL = st.secrets.get("TUTORIAL_URL", "https://www.youtube.com/")
    st.link_button("▶️ Tutorial (normal)", TUTORIAL_URL)
with b4:
    TUTORIAL_IITC_URL = st.secrets.get("TUTORIAL_IITC_URL", TUTORIAL_URL)
    st.link_button("▶️ Tutorial (via IITC)", TUTORIAL_IITC_URL)

# ---------- PWA Lite ----------
st.markdown("""
<script>
try {
  const manifest = {
    "name": "Maxfield Online",
    "short_name": "Maxfield",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#101010",
    "theme_color": "#101010",
    "icons": []
  };
  const blob = new Blob([JSON.stringify(manifest)], {type: 'application/json'});
  const murl = URL.createObjectURL(blob);
  let link = document.querySelector('link[rel="manifest"]');
  if (!link) { link = document.createElement('link'); link.rel="manifest"; document.head.appendChild(link); }
  link.href = murl;

  const swCode = `
    self.addEventListener('install', (e)=>{ self.skipWaiting(); });
    self.addEventListener('activate', (e)=>{ e.waitUntil(clients.claim()); });
    self.addEventListener('fetch', (e)=>{ /* passthrough */ });
  `;
  const swBlob = new Blob([swCode], {type: 'text/javascript'});
  const swUrl = URL.createObjectURL(swBlob);
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register(swUrl).catch(()=>{});
  }
} catch(e) {}
</script>
""", unsafe_allow_html=True)

# ---------- Entrada pré-preenchida por ?list= ----------
def get_prefill_list() -> str:
    try:
        params = getattr(st, "query_params", None)
        if params is not None:
            return (params.get("list") or "")
        else:
            qp = st.experimental_get_query_params()
            return qp.get("list", [""])[0]
    except Exception:
        return ""

prefill_text = get_prefill_list()

# ---- sessão: chaves e limpeza adiada do campo de texto ----
if "uploader_key" not in st.session_state:
    st.session_state["uploader_key"] = 0
if "txt_content" not in st.session_state:
    st.session_state["txt_content"] = prefill_text or ""
if st.session_state.get("_clear_text", False):
    st.session_state["_clear_text"] = False
    st.session_state["txt_content"] = ""

# ---------- Job Manager ----------
@st.cache_resource(show_spinner=False)
def job_manager():
    return {
        "executor": ThreadPoolExecutor(max_workers=1),
        "jobs": {}
    }

def prune_jobs(max_jobs:int = 5, max_age_s:int = 3600):
    jm = job_manager()
    now = time.time()
    to_del = []
    for jid, rec in list(jm["jobs"].items()):
        age = now - float(rec.get("t0", now))
        if age > max_age_s or (rec.get("done") and age > 300):
            to_del.append(jid)
    alive = [(jid, rec["t0"]) for jid, rec in jm["jobs"].items() if jid not in to_del]
    if len(alive) > max_jobs:
        alive.sort(key=lambda x: x[1])
        extras = [jid for jid, _ in alive[:-max_jobs]]
        to_del.extend(extras)
    for jid in to_del:
        try:
            fut = jm["jobs"][jid]["future"]
            if fut and not fut.done(): fut.cancel()
        except Exception: pass
        jm["jobs"].pop(jid, None)

def run_job(kwargs: dict) -> dict:
    t0 = time.time()
    try:
        res = processar_plano(**kwargs)
        return {"ok": True, "result": res, "elapsed": time.time() - t0}
    except Exception as e:
        return {"ok": False, "error": str(e), "elapsed": time.time() - t0}

def start_job(kwargs: dict, eta_s: float, meta: dict) -> str:
    prune_jobs()
    jm = job_manager()
    job_id = uuid.uuid4().hex[:8]
    fut = jm["executor"].submit(run_job, kwargs | {"job_id": job_id, "team": meta.get("team","")})
    jm["jobs"][job_id] = {"future": fut, "t0": time.time(), "eta": eta_s, "meta": meta, "done": False, "out": None}
    return job_id

def get_job(job_id: str):
    return job_manager()["jobs"].get(job_id)

# ---------- Restaura job por URL ----------
if "job_id" not in st.session_state:
    jid = qp_get("job", "")
    if jid:
        if get_job(jid):
            st.session_state["job_id"] = jid
        else:
            qp_set(job=None)

# ---------- Processamento principal (cache com TTL) ----------
@st.cache_data(show_spinner=False, ttl=3600)
def processar_plano(portal_bytes: bytes,
                    num_agents: int,
                    num_cpus: int,
                    res_colors: bool,
                    google_api_key: str | None,
                    google_api_secret: str | None,
                    output_csv: bool,
                    fazer_gif: bool,
                    job_id: str,
                    team: str):
    jobs_root = os.path.join("data", "jobs")
    os.makedirs(jobs_root, exist_ok=True)
    outdir = os.path.join(jobs_root, job_id)
    os.makedirs(outdir, exist_ok=True)

    portal_path = os.path.join(outdir, "portais.txt")
    with open(portal_path, "wb") as f:
        f.write(portal_bytes)

    t0 = time.time()
    def t(msg): print(f"[{time.strftime('%H:%M:%S')}] {msg}")

    log_buffer = io.StringIO()
    try:
        with redirect_stdout(log_buffer):
            t("INÍCIO processar_plano")
            print(f"[INFO] os.cpu_count()={os.cpu_count()} · cpus_req={st.session_state.get('num_cpus',0) if 'num_cpus' in st.session_state else 0} · cpus_eff={num_cpus} · gif={fazer_gif} · csv={output_csv} · team={team}")
            t("Chamando run_maxfield()…")
            run_maxfield(
                portal_path,
                num_agents=int(num_agents),
                num_cpus=int(num_cpus),
                res_colors=res_colors,
                google_api_key=(google_api_key or None),
                google_api_secret=(google_api_secret or None),
                output_csv=output_csv,
                outdir=outdir,
                verbose=True,
                skip_step_plots=(not fazer_gif),
            )
            t(f"maxfield() OK em {time.time()-t0:.1f}s")
            t1 = time.time()
            t("Compactando artefatos no ZIP…")
    except Exception as e:
        log_buffer.write(f"\n[ERRO] {e}\n")
        raise
    finally:
        pass

    zip_path = os.path.join(outdir, f"maxfield_output_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(outdir):
            for fn in files:
                if fn.endswith(".zip"): continue
                fp = os.path.join(root, fn)
                arc = os.path.relpath(fp, outdir)
                z.write(fp, arcname=arc)
    with redirect_stdout(log_buffer):
        print(f"[{time.strftime('%H:%M:%S')}] ZIP pronto em {time.time()-t1:.1f}s; total {time.time()-t0:.1f}s")

    log_txt = log_buffer.getvalue()

    log_path = os.path.join(outdir, "maxfield_log.txt")
    with open(log_path, "w", encoding="utf-8", errors="ignore") as lf:
        lf.write(log_txt or "")

    def read_bytes(path):
        return open(path, "rb").read() if os.path.exists(path) else None

    pm_bytes = read_bytes(os.path.join(outdir, "portal_map.png"))
    lm_bytes = read_bytes(os.path.join(outdir, "link_map.png"))
    gif_bytes = read_bytes(os.path.join(outdir, "plan_movie.gif"))

    # mini summary salvo em arquivos (sem botões)
    summary_md = []
    summary_md.append(f"# Plano Maxfield — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    summary_md.append(f"- **Job**: `{job_id}`")
    summary_md.append(f"- **Facção**: {'Resistance (azul)' if res_colors else 'Enlightened (verde)'}")
    summary_md.append(f"- **Agentes**: {num_agents} · **CPUs**: {num_cpus} · **CSV**: {output_csv} · **GIF**: {fazer_gif}")
    summary_md.append(f"- **Portais**: ver `portais.txt`")
    if os.path.exists(os.path.join(outdir, "portal_map.png")):
        summary_md.append(f"\n![Portal Map](portal_map.png)")
    if os.path.exists(os.path.join(outdir, "link_map.png")):
        summary_md.append(f"\n![Link Map](link_map.png)")
    summary_md.append("\n---\nLogs completos: `maxfield_log.txt`")
    summary_md = "\n".join(summary_md)
    with open(os.path.join(outdir, "summary.md"), "w", encoding="utf-8") as f:
        f.write(summary_md)

    summary_html = f"""<!doctype html><html lang="pt-br"><meta charset="utf-8">
<title>Plano Maxfield — {job_id}</title>
<style>body{{font-family:sans-serif;margin:24px}} img{{max-width:100%;height:auto}} h1{{margin-top:0}}</style>
<h1>Plano Maxfield — {datetime.now().strftime('%Y-%m-%d %H:%M')}</h1>
<p><b>Job:</b> {job_id}<br>
<b>Facção:</b> {"Resistance (azul)" if res_colors else "Enlightened (verde)"}<br>
<b>Agentes:</b> {num_agents} · <b>CPUs:</b> {num_cpus} · <b>CSV:</b> {output_csv} · <b>GIF:</b> {fazer_gif}</p>
<p>Portais: ver <code>portais.txt</code></p>
{"<h2>Portal Map</h2><img src='portal_map.png'>" if os.path.exists(os.path.join(outdir,"portal_map.png")) else ""}
{"<h2>Link Map</h2><img src='link_map.png'>" if os.path.exists(os.path.join(outdir,"link_map.png")) else ""}
<hr><p>Logs: <code>maxfield_log.txt</code></p>
</html>"""
    with open(os.path.join(outdir, "summary.html"), "w", encoding="utf-8") as f:
        f.write(summary_html)

    zip_bytes = open(zip_path, "rb").read()

    return {
        "zip_bytes": zip_bytes,
        "pm_bytes": pm_bytes,
        "lm_bytes": lm_bytes,
        "gif_bytes": gif_bytes,
        "log_txt": log_txt,
        "outdir": outdir,
        "job_id": job_id
    }

# ---------- UI Principal (tabs) ----------
ENABLE_FORUM = bool(st.secrets.get("ENABLE_FORUM", True))
tabs = ["🧩 Gerar plano", "🕑 Histórico", "📊 Métricas"]
if ENABLE_FORUM:
    tabs.append("💬 Fórum (debate e melhorias)")
tab_objs = st.tabs(tabs)

tab_gen = tab_objs[0]
tab_hist = tab_objs[1]
tab_metrics = tab_objs[2]
tab_forum = tab_objs[3] if ENABLE_FORUM else None

# ====== TAB: GERAR PLANO ======
with tab_gen:
    st.markdown('<span class="mf-chip enl">Enlightened</span><span class="mf-chip res">Resistance</span>', unsafe_allow_html=True)

    fast_mode = st.toggle("⚡ Modo rápido (desliga GIF e CSV para máxima velocidade)", value=False, key="fast_mode")

    with st.form("plan_form"):
        uploaded = st.file_uploader(
            "Arquivo de portais (.txt)", type=["txt"],
            key=f"uploader_{st.session_state['uploader_key']}"
        )
        txt_content = st.text_area(
            "Ou cole o conteúdo do arquivo de portais",
            height=200,
            key="txt_content",
            placeholder="Portal 1; https://www.ingress.com/intel?...pll=LAT,LON\nPortal 2; ..."
        )

        with st.expander("🗺️ Pré-visualização dos portais (opcional)"):
            txt_preview = txt_content or (uploaded.getvalue().decode("utf-8", errors="ignore") if uploaded else "")
            pts = extract_points(clean_invisibles(txt_preview))
            st.write(f"Detectados **{len(pts)}** portais para prévia.")
            if pts:
                import pandas as pd, pydeck as pdk
                df = pd.DataFrame(pts)
                mid_lat = df["lat"].mean()
                mid_lon = df["lon"].mean()
                layer = pdk.Layer("ScatterplotLayer", data=df, get_position='[lon, lat]', get_radius=12, pickable=True)
                st.pydeck_chart(pdk.Deck(map_style=None,
                                         initial_view_state=pdk.ViewState(latitude=mid_lat, longitude=mid_lon, zoom=14),
                                         layers=[layer]))
            else:
                st.caption("Cole/importe uma lista com URLs contendo `pll=lat,lon` para ver a prévia.")

        col1, col2 = st.columns(2)
        with col1:
            num_agents = st.number_input("Número de agentes", min_value=1, max_value=50, value=1, step=1, key="num_agents")
        with col2:
            num_cpus = st.number_input("CPUs a usar (0 = máximo)", min_value=0, max_value=128, value=0, step=1, key="num_cpus")

        team = st.selectbox("Facção (cores)", ["Enlightened (verde)", "Resistance (azul)"], key="team_select")
        output_csv_default = False if st.session_state.get("fast_mode", False) else True
        gif_default = False
        output_csv = st.checkbox("Gerar CSV", value=output_csv_default, disabled=st.session_state.get("fast_mode", False), key="out_csv")
        st.caption("Dica: no celular o CSV é ruim de editar. No Modo Rápido ele fica desativado por padrão.")
        gerar_gif_checkbox = st.checkbox("Gerar GIF (passo-a-passo)", value=gif_default, disabled=st.session_state.get("fast_mode", False), key="out_gif")

        st.markdown("**Mapa de fundo (opcional):**")
        google_key_input = st.text_input("Google Maps API key (opcional)", value="", help="Se deixar vazio e houver uma chave salva no servidor, ela será usada automaticamente.", key="g_key")
        google_api_secret_input = st.text_input("Google Maps API secret (opcional)", value="", type="password", key="g_secret")

        sem_mapa = st.checkbox("Sem mapa de fundo (mais rápido/robusto)", value=False, help="Ignora a API do Google nesta execução (útil quando a rede/quotas estão instáveis).", key="no_bg_map")

        submitted = st.form_submit_button("Gerar plano", use_container_width=True)

    if submitted:
        if uploaded:
            portal_bytes = uploaded.getvalue()
            texto_portais = portal_bytes.decode("utf-8", errors="ignore")
        else:
            if not st.session_state["txt_content"].strip():
                st.error("Envie um arquivo .txt ou cole o conteúdo.")
                st.stop()
            texto_portais = st.session_state["txt_content"]

        texto_portais = clean_invisibles(texto_portais)

        # limite server-side de portais
        MAX_PORTALS_SERVER = int(st.secrets.get("MAX_PORTALS", 200))
        linhas = texto_portais.splitlines()
        count = 0; kept = []
        for ln in linhas:
            s = ln.strip()
            if s and not s.startswith("#"):
                count += 1
                if count > MAX_PORTALS_SERVER:
                    continue
            kept.append(ln)
        if count > MAX_PORTALS_SERVER:
            st.warning(f"Lista com {count} portais; usando apenas os primeiros {MAX_PORTALS_SERVER}.")
            texto_portais = "\n".join(kept)

        portal_bytes = texto_portais.encode("utf-8")
        res_colors = team.startswith("Resistance")
        n_portais = contar_portais(texto_portais)

        fazer_gif = (not st.session_state.get("fast_mode", False)) and bool(gerar_gif_checkbox)
        if n_portais > 25 and fazer_gif:
            st.warning(f"Detectei **{n_portais} portais**. Para evitar travamentos, o GIF foi **desativado automaticamente**.")
            fazer_gif = False

        output_csv = (not st.session_state.get("fast_mode", False)) and bool(output_csv)

        if sem_mapa:
            google_api_key = None
            google_api_secret = None
            os.environ["MAXFIELD_DISABLE_BASEMAP"] = "1"
        else:
            os.environ.pop("MAXFIELD_DISABLE_BASEMAP", None)
            google_api_key = (google_key_input or "").strip() or st.secrets.get("GOOGLE_API_KEY", None)
            google_api_secret = (google_api_secret_input or "").strip() or st.secrets.get("GOOGLE_API_SECRET", None)

        eff_cpus = int(num_cpus)
        if eff_cpus == 0:
            eff_cpus = min(4, os.cpu_count() or 2)

        kwargs = dict(
            portal_bytes=portal_bytes,
            num_agents=int(num_agents),
            num_cpus=int(eff_cpus),
            res_colors=res_colors,
            google_api_key=google_api_key,
            google_api_secret=google_api_secret,
            output_csv=output_csv,
            fazer_gif=fazer_gif,
            team=team
        )

        eta_s = estimate_eta_s(n_portais, int(eff_cpus), fazer_gif)
        meta = {"n_portais": n_portais, "num_cpus": int(eff_cpus), "gif": fazer_gif, "team": team}

        st.session_state["_clear_text"] = True
        st.session_state["uploader_key"] += 1

        new_id = start_job(kwargs, eta_s, meta)
        st.session_state["job_id"] = new_id
        qp_set(job=new_id)

        try:
            st.toast(f"Job {new_id} enfileirado: {n_portais} portais · ETA ~{int(eta_s)}s")
        except Exception:
            pass

        st.rerun()

# ===== UI de acompanhamento do job =====
job_id = st.session_state.get("job_id")
if job_id:
    job = get_job(job_id)
    if not job:
        st.warning("Não encontrei o job atual (talvez tenha concluído e sido limpo).")
        qp_set(job=None)
    else:
        if job.get("done") and job.get("out") is not None:
            out = job["out"]
            if out.get("ok"):
                res = out["result"]
                st.session_state["last_result"] = res
                inc_metric("plans_completed", 1)
                try:
                    record_run(
                        int(job.get("meta", {}).get("n_portais", 0)),
                        int(job.get("meta", {}).get("num_cpus", 0)),
                        bool(job.get("meta", {}).get("gif", False)),
                        float(out.get("elapsed", 0.0)),
                    )
                    add_job_row(
                        job_id=out.get("job_id", job_id),
                        uid=UID,
                        n_portais=int(job.get("meta", {}).get("n_portais", 0)),
                        num_cpus=int(job.get("meta", {}).get("num_cpus", 0)),
                        team=str(job.get("meta", {}).get("team","")),
                        output_csv=bool(job.get("meta", {}).get("output_csv", True)),
                        fazer_gif=bool(job.get("meta", {}).get("gif", False)),
                        dur_s=float(out.get("elapsed", 0.0)),
                        out_dir=str(res.get("outdir",""))
                    )
                except Exception:
                    pass
            else:
                st.error(f"Erro ao gerar o plano: {out.get('error','desconhecido')}")
            del st.session_state["job_id"]
            qp_set(job=None)
        else:
            fut = job["future"]
            t0 = job["t0"]
            eta_s = job["eta"]
            canceled = False
            with st.status(f"⏳ Processando… (job {job_id})", expanded=True) as status:
                bar = st.progress(0)
                eta_ph = st.empty()
                cancel_ph = st.empty()
                if cancel_ph.button("🛑 Cancelar este job", key=f"cancel_{job_id}"):
                    canceled = True
                    try:
                        fut.cancel()
                        status.update(label="🛑 Cancelando…", state="error", expanded=True)
                    except Exception:
                        pass

                while not fut.done():
                    if canceled:
                        break
                    elapsed = time.time() - t0
                    pct = min(0.90, elapsed / max(1e-6, eta_s))
                    bar.progress(int(pct * 100))
                    eta_left = max(0, eta_s - elapsed)
                    eta_ph.write(f"**Estimativa:** ~{int(eta_left)}s restantes · **Decorridos:** {int(elapsed)}s")
                    time.sleep(0.3)

            if canceled:
                if fut.cancelled():
                    job["done"] = True
                    job["out"] = {"ok": False, "error": "Job cancelado pelo usuário"}
                else:
                    job["done"] = True
                    job["out"] = {"ok": False, "error": "Cancelamento solicitado (não foi possível interromper em execução)"}
                del st.session_state["job_id"]
                qp_set(job=None)
                st.stop()

            out = fut.result()
            bar.progress(100)
            job["done"] = True
            job["out"] = out
            if out.get("ok"):
                status.update(label="✅ Concluído", state="complete", expanded=False)
                res = out["result"]
                st.session_state["last_result"] = res
                inc_metric("plans_completed", 1)
                try:
                    record_run(
                        int(job.get("meta", {}).get("n_portais", 0)),
                        int(job.get("meta", {}).get("num_cpus", 0)),
                        bool(job.get("meta", {}).get("gif", False)),
                        float(out.get("elapsed", 0.0)),
                    )
                    add_job_row(
                        job_id=out.get("job_id", job_id),
                        uid=UID,
                        n_portais=int(job.get("meta", {}).get("n_portais", 0)),
                        num_cpus=int(job.get("meta", {}).get("num_cpus", 0)),
                        team=str(job.get("meta", {}).get("team","")),
                        output_csv=bool(job.get("meta", {}).get("output_csv", True)),
                        fazer_gif=bool(job.get("meta", {}).get("gif", False)),
                        dur_s=float(out.get("elapsed", 0.0)),
                        out_dir=str(res.get("outdir",""))
                    )
                except Exception:
                    pass
            else:
                status.update(label="❌ Falhou", state="error", expanded=True)
                st.error(f"Erro ao gerar o plano: {out.get('error','desconhecido')}")
            del st.session_state["job_id"]
            qp_set(job=None)

# ===== Render de resultados persistentes =====
res = st.session_state.get("last_result")
if res:
    st.success("Plano gerado com sucesso!")
    if res.get("pm_bytes"):
        st.image(res["pm_bytes"], caption="Portal Map")
    if res.get("lm_bytes"):
        st.image(res["lm_bytes"], caption="Link Map")
    if res.get("gif_bytes"):
        st.download_button(
            "Baixar GIF (plan_movie.gif)",
            data=res["gif_bytes"],
            file_name="plan_movie.gif",
            mime="image/gif",
            key="dl_gif_last",
        )
    st.download_button(
        "Baixar todos os arquivos (.zip)",
        data=res["zip_bytes"],
        file_name=f"maxfield_output_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip",
        mime="application/zip",
        key="dl_zip_last",
    )
    with st.expander("Ver logs do processamento"):
        log_txt_full = res.get("log_txt") or "(sem logs)"
        if len(log_txt_full) > 20000:
            st.caption("Log truncado (últimos ~20k caracteres).")
            log_txt = log_txt_full[-20000:]
        else:
            log_txt = log_txt_full
        st.code(log_txt, language="bash")
    if st.button("🧹 Limpar resultados", key="clear_res"):
        st.session_state.pop("last_result", None)
        qp_set(job=None)
        st.rerun()

# ---------- HISTÓRICO ----------
with tab_hist:
    st.caption(f"Seu ID anônimo: `{UID}` — os planos abaixo ficam disponíveis por 24h.")
    rows = list_jobs_recent(uid=UID, within_hours=24, limit=50)
    if not rows:
        st.info("Sem planos recentes. Gere um plano para aparecer aqui.")
    else:
        for (jid, ts, uid, n_port, ncpu, team, out_csv, do_gif, dur_s, out_dir) in rows:
            dt = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
            with st.container(border=True):
                st.write(f"**Job {jid}** — {dt} · Portais: **{n_port}** · CPUs: {ncpu} · {team} · CSV: {bool(out_csv)} · GIF: {bool(do_gif)} · Duração: {int(dur_s)}s")
                if out_dir and os.path.isdir(out_dir):
                    pm = os.path.join(out_dir, "portal_map.png")
                    lm = os.path.join(out_dir, "link_map.png")
                    gif_p = os.path.join(out_dir, "plan_movie.gif")
                    zip_p = None
                    for fn in os.listdir(out_dir):
                        if fn.endswith(".zip"): zip_p = os.path.join(out_dir, fn)
                    cols = st.columns(4)
                    if os.path.exists(pm):
                        with cols[0]:
                            st.download_button("Portal Map", data=open(pm,"rb").read(),
                                               file_name="portal_map.png", mime="image/png", key=f"dl_pm_{jid}")
                    if os.path.exists(lm):
                        with cols[1]:
                            st.download_button("Link Map", data=open(lm,"rb").read(),
                                               file_name="link_map.png", mime="image/png", key=f"dl_lm_{jid}")
                    if os.path.exists(gif_p):
                        with cols[2]:
                            st.download_button("GIF", data=open(gif_p,"rb").read(),
                                               file_name="plan_movie.gif", mime="image/gif", key=f"dl_gif_{jid}")
                    if zip_p and os.path.exists(zip_p):
                        with cols[3]:
                            st.download_button("ZIP", data=open(zip_p,"rb").read(),
                                               file_name=os.path.basename(zip_p), mime="application/zip", key=f"dl_zip_{jid}")
                else:
                    st.caption("_Arquivos expirados pela limpeza diária._")

# ---------- MÉTRICAS ----------
with tab_metrics:
    conn = get_db()
    cur = conn.execute("SELECT ts, n_portais, num_cpus, gif, dur_s FROM runs ORDER BY ts DESC LIMIT 100")
    data = cur.fetchall()
    if not data:
        st.info("Ainda sem dados suficientes para métricas.")
    else:
        import pandas as pd
        df = pd.DataFrame(data, columns=["ts","n_portais","num_cpus","gif","dur_s"])
        p50 = float(df["dur_s"].quantile(0.50))
        p90 = float(df["dur_s"].quantile(0.90))
        st.metric("Duração p50 (s)", f"{int(p50)}")
        st.metric("Duração p90 (s)", f"{int(p90)}")
        st.metric("Execuções (últimos 100)", f"{len(df)}")
        st.bar_chart(df[["dur_s"]].iloc[::-1], height=180)
        st.caption("Barras (da mais antiga para a mais recente) mostram a duração por execução.")

# ===================== FORUM / LOGIN =====================
import hashlib

ADMIN_CODE = st.secrets.get("ADMIN_CODE", "")
COMMENTS_ENABLED = bool(st.secrets.get("COMMENTS_ENABLED", True))
MAX_IMG_MB = int(st.secrets.get("MAX_IMG_MB", 2))
MAX_IMGS_PER_POST = int(st.secrets.get("MAX_IMGS_PER_POST", 3))

def _now_ts() -> int:
    return int(time.time())

def hash_pass(password: str, salt: str) -> str:
    base = f"{salt}:{password}".encode("utf-8", "ignore")
    return hashlib.sha256(base).hexdigest()

def save_avatar_file(user_id: int, avatar_bytes: bytes|None, avatar_ext: str|None) -> str|None:
    if not avatar_bytes or not avatar_ext:
        return None
    safe_ext = avatar_ext.lower().strip()
    if not safe_ext.startswith("."): safe_ext = "." + safe_ext
    if safe_ext not in (".png", ".jpg", ".jpeg", ".webp"):
        return None
    av_dir = os.path.join("data", "avatars", str(int(user_id)))
    os.makedirs(av_dir, exist_ok=True)
    av_path = os.path.join(av_dir, f"avatar{safe_ext}")
    try:
        with open(av_path, "wb") as f:
            f.write(avatar_bytes)
        conn = get_db()
        conn.execute("UPDATE users SET avatar_ext=? WHERE id=?", (safe_ext, int(user_id)))
        conn.commit()
        return safe_ext
    except Exception:
        return None

def get_user_by_username_or_email(identifier: str):
    if not identifier:
        return None
    ident = identifier.strip()
    conn = get_db()
    cur = conn.execute("""
        SELECT id, username, username_lc, pass_hash, faction, email, avatar_ext, is_admin, pass_salt
          FROM users
         WHERE username_lc = ? OR email = ?
         LIMIT 1
    """, (ident.lower(), ident))
    row = cur.fetchone()
    if not row:
        return None
    return {
        "id": int(row[0]),
        "username": row[1] or "",
        "username_lc": row[2] or "",
        "pass_hash": row[3] or "",
        "faction": row[4] or "",
        "email": row[5] or "",
        "avatar_ext": row[6] or None,
        "is_admin": int(row[7] or 0),
        "pass_salt": row[8] or "",
    }

def create_user(username: str,
                password: str,
                faction: str,
                email: str|None,
                is_admin_bool: bool,
                avatar_bytes: bytes|None,
                avatar_ext: str|None) -> int:
    if not username or not password:
        raise ValueError("username e password são obrigatórios")

    uname = username.strip()
    uname_lc = uname.lower()
    fac = (faction or "").strip()
    mail = (email or "").strip() or None
    is_admin = 1 if is_admin_bool else 0
    ts = _now_ts()

    salt = uuid.uuid4().hex[:8]
    p_hash = hash_pass(password, salt)

    conn = get_db()
    cur = conn.execute("SELECT 1 FROM users WHERE username_lc=?", (uname_lc,))
    if cur.fetchone():
        raise ValueError("Este nome de usuário já está em uso.")

    conn.execute("""
        INSERT INTO users (username, username_lc, pass_hash, pass_salt, faction, email, avatar_ext, is_admin, created_ts, updated_ts)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (uname, uname_lc, p_hash, salt, fac, mail, None, int(is_admin), ts, ts))
    conn.commit()

    cur = conn.execute("SELECT id FROM users WHERE username_lc=?", (uname_lc,))
    row = cur.fetchone()
    if not row:
        raise RuntimeError("Falha ao criar usuário.")
    user_id = int(row[0])

    if avatar_bytes and avatar_ext:
        save_avatar_file(user_id, avatar_bytes, avatar_ext)

    return user_id

def check_password(user_row: dict, password: str) -> bool:
    if not user_row or not password:
        return False
    conn = get_db()
    cur = conn.execute("SELECT pass_hash, pass_salt FROM users WHERE id=?", (int(user_row["id"]),))
    row = cur.fetchone()
    if not row:
        return False
    ph, psalt = row[0] or "", (row[1] or "")
    if psalt:
        return hash_pass(password, psalt) == ph
    import hashlib as _h
    return _h.sha256(password.encode("utf-8","ignore")).hexdigest() == ph

def create_session(user_id:int) -> str:
    token = uuid.uuid4().hex
    ts = _now_ts()
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO sessions(token,user_id,created_ts,last_seen_ts) VALUES(?,?,?,?)",
                 (token, int(user_id), ts, ts))
    conn.commit()
    return token

def get_user_by_token(token:str):
    conn = get_db()
    cur = conn.execute("""
        SELECT u.id, u.username, u.username_lc, u.faction, u.email, u.avatar_ext, u.is_admin
          FROM sessions s JOIN users u ON u.id=s.user_id
         WHERE s.token=? LIMIT 1
    """, (token,))
    row = cur.fetchone()
    if not row:
        return None
    conn.execute("UPDATE sessions SET last_seen_ts=? WHERE token=?", (_now_ts(), token))
    conn.commit()
    return {
        "id": int(row[0]),
        "username": row[1] or "",
        "username_lc": row[2] or "",
        "faction": row[3] or "",
        "email": row[4] or "",
        "avatar_ext": row[5] or None,
        "is_admin": int(row[6] or 0),
    }

def signout_current():
    token = qp_get("token","")
    if token:
        get_db().execute("DELETE FROM sessions WHERE token=?", (token,))
        get_db().commit()
    st.session_state.pop("user", None)
    qp_set(token=None)
    try: st.toast("Você saiu.")
    except: pass
    st.experimental_rerun()

def current_user():
    if "user" in st.session_state and st.session_state["user"]:
        return st.session_state["user"]
    token = qp_get("token","")
    if token:
        u = get_user_by_token(token)
        if u:
            st.session_state["user"] = u
            return u
    return None

def forum_count_comments(post_id:int) -> int:
    cur = get_db().execute("SELECT COUNT(*) FROM forum_comments WHERE post_id=? AND (deleted_ts IS NULL)", (int(post_id),))
    return int(cur.fetchone()[0])

def forum_create_post(cat:str, title:str, body_md:str, images, author:dict) -> int:
    ts = _now_ts()
    conn = get_db()
    conn.execute("""
        INSERT INTO forum_posts(cat,title,body_md,author_id,author_name,author_faction,created_ts,updated_ts,images_json,is_pinned)
        VALUES(?,?,?,?,?,?,?,?,?,0)
    """, (cat, title.strip(), body_md.strip(), int(author["id"]), author["username"], author["faction"], ts, ts, "[]"))
    conn.commit()
    cur = conn.execute("SELECT id FROM forum_posts WHERE author_id=? ORDER BY id DESC LIMIT 1", (int(author["id"]),))
    row = cur.fetchone()
    post_id = int(row[0])

    saved = []
    if images:
        root = os.path.join("data","posts",str(post_id))
        os.makedirs(root, exist_ok=True)
        for i, f in enumerate(images[:MAX_IMGS_PER_POST], start=1):
            data = f.getvalue()
            if len(data) > MAX_IMG_MB*1024*1024:
                continue
            name = f.name.lower()
            ext = ".png"
            for e in (".png",".jpg",".jpeg",".webp"):
                if name.endswith(e):
                    ext = e; break
            p = os.path.join(root, f"img{i}{ext}")
            with open(p,"wb") as out:
                out.write(data)
            saved.append(os.path.basename(p))
    conn.execute("UPDATE forum_posts SET images_json=? WHERE id=?", (json.dumps(saved), post_id))
    conn.commit()
    return post_id

def forum_list_posts(cat:str):
    cur = get_db().execute("""
        SELECT id, title, author_name, author_faction, created_ts, images_json, author_id
          FROM forum_posts
         WHERE cat=?
         ORDER BY is_pinned DESC, created_ts DESC
    """, (cat,))
    return cur.fetchall()

def forum_get_post(post_id:int):
    cur = get_db().execute("""
        SELECT id, cat, title, body_md, author_id, author_name, author_faction, created_ts, images_json
          FROM forum_posts WHERE id=? LIMIT 1
    """, (int(post_id),))
    return cur.fetchone()

def forum_update_post(post_id:int, new_title:str, new_body:str):
    conn = get_db()
    conn.execute("UPDATE forum_posts SET title=?, body_md=?, updated_ts=? WHERE id=?", (new_title.strip(), new_body.strip(), _now_ts(), int(post_id)))
    conn.commit()

def forum_add_comment(post_id:int, author:dict, body_md:str):
    ts = _now_ts()
    get_db().execute("""
        INSERT INTO forum_comments(post_id,author_id,author_name,author_faction,body_md,created_ts,deleted_ts)
        VALUES(?,?,?,?,?,?,NULL)
    """, (int(post_id), int(author["id"]), author["username"], author["faction"], body_md.strip(), ts))
    get_db().commit()

def forum_update_comment(comment_id:int, new_body:str):
    get_db().execute("UPDATE forum_comments SET body_md=? WHERE id=?", (new_body.strip(), int(comment_id)))
    get_db().commit()

def forum_list_comments(post_id:int):
    cur = get_db().execute("""
        SELECT id, author_id, author_name, author_faction, body_md, created_ts, deleted_ts
          FROM forum_comments
         WHERE post_id=?
         ORDER BY created_ts ASC
    """, (int(post_id),))
    return cur.fetchall()

def forum_delete_comment(comment_id:int):
    get_db().execute("UPDATE forum_comments SET deleted_ts=? WHERE id=?", (_now_ts(), int(comment_id)))
    get_db().commit()

def user_avatar_bytes(user_id:int, avatar_ext:str|None):
    if not avatar_ext: return None
    p = os.path.join("data","avatars",str(user_id), f"avatar{avatar_ext}")
    if os.path.exists(p):
        try: return open(p,"rb").read()
        except: return None
    return None

# ---- Fórum UI ----
if tab_forum is not None:
    with tab_forum:
        auto = st.toggle("🔄 Auto-atualizar a cada 20s", value=False, key="forum_auto")
        if auto:
            st.markdown("<script>setTimeout(()=>location.reload(),20000)</script>", unsafe_allow_html=True)

        u = current_user()

        # Banner visitante + login compacto
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
                        if not usr or not check_password(usr, li_pass):
                            st.error("Usuário ou senha inválidos.")
                        else:
                            token = create_session(usr["id"])
                            st.session_state["user"] = usr
                            qp_set(token=token)
                            try: st.toast("Login ok!")
                            except: pass
                            st.experimental_rerun()
                with colR:
                    st.markdown("#### Criar conta")
                    su_user = st.text_input("Nome de usuário (único)", key="su_user")
                    su_faction = st.selectbox("Facção", ["Enlightened", "Resistance"], key="su_faction")
                    su_email = st.text_input("E-mail (opcional)", key="su_email")
                    su_pass = st.text_input("Senha", type="password", key="su_pass")
                    su_pass2 = st.text_input("Confirmar senha", type="password", key="su_pass2")
                    su_avatar = st.file_uploader("Avatar (opcional)", type=["png","jpg","jpeg","webp"], key="su_avatar")
                    su_admin_code = st.text_input("Código de admin (se houver)", type="password", key="su_admin_code")
                    if st.button("Criar conta", key="su_btn"):
                        if not su_user or not su_pass:
                            st.error("Preencha usuário e senha.")
                        elif su_pass != su_pass2:
                            st.error("As senhas não conferem.")
                        else:
                            is_admin = bool(ADMIN_CODE) and (su_admin_code.strip() == ADMIN_CODE.strip())
                            av_bytes, av_ext = None, None
                            if su_avatar is not None:
                                av_bytes = su_avatar.getvalue()
                                n = su_avatar.name.lower()
                                if n.endswith(".png"): av_ext=".png"
                                elif n.endswith(".jpg") or n.endswith(".jpeg"): av_ext=".jpg"
                                elif n.endswith(".webp"): av_ext=".webp"
                                else: av_ext=None
                            try:
                                uid_new = create_user(su_user, su_pass, su_faction, (su_email or "").strip() or None, is_admin, av_bytes, av_ext)
                                usr = get_user_by_username_or_email(su_user)
                                token = create_session(usr["id"])
                                st.session_state["user"] = usr
                                qp_set(token=token)
                                st.success("Conta criada! Você já está logado.")
                                st.experimental_rerun()
                            except ValueError as ve:
                                st.error(str(ve))
                            except Exception as e:
                                st.error(f"Erro ao criar conta: {e}")
        else:
            with st.container(border=True):
                colA, colB, colC = st.columns([0.6,0.25,0.15])
                with colA:
                    st.write(f"Logado como **{u['username']}** ({u['faction']}){' · 🛡️ Admin' if u['is_admin'] else ''}")
                with colB:
                    # Trocar avatar
                    st.caption("Trocar avatar")
                    new_av = st.file_uploader(" ", type=["png","jpg","jpeg","webp"], key="new_avatar")
                    if new_av is not None:
                        data = new_av.getvalue()
                        name = new_av.name.lower()
                        ext = ".png"
                        for e in (".png",".jpg",".jpeg",".webp"):
                            if name.endswith(e): ext=e; break
                        if save_avatar_file(int(u["id"]), data, ext):
                            # refresh usuário
                            usr = get_user_by_username_or_email(u["username"])
                            st.session_state["user"] = usr
                            st.toast("Avatar atualizado!")
                            st.experimental_rerun()
                with colC:
                    if st.button("Sair", key="logout_btn"):
                        signout_current()
                # avatar atual
                av_bytes = user_avatar_bytes(u["id"], u.get("avatar_ext"))
                if av_bytes:
                    st.image(av_bytes, width=64)

        st.markdown("---")
        st.subheader("Tópicos")

        cat_tabs = st.tabs(["📢 Atualizações", "💡 Sugestões", "🧱 Críticas", "❓ Dúvidas"])
        CATS = ["Atualizações","Sugestões","Críticas","Dúvidas"]

        for ci, ct in enumerate(cat_tabs):
            with ct:
                cat = CATS[ci]
                can_create = (cat == "Atualizações" and u and u["is_admin"]==1) or (cat in ("Sugestões","Críticas","Dúvidas") and u)

                # Criar tópico (controlado por estado)
                exp_key = f"new_topic_open_{cat}"
                if exp_key not in st.session_state:
                    st.session_state[exp_key] = False
                nonce_key = f"nt_uploader_nonce_{cat}"
                if nonce_key not in st.session_state:
                    st.session_state[nonce_key] = 0

                if can_create:
                    if not st.session_state[exp_key]:
                        if st.button("➕ Novo tópico", key=f"open_new_topic_{cat}"):
                            st.session_state[exp_key] = True
                            st.experimental_rerun()
                    else:
                        with st.expander("➕ Novo tópico", expanded=True):
                            st.caption("Crie um novo tópico para esta categoria.")
                            nt_title = st.text_input("Título", key=f"nt_title_{cat}")
                            nt_body = st.text_area("Conteúdo (Markdown)", key=f"nt_body_{cat}", height=140)
                            nt_imgs = st.file_uploader(
                                f"Imagens (até {MAX_IMGS_PER_POST} × {MAX_IMG_MB}MB)",
                                type=["png","jpg","jpeg","webp"],
                                accept_multiple_files=True,
                                key=f"nt_imgs_{cat}_{st.session_state[nonce_key]}",
                            )
                            col_btns = st.columns([0.2,0.2,0.6])
                            with col_btns[0]:
                                post_clicked = st.button("Postar", key=f"nt_post_{cat}")
                            with col_btns[1]:
                                cancel_clicked = st.button("Cancelar", key=f"nt_cancel_{cat}")

                            if cancel_clicked:
                                # limpa e fecha
                                st.session_state[f"nt_title_{cat}"] = ""
                                st.session_state[f"nt_body_{cat}"] = ""
                                st.session_state[nonce_key] += 1
                                st.session_state[exp_key] = False
                                st.toast("Criação cancelada.")
                                st.experimental_rerun()

                            if post_clicked:
                                if not (st.session_state.get(f"nt_title_{cat}", "")).strip():
                                    st.error("Informe um título.")
                                else:
                                    forum_create_post(cat, st.session_state.get(f"nt_title_{cat}", ""), st.session_state.get(f"nt_body_{cat}", ""), nt_imgs, u)
                                    # limpa agressivo + fecha
                                    st.session_state[f"nt_title_{cat}"] = ""
                                    st.session_state[f"nt_body_{cat}"] = ""
                                    st.session_state[nonce_key] += 1
                                    st.session_state[exp_key] = False
                                    st.toast("Postagem enviada!")
                                    st.experimental_rerun()
                else:
                    if not u:
                        st.caption("_Entre para criar um novo tópico._")
                    elif cat == "Atualizações" and u and u['is_admin'] != 1:
                        st.caption("_Apenas admin pode publicar em Atualizações._")

                # Lista de posts
                posts = forum_list_posts(cat)
                if not posts:
                    st.info("Nenhum tópico ainda.")
                else:
                    for (pid, title, author_name, author_faction, cts, images_json, author_id) in posts:
                        cnt = forum_count_comments(pid)
                        user_row = get_db().execute("SELECT avatar_ext FROM users WHERE id=?", (int(author_id),)).fetchone()
                        av_ext = user_row[0] if user_row else None
                        av_bytes = user_avatar_bytes(author_id, av_ext)
                        with st.container(border=True):
                            head_cols = st.columns([0.1,0.55,0.35])
                            with head_cols[0]:
                                if av_bytes:
                                    st.image(av_bytes, width=48)
                            with head_cols[1]:
                                dt = datetime.fromtimestamp(cts).strftime("%Y-%m-%d %H:%M")
                                st.markdown(f"**{title}**  <span class='mf-badge'>{cnt} comentários</span><br><small>por {author_name} · {author_faction} · {dt}</small>", unsafe_allow_html=True)
                            with head_cols[2]:
                                # ações (editar/apagar) lado a lado, largura total
                                ac1, ac2 = st.columns(2)
                                with ac1:
                                    if u and (u.get("is_admin",0)==1 or int(u["id"])==int(author_id)):
                                        if st.button("Editar tópico", key=f"edit_post_{pid}"):
                                            st.session_state[f"edit_post_open_{pid}"] = True
                                with ac2:
                                    if u and u.get("is_admin",0)==1:
                                        if st.button("Apagar tópico", key=f"del_post_{pid}"):
                                            get_db().execute("DELETE FROM forum_posts WHERE id=?", (int(pid),))
                                            get_db().execute("DELETE FROM forum_comments WHERE post_id=?", (int(pid),))
                                            get_db().commit()
                                            st.success("Tópico removido.")
                                            st.experimental_rerun()

                            # modo edição de tópico
                            if st.session_state.get(f"edit_post_open_{pid}", False):
                                post = forum_get_post(pid)
                                _id, _cat, _title, _body_md, _aid, _aname, _afac, _cts, _imgs = post
                                st.markdown("**Editar tópico**")
                                edt_title = st.text_input("Título", value=_title, key=f"edt_title_{pid}")
                                edt_body  = st.text_area("Conteúdo (Markdown)", value=_body_md or "", key=f"edt_body_{pid}", height=140)
                                ec1, ec2, ec3 = st.columns([0.25,0.25,0.5])
                                with ec1:
                                    if st.button("Salvar", key=f"save_edit_{pid}"):
                                        forum_update_post(pid, edt_title, edt_body)
                                        st.session_state[f"edit_post_open_{pid}"] = False
                                        st.toast("Tópico atualizado!")
                                        st.experimental_rerun()
                                with ec2:
                                    if st.button("Cancelar", key=f"cancel_edit_{pid}"):
                                        st.session_state[f"edit_post_open_{pid}"] = False
                                        st.experimental_rerun()
                            else:
                                # conteúdo
                                post = forum_get_post(pid)
                                if post:
                                    _id, _cat, _title, _body_md, _aid, _aname, _afac, _cts, _imgs = post
                                    if _body_md:
                                        st.markdown(_body_md)
                                    try:
                                        imgs = json.loads(_imgs or "[]")
                                    except:
                                        imgs = []
                                    if imgs:
                                        st.caption("Imagens:")
                                        ig_cols = st.columns(min(3,len(imgs)))
                                        root = os.path.join("data","posts",str(pid))
                                        for i, name in enumerate(imgs):
                                            p = os.path.join(root, name)
                                            if os.path.exists(p):
                                                with ig_cols[i % len(ig_cols)]:
                                                    st.image(open(p,"rb").read())

                            # ===== Comentários =====
                            if COMMENTS_ENABLED:
                                st.markdown("**Comentários:**")
                                comms = forum_list_comments(pid)
                                if not comms:
                                    st.caption("Seja o primeiro a comentar.")
                                else:
                                    for (cid, caid, caname, cafac, cbody, ctime, cdel) in comms:
                                        if cdel:
                                            st.caption("_comentário removido_")
                                            continue
                                        row_cols = st.columns([0.1,0.9])
                                        with row_cols[0]:
                                            cav_row = get_db().execute("SELECT avatar_ext FROM users WHERE id=?", (int(caid),)).fetchone()
                                            cav_ext = cav_row[0] if cav_row else None
                                            cav_bytes = user_avatar_bytes(caid, cav_ext)
                                            if cav_bytes:
                                                st.image(cav_bytes, width=40)
                                        with row_cols[1]:
                                            line = f"**{caname}** · {cafac} · {datetime.fromtimestamp(ctime).strftime('%Y-%m-%d %H:%M')}"
                                            st.markdown(line)
                                            # modo edição de comentário?
                                            if st.session_state.get(f"edit_comment_{cid}", False):
                                                new_body = st.text_area("Editar comentário",
                                                                        value=cbody or "",
                                                                        key=f"edit_body_{cid}",
                                                                        height=100)
                                                cc1, cc2, cc3 = st.columns([0.22,0.22,0.56])
                                                with cc1:
                                                    if st.button("Salvar", key=f"save_comment_{cid}"):
                                                        forum_update_comment(cid, new_body)
                                                        st.session_state[f"edit_comment_{cid}"] = False
                                                        st.toast("Comentário atualizado!")
                                                        st.experimental_rerun()
                                                with cc2:
                                                    if st.button("Cancelar", key=f"cancel_comment_{cid}"):
                                                        st.session_state[f"edit_comment_{cid}"] = False
                                                        st.experimental_rerun()
                                            else:
                                                if cbody:
                                                    st.markdown(cbody)
                                                # ações do comentário
                                                ac = st.columns(3)
                                                if u and (u.get("is_admin",0)==1 or int(u["id"])==int(caid)):
                                                    with ac[0]:
                                                        if st.button("Editar", key=f"btn_editc_{cid}"):
                                                            st.session_state[f"edit_comment_{cid}"] = True
                                                    with ac[1]:
                                                        if st.button("🗑️ Apagar", key=f"delc_{cid}"):
                                                            forum_delete_comment(cid)
                                                            st.success("Comentário apagado.")
                                                            st.experimental_rerun()

                                # caixa de comentário com NONCE para limpar após envio
                                if u:
                                    nonce_key_c = f"comment_nonce_{pid}"
                                    if nonce_key_c not in st.session_state:
                                        st.session_state[nonce_key_c] = 0
                                    nc = st.text_area(
                                        "Escreva um comentário",
                                        key=f"nc_{pid}_{st.session_state[nonce_key_c]}",
                                        height=100
                                    )
                                    if st.button("Comentar", key=f"btn_nc_{pid}"):
                                        if not (nc or "").strip():
                                            st.error("O comentário está vazio.")
                                        else:
                                            forum_add_comment(pid, u, nc)
                                            st.session_state[nonce_key_c] += 1
                                            st.toast("Comentário enviado!")
                                            st.experimental_rerun()
                                else:
                                    st.caption("_Entre para comentar._")
                            else:
                                st.caption("_Comentários desabilitados._")

# ---------- Rodapé ----------
st.markdown("---")
left, right = st.columns(2)

PIX_PHONE_DISPLAY = "+55 79 99834-5186"
WHATS_NUMBER_DIGITS = "5579998345186"
WHATS_URL = f"https://wa.me/{WHATS_NUMBER_DIGITS}"
TELEGRAM_USER = st.secrets.get("TELEGRAM_USER", "@HiperionBR")
TELEGRAM_URL = f"https://t.me/{TELEGRAM_USER.lstrip('@')}"

with left:
    st.subheader("💙 Apoie este projeto")
    pix_qr_url = st.secrets.get("PIX_QR_URL", "")
    if pix_qr_url:
        st.image(pix_qr_url, caption="Use o QR Code para doar via PIX", width=220)
    st.markdown(f"Ou copie a chave PIX (celular): **{PIX_PHONE_DISPLAY}**")
    st.markdown(f"[📲 Entrar em contato no WhatsApp]({WHATS_URL})", unsafe_allow_html=True)
    st.markdown(f"[✈️ Falar no Telegram]({TELEGRAM_URL})", unsafe_allow_html=True)

with right:
    st.subheader("📰 Informes")
    news_md = st.secrets.get("NEWS_MD", "").strip()
    if news_md:
        st.markdown(news_md)
    else:
        st.markdown(
            '''
- Bem-vindo ao **Maxfield Online**!  
- Você pode enviar portais via **arquivo**, **colar texto** ou pelo **plugin do IITC**.  
- Feedbacks e ideias são muito bem-vindos.
  
> Dica: para editar este bloco sem atualizar o código, adicione `NEWS_MD = """Seu markdown aqui"""` em `.streamlit/secrets.toml`.
            '''
        )
