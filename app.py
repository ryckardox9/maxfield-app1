import os
import sys
import types
import zipfile
import tempfile
import shutil
from datetime import datetime

from PIL import Image, ImageSequence
import streamlit as st

# --- Desativa optimize() do pygifsicle (evita dependência do gifsicle) ---
fake = types.ModuleType("pygifsicle")
def optimize(*args, **kwargs):
    return
fake.optimize = optimize
sys.modules["pygifsicle"] = fake
# -------------------------------------------------------------------------

# Importa a função principal do Maxfield
from maxfield.maxfield import maxfield as run_maxfield

st.set_page_config(page_title="Maxfield Online (Protótipo)", page_icon="🗺️", layout="centered")
st.title("Ingress Maxfield — Gerador de Planos (Protótipo)")

st.markdown(
    """
    **Como usar**
    1) Envie o **arquivo .txt de portais** (ou cole o conteúdo).
    2) Informe **nº de agentes** e **CPUs**.
    3) (Opcional) Se tiver sua **Google Maps API key**, digite. Se deixar vazio, uso a chave privada dos *secrets*.
    4) **(Recomendado)** Deixe o GIF **desligado** para muitos portais. Ative só quando precisar e use o modo compacto.
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

    st.divider()
    st.markdown("### Mapa de fundo (opcional)")
    google_api_key_input = st.text_input(
        "Google Maps API key (opcional)",
        value="",
        help="Se vazio, uso a chave privada configurada em secrets."
    )
    google_api_secret_input = st.text_input(
        "Google Maps API secret (opcional)",
        value="",
        type="password",
        help="Se vazio, uso o secret configurado em secrets (se houver)."
    )

    st.divider()
    st.markdown("### GIF de passos (pesado)")
    make_gif = st.checkbox("Gerar GIF (pode travar com muitos portais)", value=False)
    colg1, colg2 = st.columns(2)
    with colg1:
        gif_width = st.selectbox("Largura do GIF", [640, 480, 360], index=1)  # default 480
    with colg2:
        frame_stride = st.selectbox("Pular frames", [1, 2, 3, 4, 5], index=2, help="2 = usa 1 em cada 2; 3 = 1 em cada 3, etc.")

    submitted = st.form_submit_button("Gerar plano")

def compress_gif(in_path, out_path, max_width=480, stride=3, duration_ms=200):
    """
    Reprocessa o GIF para ficar menor:
    - Redimensiona para max_width, preservando proporção
    - Pula frames (stride)
    - Salva otimizado
    """
    with Image.open(in_path) as im:
        frames = []
        orig_w, orig_h = im.size
        if orig_w > max_width:
            new_w = max_width
            new_h = int(orig_h * (max_width / float(orig_w)))
        else:
            new_w, new_h = orig_w, orig_h

        idx = 0
        for frame in ImageSequence.Iterator(im):
            if idx % max(1, int(stride)) != 0:
                idx += 1
                continue
            fr = frame.convert("P", palette=Image.ADAPTIVE)
            if (new_w, new_h) != (orig_w, orig_h):
                fr = fr.resize((new_w, new_h), Image.LANCZOS)
            frames.append(fr)
            idx += 1

        if not frames:
            # fallback: pelo menos 1 frame
            frames = [im.convert("P", palette=Image.ADAPTIVE)]

        # salva otimizado
        frames[0].save(
            out_path,
            save_all=True,
            append_images=frames[1:],
            loop=0,
            duration=duration_ms,
            optimize=True,
            disposal=2
        )

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

    # Facção -> cor
    res_colors = team.startswith("Resistance")

    # Chaves: input do usuário -> secrets
    google_api_key = (google_api_key_input or "").strip() or st.secrets.get("GOOGLE_API_KEY", None)
    google_api_secret = (google_api_secret_input or "").strip() or st.secrets.get("GOOGLE_API_SECRET", None)

    # Se GIF desligado, pulamos step plots dentro do Maxfield
    skip_step_plots = not make_gif

    st.info("Processando o plano... aguarde.")
    try:
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
            # Estes dois são suportados no Maxfield original:
            # - skip_plots: pular todos os plots
            # - skip_step_plots: pular geração de frames + GIF
            skip_step_plots=skip_step_plots
        )

        st.success("Plano gerado com sucesso!")

        # Mostrar PNGs
        pm = os.path.join(outdir, "portal_map.png")
        lm = os.path.join(outdir, "link_map.png")
        gif_path = os.path.join(outdir, "plan_movie.gif")
        frames_dir = os.path.join(outdir, "frames")

        if os.path.exists(pm):
            st.image(pm, caption="Portal Map")
        if os.path.exists(lm):
            st.image(lm, caption="Link Map")

        # Se pediu GIF, tentar comprimir e mostrar download
        if make_gif and os.path.exists(gif_path):
            compact_gif = os.path.join(outdir, "plan_movie_compact.gif")
            try:
                compress_gif(gif_path, compact_gif, max_width=int(gif_width), stride=int(frame_stride), duration_ms=250)
                # substitui o pesado pelo compacto
                os.replace(compact_gif, gif_path)
            except Exception as ce:
                st.warning(f"Não foi possível comprimir o GIF ({ce}). Usando original.")

            # apagar frames para economizar disco
            if os.path.isdir(frames_dir):
                shutil.rmtree(frames_dir, ignore_errors=True)

            # botão para baixar o GIF
            with open(gif_path, "rb") as g:
                st.download_button("Baixar GIF (plan_movie.gif)", data=g, file_name="plan_movie.gif", mime="image/gif")
        else:
            if not make_gif:
                st.info("GIF não gerado (opção desativada). Ative “Gerar GIF” somente quando precisar — pode ser pesado.")

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

        st.caption("Dica: para muitos portais, deixe o GIF desligado ou use largura 360 e pular frames 3+.")
    except TypeError as te:
        # Caso sua versão do Maxfield não aceite skip_step_plots (muito antiga),
        # caímos sem o parâmetro e geramos sem GIF.
        if "skip_step_plots" in str(te):
            st.warning("Sua versão do Maxfield não aceita 'skip_step_plots'. Gerando sem GIF.")
            try:
                run_maxfield(
                    portal_path,
                    num_agents=int(num_agents),
                    num_cpus=int(num_cpus),
                    res_colors=res_colors,
                    google_api_key=(google_api_key or None),
                    google_api_secret=(google_api_secret or None),
                    output_csv=output_csv,
                    outdir=outdir,
                    verbose=True
                )
                st.success("Plano gerado (sem GIF).")
            except Exception as e2:
                st.error(f"Erro ao gerar o plano: {e2}")
        else:
            st.error(f"Erro ao gerar o plano: {te}")
    except Exception as e:
        st.error(f"Erro ao gerar o plano: {e}")