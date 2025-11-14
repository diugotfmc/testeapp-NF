import streamlit as st
import pdfplumber
import re
import pandas as pd
import io

# =========================
# Configuração e Título
# =========================
st.set_page_config(page_title="Leitor e Conciliação de NF por NM", layout="wide")
st.title("📄 Leitor de Nota Fiscal em PDF + 🔗 Conciliação por NM (PDF de referência)")
st.caption("Extrai itens da NF e relaciona com um PDF em colunas, usando NM como chave.")

# =========================
# Uploads
# =========================
col_up1, col_up2 = st.columns(2)
with col_up1:
    nf_file = st.file_uploader("NF-e (PDF)", type=["pdf"], key="nf")
with col_up2:
    ref_file = st.file_uploader("PDF de referência (colunas por item)", type=["pdf"], key="ref")

# =========================
# Utilitários
# =========================
NUM_RE = re.compile(r'(?:\d{1,3}(?:\.\d{3})+|\d+),\d{2,4}')
UNITS = ['UN', 'KG', 'PC', 'CJ', 'KIT', 'PAR', 'M', 'L', 'LT', 'CX']
UNIT_QTD_RE = re.compile(
    r'(?P<qtd>(?:\d{1,3}(?:\.\d{3})+|\d+)(?:,\d{2,4})?)\s*(?P<un>' + '|'.join(UNITS) + r')\b'
)
UNIT_FIRST_RE = re.compile(
    r'(?P<un>' + '|'.join(UNITS) + r')\s*(?P<qtd>(?:\d{1,3}(?:\.\d{3})+|\d+)(?:,\d{2,4})?)\b'
)

def to_float_br(s: str) -> float:
    return float(s.replace('.', '').replace(',', '.'))

def format_nm(nm_text: str) -> str:
    """
    'NM12773524' -> '12.773.524' (2-3-3).
    Para entradas já no formato '12.773.524' (do PDF de referência), re-normaliza para garantir padrão.
    """
    if not nm_text:
        return None
    # Extrai dígitos
    digits = ''.join(re.findall(r'\d', nm_text))
    # 8 dígitos -> 2-3-3
    if len(digits) == 8:
        return f"{digits[:2]}.{digits[2:5]}.{digits[5:]}"
    # Se já veio com pontos (ex.: 12.773.524), normaliza via dígitos
    if len(digits) == 8:
        return f"{digits[:2]}.{digits[2:5]}.{digits[5:]}"
    # Fallback (agrupamento por milhar)
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
    Regras de formatação do Código:
      - Se contiver 'BJ' + 8 dígitos: AC0505BJ08000200 -> BJ 080.00200
      - Se contiver 'BJ' + (3+5): BJ02800629 -> BJ 028.00629
      - Se contiver 'BX' + 3 dígitos: BX156 -> BX 156
      - Caso não caiba nas regras, retorna original.
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
# Parser da NF (itens)
# =========================
def parse_nf_pdf(file) -> pd.DataFrame:
    texto = ""
    with pdfplumber.open(file) as pdf:
        for p in pdf.pages:
            texto += (p.extract_text() or "") + "\n"

    linhas = [l.strip() for l in texto.splitlines() if l.strip()]
    padrao_inicio_item = re.compile(r"^[A-Z]{2,4}\d{2,}[A-Z0-9]*")

    blocos_itens, bloco_atual = [], []
    for linha in linhas:
        if padrao_inicio_item.match(linha):
            if bloco_atual:
                blocos_itens.append(" ".join(bloco_atual))
                bloco_atual = []
        if bloco_atual or padrao_inicio_item.match(linha):
            bloco_atual.append(linha)
    if bloco_atual:
        blocos_itens.append(" ".join(bloco_atual))

    itens = []
    for bloco in blocos_itens:
        m = re.search(
            r'^(?P<codigo>[A-Z0-9]{2,}\d{2,}[A-Z0-9]*)\s+(?P<miolo>.+?)\s+(?P<ncm>\d{8})\s+\d{3}\s+(?P<cfop>\d{4})',
            bloco
        )
        if not m:
            continue

        codigo_raw = m.group('codigo').strip()
        miolo = m.group('miolo').strip()   # "ITxxx - NMyyyyyy - Descrição"
        ncm = m.group('ncm').strip()
        cfop = m.group('cfop').strip()
        resto = bloco[m.end():]

        it_match = re.search(r'\bIT\s*\d+\b', miolo)
        nm_match = re.search(r'\bNM\d+\b', miolo)
        it_val = format_it(it_match.group(0)) if it_match else None
        nm_fmt = format_nm(nm_match.group(0)) if nm_match else None

        descricao = miolo
        descricao = re.sub(r'\bIT\s*\d+\b', '', descricao)
        descricao = re.sub(r'\bNM\d+\b', '', descricao)
        descricao = re.sub(r'\s*-\s*', ' - ', descricao)
        descricao = re.sub(r'\s{2,}', ' ', descricao).strip(' -')

        # QTD (string) e UN
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

        # VUnit/VTotal por consistência
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
                        for j in range(i+1, len(values)):
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

        itens.append({
            "Código (Raw)": codigo_raw,
            "Código": format_codigo(codigo_raw),
            "IT": it_val,
            "NM": nm_fmt,  # chave de conciliação
            "Descrição (NF)": descricao,
            "NCM/SH": ncm,
            "CFOP": cfop,
            "UN (NF)": un,
            "QTD (NF)": qtd_str,  # manter como texto (ex.: 1,0000)
            "V. Unitário (R$)": v_unit,
            "V. Total (R$)": v_total
        })

    return pd.DataFrame(itens)

