import streamlit as st
import pdfplumber
import re
import pandas as pd
import io

# =========================
# Configuração e Título
# =========================
st.set_page_config(page_title="Leitor e Conciliação de NF por NM", layout="wide")
st.title("📄 Leitor de Nota Fiscal (PDF) + 🔗 Conciliação por NM (TXT por linha)")
st.caption("Extrai itens da NF (PDF) e cruza com um TXT tabular (1 linha = 1 item), usando NM como chave.")

# =========================
# Uploads
# =========================
col_up1, col_up2 = st.columns(2)
with col_up1:
    nf_file = st.file_uploader("NF-e (PDF)", type=["pdf"], key="nf")
with col_up2:
    txt_file = st.file_uploader("TXT de referência (1 linha = 1 item)", type=["txt"], key="txt_ref")

# =========================
# Utilitários
# =========================
NUM_RE = re.compile(r'(?:\d{1,3}(?:\.\d{3})+|\d+),\d{2,4}')
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
    'NM12773524' -> '12.773.524' (2-3-3).
    Aceita também '12773524' ou '12.773.524'.
    """
    if not nm_text:
        return None
    digits = ''.join(re.findall(r'\d', str(nm_text)))
    if len(digits) == 8:
        return f"{digits[:2]}.{digits[2:5]}.{digits[5:]}"
    # fallback: agrupamento por milhares (mantém algo legível mesmo fora do padrão)
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
# NF (PDF) -> DataFrame
# =========================
def parse_nf_pdf(file) -> pd.DataFrame:
    if file is None:
        return pd.DataFrame()

    texto = ""
    with pdfplumber.open(file) as pdf:
        for p in pdf.pages:
            texto += (p.extract_text() or "") + "\n"

    linhas_brutas = [l for l in texto.splitlines() if l.strip()]
    padrao_inicio_item = re.compile(r"^[A-Z]{2,4}\d{2,}[A-Z0-9]*")

    blocos_itens, bloco_atual = [], []
    for linha in linhas_brutas:
        if padrao_inicio_item.match(linha.strip()):
            if bloco_atual:
                blocos_itens.append(bloco_atual)
                bloco_atual = []
        if bloco_atual or padrao_inicio_item.match(linha.strip()):
            bloco_atual.append(linha)
    if bloco_atual:
        blocos_itens.append(bloco_atual)

    itens = []
    for bloco in blocos_itens:
        bloco_text = " ".join([b.strip() for b in bloco])

        m = re.search(
            r'^(?P<codigo>[A-Z0-9]{2,}\d{2,}[A-Z0-9]*)\s+(?P<miolo>.+?)\s+(?P<ncm>\d{8})\s+\d{3}\s+(?P<cfop>\d{4})',
            bloco_text
        )
        if not m:
            continue

        codigo_raw_base = m.group('codigo').strip()
        miolo = m.group('miolo').strip()
        ncm = m.group('ncm').strip()
        cfop = m.group('cfop').strip()
        resto = bloco_text[m.end():]

        it_match = re.search(r'\bIT\s*\d+\b', miolo)
        nm_match = re.search(r'\bNM\d+\b', miolo)
        it_val = format_it(it_match.group(0)) if it_match else None
        nm_fmt = format_nm(nm_match.group(0)) if nm_match else None

        descricao = miolo
        descricao = re.sub(r'\bIT\s*\d+\b', '', descricao)
        descricao = re.sub(r'\bNM\d+\b', '', descricao)
        descricao = re.sub(r'\s*-\s*', ' - ', descricao)
        descricao = re.sub(r'\s{2,}', ' ', descricao).strip(' -')

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

        v_unit = v_total = None
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

        codigo_raw = codigo_raw_base + (sufixo_clean or "")
        codigo_fmt_base = format_codigo(codigo_raw_base)
        codigo_fmt = codigo_fmt_base + (f"\n{sufixo_clean}" if sufixo_clean else "")

        itens.append({
            "Código (Raw Base)": codigo_raw_base,
            "Código (Raw)": codigo_raw,
            "Código": codigo_fmt,
            "IT": it_val,
            "NM": nm_fmt,
            "Descrição (NF)": descricao,
            "NCM/SH": ncm,
            "CFOP": cfop,
            "UN (NF)": un,
            "QTD (NF)": qtd_str,
            "V. Unitário (R$)": v_unit,
            "V. Total (R$)": v_total
        })

    return pd.DataFrame(itens)

# =========================
# TXT por linha -> DataFrame (auto-detecta delimitador e cabeçalho)
# =========================
def detect_delimiter(sample: str, candidates=(';', '\t', '|', ',')) -> str | None:
    """
    Heurística: escolhe o delimitador que produz o maior 'modo' de contagem de colunas
    nas primeiras linhas não vazias, privilegiando consistência.
    """
    lines = [ln for ln in sample.splitlines() if ln.strip()]
    lines = lines[:50]  # amostra
    best = (None, 0, 0)  # (sep, mode_cols, valid_lines)
    for sep in candidates:
        counts = []
        for ln in lines:
            parts = ln.split(sep)
            counts.append(len(parts))
        if not counts:
            continue
        # modo (tamanho de coluna mais frequente)
        from collections import Counter
        c = Counter(counts)
        mode_cols, freq = max(c.items(), key=lambda x: (x[1], x[0]))
        # guardamos também quantas linhas têm exatamente mode_cols
        valid = sum(1 for k in counts if k == mode_cols)
        score = (mode_cols, valid)
        if score > (best[1], best[2]):
            best = (sep, mode_cols, valid)
    return best[0]

def normalize_ref_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Normaliza nomes de colunas do TXT para o padrão:
    ['NM','Texto breve material (REF)','QTD (REF)','UM (REF)','Centro (REF)','Elemento PEP (REF)']
    Aceita variações de header; se não houver header, assume ordem das 6 colunas.
    """
    # Se não há nomes (ou nomes são 0..N-1), tratamos como sem header
    has_header = not all(isinstance(c, int) for c in df.columns)

    # Mapa de nomes -> padrão
    def norm(s): return re.sub(r'[^a-z0-9]+', '', str(s).strip().lower())
    target = {
        'nm': 'NM',
        'material': 'NM',
        'texto_breve_material': 'Texto breve material (REF)',
        'textobrevematerial': 'Texto breve material (REF)',
        'descricaomaterial': 'Texto breve material (REF)',
        'qtd': 'QTD (REF)',
        'qtd.': 'QTD (REF)',
        'quantidade': 'QTD (REF)',
        'um': 'UM (REF)',
        'umr': 'UM (REF)',
        'unidade': 'UM (REF)',
        'cen.': 'Centro (REF)',
        'cen': 'Centro (REF)',
        'centro': 'Centro (REF)',
        'elementopep': 'Elemento PEP (REF)',
        'pep': 'Elemento PEP (REF)',
    }

    if has_header:
        newcols = []
        for c in df.columns:
            key = norm(c)
            newcols.append(target.get(key, str(c).strip()))
        df.columns = newcols

    # Se ainda não temos todas as 6 colunas, e a contagem bate com 6, assume ordem fixa
    expected = ['NM', 'Texto breve material (REF)', 'QTD (REF)', 'UM (REF)', 'Centro (REF)', 'Elemento PEP (REF)']
    if not has_header and df.shape[1] == 6:
        df.columns = expected

    # Completa/renomeia o que faltou
    rename_map = {}
    for c in df.columns:
        key = norm(c)
        if key in target:
            rename_map[c] = target[key]
    if rename_map:
        df = df.rename(columns=rename_map)

    # Se após o processo ainda estiver faltando algo essencial, falha (será tratado no chamador)
    return df

