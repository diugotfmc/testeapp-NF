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
    NM12773524 -> 12.773.524 (2-3-3). Fallback: separação por milhares.
    """
    if not nm_text:
        return None
    digits = ''.join(re.findall(r'\d', nm_text))
    if len(digits) == 8:
        return f"{digits[:2]}.{digits[2:5]}.{digits[5:]}"
    # fallback (milhares)
    rev = digits[::-1]
    chunks = [rev[i:i+3] for i in range(0, len(rev), 3)]
    return '.'.join(ch[::-1] for ch in chunks[::-1]) if digits else None

def format_codigo(codigo_raw: str) -> str:
    """
    Regras de formatação do Código:
      - Se contiver 'BJ' seguido de 8 dígitos: BJ######## -> BJ xxx.yyyyy (3+5)
        Ex: AC0505BJ08000200 -> BJ 080.00200
      - Senão, se houver padrão 'BJ' + 8 dígitos em qualquer parte (sem prefixos), ex.: BJ02800629 -> BJ 028.00629
      - Senão, se contiver 'BX' + 3 dígitos: BX123 -> BX 123
      - Caso contrário, retorna o código original.
    """
    if not codigo_raw:
        return codigo_raw

    # 1) '...BJ########...'
    m_bj8 = re.search(r'BJ(\d{8})', codigo_raw)
    if m_bj8:
        num = m_bj8.group(1)
        return f"BJ {num[:3]}.{num[3:]}"

    # 2) '...BJ\d{3}\d{5}...' (ex.: BJ02800629)
    m_bj_3_5 = re.search(r'\bBJ(\d{3})(\d{5})\b', codigo_raw)
    if m_bj_3_5:
        return f"BJ {m_bj_3_5.group(1)}.{m_bj_3_5.group(2)}"

    # 3) '...BX\d{3}...' (ex.: BX156, BX156INCO)
    m_bx3 = re.search(r'BX(\d{3})', codigo_raw)
    if m_bx3:
        return f"BX {m_bx3.group(1)}"

    # Pode adicionar outras famílias de códigos aqui (ex.: 'AC', 'TC' etc.)
    return codigo_raw

def parse_item_bloco(bloco: str):
    """
    Extrai: Código (raw e formatado), IT, NM(formatado), Descrição, NCM, CFOP, UN, QTD (string PT-BR),
            V. Unitário (float), V. Total (float)
    """
    # 1) Código + "miolo" + NCM + CFOP
    m = re.search(
        r'^(?P<codigo>[A-Z0-9]{2,}\d{2,}[A-Z0-9]*)\s+(?P<miolo>.+?)\s+(?P<ncm>\d{8})\s+\d{3}\s+(?P<cfop>\d{4})',
        bloco
    )
    if not m:
        return None

    codigo_raw = m.group('codigo').strip()
    miolo = m.group('miolo').strip()  # contém "ITxxx - NMyyyyyy - Descrição"
    ncm = m.group('ncm').strip()
    cfop = m.group('cfop').strip()
    resto = bloco[m.end():]

    # 2) IT e NM
    it_match = re.search(r'\b(IT\d+)\b', miolo)
    nm_match = re.search(r'\b(NM\d+)\b', miolo)

    it_val = it_match.group(1) if it_match else None
    nm_raw = nm_match.group(1) if nm_match else None
    nm_fmt = format_nm(nm_raw) if nm_raw else None

    # 3) Descrição limpa
    descricao = miolo
    descricao = re.sub(r'\bIT\d+\b', '', descricao)
    descricao = re.sub(r'\bNM\d+\b', '', descricao)
    descricao = re.sub(r'\s*-\s*', ' - ', descricao)     # normaliza hifens
    descricao = re.sub(r'\s{2,}', ' ', descricao).strip(' -')

    # 4) QTD e UN (QTD preservada como string com vírgula)
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
            m_u = re.search('|'.join(UNITS), resto)
            if m_u:
                un = m_u.group(0)
                prev = resto[:m_u.start()]
                m_num_prev = list(NUM_RE.finditer(prev))
                if m_num_prev:
                    qtd_str = m_num_prev[-1].group(0)

    # 5) V.Unit e V.Total por consistência
    v_unit = None
    v_total = None
    if qtd_str:
        try:
            qtd_val = to_float_br(qtd_str)
            if qtd_val > 0:
                nums = [n.group(0) for n in NUM_RE.finditer(resto)]
                values = [(to_float_br(s), s) for s in nums]  # (float, texto)
                best = None
                best_score = (1e9, 0)  # (erro_abs, -total)
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
                                best = (values[i][1], values[j][1])
                if best:
                    v_unit = to_float_br(best[0])
                    v_total = to_float_br(best[1])
        except Exception:
            pass

    # 6) Código formatado
    codigo_fmt = format_codigo(codigo_raw)

    return {
        # guardo o original e o formatado
        "Código (Raw)": codigo_raw,
        "Código": codigo_fmt,
        "IT": it_val,
        "NM": nm_fmt,
        "Descrição": descricao,
        "NCM/SH": ncm,
        "CFOP": cfop,
        "UN": un,
        "QTD": qtd_str,  # preservado como texto PT-BR
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
        if item and item["Código (Raw)"]:
            itens.append(item)

    # ------------------------------
    # EXIBIÇÃO
    # ------------------------------
    if not itens:
        st.warning("⚠️ Nenhum item identificado. Pode ser necessário ajustar o padrão de leitura.")
    else:
        df_itens = pd.DataFrame(itens)

        # Ordena colunas, usando o Código já formatado como principal
        cols = ["Código", "Código (Raw)", "IT", "NM", "Descrição", "NCM/SH", "CFOP", "UN", "QTD", "V. Unitário (R$)", "V. Total (R$)"]
        df_itens = df_itens.reindex(columns=[c for c in cols if c in df_itens.columns])

        st.subheader("📋 Itens Identificados na Nota Fiscal")
        st.dataframe(df_itens, use_container_width=True)

        st.markdown("---")
        st.write("Selecione os itens carregados:")
        selecoes = []
        for i, row in df_itens.iterrows():
            # Usa o código formatado no checkbox
            label = f"{row['Código']} - {row['Descrição']}"
            if st.checkbox(label, key=f"item_{i}"):
                selecoes.append(row)

        if selecoes:
            df_sel = pd.DataFrame(selecoes)
            st.success(f"{len(df_sel)} item(ns) selecionado(s)!")
            st.dataframe(df_sel, use_container_width=True)

            # Exportar Excel (QTD como texto e NM/Código formatados)
            excel_output = io.BytesIO()
            df_sel.to_excel(excel_output, index=False)
            excel_output.seek(0)
            st.download_button(
                label="📥 Baixar Itens Selecionados (Excel)",
                data=excel_output,
                file_name="itens_selecionados.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
