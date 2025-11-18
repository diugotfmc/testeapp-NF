import streamlit as st
import pdfplumber
import re
import pandas as pd
import io

# =========================
# Configuração e Título
# =========================
st.set_page_config(page_title="Leitor e Conciliação de NF por NM", layout="wide")
st.title("📄 Leitor de Nota Fiscal (PDF) + 🔗 Conciliação por NM (TXT | pipe)")
st.caption("Extrai itens da NF e relaciona com um TXT delimitado por '|', usando NM como chave.")

# =========================
# Uploads
# =========================
col_up1, col_up2 = st.columns(2)
with col_up1:
    nf_file = st.file_uploader("NF-e (PDF)", type=["pdf"], key="nf")
with col_up2:
    txt_file = st.file_uploader("TXT de referência (delimitado por |)", type=["txt"], key="txt_ref")

# =========================
# Utilitários
# =========================
NUM_RE = re.compile(r'(?:\d{1,3}(?:\.\d{3})+|\d+),\d{2,4}')  # números PT-BR
UNITS = ['UN', 'KG', 'PC', 'CJ', 'KIT', 'PAR', 'M', 'L', 'LT', 'CX']

UNIT_QTD_RE = re.compile(
    r'(?P<qtd>(?:\d{1,3}(?:\.\d{3})+|\d+),\d{2,4})\s*(?P<un>' + '|'.join(UNITS) + r')\b'
)
UNIT_FIRST_RE = re.compile(
    r'(?P<un>' + '|'.join(UNITS) + r')\s*(?P<qtd>(?:\d{1,3}(?:\.\d{3})+|\d+),\d{2,4})\b'
)

def to_float_br(s: str) -> float:
    return float(s.replace('.', '').replace(',', '.'))

def format_nm(nm_text: str) -> str:
    """
    'NM12773524' -> '12.773.524' (2-3-3) ou normaliza '12.773.524' se já vier assim.
    """
    if not nm_text:
        return None
    digits = ''.join(re.findall(r'\d', nm_text))
    if len(digits) == 8:
        return f"{digits[:2]}.{digits[2:5]}.{digits[5:]}"
    # fallback: milhar
    rev = digits[::-1]
    chunks = [rev[i:i+3] for i in range(0, len(rev), 3)]
    return '.'.join(ch[::-1] for ch in chunks[::-1]) if digits else None

def format_it(it_text: str) -> str:
    """'IT200' ou 'IT 200' -> '200' (apenas dígitos)."""
    if not it_text:
        return None
    digits = ''.join(re.findall(r'\d', it_text))
    return digits or None

def format_codigo(codigo_raw: str) -> str:
    """
    Formata o código da NF:
      - '...BJ########...' -> 'BJ xxx.yyyyy'  (ex.: AC0505BJ08000200 -> BJ 080.00200)
      - '...BJ(\\d{3})(\\d{5})...' -> 'BJ 028.00629'
      - '...BX\\d{3}...' -> 'BX 156'
      - Caso contrário, retorna o original.
    """
    if not codigo_raw:
        return codigo_raw
    m_bj8 = re.search(r'BJ(\d{8})', codigo_raw)
    if m_bj8:
        num = m_bj8.group(1)
        return f"BJ {num[:3]}.{num[3:]}"
    m_bj_3_5 = re.search(r'\bBJ(\d{3})(\d{5})\b', codigo_raw)
    if m_bj_3_5:
        return f"BJ {m_bj_3_5.group(1)}.{m_bj_3_5.group(2)}"
    m_bx3 = re.search(r'BX(\d{3})', codigo_raw)
    if m_bx3:
        return f"BX {m_bx3.group(1)}"
    return codigo_raw

