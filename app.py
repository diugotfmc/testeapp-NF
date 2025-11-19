import streamlit as st
import pdfplumber
import re
import pandas as pd
import io

# =========================
# Configuração e Título
# =========================
st.set_page_config(page_title="Leitor e Conciliação de NF por NM", layout="wide")
st.title("📄 Leitor de Nota Fiscal (PDF) + 🔗 Conciliação por NM (TXT | pipe) + 🧩 Máscaras por item")
st.caption("Extrai itens da NF e relaciona com um TXT delimitado por '|', usando NM como chave. Gera máscaras editáveis por item.")

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
    digits = ''.join(re.findall(r'\d', str(nm_text)))
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
      - '...BJ(\d{3})(\d{5})...' -> 'BJ 028.00629'
      - '...BX\d{3}...' -> 'BX 156'
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
    sep_re = re.compile(r'^[\-\=\*_\\\/\s]{5,}$')
    lines = [ln for ln in lines if ln and not sep_re.match(ln)]

    rows = []
    header_seen = False

    for ln in lines:
        if '|' not in ln:
            continue

        ln_clean = ln.strip().strip('|').strip()
        parts = [p.strip() for p in re.split(r'\s*\|\s*', ln_clean)]

        # ignora header
        if not header_seen and len(parts) >= 2:
            if parts[0].lower().startswith('material') and 'texto' in parts[1].lower():
                header_seen = True
                continue

        if len(parts) < 6:
            continue

        material, desc, qtd, um, centro, pep = parts[:6]

        # valida NM
        m_ok = re.match(r'^\d{2}\.\d{3}\.\d{3}$', material) or re.match(r'^\d{8}$', re.sub(r'\D', '', material))
        if not m_ok:
            continue
        if not qtd or not um or not centro or not pep:
            continue

        rows.append({
            "NM": format_nm(material),
            "Texto breve material (REF)": desc,   # MAIÚSCULAS serão aplicadas depois (Opção 2)
            "QTD (REF)": qtd,                     # mantém como texto (ex.: '100,000')
            "UM (REF)": um,                       # aceita UM/UMR
            "Centro (REF)": centro,
            "Elemento PEP (REF)": pep
        })

    return pd.DataFrame(rows)

# =========================
# Execução principal: NF + REF
# =========================
df_nf  = parse_nf_pdf(nf_file) if nf_file else pd.DataFrame()
df_ref = parse_ref_txt_pipe(txt_file) if txt_file else pd.DataFrame()

