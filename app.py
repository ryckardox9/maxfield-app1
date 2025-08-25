import os
import io
import sys
import types
import zipfile
import tempfile
from datetime import datetime
from contextlib import redirect_stdout

import streamlit as st

# --- Desliga o optimize() do pygifsicle (para não depender do gifsicle) ---
fake = types.ModuleType("pygifsicle")
def optimize(*args, **kwargs):
    # no-op
    return
fake.optimize = optimize
sys.modules["pygifsicle"] = fake
# --------------------------------------------------------------------------

# Importa o Maxfield (pasta local copiada para o repo)
from maxfield.maxfield import maxfield as run_maxfield


st.set_page_config(page_title="Maxfield Online (Protótipo)", page_icon="🗺️", layout="centered")
st.title("Ingress Maxfield — Gerador de Planos (Protótipo)")

st.markdown(
    """
    - Envie o **arquivo .txt de portais** (mesmo formato do Maxfield) **ou** cole o conteúdo.
    - Informe **nº de agentes** e **CPUs**.
    - **Mapa de fundo (opcional)**: informe uma **Google Maps API key**. Se deixar vazio e houver chave em `secrets`, ela será usada automaticamente.
    - Resultados: imagens, CSVs e (se permitido) **GIF** com o passo-a-passo.
    """
)

# ---------- utilitários ----------
def contar_portais(texto: str) -> int:
    """Conta linhas úteis (ignora vazias e linhas iniciadas por '#')."""
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
    """
    Roda o Maxfield numa pasta temporária e retorna:
      - zip_bytes: bytes do .zip com todos os arquivos
      - pm_bytes / lm_bytes / gif_bytes (se existirem)
      - log_txt: saída verbose capturada
    Essa função é cacheada (chamadas idênticas reutilizam o resultado).
    """
    # pasta de trabalho
    workdir = tempfile.mkdtemp(prefix="maxfield_")
    outdir = os.path.join(workdir, "output")
    os.makedirs(outdir, exist_ok=True)

    # salva arquivo de portais
    portal_path = os.path.join(workdir, "portais.txt")
    with open(portal_path, "wb") as f:
        f.write(portal_bytes)

    # captura log do Maxfield (verbose=True)
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
                # se não for fazer GIF, pula os step plots (mais leve)
                skip_step_plots=(not fazer_gif),
            )
    except Exception as e:
        # anexa exception ao log e propaga
        log_buffer.write(f"\n[ERRO] {e}\n")
        raise
    finally:
        log_txt = log_buffer.getvalue()

    # coleta arquivos principais como bytes (se existirem)
    pm_path = os.path.join(outdir, "portal_map.png")
    lm_path = os.path.join(outdir, "link_map.png")
    gif_path = os.path.join(outdir, "plan_movie.gif")

    def read_bytes(path):
        return open(path, "rb").read() if os.path.exists(path) else None

    pm_bytes = read_bytes(pm_path)
    lm_bytes = read_bytes(lm_path)
    gif_bytes = read_bytes(gif_path)

    # monta um .zip com TODO o conteúdo de outdir (sem carregar tudo em memória ao mesmo tempo)
    zip_path = os.path.join(workdir, f"maxfield_output_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(outdir):
            for fn in files:
                fp = os.path.join(root, fn)
                arc = os.path.relpath(fp, outdir)
                z.write(fp, arcname=arc)
    zip_bytes = open(zip_path, "rb").read()

    return {
        "zip_bytes": zip_bytes,
        "pm_bytes": pm_bytes,
        "lm_bytes": lm_bytes,
        "gif_bytes": gif_bytes,
        "log_txt": log_txt,
    }


# ---------- UI ----------
with st.form("plan_form"):
    uploaded = st.file_uploader("Arquivo de portais (.txt)", type=["txt"])
    txt_content = st.text_area("Ou cole o conteúdo do arquivo de portais", height=200, placeholder="Portal 1; https://www.ingress.com/intel?...pll=LAT,LON\nPortal 2; ...")

    col1, col2 = st.columns(2)
    with col1:
        num_agents = st.number_input("Número de agentes", min_value=1, max_value=50, value=3, step=1)
    with col2:
        num_cpus = st.number_input("CPUs a usar (0 = máximo)", min_value=0, max_value=128, value=0, step=1)

    team = st.selectbox("Facção (cores)", ["Enlightened (verde)", "Resistance (azul)"])
    output_csv = st.checkbox("Gerar CSV", value=True)

    st.markdown("**Mapa de fundo (opcional):**")
    # Inputs vazios por padrão. Se o usuário deixar vazio e houver secrets, usaremos secrets sem mostrar a chave.
    google_key_input = st.text_input("Google Maps API key (opcional)", value="", help="Se deixar vazio e houver uma chave salva no servidor, ela será usada automaticamente.")
    google_secret_input = st.text_input("Google Maps API secret (opcional)", value="", type="password")

    # Preferência do usuário para gerar GIF; pode ser desativada automaticamente se houver muitos portais
    gerar_gif_checkbox = st.checkbox("Gerar GIF (passo-a-passo)", value=True, help="Gera frames e um GIF do plano. Pesado para muitos portais.")

    submitted = st.form_submit_button("Gerar plano")

if submitted:
    # lê conteúdo dos portais
    if uploaded:
        portal_bytes = uploaded.getvalue()
        texto_portais = portal_bytes.decode("utf-8", errors="ignore")
    else:
        if not txt_content.strip():
            st.error("Envie um arquivo .txt ou cole o conteúdo.")
            st.stop()
        texto_portais = txt_content
        portal_bytes = texto_portais.encode("utf-8")

    # converte facção -> esquema de cor
    res_colors = team.startswith("Resistance")

    # conta portais e decide sobre GIF
    n_portais = contar_portais(texto_portais)
    fazer_gif = bool(gerar_gif_checkbox)
    if n_portais > 25 and fazer_gif:
        st.warning(f"Detectei **{n_portais} portais**. Para evitar travamentos, o GIF foi **desativado automaticamente**. Você ainda receberá imagens e CSVs.")
        fazer_gif = False

    # chaves do Google:
    # prioridade: input do usuário -> secrets -> None
    google_api_key = (google_key_input or "").strip()
    google_api_secret = (google_secret_input or "").strip()
    if not google_api_key:
        google_api_key = st.secrets.get("GOOGLE_API_KEY", None)
    if not google_api_secret:
        google_api_secret = st.secrets.get("GOOGLE_API_SECRET", None)

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

        # mostra imagens/gif
        if result["pm_bytes"]:
            st.image(result["pm_bytes"], caption="Portal Map")
        if result["lm_bytes"]:
            st.image(result["lm_bytes"], caption="Link Map")
        if result["gif_bytes"]:
            st.download_button("Baixar GIF (plan_movie.gif)", data=result["gif_bytes"], file_name="plan_movie.gif", mime="image/gif")

        # botão para baixar tudo
        st.download_button(
            "Baixar todos os arquivos (.zip)",
            data=result["zip_bytes"],
            file_name=f"maxfield_output_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip",
            mime="application/zip",
        )

        # logs
        with st.expander("Ver logs do processamento"):
            st.code(result["log_txt"] or "(sem logs)", language="bash")

        st.caption("Observação: sem Google API key, o fundo do mapa ficará branco (apenas portais/links).")

    except Exception as e:
        st.error(f"Erro ao gerar o plano: {e}")