# =========================
# Parser da NF (PDF)
# =========================
def parse_nf_pdf(file) -> pd.DataFrame:
    if file is None:
        return pd.DataFrame()

    # Extrai texto do PDF
    texto = ""
    with pdfplumber.open(file) as pdf:
        for p in pdf.pages:
            texto += (p.extract_text() or "") + "\n"

    # Mantém as linhas originais (para capturar sufixo da 2ª linha)
    linhas_brutas = [l for l in texto.splitlines() if l.strip()]
    padrao_inicio_item = re.compile(r"^[A-Z]{2,4}\d{2,}[A-Z0-9]*")

    blocos_itens, bloco_atual = [], []
    for linha in linhas_brutas:
        if padrao_inicio_item.match(linha.strip()):
            if bloco_atual:
                blocos_itens.append(bloco_atual)  # lista de linhas do item
                bloco_atual = []
        if bloco_atual or padrao_inicio_item.match(linha.strip()):
            bloco_atual.append(linha)
    if bloco_atual:
        blocos_itens.append(bloco_atual)

    itens = []
    for bloco in blocos_itens:
        bloco_text = " ".join([b.strip() for b in bloco])

        # 1) Código + "miolo" + NCM + CFOP
        m = re.search(
            r'^(?P<codigo>[A-Z0-9]{2,}\d{2,}[A-Z0-9]*)\s+(?P<miolo>.+?)\s+(?P<ncm>\d{8})\s+\d{3}\s+(?P<cfop>\d{4})',
            bloco_text
        )
        if not m:
            continue

        codigo_raw_base = m.group('codigo').strip()
        miolo = m.group('miolo').strip()   # "ITxxx - NMyyyyyy - Descrição"
        ncm = m.group('ncm').strip()
        cfop = m.group('cfop').strip()
        resto = bloco_text[m.end():]

        # 2) IT e NM
        it_match = re.search(r'\bIT\s*\d+\b', miolo)
        nm_match = re.search(r'\bNM\d+\b', miolo)

        it_val = format_it(it_match.group(0)) if it_match else None
        nm_fmt = format_nm(nm_match.group(0)) if nm_match else None

        # 3) Descrição limpa
        descricao = miolo
        descricao = re.sub(r'\bIT\s*\d+\b', '', descricao)
        descricao = re.sub(r'\bNM\d+\b', '', descricao)
        descricao = re.sub(r'\s*-\s*', ' - ', descricao)
        descricao = re.sub(r'\s{2,}', ' ', descricao).strip(' -')

        # 4) SUFIXO da 2ª linha: ITEMxx / POS xx
        sufixo = None
        if len(bloco) > 1:
            m2 = re.search(r'\b(ITEM\s*\d+|POS\s*\d+)\b', bloco[1], flags=re.IGNORECASE)
            if m2:
                sufixo = m2.group(1)
        if not sufixo and len(bloco) > 2:
            for ln in bloco[2:]:
                m_more = re.search(r'\b(ITEM\s*\d+|POS\s*\d+)\b', ln, flags=re.IGNORECASE)
                if m_more:
                    sufixo = m_more.group(1)
                    break
        sufixo_clean = re.sub(r'\s+', '', sufixo.upper()) if sufixo else None

        # 5) QTD (string) e UN
        qtd_str, un = None, None
        m_q = UNIT_QTD_RE.search(resto)
        if m_q:
            qtd_str = m_q.group('qtd')
            un = m_q.group('un')
        else:
            m_q2 = UNIT_FIRST_RE.search(resto)
            if m_q2:
                qtd_str = m_q2.group('qtd')
                un = m_q2.group('un')
            else:
                m_u = re.search('|'.join(UNITS), resto)
                if m_u:
                    un = m_u.group(0)
                    prev = resto[:m_u.start()]
                    m_num_prev = list(NUM_RE.finditer(prev))
                    if m_num_prev:
                        qtd_str = m_num_prev[-1].group(0)

        # 6) V.Unit e V.Total por consistência
        v_unit, v_total = None, None
        if qtd_str:
            try:
                qtd_val = to_float_br(qtd_str)
                if qtd_val > 0:
                    nums = [n.group(0) for n in NUM_RE.finditer(resto)]
                    values = [(to_float_br(s), s) for s in nums]
                    best, best_score = None, (1e9, 0)
                    for i in range(len(values)):
                        a = values[i][0]
                        if a <= 0:
                            continue
                        for j in range(i + 1, len(values)):
                            b = values[j][0]
                            if b <= 0 or b < a:
                                continue
                            err = abs(a * qtd_val - b)
                            tol = max(0.001 * max(1.0, b), 0.05)
                            if err < tol:
                                score = (err, -b)
                                if score < best_score:
                                    best_score = score
                                    best = (values[i][1], values[j][1])
                    if best:
                        v_unit = to_float_br(best[0])
                        v_total = to_float_br(best[1])
            except Exception:
                pass

        # 7) Monta códigos
        codigo_raw = codigo_raw_base + (sufixo_clean or "")     # ex.: AC0703BJ10500004ITEM15
        codigo_fmt_base = format_codigo(codigo_raw_base)        # ex.: "BJ 105.00004"
        codigo_fmt = codigo_fmt_base + (f"\n{sufixo_clean}" if sufixo_clean else "")

        itens.append({
            "Código (Raw Base)": codigo_raw_base,
            "Código (Raw)": codigo_raw,
            "Código": codigo_fmt,
            "IT": it_val,                 # só dígitos
            "NM": nm_fmt,                 # chave de conciliação
            "Descrição (NF)": descricao,
            "NCM/SH": ncm,
            "CFOP": cfop,
            "UN (NF)": un,
            "QTD (NF)": qtd_str,          # texto '1,0000'
            "V. Unitário (R$)": v_unit,
            "V. Total (R$)": v_total
        })

    return pd.DataFrame(itens)