def parse_ref_txt_table(file) -> pd.DataFrame:
    """
    Lê um TXT tabular (1 linha = 1 item).
    - Auto-detecta delimitador (entre ;, \\t, |, ,).
    - Tenta header; se não houver, assume 6 colunas na ordem: NM, Descrição, Qtd, UM, Centro, PEP.
    - Normaliza NM (12.773.524), mantém QTD como string.
    """
    if file is None:
        return pd.DataFrame()

    raw = file.read()
    # detecta encoding
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

    # detecta delimitador
    sep = detect_delimiter(text)
    if not sep:
        # Se não detectar, tenta CSV padrão como último recurso
        sep = ','

    # Tenta com header
    try:
        df = pd.read_csv(io.StringIO(text), sep=sep, dtype=str, keep_default_na=False)
    except Exception:
        # fallback: tenta sem header
        df = pd.read_csv(io.StringIO(text), sep=sep, dtype=str, header=None, keep_default_na=False)

    df = df.applymap(lambda x: x.strip() if isinstance(x, str) else x)
    df = normalize_ref_columns(df)

    # Se colunas essenciais não existem, tentar sem header com 6 colunas
    essentials = {'NM', 'Texto breve material (REF)', 'QTD (REF)', 'UM (REF)', 'Centro (REF)', 'Elemento PEP (REF)'}
    if not essentials.issubset(set(df.columns)) and df.shape[1] == 6:
        df.columns = ['NM', 'Texto breve material (REF)', 'QTD (REF)', 'UM (REF)', 'Centro (REF)', 'Elemento PEP (REF)']

    # Valida novamente
    if not essentials.issubset(set(df.columns)):
        st.error("Não foi possível identificar as colunas do TXT. Verifique o cabeçalho e o delimitador (;, TAB, | ou ,).")
        return pd.DataFrame()

    # Normaliza NM e mantém QTD textual
    df['NM'] = df['NM'].map(format_nm)
    # Remove linhas sem NM válida
    df = df[df['NM'].notna() & (df['NM'].str.len() > 0)]

    # Opcional: normaliza UM para maiúsculas
    df['UM (REF)'] = df['UM (REF)'].str.upper()

    return df.reset_index(drop=True)

# =========================
# Execução principal
# =========================
df_nf  = parse_nf_pdf(nf_file) if nf_file else pd.DataFrame()
df_ref = parse_ref_txt_table(txt_file) if txt_file else pd.DataFrame()

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
with st.expander("Linhas do TXT de referência (colunas)", expanded=False):
    if not df_ref.empty:
        st.dataframe(df_ref, use_container_width=True)
        buf_ref = io.BytesIO()
        df_ref.to_excel(buf_ref, index=False)
        buf_ref.seek(0)
        st.download_button("📥 Baixar Referência (Excel)", buf_ref, "referencia_itens.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    else:
        st.info("Envie o TXT tabular (1 linha = 1 item) para ver as linhas extraídas.")

# Conciliação por NM
st.markdown("---")
st.subheader("📊 Painel de Conciliação por NM")

if df_nf.empty or df_ref.empty:
    st.warning("Envie **os dois arquivos** (NF e TXT) para gerar a conciliação.")
else:
    df_merge = pd.merge(
        df_nf, df_ref, on="NM", how="outer", indicator=True, suffixes=(" (NF)", " (REF)")
    )

    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric("Conciliados (NM em ambos)", int((df_merge['_merge'] == 'both').sum()))
    with c2:
        st.metric("Somente na NF", int((df_merge['_merge'] == 'left_only').sum()))
    with c3:
        st.metric("Somente no TXT", int((df_merge['_merge'] == 'right_only').sum()))

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
