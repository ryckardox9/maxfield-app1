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
import hashlib
from datetime import datetime, timedelta
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

# ===== Fundo + cartão responsivo (claro/escuro automático) =====
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

    /* Chips de cor de facção (preview) */
    .mf-chip {{
      display:inline-block;padding:4px 10px;border-radius:999px;font-size:12px;margin-right:8px;
      color:#fff; box-shadow:0 2px 6px rgba(0,0,0,.2)
    }}
    .mf-chip.enl {{ background:#25c025; }}
    .mf-chip.res {{ background:#2b6dff; }}

    /* Avatar redondo */
    .mf-avatar {{
      width: 28px; height: 28px; border-radius: 50%; object-fit: cover; vertical-align: middle; margin-right: 6px;
      border: 1px solid rgba(0,0,0,.15);
    }}
    </style>
    """,
    unsafe_allow_html=True
)

# ---------- Persistência simples (SQLite) + MIGRAÇÃO ----------
@st.cache_resource(show_spinner=False)
def get_db():
    os.makedirs("data", exist_ok=True)
    db_path = os.path.join("data", "app.db")
    conn = sqlite3.connect(db_path, check_same_thread=False)

    # --- helpers de migração ---
    def has_column(table: str, col: str) -> bool:
        try:
            cur = conn.execute(f"PRAGMA table_info({table})")
            return any(r[1] == col for r in cur.fetchall())
        except Exception:
            return False
    def add_col_if_missing(table: str, col: str, decl: str):
        if not has_column(table, col):
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass

    # métricas
    conn.execute("""
        CREATE TABLE IF NOT EXISTS metrics (
            key   TEXT PRIMARY KEY,
            value INTEGER NOT NULL
        )
    """)
    for k in ("visits", "plans_completed"):
        conn.execute("INSERT OR IGNORE INTO metrics(key, value) VALUES (?, 0)", (k,))

    # runs
    conn.execute("""
        CREATE TABLE IF NOT EXISTS runs(
            ts INTEGER, n_portais INTEGER, num_cpus INTEGER, gif INTEGER, dur_s REAL
        )
    """)

    # jobs
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

    # housekeeping
    conn.execute("""
        CREATE TABLE IF NOT EXISTS housekeeping(
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    # ===== usuários / sessões / fórum =====
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users(
            id TEXT PRIMARY KEY,
            username TEXT UNIQUE,
            username_lc TEXT UNIQUE,
            pass_hash TEXT,
            faction TEXT,
            email TEXT,
            is_admin INTEGER DEFAULT 0,
            avatar_path TEXT,
            created_ts INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions(
            token TEXT PRIMARY KEY,
            user_id TEXT,
            created_ts INTEGER,
            last_seen_ts INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS forum_posts(
            id TEXT PRIMARY KEY,
            ts INTEGER,
            author_id TEXT,
            author_name TEXT,
            category TEXT,
            title TEXT,
            body TEXT,
            images_json TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS forum_comments(
            id TEXT PRIMARY KEY,
            ts INTEGER,
            post_id TEXT,
            author_id TEXT,
            author_name TEXT,
            body TEXT
        )
    """)

    # ===== MIGRAÇÕES: adiciona colunas ausentes de versões antigas =====
    add_col_if_missing("forum_posts", "author_id",   "TEXT")
    add_col_if_missing("forum_posts", "author_name", "TEXT")
    add_col_if_missing("forum_posts", "category",    "TEXT")
    add_col_if_missing("forum_posts", "images_json", "TEXT")
    add_col_if_missing("forum_comments", "author_id",   "TEXT")
    add_col_if_missing("forum_comments", "author_name", "TEXT")

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

# histórico de durações para melhorar ETA
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
                lat = float(lat_s)
                lon = float(lon_s)
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

# ---------- Identificador anônimo (UID) p/ histórico de planos ----------
if "uid" not in st.session_state:
    cur_uid = qp_get("uid", "")
    if not cur_uid:
        cur_uid = uuid.uuid4().hex[:8]
        qp_set(uid=cur_uid)
    st.session_state["uid"] = cur_uid
UID = st.session_state["uid"]

# ---------- Parâmetros públicos do userscript ----------
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

# ---------- Userscript IITC (polido: contador + copiar txt) ----------
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

  // ===== Config =====
  self.MIN_ZOOM    = 15;
  self.MAX_PORTALS = 200;
  self.MAX_URL_LEN = 6000;
  self.DEST        = '__DEST__';
  // ==================

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
    const zoom = map && map.getZoom ? map.getZoom() : 0;
    if (zoom < self.MIN_ZOOM) {
      alert('Zoom insuficiente (mínimo ' + self.MIN_ZOOM + ').\\n\\nDica: aproxime com o botão + até enquadrar apenas a área desejada, e tente novamente.');
      return;
    }

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
    if (!lines.length) {
      alert('Nenhum portal visível para copiar.');
      return;
    }
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
    const mk = (label, cb) => {
      const btn = document.createElement('a');
      btn.textContent = label;
      btn.style.cssText = 'padding:6px 10px;background:#2b8;color:#fff;border-radius:4px;font:12px/1.3 sans-serif;cursor:pointer;box-shadow:0 2px 6px rgba(0,0,0,.25)';
      btn.addEventListener('click', function(e){ e.preventDefault(); cb(); });
      return btn;
    };
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

// injeta no contexto da página (padrão IITC)
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
- Dica: use **🔖 Salvar rascunho na URL** para preservar sua lista **antes** de gerar (seguro dar F5).
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

# ---------- Seção de Rascunho ----------
st.markdown("### 📝 Rascunho")
c1, c2 = st.columns(2)
if c1.button("🔖 Salvar rascunho na URL", key="btn_save_rascunho"):
    qp_set(list=st.session_state.get("txt_content", "") or "")
    try: st.toast("Rascunho salvo em ?list= (pode dar F5 com segurança).")
    except Exception: pass
if c2.button("🧹 Limpar rascunho da URL", key="btn_clear_rascunho"):
    qp_set(list=None)
    try: st.toast("Rascunho removido da URL.")
    except Exception: pass

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

    log_buffer = io.StringIO()
    try:
        with redirect_stdout(log_buffer):
            print(f"[INFO] os.cpu_count()={os.cpu_count()} · num_cpus={num_cpus} · gif={fazer_gif} · csv={output_csv}")
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
    except Exception as e:
        log_buffer.write(f"\n[ERRO] {e}\n")
        raise
    finally:
        log_txt = log_buffer.getvalue()

    # Salva log
    log_path = os.path.join(outdir, "maxfield_log.txt")
    with open(log_path, "w", encoding="utf-8", errors="ignore") as lf:
        lf.write(log_txt or "")

    # Compacta tudo do outdir
    zip_path = os.path.join(outdir, f"maxfield_output_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(outdir):
            for fn in files:
                if fn.endswith(".zip"): continue
                fp = os.path.join(root, fn)
                arc = os.path.relpath(fp, outdir)
                z.write(fp, arcname=arc)
    zip_bytes = open(zip_path, "rb").read()

    # lê artefatos
    def read_bytes(path):
        return open(path, "rb").read() if os.path.exists(path) else None

    pm_bytes = read_bytes(os.path.join(outdir, "portal_map.png"))
    lm_bytes = read_bytes(os.path.join(outdir, "link_map.png"))
    gif_bytes = read_bytes(os.path.join(outdir, "plan_movie.gif"))

    # plano resumido
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
tab_gen, tab_hist, tab_metrics, tab_forum = st.tabs(["🧩 Gerar plano", "🕑 Histórico", "📊 Métricas", "💬 Fórum"])

# =======================
# TAB: Gerar plano
# =======================
with tab_gen:
    st.markdown('<span class="mf-chip enl">Enlightened</span><span class="mf-chip res">Resistance</span>', unsafe_allow_html=True)

    fast_mode = st.toggle("⚡ Modo rápido (desliga GIF e CSV para máxima velocidade)", value=False, key="fast_mode_toggle")

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

        # Pré-visualização (pydeck)
        with st.expander("🗺️ Pré-visualização dos portais (opcional)"):
            txt_preview = txt_content or (uploaded.getvalue().decode("utf-8", errors="ignore") if uploaded else "")
            pts = extract_points(clean_invisibles(txt_preview))
            st.write(f"Detectados **{len(pts)}** portais para prévia.")
            if pts:
                import pandas as pd, pydeck as pdk
                df = pd.DataFrame(pts)
                mid_lat = df["lat"].mean()
                mid_lon = df["lon"].mean()
                layer = pdk.Layer(
                    "ScatterplotLayer",
                    data=df,
                    get_position='[lon, lat]',
                    get_radius=12,
                    pickable=True
                )
                st.pydeck_chart(pdk.Deck(map_style=None,
                                         initial_view_state=pdk.ViewState(latitude=mid_lat, longitude=mid_lon, zoom=14),
                                         layers=[layer]))
            else:
                st.caption("Cole/importe uma lista com URLs contendo `pll=lat,lon` para ver a prévia.")

        col1, col2 = st.columns(2)
        with col1:
            num_agents = st.number_input("Número de agentes", min_value=1, max_value=50, value=1, step=1)
        with col2:
            num_cpus = st.number_input("CPUs a usar (0 = máximo)", min_value=0, max_value=128, value=0, step=1)

        team = st.selectbox("Facção (cores)", ["Enlightened (verde)", "Resistance (azul)"], key="team_select")

        output_csv_default = False if fast_mode else True
        gif_default = False
        output_csv = st.checkbox("Gerar CSV", value=output_csv_default, disabled=fast_mode, key="chk_csv")
        st.caption("Dica: no celular o CSV é ruim de editar. No Modo Rápido ele fica desativado por padrão.")
        gerar_gif_checkbox = st.checkbox("Gerar GIF (passo-a-passo)", value=gif_default, disabled=fast_mode, key="chk_gif")

        st.markdown("**Mapa de fundo (opcional):**")
        google_key_input = st.text_input(
            "Google Maps API key (opcional)",
            value="",
            help="Se deixar vazio e houver uma chave salva no servidor, ela será usada automaticamente.",
            key="txt_google_key"
        )
        google_api_secret = st.text_input("Google Maps API secret (opcional)", value="", type="password", key="txt_google_secret")

        submitted = st.form_submit_button("Gerar plano")

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
        portal_bytes = texto_portais.encode("utf-8")

        res_colors = team.startswith("Resistance")
        n_portais = contar_portais(texto_portais)

        fazer_gif = (not fast_mode) and bool(gerar_gif_checkbox)
        if n_portais > 25 and fazer_gif:
            st.warning(f"Detectei **{n_portais} portais**. Para evitar travamentos, o GIF foi **desativado automaticamente**.")
            fazer_gif = False

        output_csv = (not fast_mode) and bool(output_csv)

        google_api_key = (google_key_input or "").strip() or st.secrets.get("GOOGLE_API_KEY", None)
        google_api_secret = (google_api_secret or "").strip() or st.secrets.get("GOOGLE_API_SECRET", None)

        kwargs = dict(
            portal_bytes=portal_bytes,
            num_agents=int(num_agents),
            num_cpus=int(num_cpus),
            res_colors=res_colors,
            google_api_key=google_api_key,
            google_api_secret=google_api_secret,
            output_csv=output_csv,
            fazer_gif=fazer_gif,
            team=team
        )

        eta_s = estimate_eta_s(n_portais, int(num_cpus), fazer_gif)
        meta = {"n_portais": n_portais, "num_cpus": int(num_cpus), "gif": fazer_gif, "team": team}

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
            with st.status(f"⏳ Processando… (job {job_id})", expanded=True) as status:
                bar = st.progress(0)
                eta_ph = st.empty()
                while not fut.done():
                    elapsed = time.time() - t0
                    pct = min(0.90, elapsed / max(1e-6, eta_s))
                    bar.progress(int(pct * 100))
                    eta_left = max(0, eta_s - elapsed)
                    eta_ph.write(f"**Estimativa:** ~{int(eta_left)}s restantes · **Decorridos:** {int(elapsed)}s")
                    time.sleep(0.3)
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
            mime="image/gif"
        )

    st.download_button(
        "Baixar todos os arquivos (.zip)",
        data=res["zip_bytes"],
        file_name=f"maxfield_output_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip",
        mime="application/zip",
    )

    outdir = res.get("outdir")
    if outdir and os.path.isdir(outdir):
        md_path = os.path.join(outdir, "summary.md")
        html_path = os.path.join(outdir, "summary.html")
        if os.path.exists(md_path):
            st.download_button("📝 Baixar plano resumido (.md)", data=open(md_path,"rb").read(),
                               file_name="summary.md", mime="text/markdown")
        if os.path.exists(html_path):
            st.download_button("🖨️ Baixar para impressão (.html)", data=open(html_path,"rb").read(),
                               file_name="summary.html", mime="text/html")

    with st.expander("Ver logs do processamento"):
        log_txt_full = res.get("log_txt") or "(sem logs)"
        if len(log_txt_full) > 20000:
            st.caption("Log truncado (últimos ~20k caracteres).")
            log_txt = log_txt_full[-20000:]
        else:
            log_txt = log_txt_full
        st.code(log_txt, language="bash")

    if st.button("🧹 Limpar resultados", key="btn_clear_results"):
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
                                               file_name="portal_map.png", mime="image/png", key=f"dpm_{jid}")
                    if os.path.exists(lm):
                        with cols[1]:
                            st.download_button("Link Map", data=open(lm,"rb").read(),
                                               file_name="link_map.png", mime="image/png", key=f"dlm_{jid}")
                    if os.path.exists(gif_p):
                        with cols[2]:
                            st.download_button("GIF", data=open(gif_p,"rb").read(),
                                               file_name="plan_movie.gif", mime="image/gif", key=f"dgf_{jid}")
                    if zip_p and os.path.exists(zip_p):
                        with cols[3]:
                            st.download_button("ZIP", data=open(zip_p,"rb").read(),
                                               file_name=os.path.basename(zip_p), mime="application/zip", key=f"dzp_{jid}")
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

# =======================
# AUTENTICAÇÃO LEVE + FÓRUM
# =======================

AUTH_PEPPER = st.secrets.get("AUTH_PEPPER", "")
ADMIN_CODE_SECRET = st.secrets.get("ADMIN_CODE", "")

def hash_pass(username_lc: str, password: str) -> str:
    raw = (username_lc + ":" + password + ":" + AUTH_PEPPER).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()

def get_user_by_username_or_email(login_str: str):
    conn = get_db()
    login_lc = (login_str or "").strip().lower()
    cur = conn.execute("SELECT id, username, username_lc, pass_hash, faction, email, is_admin, avatar_path FROM users WHERE username_lc=? OR email=?", (login_lc, login_lc))
    return cur.fetchone()

def create_user(username: str, password: str, faction: str, email: str|None, is_admin: int, avatar_bytes: bytes|None, avatar_ext: str|None):
    conn = get_db()
    uid = uuid.uuid4().hex[:8]
    username_lc = username.strip().lower()
    ph = hash_pass(username_lc, password)
    ts = int(time.time())
    avatar_path = None
    if avatar_bytes and avatar_ext:
        av_dir = os.path.join("data", "avatars", uid)
        os.makedirs(av_dir, exist_ok=True)
        avatar_path = os.path.join(av_dir, f"avatar{avatar_ext}")
        with open(avatar_path, "wb") as f:
            f.write(avatar_bytes)
    conn.execute("""
        INSERT INTO users(id, username, username_lc, pass_hash, faction, email, is_admin, avatar_path, created_ts)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (uid, username, username_lc, ph, faction, email or None, int(is_admin), avatar_path, ts))
    conn.commit()
    return uid

def create_session(user_id: str):
    conn = get_db()
    token = uuid.uuid4().hex
    ts = int(time.time())
    conn.execute("INSERT OR REPLACE INTO sessions(token, user_id, created_ts, last_seen_ts) VALUES (?,?,?,?)",
                 (token, user_id, ts, ts))
    conn.commit()
    return token

def get_session(token: str):
    if not token: return None
    conn = get_db()
    cur = conn.execute("SELECT token, user_id, created_ts, last_seen_ts FROM sessions WHERE token=?", (token,))
    row = cur.fetchone()
    if row:
        conn.execute("UPDATE sessions SET last_seen_ts=? WHERE token=?", (int(time.time()), token))
        conn.commit()
    return row

def get_user(user_id: str):
    conn = get_db()
    cur = conn.execute("SELECT id, username, username_lc, pass_hash, faction, email, is_admin, avatar_path FROM users WHERE id=?", (user_id,))
    return cur.fetchone()

def require_login_ui():
    st.subheader("🔐 Entrar no Fórum")
    with st.form("login_form"):
        li_user = st.text_input("Usuário ou e-mail", key="li_user")
        li_pass = st.text_input("Senha", type="password", key="li_pass")
        li_submit = st.form_submit_button("Entrar")
    if li_submit:
        row = get_user_by_username_or_email(li_user)
        if not row:
            st.error("Usuário/e-mail não encontrado.")
        else:
            uid, username, username_lc, pass_hash, faction, email, is_admin, avatar_path = row
            if hash_pass(username_lc, li_pass) != pass_hash:
                st.error("Senha incorreta.")
            else:
                token = create_session(uid)
                qp_set(token=token)
                try: st.toast("Login realizado!")
                except Exception: pass
                st.experimental_rerun()

    st.divider()
    st.subheader("🆕 Criar conta")
    with st.form("signup_form"):
        su_user = st.text_input("Nome de usuário (único)", key="su_user")
        su_pass = st.text_input("Senha", type="password", key="su_pass")
        su_faction = st.selectbox("Facção", ["Enlightened", "Resistance"], key="su_faction")
        su_email = st.text_input("E-mail (opcional)", key="su_email")
        su_admin_code = st.text_input("Código de administrador (opcional)", type="password", key="su_admin_code")
        su_avatar = st.file_uploader("Avatar (opcional)", type=["png","jpg","jpeg","webp"], key="su_avatar")
        su_submit = st.form_submit_button("Criar conta")
    if su_submit:
        username = (su_user or "").strip()
        if not username:
            st.error("Informe um nome de usuário.")
        else:
            if get_user_by_username_or_email(username):
                st.error("Este nome de usuário já existe (ou e-mail em uso).")
            else:
                is_admin = 1 if (ADMIN_CODE_SECRET and su_admin_code.strip() == ADMIN_CODE_SECRET) else 0
                av_bytes, av_ext = None, None
                if su_avatar is not None:
                    av_bytes = su_avatar.read()
                    ext = os.path.splitext(su_avatar.name)[1].lower()
                    av_ext = ext if ext in (".png",".jpg",".jpeg",".webp") else ".png"
                uid = create_user(username, su_pass, su_faction, su_email.strip() or None, is_admin, av_bytes, av_ext)
                token = create_session(uid)
                qp_set(token=token)
                try: st.toast("Conta criada e login efetuado!")
                except Exception: pass
                st.experimental_rerun()

# ---------- Fórum: CRUD ----------
FORUM_CATS = [
    ("Atualizações", "admin_only"),
    ("Sugestões", "open"),
    ("Críticas", "open"),
    ("Dúvidas", "open"),
]

MAX_IMG_MB = int(st.secrets.get("MAX_IMG_MB", 2))
MAX_IMGS_PER_POST = int(st.secrets.get("MAX_IMGS_PER_POST", 3))

def forum_create_post(author_id: str, author_name: str, category: str, title: str, body: str, images) -> str:
    conn = get_db()
    post_id = uuid.uuid4().hex[:10]
    ts = int(time.time())
    imgs_meta = []
    if images:
        img_root = os.path.join("data", "forum_images", post_id)
        os.makedirs(img_root, exist_ok=True)
        for i, up in enumerate(images[:MAX_IMGS_PER_POST]):
            if up is None: continue
            up_bytes = up.read()
            if len(up_bytes) > MAX_IMG_MB*1024*1024:
                continue
            ext = os.path.splitext(up.name)[1].lower() or ".png"
            if ext not in (".png",".jpg",".jpeg",".webp",".gif"):
                ext = ".png"
            fn = f"img_{i}{ext}"
            fp = os.path.join(img_root, fn)
            with open(fp, "wb") as f:
                f.write(up_bytes)
            imgs_meta.append(os.path.join("data", "forum_images", post_id, fn))
    conn.execute("""
        INSERT INTO forum_posts(id, ts, author_id, author_name, category, title, body, images_json)
        VALUES(?,?,?,?,?,?,?,?)
    """, (post_id, ts, author_id, author_name, category, title, body, json.dumps(imgs_meta)))
    conn.commit()
    return post_id

def forum_add_comment(post_id: str, author_id: str, author_name: str, body: str) -> str:
    conn = get_db()
    cid = uuid.uuid4().hex[:10]
    ts = int(time.time())
    conn.execute("""
        INSERT INTO forum_comments(id, ts, post_id, author_id, author_name, body)
        VALUES(?,?,?,?,?,?)
    """, (cid, ts, post_id, author_id, author_name, body))
    conn.commit()
    return cid

def forum_delete_post(post_id: str):
    conn = get_db()
    conn.execute("DELETE FROM forum_comments WHERE post_id=?", (post_id,))
    conn.execute("DELETE FROM forum_posts WHERE id=?", (post_id,))
    conn.commit()
    img_root = os.path.join("data", "forum_images", post_id)
    if os.path.isdir(img_root):
        for base, _, files in os.walk(img_root, topdown=False):
            for fn in files:
                try: os.remove(os.path.join(base, fn))
                except: pass
            try: os.rmdir(base)
            except: pass

def forum_delete_comment(comment_id: str):
    conn = get_db()
    conn.execute("DELETE FROM forum_comments WHERE id=?", (comment_id,))
    conn.commit()

def forum_list_posts(category: str):
    conn = get_db()
    # inclui contagem de comentários
    cur = conn.execute("""
        SELECT p.id, p.ts, p.author_id, p.author_name, p.category, p.title, p.body, p.images_json,
               (SELECT COUNT(1) FROM forum_comments c WHERE c.post_id = p.id) as n_comments
        FROM forum_posts p
        WHERE p.category = ?
        ORDER BY p.ts DESC
    """, (category,))
    return cur.fetchall()

def forum_get_post(post_id: str):
    conn = get_db()
    cur = conn.execute("""
        SELECT id, ts, author_id, author_name, category, title, body, images_json
        FROM forum_posts WHERE id=?
    """, (post_id,))
    return cur.fetchone()

# ---------- Sessão / Login persistente via ?token= ----------
def current_user():
    token = qp_get("token", "")
    sess = get_session(token) if token else None
    if not sess:
        return None
    _, user_id, _, _ = sess
    return get_user(user_id)

# ---------- TAB Fórum ----------
with tab_forum:
    st.subheader("💬 Fórum (debate e melhorias)")

    # Auto refresh
    if "forum_autorefresh" not in st.session_state:
        st.session_state["forum_autorefresh"] = False
    st.session_state["forum_autorefresh"] = st.toggle("🔄 Auto-atualizar esta aba a cada 20s", value=st.session_state["forum_autorefresh"], key="forum_autorefresh_toggle")
    if st.session_state["forum_autorefresh"]:
        st.markdown("""
        <script>
        setTimeout(function(){
            if (document.visibilityState === 'visible') {
                window.location.reload();
            }
        }, 20000);
        </script>
        """, unsafe_allow_html=True)

    user = current_user()
    if not user:
        require_login_ui()
    else:
        uid, username, username_lc, pass_hash, faction, email, is_admin, avatar_path = user
        col_a, col_b = st.columns([0.7,0.3])
        with col_a:
            st.markdown(f"**Logado como:** {username} ({faction}) {'· 🛡️ Admin' if is_admin else ''}")
        with col_b:
            if st.button("Sair", key="btn_logout_forum"):
                qp_set(token=None)
                try: st.toast("Sessão encerrada.")
                except Exception: pass
                st.experimental_rerun()

        # Perfil rápido (avatar)
        with st.expander("👤 Perfil"):
            av_col1, av_col2 = st.columns([0.5,0.5])
            with av_col1:
                st.write("Avatar atual:")
                if avatar_path and os.path.exists(avatar_path):
                    st.image(avatar_path, width=96)
                else:
                    st.caption("Sem avatar.")
            with av_col2:
                upa = st.file_uploader("Trocar avatar", type=["png","jpg","jpeg","webp"], key="profile_avatar_up")
                if st.button("Salvar avatar", key="btn_save_avatar"):
                    if upa is not None:
                        av_dir = os.path.join("data", "avatars", uid)
                        os.makedirs(av_dir, exist_ok=True)
                        ext = os.path.splitext(upa.name)[1].lower() or ".png"
                        if ext not in (".png",".jpg",".jpeg",".webp"): ext = ".png"
                        new_path = os.path.join(av_dir, f"avatar{ext}")
                        with open(new_path, "wb") as f:
                            f.write(upa.read())
                        conn = get_db()
                        conn.execute("UPDATE users SET avatar_path=? WHERE id=?", (new_path, uid))
                        conn.commit()
                        try: st.toast("Avatar atualizado.")
                        except Exception: pass
                        st.experimental_rerun()
                    else:
                        st.warning("Selecione um arquivo de imagem.")

        st.divider()

        # Abas do fórum por categoria
        cat_tabs = st.tabs([c[0] for c in FORUM_CATS])

        for idx, (cat, mode) in enumerate(FORUM_CATS):
            with cat_tabs[idx]:
                can_post = (mode == "open") or (is_admin == 1)
                st.write(f"**Categoria:** {cat} " + ("(somente admin pode iniciar tópicos)" if mode=="admin_only" else ""))

                # Criar novo tópico
                if can_post:
                    with st.expander("➕ Novo tópico"):
                        nt_title = st.text_input("Título do tópico", key=f"nt_title_{cat}")
                        nt_body = st.text_area("Conteúdo (markdown simples ou texto)", key=f"nt_body_{cat}")
                        nt_imgs = st.file_uploader("Imagens (opcional)", type=["png","jpg","jpeg","webp","gif"], accept_multiple_files=True, key=f"nt_imgs_{cat}")
                        if st.button("Postar", key=f"btn_post_{cat}"):
                            if not nt_title.strip():
                                st.error("Informe um título.")
                            else:
                                pid = forum_create_post(
                                    author_id=uid,
                                    author_name=username,
                                    category=cat,
                                    title=nt_title.strip(),
                                    body=nt_body.strip(),
                                    images=nt_imgs or []
                                )
                                try: st.toast("Tópico publicado!")
                                except Exception: pass
                                st.experimental_rerun()
                else:
                    st.info("Apenas administradores podem iniciar tópicos aqui.")

                # Lista de tópicos (com contagem de comentários)
                posts = forum_list_posts(cat)
                if not posts:
                    st.caption("Nenhum tópico ainda.")
                else:
                    for (pid, pts, pauthor_id, pauthor_name, pcat, ptitle, pbody, pimgs_json, n_comments) in posts:
                        dt = datetime.fromtimestamp(pts).strftime("%Y-%m-%d %H:%M")
                        with st.container(border=True):
                            # Cabeçalho com avatar + título + contagem
                            colH1, colH2 = st.columns([0.80, 0.20])
                            with colH1:
                                # Avatar do autor
                                conn = get_db()
                                avatar_src = None
                                if pauthor_id:
                                    cur = conn.execute("SELECT avatar_path FROM users WHERE id=?", (pauthor_id,))
                                    r = cur.fetchone()
                                    if r and r[0] and os.path.exists(r[0]):
                                        avatar_src = r[0]
                                av_html = f"<img src='{avatar_src}' class='mf-avatar'/>" if avatar_src else ""
                                st.markdown(f"{av_html} **{ptitle}**  — _por {pauthor_name}_  • _{dt}_  • 💬 {n_comments}", unsafe_allow_html=True)
                                if pbody:
                                    st.markdown(pbody)
                                if pimgs_json:
                                    try:
                                        imgs = json.loads(pimgs_json)
                                        if isinstance(imgs, list):
                                            for im in imgs:
                                                if os.path.exists(im):
                                                    st.image(im)
                                    except Exception:
                                        pass
                            with colH2:
                                # ações do post
                                if is_admin or (uid == pauthor_id):
                                    if st.button("🗑️ Apagar tópico", key=f"del_post_{pid}"):
                                        forum_delete_post(pid)
                                        try: st.toast("Tópico apagado.")
                                        except Exception: pass
                                        st.experimental_rerun()

                            # Comentários do tópico
                            conn = get_db()
                            curc = conn.execute("""
                                SELECT id, ts, post_id, author_id, author_name, body
                                FROM forum_comments WHERE post_id=?
                                ORDER BY ts ASC
                            """, (pid,))
                            comments = curc.fetchall()
                            if comments:
                                st.markdown("**Comentários:**")
                                for (cid, cts, cpid, cauthor_id, cauthor_name, cbody) in comments:
                                    cdt = datetime.fromtimestamp(cts).strftime("%Y-%m-%d %H:%M")
                                    with st.container(border=True):
                                        # Avatar do comentarista
                                        avatar_c = None
                                        if cauthor_id:
                                            cur = conn.execute("SELECT avatar_path FROM users WHERE id=?", (cauthor_id,))
                                            rr = cur.fetchone()
                                            if rr and rr[0] and os.path.exists(rr[0]):
                                                avatar_c = rr[0]
                                        avc_html = f"<img src='{avatar_c}' class='mf-avatar'/>" if avatar_c else ""
                                        st.markdown(f"{avc_html} **{cauthor_name}** — _{cdt}_", unsafe_allow_html=True)
                                        st.write(cbody)
                                        if is_admin or (uid == cauthor_id):
                                            if st.button("Apagar comentário", key=f"del_c_{cid}"):
                                                forum_delete_comment(cid)
                                                try: st.toast("Comentário apagado.")
                                                except Exception: pass
                                                st.experimental_rerun()
                            else:
                                st.caption("Sem comentários ainda.")

                            # Adicionar comentário
                            with st.form(f"comment_form_{pid}"):
                                cm_body = st.text_area("Escreva um comentário", key=f"cm_body_{pid}")
                                cm_btn = st.form_submit_button("Comentar")
                            if cm_btn:
                                if not cm_body.strip():
                                    st.warning("Digite algo para comentar.")
                                else:
                                    forum_add_comment(pid, uid, username, cm_body.strip())
                                    try: st.toast("Comentário enviado!")
                                    except Exception: pass
                                    st.experimental_rerun()

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