# =========================
# Parser do TXT (pipe |) de referência
# =========================
def parse_ref_txt_pipe(file) -> pd.DataFrame:
    """
    Lê TXT delimitado por '|', com colunas:
      Material | Texto breve material | Qtd. | UM (ou UMR) | Cen. | Elemento PEP
    Ignora cabeçalhos/linhas de traços e linhas inválidas.
    """
    if file is None:
        return pd.DataFrame()

    # decodificação robusta
    raw = file.read()
    if isinstance(raw, bytes):
        for enc in ("utf-8", "cp1252", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = raw.decode("latin-1", errors="ignore")
    else:
        text = str(raw)

    # quebra em linhas e filtra ruído
    lines = [ln.strip() for ln in text.splitlines()]
    # remove linhas vazias e separadores longos
    sep_re = re.compile(r'^[\-\=\*_\\\/\s]{5,}$')
    lines = [ln for ln in lines if ln and not sep_re.match(ln)]

    rows = []
    header_seen = False

    for ln in lines:
        # precisa ter pipe
        if '|' not in ln:
            continue

        # remove pipes extras nas bordas e divide por pipe tolerante a espaços
        ln_clean = ln.strip().strip('|').strip()
        parts = [p.strip() for p in re.split(r'\s*\|\s*', ln_clean)]

        # Detecta e ignora header (ex.: Material|Texto breve material|Qtd.|UMR|Cen.|Elemento PEP)
        if not header_seen and len(parts) >= 2:
            if parts[0].lower().startswith('material') and 'texto' in parts[1].lower():
                header_seen = True
                continue

        # Espera pelo menos 6 colunas de dados
        if len(parts) < 6:
            continue

        material, desc, qtd, um, centro, pep = parts[:6]

        # valida material (NM) no formato 12.773.524 (com pontos) ou somente dígitos
        m_ok = re.match(r'^\d{2}\.\d{3}\.\d{3}$', material) or re.match(r'^\d{8}$', re.sub(r'\D', '', material))
        if not m_ok:
            continue

        # validações suaves para os demais (não bloqueiam se vierem com pequenas variações)
        if not qtd:
            continue
        if not um:
            continue
        if not centro:
            continue
        if not pep:
            continue

        rows.append({
            "NM": format_nm(material),
            "Texto breve material (REF)": desc,
            "QTD (REF)": qtd,          # mantém como texto (ex.: '100,000')
            "UM (REF)": um,            # aceita UM/UMR
            "Centro (REF)": centro,
            "Elemento PEP (REF)": pep
        })

    return pd.DataFrame(rows)

# =========================
# Execução principal
# =========================
df_nf  = parse_nf_pdf(nf_file) if nf_file else pd.DataFrame()
df_ref = parse_ref_txt_pipe(txt_file) if txt_file else pd.DataFrame()

# Painel NF
with st.expander("Itens extraídos da NF", expanded=False):
    if not df_nf.empty:
        st.dataframe(df_nf, use_container_width=True)
        buf_nf = io.BytesIO()
        df_nf.to_excel(buf_nf, index=False)
        buf_nf.seek(0)
        st.download_button("📥 Baixar NF (Excel)", buf_nf, "nf_itens.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    else:
        st.info("Envie uma NF (PDF) para ver os itens extraídos.")

# Painel TXT de referência
with st.expander("Linhas do TXT de referência (pipe '|')", expanded=False):
    if not df_ref.empty:
        st.dataframe(df_ref, use_container_width=True)
        buf_ref = io.BytesIO()
        df_ref.to_excel(buf_ref, index=False)
        buf_ref.seek(0)
        st.download_button("📥 Baixar Referência (Excel)", buf_ref, "referencia_itens.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    else:
        st.info("Envie o TXT de referência delimitado por '|' no padrão informado.")

# Conciliação
st.markdown("---")
st.subheader("📊 Painel de Conciliação por NM")

if df_nf.empty or df_ref.empty:
    st.warning("Envie **os dois arquivos** (NF e TXT) para gerar a conciliação.")
else:
    df_merge = pd.merge(
        df_nf, df_ref, on="NM", how="outer", indicator=True, suffixes=(" (NF)", " (REF)")
    )

    # Métricas
    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric("Conciliados (NM em ambos)", int((df_merge['_merge'] == 'both').sum()))
    with c2:
        st.metric("Somente na NF", int((df_merge['_merge'] == 'left_only').sum()))
    with c3:
        st.metric("Somente no TXT", int((df_merge['_merge'] == 'right_only').sum()))

    # Abas (use o "olho" para ocultar/mostrar colunas)
    tab_both, tab_nf_only, tab_ref_only = st.tabs(["✔️ Conciliados", "📄 Somente NF", "📑 Somente TXT"])

    with tab_both:
        df_both = df_merge[df_merge['_merge'] == 'both'].drop(columns=['_merge'])
        st.dataframe(df_both, use_container_width=True)
        buf = io.BytesIO()
        df_both.to_excel(buf, index=False)
        buf.seek(0)
        st.download_button("📥 Baixar Conciliados (Excel)", buf, "conciliados.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    with tab_nf_only:
        df_l = df_merge[df_merge['_merge'] == 'left_only'].drop(columns=['_merge'])
        st.dataframe(df_l, use_container_width=True)
        buf_l = io.BytesIO()
        df_l.to_excel(buf_l, index=False)
        buf_l.seek(0)
        st.download_button("📥 Baixar Somente NF (Excel)", buf_l, "somente_nf.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    with tab_ref_only:
        df_r = df_merge[df_merge['_merge'] == 'right_only'].drop(columns=['_merge'])
        st.dataframe(df_r, use_container_width=True)
        buf_r = io.BytesIO()
        df_r.to_excel(buf_r, index=False)
        buf_r.seek(0)
        st.download_button("📥 Baixar Somente TXT (Excel)", buf_r, "somente_txt.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
