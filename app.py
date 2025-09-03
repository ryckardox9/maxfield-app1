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

# ===== Fundo + cartão responsivo (claro/escuro automático) =====
bg_url = st.secrets.get("BG_URL", "").strip()
st.markdown(
    f"""
    <style>
    .stApp {{
      {"background: url('" + bg_url + "') no-repeat center center fixed; background-size: cover;" if bg_url else ""}
    }}

    /* Cartão central adapta a preferência do SO (sem botão) */
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS metrics (
            key   TEXT PRIMARY KEY,
            value INTEGER NOT NULL
        )
    """)
    for k in ("visits", "plans_completed"):
        conn.execute("INSERT OR IGNORE INTO metrics(key, value) VALUES (?, 0)", (k,))
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS runs(
            ts INTEGER, n_portais INTEGER, num_cpus INTEGER, gif INTEGER, dur_s REAL
        )
    """)
    conn.execute("INSERT INTO runs(ts,n_portais,num_cpus,gif,dur_s) VALUES (?,?,?,?,?)",
                 (int(time.time()), n_portais, num_cpus, 1 if gif else 0, float(dur_s)))
    conn.commit()

def estimate_eta_s(n_portais:int, num_cpus:int, gif:bool) -> float:
    # “chute” inicial
    base_pp = 0.35 if not gif else 0.55  # s por portal
    base_overhead = 3.0 if not gif else 8.0
    cpu_factor = 1.0 / max(1.0, (0.6 + 0.5*min(num_cpus, 8)**0.5))
    est = (base_overhead + base_pp*n_portais) * cpu_factor

    # refina com histórico recente
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

@st.cache_data(show_spinner=False)
def processar_plano(portal_bytes: bytes,
                    num_agents: int,
                    num_cpus: int,
                    res_colors: bool,
                    google_api_key: str | None,
                    google_api_secret: str | None,
                    output_csv: bool,
                    fazer_gif: bool):
    workdir = tempfile.mkdtemp(prefix="maxfield_")
    outdir = os.path.join(workdir, "output")
    os.makedirs(outdir, exist_ok=True)

    portal_path = os.path.join(workdir, "portais.txt")
    with open(portal_path, "wb") as f:
        f.write(portal_bytes)

    log_buffer = io.StringIO()
    try:
        with redirect_stdout(log_buffer):
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

    def read_bytes(path):
        return open(path, "rb").read() if os.path.exists(path) else None

    pm_bytes = read_bytes(os.path.join(outdir, "portal_map.png"))
    lm_bytes = read_bytes(os.path.join(outdir, "link_map.png"))
    gif_bytes = read_bytes(os.path.join(outdir, "plan_movie.gif"))

    zip_path = os.path.join(workdir, f"maxfield_output_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(outdir):
            for fn in files:
                fp = os.path.join(root, fn)
                arc = os.path.relpath(fp, outdir)
                z.write(fp, arcname=arc)
    zip_bytes = open(zip_path, "rb").read()

    return {"zip_bytes": zip_bytes, "pm_bytes": pm_bytes, "lm_bytes": lm_bytes, "gif_bytes": gif_bytes, "log_txt": log_txt}

# ---------- Exemplo de entrada (.txt) ----------
EXEMPLO_TXT = """# Exemplo de arquivo de portais (uma linha por portal)
# Formato: Nome do Portal; URL do Intel (com pll=LAT,LON)
Portal 1; https://intel.ingress.com/intel?pll=-10.912345,-37.065432
Portal 2; https://intel.ingress.com/intel?pll=-10.913210,-37.061234
Portal 3; https://intel.ingress.com/intel?pll=-10.910987,-37.060001
"""

# ---------- Userscript IITC (GET ?list=...) ----------
DEST = "https://maxfield.fun/"  # domínio público do seu app
IITC_USERSCRIPT = f"""// ==UserScript==
// @name         Send portals to Maxfield (viewport-only + fallback)
// @namespace    {DEST}
// @version      0.3
// @description  Envia apenas os portais visíveis no mapa do IITC para maxfield.fun; tem limite, zoom mínimo e fallback p/ clipboard.
// @match        https://intel.ingress.com/*
// @grant        none
// ==/UserScript==