# Opção 2: padroniza 'Texto breve material (REF)' em MAIÚSCULAS após montar df_ref
if not df_ref.empty and "Texto breve material (REF)" in df_ref.columns:
    df_ref["Texto breve material (REF)"] = df_ref["Texto breve material (REF)"].astype(str).str.upper()

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

        # =========================
        # 🧩 Máscaras por item (conciliados por NM)
        # =========================
        st.markdown("### 🧩 Máscaras por item (conciliados por NM)")
        st.caption("Edite os campos e copie/baixe cada máscara individualmente.")

        # Itera por cada item conciliado e renderiza uma máscara editável
        for idx, row in df_both.reset_index(drop=True).iterrows():
            with st.expander(f"Máscara do item #{idx+1} — NM: {row.get('NM','')}", expanded=False):
                # Campos vindos do merge (com defaults)
                texto_breve_default = str(row.get("Texto breve material (REF", row.get("Texto breve material (REF)",""))).upper()
                nm_default          = str(row.get("NM",""))
                centro_default      = str(row.get("Centro (REF)",""))
                codigo_default      = str(row.get("Código","")).replace("\n", " ")  # evita quebra no desenho
                qtd_default         = str(row.get("QTD (NF)",""))
                un_default          = str(row.get("UN (NF)",""))
                pep_default         = str(row.get("Elemento PEP (REF)",""))

                # Inputs editáveis (pré-preenchidos)
                texto_breve = st.text_input("1 & 3) Texto breve material (REF)", value=texto_breve_default, key=f"tb_{idx}")
                doc_chegada = st.text_input("2) DOCUMENTO DE CHEGADA", value="", key=f"dc_{idx}")
                nota_petro  = st.text_input("4) NOTA PETROBRAS", value="", key=f"np_{idx}")
                nota_base   = st.text_input("5) NOTA DE SAÍDA BASE", value="", key=f"nb_{idx}")
                nota_pet_sa = st.text_input("6) NOTA DE SAÍDA PETROBRAS", value="", key=f"nps_{idx}")
                csp         = st.text_input("7) N° DE CSP", value="", key=f"csp_{idx}")
                projeto     = st.text_input("8) PROJETO", value="", key=f"prj_{idx}")
                rt          = st.text_input("9) RT", value="N/A", key=f"rt_{idx}")
                invent      = st.text_input("10) MATERIAL INVENTARIADO", value="SIM", key=f"inv_{idx}")
                ferram      = st.text_input("11) FERRAMENTA", value="NÃO", key=f"fer_{idx}")
                tag_bcds    = st.text_input("12) TAG BCDS DA FERRAMENTA", value="N/A", key=f"tag_{idx}")
                nm_val      = st.text_input("13) NM", value=nm_default, key=f"nm_{idx}")
                centro_val  = st.text_input("14) CENTRO (REF)", value=centro_default, key=f"cen_{idx}")
                desenho     = st.text_input("15) DESENHO (Código NF)", value=codigo_default, key=f"des_{idx}")
                imob        = st.text_input("16) IMOBILIZADO", value="N/A", key=f"imob_{idx}")
                qtdnf       = st.text_input("17) QUANTIDADE (QTD NF)", value=qtd_default, key=f"qtd_{idx}")
                unnf        = st.text_input("17) UN (NF)", value=un_default, key=f"un_{idx}")
                caixa       = st.text_input("18) N° DE CAIXA", value="", key=f"cx_{idx}")
                pep_val     = st.text_input("19) DIAGRAMA DE REDE / ELEMENTO PEP (REF)", value=pep_default, key=f"pep_{idx}")
                ptm         = st.text_input("20) PTM", value="N/A", key=f"ptm_{idx}")
                dsm         = st.text_input("21) DSM", value="N/A", key=f"dsm_{idx}")
                nt_mar      = st.text_input("22) NOTA DE TRANSF. MAR", value="N/A", key=f"ntm_{idx}")
                protocolo   = st.text_input("23) PROTOCOLO", value="N/A", key=f"prot_{idx}")

                # Monta a máscara (com aspas onde solicitado)
                mask_text = (
f'1- "{texto_breve}"\n'
f'2-DOCUMENTO DE CHEGADA: {doc_chegada}\n'
f'3-MATERIAL:   "{texto_breve}"\n'
f'4-NOTA PETROBRAS: {nota_petro}\n'
f'5-NOTA DE SAÍDA BASE: {nota_base}\n'
f'6-NOTA DE SAÍDA PETROBRAS: {nota_pet_sa}\n'
f'7-N° DE CSP: {csp}\n'
f'8-PROJETO: {projeto}\n'
f'9-RT: {rt}\n'
f'10-MATERIAL INVENTARIADO: {invent}\n'
f'11-FERRAMENTA: {ferram}\n'
f'12-TAG BCDS DA FERRAMENTA: {tag_bcds}\n'
f'13-NM:  "{nm_val}"\n'
f'14-CENTRO:  "{centro_val}"\n'
f'15-DESENHO:  "{desenho}"\n'
f'16-IMOBILIZADO: {imob}\n'
f'17-QUANTIDADE:  "{qtdnf}""{unnf}"\n'
f'18-N° DE CAIXA: {caixa}\n'
f'19-DIAGRAMA DE REDE / ELEMENTO PEP:"{pep_val}"\n'
f'20-PTM: {ptm}\n'
f'21-DSM: {dsm}\n'
f'22-NOTA DE TRANSF. MAR: {nt_mar}\n'
f'23-PROTOCOLO: {protocolo}'
                )

                # Área editável e botões de copiar/baixar
                st.text_area("📋 Máscara gerada (editável)", value=mask_text, height=300, key=f"mask_area_{idx}")

                # Botão de copiar (JS simples) + fallback de download
                html_copy = f"""
<div>
  <button onclick="navigator.clipboard.writeText(document.getElementById('mask_txt_{idx}').value)"
          style="margin-right:8px;">📋 Copiar máscara #{idx+1}</button>
  <textarea id="mask_txt_{idx}" style="position:absolute; left:-10000px;">{mask_text}</textarea>
</div>
"""
                st.markdown(html_copy, unsafe_allow_html=True)

                buf_item = io.BytesIO(mask_text.encode("utf-8"))
                st.download_button(
                    label=f"⬇️ Baixar máscara #{idx+1} (.txt)",
                    data=buf_item,
                    file_name=f"mascara_item_{idx+1}.txt",
                    mime="text/plain"
                )

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
