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

def parse_item_bloco(bloco: str):
    """
    Extrai: Código, IT, NM, Descrição, NCM, CFOP, UN, QTD (string PT-BR),
            V. Unitário (float), V. Total (float)
    a partir de um bloco de texto (uma ou mais linhas do item).
    """
    # 1) Código + "miolo" + NCM + CFOP
    m = re.search(
        r'^(?P<codigo>[A-Z0-9]{2,}\d{2,}[A-Z0-9]*)\s+(?P<miolo>.+?)\s+(?P<ncm>\d{8})\s+\d{3}\s+(?P<cfop>\d{4})',
        bloco
    )
    if not m:
        return None

    codigo = m.group('codigo').strip()
    miolo = m.group('miolo').strip()  # contém "ITxxx - NMyyyyyy - Descrição"
    ncm = m.group('ncm').strip()
    cfop = m.group('cfop').strip()
    resto = bloco[m.end():]

    # 2) IT e NM a partir do "miolo"
    it_match = re.search(r'\b(IT\d+)\b', miolo)
    nm_match = re.search(r'\b(NM\d+)\b', miolo)

    it_val = it_match.group(1) if it_match else None
    nm_val = nm_match.group(1) if nm_match else None

    # 3) Descrição: remove ITxxx e NMxxxxxx e hifens excedentes
    descricao = miolo
    descricao = re.sub(r'\bIT\d+\b', '', descricao)
    descricao = re.sub(r'\bNM\d+\b', '', descricao)
    descricao = re.sub(r'\s*-\s*', ' - ', descricao)     # normaliza os hifens
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
                        tol = max(0.001 * max(1.0, b), 0.05)  # tolerância
                        if err < tol:
                            score = (err, -b)
                            if score < best_score:
                                best_score = score
                                best = (values[i][1], values[j][1])  # guarda os textos originais
                if best:
                    v_unit = to_float_br(best[0])
                    v_total = to_float_br(best[1])
        except Exception:
            pass

    return {
        "Código": codigo,
        "IT": it_val,
        "NM": nm_val,
        "Descrição": descricao,
        "NCM/SH": ncm,
        "CFOP": cfop,
        "UN": un,
        # >>> mantém QTD como string com vírgula e 4 casas:
        "QTD": qtd_str,
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

        # Ordena colunas para destacar IT/NM
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

            # Exportar Excel (mantendo QTD como texto)
            # Dica: para garantir que o Excel não converta, podemos deixar como texto mesmo.
            excel_output = io.BytesIO()
            df_sel.to_excel(excel_output, index=False)
            excel_output.seek(0)
            st.download_button(
                label="📥 Baixar Itens Selecionados (Excel)",
                data=excel_output,
                file_name="itens_selecionados.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