(function() {{
  'use strict';
  const MIN_ZOOM = 15;
  const MAX_PORTALS = 200;
  const MAX_URL_LEN = 6000;
  const DEST = "{DEST}";

  function visiblePortals() {{
    const bounds = window.map.getBounds();
    const out = [];
    for (const id in window.portals) {{
      const p = window.portals[id];
      if (!p || !p.getLatLng) continue;
      const ll = p.getLatLng();
      if (!bounds.contains(ll)) continue;

      const lat = ll.lat.toFixed(6);
      const lng = ll.lng.toFixed(6);
      const name = (p.options?.data?.title || 'Portal');
      out.push(`${{name}}; https://intel.ingress.com/intel?pll=${{lat}},${{lng}}`);
    }}
    return out;
  }}

  async function sendToMaxfield() {{
    const zoom = window.map.getZoom();
    if (zoom < MIN_ZOOM) {{
      alert(`Aproxime mais o mapa (zoom mínimo ${{MIN_ZOOM}}).\\nZoom atual: ${{zoom}}`);
      return;
    }}

    let lines = visiblePortals();
    if (!lines.length) {{
      alert("Nenhum portal visível nesta área.");
      return;
    }}
    if (lines.length > MAX_PORTALS) {{
      alert(`Foram encontrados ${{lines.length}} portais visíveis.\\nLimitando para ${{MAX_PORTALS}}.`);
      lines = lines.slice(0, MAX_PORTALS);
    }}

    const text = lines.join('\\n');
    const qs = "?list=" + encodeURIComponent(text);
    const full = DEST + qs;

    if (full.length > MAX_URL_LEN) {{
      try {{
        await navigator.clipboard.writeText(text);
        alert(`URL muito grande. A lista foi copiada. Abra o Maxfield e cole (Ctrl+V).`);
      }} catch (e) {{
        alert("URL muito grande e não consegui copiar automaticamente. Copie manualmente.");
        console.error(e);
      }}
      window.open(DEST, "_blank");
    }} else {{
      window.open(full, "_blank");
    }}
  }}

  function addButton() {{
    if (document.getElementById('btn-send-maxfield')) return;
    const btn = document.createElement('a');
    btn.id = 'btn-send-maxfield';
    btn.textContent = 'Send to Maxfield';
    btn.style.position = 'fixed';
    btn.style.right = '10px';
    btn.style.bottom = '10px';
    btn.style.zIndex = 9999;
    btn.style.padding = '6px 10px';
    btn.style.background = '#2b8';
    btn.style.color = '#fff';
    btn.style.borderRadius = '4px';
    btn.style.font = '12px/1.3 sans-serif';
    btn.style.cursor = 'pointer';
    btn.onclick = (e) => {{ e.preventDefault(); sendToMaxfield(); }};
    document.body.appendChild(btn);
  }}

  const ready = () => addButton();
  if (document.readyState === 'complete') ready();
  else window.addEventListener('load', ready);
}})();
"""

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

# ---------- Pré-preencher via ?list= ----------
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

# ---------- UI principal ----------
with st.form("plan_form"):
    uploaded = st.file_uploader("Arquivo de portais (.txt)", type=["txt"])
    txt_content = st.text_area(
        "Ou cole o conteúdo do arquivo de portais",
        height=200,
        value=prefill_text or "",
        placeholder="Portal 1; https://www.ingress.com/intel?...pll=LAT,LON\nPortal 2; ..."
    )

    col1, col2 = st.columns(2)
    with col1:
        num_agents = st.number_input("Número de agentes", min_value=1, max_value=50, value=1, step=1)
    with col2:
        num_cpus = st.number_input("CPUs a usar (0 = máximo)", min_value=0, max_value=128, value=0, step=1)

    team = st.selectbox("Facção (cores)", ["Enlightened (verde)", "Resistance (azul)"])
    output_csv = st.checkbox("Gerar CSV", value=True)

    st.markdown("**Mapa de fundo (opcional):**")
    google_key_input = st.text_input(
        "Google Maps API key (opcional)",
        value="",
        help="Se deixar vazio e houver uma chave salva no servidor, ela será usada automaticamente."
    )
    google_secret_input = st.text_input("Google Maps API secret (opcional)", value="", type="password")

    gerar_gif_checkbox = st.checkbox("Gerar GIF (passo-a-passo)", value=False)

    submitted = st.form_submit_button("Gerar plano")

# ---------- Execução com ETA (robusta a reconexão) ----------

@st.cache_resource(show_spinner=False)
def job_manager():
    """Executor + mapa de jobs vivos. Persiste entre reruns/reconexões."""
    return {
        "executor": ThreadPoolExecutor(max_workers=1),
        "jobs": {}  # job_id -> {"future": Future, "t0": float, "eta": float, "meta": {...}}
    }

def run_job(kwargs: dict) -> dict:
    t0 = time.time()
    try:
        res = processar_plano(**kwargs)
        return {"ok": True, "result": res, "elapsed": time.time() - t0}
    except Exception as e:
        return {"ok": False, "error": str(e), "elapsed": time.time() - t0}

def start_job(kwargs: dict, eta_s: float, meta: dict) -> str:
    jm = job_manager()
    job_id = uuid.uuid4().hex[:8]
    fut = jm["executor"].submit(run_job, kwargs)
    jm["jobs"][job_id] = {"future": fut, "t0": time.time(), "eta": eta_s, "meta": meta}
    return job_id

def get_job(job_id: str):
    return job_manager()["jobs"].get(job_id)

# ===== Enfileirar job quando o usuário envia =====
if submitted:
    if uploaded:
        portal_bytes = uploaded.getvalue()
        texto_portais = portal_bytes.decode("utf-8", errors="ignore")
    else:
        if not txt_content.strip():
            st.error("Envie um arquivo .txt ou cole o conteúdo.")
            st.stop()
        texto_portais = txt_content
        portal_bytes = texto_portais.encode("utf-8")

    res_colors = team.startswith("Resistance")
    n_portais = contar_portais(texto_portais)
    fazer_gif = bool(gerar_gif_checkbox)
    if n_portais > 25 and fazer_gif:
        st.warning(f"Detectei **{n_portais} portais**. Para evitar travamentos, o GIF foi **desativado automaticamente**.")
        fazer_gif = False

    google_api_key = (google_key_input or "").strip() or st.secrets.get("GOOGLE_API_KEY", None)
    google_api_secret = (google_secret_input or "").strip() or st.secrets.get("GOOGLE_API_SECRET", None)

    kwargs = dict(
        portal_bytes=portal_bytes,
        num_agents=int(num_agents),
        num_cpus=int(num_cpus),
        res_colors=res_colors,
        google_api_key=google_api_key,
        google_api_secret=google_api_secret,
        output_csv=output_csv,
        fazer_gif=fazer_gif,
    )

    eta_s = estimate_eta_s(n_portais, int(num_cpus), fazer_gif)
    meta = {"n_portais": n_portais, "num_cpus": int(num_cpus), "gif": fazer_gif}

    st.session_state["job_id"] = start_job(kwargs, eta_s, meta)
    st.rerun()

# ===== UI de acompanhamento do job (sobrevive a reconexões) =====
job_id = st.session_state.get("job_id")
if job_id:
    job = get_job(job_id)
    if not job:
        st.warning("Não encontrei o job atual (talvez tenha concluído e sido limpo).")
    else:
        fut = job["future"]
        t0 = job["t0"]
        eta_s = job["eta"]
        meta = job.get("meta", {})

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

        # terminou
        out = fut.result()
        bar.progress(100)

        if out.get("ok"):
            status.update(label="✅ Concluído", state="complete", expanded=False)
            res = out["result"]

            inc_metric("plans_completed", 1)
            # grava histórico real para refinar ETAs futuros
            try:
                record_run(
                    int(meta.get("n_portais", 0)),
                    int(meta.get("num_cpus", 0)),
                    bool(meta.get("gif", False)),
                    float(out.get("elapsed", 0.0)),
                )
            except Exception:
                pass

            if res["pm_bytes"]:
                st.image(res["pm_bytes"], caption="Portal Map")
            if res["lm_bytes"]:
                st.image(res["lm_bytes"], caption="Link Map")
            if res["gif_bytes"]:
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

            with st.expander("Ver logs do processamento"):
                st.code(res["log_txt"] or "(sem logs)", language="bash")
        else:
            status.update(label="❌ Falhou", state="error", expanded=True)
            st.error(f"Erro ao gerar o plano: {out.get('error','desconhecido')}")

        # limpa o job para não reaparecer no próximo refresh
        del st.session_state["job_id"]

# ---------- Rodapé: Doações (esq) + Informes (dir) ----------
st.markdown("---")
left, right = st.columns(2)

# contatos atualizados
PIX_PHONE_DISPLAY = "+55 79 99834-5186"
WHATS_NUMBER_DIGITS = "5579998345186"  # para wa.me
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
