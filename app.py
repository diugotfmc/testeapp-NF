import streamlit as st
import pdfplumber
import re
import pandas as pd
import io

st.set_page_config(page_title="Leitor de Nota Fiscal (Itens de Produto)", layout="wide")
st.title("📄 Leitor de Nota Fiscal em PDF")
st.subheader("📎 Envie o arquivo PDF da Nota Fiscal")

pdf_file = st.file_uploader("Selecione a nota fiscal (PDF)", type=["pdf"])

# ------------------------------
# Funções auxiliares de parsing
# ------------------------------
# Números em formato PT-BR, aceitando com/sem ponto de milhar
NUM_RE = re.compile(r'(?:\d{1,3}(?:\.\d{3})+|\d+),\d{2,4}')

# Unidades mais comuns na DANFE
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
    'NM12773524' -> '12.773.524' (2-3-3). Fallback: separação por milhares.
    """
    if not nm_text:
        return None
    digits = ''.join(re.findall(r'\d', nm_text))
    if len(digits) == 8:
        return f"{digits[:2]}.{digits[2:5]}.{digits[5:]}"
    # fallback: milhar padrão
    rev = digits[::-1]
    chunks = [rev[i:i+3] for i in range(0, len(rev), 3)]
    return '.'.join(ch[::-1] for ch in chunks[::-1]) if digits else None

def format_it(it_text: str) -> str:
    """
    Normaliza IT: 'IT200' ou 'IT 200' -> '200' (apenas dígitos).
    """
    if not it_text:
        return None
    digits = ''.join(re.findall(r'\d', it_text))
    return digits or None

def parse_item_bloco(bloco: str):
    """
    Extrai: Código, IT (apenas dígitos), NM (formatado), Descrição, NCM, CFOP, UN,
            QTD (string PT-BR), V. Unitário (float), V. Total (float).
    """
    # 1) Código + "miolo" + NCM + CFOP
    m = re.search(
        r'^(?P<codigo>[A-Z0-9]{2,}\d{2,}[A-Z0-9]*)\s+(?P<miolo>.+?)\s+(?P<ncm>\d{8})\s+\d{3}\s+(?P<cfop>\d{4})',
        bloco
    )
    if not m:
        return None

    codigo = m.group('codigo').strip()
    miolo = m.group('miolo').strip()  # ex.: "IT180 - NM12773524 - VERTEBRA"
    ncm = m.group('ncm').strip()
    cfop = m.group('cfop').strip()
    resto = bloco[m.end():]

    # 2) IT e NM a partir do "miolo"
    # aceita IT com ou sem espaço: IT180 | IT 180
    it_match = re.search(r'\bIT\s*\d+\b', miolo)
    nm_match = re.search(r'\bNM\d+\b', miolo)

    it_raw = it_match.group(0) if it_match else None
    nm_raw = nm_match.group(0) if nm_match else None

    it_val = format_it(it_raw) if it_raw else None
    nm_fmt = format_nm(nm_raw) if nm_raw else None

    # 3) Descrição: remove IT e NM e hifens excedentes
    descricao = miolo
    descricao = re.sub(r'\bIT\s*\d+\b', '', descricao)  # remove IT com/sem espaço
    descricao = re.sub(r'\bNM\d+\b', '', descricao)     # remove NM
    descricao = re.sub(r'\s*-\s*', ' - ', descricao)    # normaliza hifens
    descricao = re.sub(r'\s{2,}', ' ', descricao).strip(' -')

    # 4) QTD e UN (mantendo QTD no formato string PT-BR)
    qtd_str = None
    un = None

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
            # fallback: procura unidade e pega o número imediatamente anterior
            m_u = re.search('|'.join(UNITS), resto)
            if m_u:
                un = m_u.group(0)
                prev = resto[:m_u.start()]
                m_num_prev = list(NUM_RE.finditer(prev))
                if m_num_prev:
                    qtd_str = m_num_prev[-1].group(0)

    # 5) V.Unit e V.Total por consistência (V.Unit * QTD ≈ V.Total)
    v_unit = None
    v_total = None
    if qtd_str:
        try:
            qtd_val = to_float_br(qtd_str)
            if qtd_val > 0:
                nums = [n.group(0) for n in NUM_RE.finditer(resto)]
                values = [(to_float_br(s), s) for s in nums]  # (float, texto)
                best = None
                best_score = (1e9, 0)  # (erro_abs, -total) => prefere menor erro e total maior
                for i in range(len(values)):
                    a = values[i][0]  # candidato V.Unit
                    if a <= 0:
                        continue
                    for j in range(i + 1, len(values)):
                        b = values[j][0]  # candidato V.Total
                        if b <= 0 or b < a:
                            continue
                        err = abs(a * qtd_val - b)
                        tol = max(0.001 * max(1.0, b), 0.05)
                        if err < tol:
                            score = (err, -b)
                            if score < best_score:
                                best_score = score
                                best = (values[i][1], values[j][1])  # textos originais
                if best:
                    v_unit = to_float_br(best[0])
                    v_total = to_float_br(best[1])
        except Exception:
            pass

    return {
        "Código": codigo,
        "IT": it_val,          # << somente dígitos (ex.: '200')
        "NM": nm_fmt,          # << ex.: '12.773.524'
        "Descrição": descricao,
        "NCM/SH": ncm,
        "CFOP": cfop,
        "UN": un,
        "QTD": qtd_str,        # mantido como '1,0000'
        "V. Unitário (R$)": v_unit,
        "V. Total (R$)": v_total
    }

if pdf_file:
    # ------------------------------
    # LEITURA DO PDF
    # ------------------------------
    with pdfplumber.open(pdf_file) as pdf:
        texto_nf = ""
        for pagina in pdf.pages:
            texto_nf += (pagina.extract_text() or "") + "\n"

    # ------------------------------
    # AGRUPA BLOCOS DE ITENS
    # ------------------------------
    linhas = [l.strip() for l in texto_nf.splitlines() if l.strip()]
    blocos_itens = []
    bloco_atual = []

    padrao_inicio_item = re.compile(r"^[A-Z]{2,4}\d{2,}[A-Z0-9]*")

    for linha in linhas:
        if padrao_inicio_item.match(linha):
            if bloco_atual:
                blocos_itens.append(" ".join(bloco_atual))
                bloco_atual = []
        if bloco_atual or padrao_inicio_item.match(linha):
            bloco_atual.append(linha)

    if bloco_atual:
        blocos_itens.append(" ".join(bloco_atual))

    # ------------------------------
    # PARSE DE CADA BLOCO
    # ------------------------------
    itens = []
    for bloco in blocos_itens:
        item = parse_item_bloco(bloco)
        if item and item["Código"]:
            itens.append(item)

    # ------------------------------
    # EXIBIÇÃO
    # ------------------------------
    if not itens:
        st.warning("⚠️ Nenhum item identificado. Pode ser necessário ajustar o padrão de leitura.")
    else:
        df_itens = pd.DataFrame(itens)
        cols = ["Código", "IT", "NM", "Descrição", "NCM/SH", "CFOP", "UN", "QTD", "V. Unitário (R$)", "V. Total (R$)"]
        df_itens = df_itens.reindex(columns=[c for c in cols if c in df_itens.columns])

        st.subheader("📋 Itens Identificados na Nota Fiscal")
        st.dataframe(df_itens, use_container_width=True)

        st.markdown("---")
        st.write("Selecione os itens carregados:")
        selecoes = []
        for i, row in df_itens.iterrows():
            if st.checkbox(f"{row['Código']} - {row['Descrição']}", key=f"item_{i}"):
                selecoes.append(row)

        if selecoes:
            df_sel = pd.DataFrame(selecoes)
            st.success(f"{len(df_sel)} item(ns) selecionado(s)!")
            st.dataframe(df_sel, use_container_width=True)

            # Exportar Excel (QTD como texto e NM já formatado)
            excel_output = io.BytesIO()
            df_sel.to_excel(excel_output, index=False)
            excel_output.seek(0)
            st.download_button(
                label="📥 Baixar Itens Selecionados (Excel)",
                data=excel_output,
                file_name="itens_selecionados.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
