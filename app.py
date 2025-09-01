import os
import io
import sys
import types
import zipfile
import tempfile
import sqlite3
from datetime import datetime
from contextlib import redirect_stdout

import streamlit as st

# --- Desliga o optimize() do pygifsicle (para não depender do gifsicle) ---
fake = types.ModuleType("pygifsicle")
def optimize(*args, **kwargs):
    return
fake.optimize = optimize
sys.modules["pygifsicle"] = fake
# --------------------------------------------------------------------------

# Importa o Maxfield
from maxfield.maxfield import maxfield as run_maxfield

# ---------- Config do Streamlit ----------
st.set_page_config(page_title="Maxfield Online (Protótipo)", page_icon="🗺️", layout="centered")

# ===== Fundo do site (usa BG_URL dos secrets) =====
bg_url = st.secrets.get("BG_URL", "").strip()
if bg_url:
    st.markdown(
        f"""
        <style>
        /* Fundo de tela */
        .stApp {{
            background: url('{bg_url}') no-repeat center center fixed;
            background-size: cover;
        }}
        /* Caixa translúcida pra leitura melhor */
        .stApp .block-container {{
            background: rgba(255,255,255,0.85);
            border-radius: 12px;
            padding: 1rem 1.2rem 2rem 1.2rem;
        }}
        </style>
        """,
        unsafe_allow_html=True
    )
# ==================================================

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
    # inicializa chaves
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

# Conta visita 1x por sessão
if "visit_counted" not in st.session_state:
    inc_metric("visits", 1)
    st.session_state["visit_counted"] = True

# ---------- Título ----------
st.title("Ingress Maxfield — Gerador de Planos (Protótipo)")

# KPIs
colv, colp = st.columns(2)
with colv:
    st.metric("Acessos (sessões)", f"{get_metric('visits'):,}")
with colp:
    st.metric("Planos gerados", f"{get_metric('plans_completed'):,}")

st.markdown(
    """
    - Envie o **arquivo .txt de portais** (mesmo formato do Maxfield) **ou** cole o conteúdo.
    - Informe **nº de agentes** e **CPUs**.
    - **Mapa de fundo (opcional)**: informe uma **Google Maps API key**. 
      Se deixar vazio e houver chave em `secrets`, ela será usada automaticamente.
    - Resultados: imagens, CSVs e (se permitido) **GIF** com o passo-a-passo.
    """
)

# ---------- utilitários ----------
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

# ---------- UI ----------
with st.form("plan_form"):
    uploaded = st.file_uploader("Arquivo de portais (.txt)", type=["txt"])
    txt_content = st.text_area(
        "Ou cole o conteúdo do arquivo de portais",
        height=200,
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

# Botões/links FORA do form (download em form dá erro)
c1, c2 = st.columns(2)
with c1:
    st.download_button(
        "📄 Baixar modelo (.txt)",
        data=EXEMPLO_TXT.encode("utf-8"),
        file_name="modelo_portais.txt",
        mime="text/plain",
        help="Baixe um modelo de como preparar o .txt de portais",
    )
with c2:
    TUTORIAL_URL = st.secrets.get("TUTORIAL_URL", "https://www.youtube.com/")
    st.link_button("▶️ Tutorial (YouTube)", TUTORIAL_URL)

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
        st.warning(
            f"Detectei **{n_portais} portais**. Para evitar travamentos, o GIF foi **desativado automaticamente**."
        )
        fazer_gif = False

    google_api_key = (google_key_input or "").strip() or st.secrets.get("GOOGLE_API_KEY", None)
    google_api_secret = (google_secret_input or "").strip() or st.secrets.get("GOOGLE_API_SECRET", None)

    st.info("Processando o plano... aguarde.")
    try:
        result = processar_plano(
            portal_bytes=portal_bytes,
            num_agents=int(num_agents),
            num_cpus=int(num_cpus),
            res_colors=res_colors,
            google_api_key=google_api_key,
            google_api_secret=google_api_secret,
            output_csv=output_csv,
            fazer_gif=fazer_gif,
        )
        st.success("Plano gerado com sucesso!")

        # incrementa métrica de planos gerados
        inc_metric("plans_completed", 1)

        if result["pm_bytes"]:
            st.image(result["pm_bytes"], caption="Portal Map")
        if result["lm_bytes"]:
            st.image(result["lm_bytes"], caption="Link Map")
        if result["gif_bytes"]:
            st.download_button(
                "Baixar GIF (plan_movie.gif)",
                data=result["gif_bytes"],
                file_name="plan_movie.gif",
                mime="image/gif"
            )

        st.download_button(
            "Baixar todos os arquivos (.zip)",
            data=result["zip_bytes"],
            file_name=f"maxfield_output_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip",
            mime="application/zip",
        )

        with st.expander("Ver logs do processamento"):
            st.code(result["log_txt"] or "(sem logs)", language="bash")

    except Exception as e:
        st.error(f"Erro ao gerar o plano: {e}")

# ---------- Rodapé com PIX e WhatsApp ----------
st.markdown("---")
st.subheader("💙 Apoie este projeto")

pix_qr_url = st.secrets.get("PIX_QR_URL", "")
if pix_qr_url:
    st.image(pix_qr_url, caption="Use o QR Code para doar via PIX", width=200)

st.markdown("Ou copie a chave PIX (celular): **+55 79 99816-0693**")
st.markdown(
    "[📲 Entrar em contato no WhatsApp](https://wa.me/5579998160693)",
    unsafe_allow_html=True,
)