# =========================
# Parser do PDF de referência (colunas)
# =========================
def parse_ref_pdf(file) -> pd.DataFrame:
    """
    Extrai linhas começando com NM no formato 12.773.524 e quebra em:
      NM, Texto breve material, Qtd (REF), UM (REF), Centro, Elemento PEP
    Ignora linhas que não seguem o padrão.
    """
    texto = ""
    with pdfplumber.open(file) as pdf:
        for p in pdf.pages:
            texto += (p.extract_text() or "") + "\n"

    linhas = [l.strip() for l in texto.splitlines() if l.strip()]
    rows = []
    for ln in linhas:
        # Começa com NM já pontuado (12.773.524 ...)
        m = re.match(r'^(?P<nm>\d{2}\.\d{3}\.\d{3})\s+(?P<resto>.+)$', ln)
        if not m:
            continue

        nm_fmt = format_nm(m.group('nm'))  # normaliza
        tail = m.group('resto')

        # Captura Qtd + UM + Centro + PEP (ao final da linha)
        # Ex.: "... 2 UN 4419 JV-3A26-17-465-3"
        m_tail = re.search(
            r'(?P<qtd>(?:\d{1,3}(?:\.\d{3})*|\d+)(?:,\d{3})?)\s+'
            r'(?P<um>' + '|'.join(UNITS) + r')\s+'
            r'(?P<centro>\d{3,5})\s+'
            r'(?P<pep>[A-Z0-9\-\\/]+)\s*$',
            tail
        )
        if not m_tail:
            # se não casar essa terminação, pula a linha (ou poderíamos logar)
            continue

        qtd_ref = m_tail.group('qtd')
        um_ref = m_tail.group('um')
        centro = m_tail.group('centro')
        pep = m_tail.group('pep')

        # Descrição (REF) é o que sobrou antes de Qtd/UM/Centro/PEP
        desc_ref = tail[:m_tail.start()].strip()

        rows.append({
            "NM": nm_fmt,
            "Texto breve material (REF)": desc_ref,
            "QTD (REF)": qtd_ref,        # mantido no padrão original do PDF
            "UM (REF)": um_ref,
            "Centro (REF)": centro,
            "Elemento PEP (REF)": pep,
        })

    return pd.DataFrame(rows)

# =========================
# Execução principal
# =========================
df_nf = parse_nf_pdf(nf_file) if nf_file else pd.DataFrame()
df_ref = parse_ref_pdf(ref_file) if ref_file else pd.DataFrame()

# Painel NF isolado (opcional)
with st.expander("Itens extraídos da NF", expanded=False):
    if not df_nf.empty:
        st.dataframe(df_nf, use_container_width=True)
        buf_nf = io.BytesIO()
        df_nf.to_excel(buf_nf, index=False)
        buf_nf.seek(0)
        st.download_button("📥 Baixar NF (Excel)", buf_nf, "nf_itens.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    else:
        st.info("Envie uma NF-e em PDF para ver os itens extraídos.")

# Painel REF isolado (opcional)
with st.expander("Linhas do PDF de referência (colunas)", expanded=False):
    if not df_ref.empty:
        st.dataframe(df_ref, use_container_width=True)
        buf_ref = io.BytesIO()
        df_ref.to_excel(buf_ref, index=False)
        buf_ref.seek(0)
        st.download_button("📥 Baixar Referência (Excel)", buf_ref, "referencia_itens.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    else:
        st.info("Envie o PDF em colunas para ver as linhas extraídas.")

# =========================
# Conciliação por NM
# =========================
st.markdown("---")
st.subheader("📊 Painel de Conciliação por NM")

if df_nf.empty or df_ref.empty:
    st.warning("Envie **os dois PDFs** (NF-e e Referência) para gerar a conciliação.")
else:
    # Merge por NM
    df_merge = pd.merge(
        df_nf, df_ref, on="NM", how="outer", indicator=True, suffixes=(" (NF)", " (REF)")
    )

    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric("Conciliados (NM em ambos)", int((df_merge['_merge'] == 'both').sum()))
    with c2:
        st.metric("Somente na NF", int((df_merge['_merge'] == 'left_only').sum()))
    with c3:
        st.metric("Somente no PDF de referência", int((df_merge['_merge'] == 'right_only').sum()))

    # Abas com as três visões
    tab_both, tab_nf_only, tab_ref_only = st.tabs(["✔️ Conciliados", "📄 Somente NF", "📑 Somente REF"])

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
        st.download_button("📥 Baixar Somente Referência (Excel)", buf_r, "somente_referencia.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
