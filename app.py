import os
import sys
import types
import zipfile
import tempfile
from datetime import datetime

import streamlit as st

# --- Desliga o optimize() do pygifsicle (para não depender do gifsicle) ---
fake = types.ModuleType("pygifsicle")
def optimize(*args, **kwargs):
    return  # no-op
fake.optimize = optimize
sys.modules["pygifsicle"] = fake
# --------------------------------------------------------------------------

# Importa o Maxfield (use a pasta copiada: maxfield/)
from maxfield.maxfield import maxfield as run_maxfield

st.set_page_config(page_title="Maxfield Online (Protótipo)", page_icon="🗺️", layout="centered")
st.title("Ingress Maxfield — Gerador de Planos (Protótipo)")

st.markdown(
    """
    - Envie o **arquivo .txt de portais** (mesmo formato que você já usa no Maxfield) **ou** cole o conteúdo.
    - Informe **nº de agentes** e **CPUs**.
    - (Opcional) Adicione uma **Google Maps API key** para ter o **mapa de fundo**.
    - Ao final, baixe o **.zip** com tudo ou os arquivos individuais.
    """
)

with st.form("plan_form"):
    uploaded = st.file_uploader("Arquivo de portais (.txt)", type=["txt"])
    txt_content = st.text_area("Ou cole o conteúdo do arquivo de portais", height=200)

    col1, col2 = st.columns(2)
    with col1:
        num_agents = st.number_input("Número de agentes", min_value=1, max_value=50, value=3, step=1)
    with col2:
        num_cpus = st.number_input("CPUs a usar (0 = máximo)", min_value=0, max_value=128, value=0, step=1)

    team = st.selectbox("Facção (cores)", ["Enlightened (verde)", "Resistance (azul)"])
    output_csv = st.checkbox("Gerar CSV", value=True)

    st.markdown("**Mapa de fundo (opcional):**")
    # ⚠️ Não pré-preenche com a chave dos secrets (para não vazar).
    # Se o usuário deixar em branco, usaremos os secrets internamente.
    google_api_key_input = st.text_input(
        "Google Maps API key",
        value="",
        type="password",
        placeholder="Cole aqui apenas se quiser sobrescrever a chave privada"
    )
    google_api_secret_input = st.text_input(
        "Google Maps API secret (opcional)",
        value="",
        type="password",
        placeholder="Normalmente vazio (apenas para URLs assinadas)"
    )

    submitted = st.form_submit_button("Gerar plano")

if submitted:
    if not uploaded and not txt_content.strip():
        st.error("Envie um arquivo .txt ou cole o conteúdo.")
        st.stop()

    # Pasta temporária de trabalho/saída
    workdir = tempfile.mkdtemp(prefix="maxfield_")
    outdir = os.path.join(workdir, "output")
    os.makedirs(outdir, exist_ok=True)

    # Salva o arquivo de portais
    portal_path = os.path.join(workdir, "portais.txt")
    data = uploaded.getvalue() if uploaded else txt_content.encode("utf-8")
    with open(portal_path, "wb") as f:
        f.write(data)

    # Converte facção -> esquema de cor do Maxfield
    res_colors = team.startswith("Resistance")

    # --------- Chaves do Google: prioridade (input do usuário) -> secrets ----------
    key = (google_api_key_input or "").strip()
    secret = (google_api_secret_input or "").strip()

    # Se o usuário não digitou, tenta pegar dos secrets (sem exibir)
    if not key:
        key = (st.secrets.get("GOOGLE_API_KEY", "") or "").strip()
    if not secret:
        secret = (st.secrets.get("GOOGLE_API_SECRET", "") or "").strip()

    # Normaliza valores vazios para None (o Maxfield só tenta mapa se key != None)
    google_api_key = key if key else None
    google_api_secret = secret if secret else None
    # -------------------------------------------------------------------------------

    # Feedback sem revelar a chave
    if google_api_key:
        st.caption("Mapa de fundo: **ativado** (usando chave privada).")
    else:
        st.caption("Mapa de fundo: **desativado** (sem key).")

    st.info("Processando o plano... aguarde.")
    try:
        # Chama a função principal do Maxfield
        run_maxfield(
            portal_path,
            num_agents=int(num_agents),
            num_cpus=int(num_cpus),
            res_colors=res_colors,
            google_api_key=google_api_key,
            google_api_secret=google_api_secret,
            output_csv=output_csv,
            outdir=outdir,
            verbose=True
        )

        st.success("Plano gerado com sucesso!")

        # Mostra imagens/gif se existirem
        pm = os.path.join(outdir, "portal_map.png")
        lm = os.path.join(outdir, "link_map.png")
        gif = os.path.join(outdir, "plan_movie.gif")

        if os.path.exists(pm):
            st.image(pm, caption="Portal Map")
        if os.path.exists(lm):
            st.image(lm, caption="Link Map")
        if os.path.exists(gif):
            with open(gif, "rb") as g:
                st.download_button("Baixar GIF (plan_movie.gif)", data=g, file_name="plan_movie.gif", mime="image/gif")

        # Compacta toda a saída em .zip
        zip_path = os.path.join(workdir, f"maxfield_output_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for root, _, files in os.walk(outdir):
                for fn in files:
                    fp = os.path.join(root, fn)
                    arc = os.path.relpath(fp, outdir)
                    z.write(fp, arcname=arc)

        with open(zip_path, "rb") as f:
            st.download_button("Baixar todos os arquivos (.zip)", data=f.read(), file_name=os.path.basename(zip_path), mime="application/zip")

        st.caption("Dica: sem Google API key o fundo do mapa ficará branco (apenas portais/links).")

    except Exception as e:
        st.error(f"Erro ao gerar o plano: {e}")