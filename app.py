import streamlit as st
import pdfplumber
import re
import pandas as pd
import io
st.set_page_config(page_title="Leitor de Nota Fiscal (Itens de Produto)", layout="wide")
st.title("📄 Leitor de Nota Fiscal em PDF")
st.subheader("📎 Envie o arquivo PDF da Nota Fiscal")
pdf_file = st.file_uploader("Selecione a nota fiscal (PDF)", type=["pdf"])
if pdf_file:
   with pdfplumber.open(pdf_file) as pdf:
       texto_nf = ""
       for pagina in pdf.pages:
           texto_nf += pagina.extract_text() + "\n"
   # ===============================
   # PRÉ-PROCESSAMENTO DAS LINHAS
   # ===============================
   linhas = texto_nf.splitlines()
   blocos_itens = []
   bloco_atual = []
   # Regex que identifica início de um item
   padrao_inicio_item = re.compile(r"^[A-Z]{2,4}\d{2,}")
   for linha in linhas:
       linha = linha.strip()
       if not linha:
           continue
       # Se a linha começa com código (ex: ACU205..., BX156, TC450 etc.)
       if padrao_inicio_item.match(linha):
           # Salva o bloco anterior
           if bloco_atual:
               blocos_itens.append(" ".join(bloco_atual))
               bloco_atual = []
       bloco_atual.append(linha)
   # Adiciona o último bloco
   if bloco_atual:
       blocos_itens.append(" ".join(bloco_atual))
   # ===============================
   # REGEX PARA EXTRAIR OS CAMPOS
   # ===============================
   padrao_item = re.compile(
       r"^([A-Z]{2,4}\d{2,}[A-Z0-9]*)\s+(.+?)\s+(\d{8})\s+(\d{3,4})\s+([A-Z]{1,3})\s+([\d,\.]+)\s+([\d,\.]+)\s+([\d,\.]+)",
       re.IGNORECASE
   )
   itens = []
   for bloco in blocos_itens:
       match = padrao_item.search(bloco)
       if match:
           codigo = match.group(1).strip()
           descricao = match.group(2).strip()
           ncm = match.group(3)
           cfop = match.group(4)
           unidade = match.group(5)
           qtd = match.group(6).replace(".", "").replace(",", ".")
           v_unit = match.group(7).replace(".", "").replace(",", ".")
           v_total = match.group(8).replace(".", "").replace(",", ".")
           itens.append({
               "Código": codigo,
               "Descrição": descricao,
               "NCM/SH": ncm,
               "CFOP": cfop,
               "UN": unidade,
               "QTD": float(qtd),
               "V. Unitário (R$)": float(v_unit),
               "V. Total (R$)": float(v_total)
           })
   # ===============================
   # EXIBIÇÃO
   # ===============================
   if not itens:
       st.warning("⚠️ Nenhum item identificado. Pode ser necessário ajustar o padrão de leitura.")
   else:
       df_itens = pd.DataFrame(itens)
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
           # Exportar Excel
           excel_output = io.BytesIO()
           df_sel.to_excel(excel_output, index=False)
           excel_output.seek(0)
           st.download_button(
               label="📥 Baixar Itens Selecionados (Excel)",
               data=excel_output,
               file_name="itens_selecionados.xlsx",
               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
           )
