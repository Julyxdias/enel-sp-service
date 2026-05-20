"""
ocr_parser.py
Extrai dados estruturados do PDF de conta da Enel SP (Eletropaulo) usando pdfplumber.

Campos retornados mapeiam 1-para-1 com DadosOcr do enel_client.py.
Calibrado com PDF real da Enel SP (layout Eletropaulo Metropolitana).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

import pdfplumber

log = logging.getLogger(__name__)

_MESES = {
    "JAN": 1, "FEV": 2, "MAR": 3, "ABR": 4, "MAI": 5, "JUN": 6,
    "JUL": 7, "AGO": 8, "SET": 9, "OUT": 10, "NOV": 11, "DEZ": 12,
}


# ── Modelos ────────────────────────────────────────────────────────────────────

@dataclass
class ItemFatura:
    descricao: str
    valor: Decimal

    def to_dict(self):
        return {"descricao": self.descricao, "valor": str(self.valor)}


@dataclass
class DadosOcr:
    cliente: Optional[str] = None
    distribuidora: Optional[str] = None
    nota_fiscal: Optional[str] = None
    aviso: Optional[str] = None
    endereco: Optional[str] = None
    codigo_barras: Optional[str] = None
    classe: Optional[str] = None
    subclasse: Optional[str] = None
    grupo: Optional[str] = None
    subgrupo: Optional[str] = None
    ref_mes: Optional[int] = None
    ref_ano: Optional[int] = None
    emissao_data: Optional[str] = None
    data_apresentacao: Optional[str] = None
    leitura_anterior_data: Optional[str] = None
    leitura_data: Optional[str] = None
    leitura_proxima_data: Optional[str] = None
    energia: Optional[Decimal] = None
    valor: Optional[Decimal] = None
    vencimento: Optional[str] = None
    preco_te: Optional[Decimal] = None
    preco_tusd: Optional[Decimal] = None
    normalizado_preco_te: Optional[Decimal] = None
    normalizado_preco_tusd: Optional[Decimal] = None
    normalizado_valor: Optional[Decimal] = None
    itens_fatura: list[ItemFatura] = field(default_factory=list)

    def to_dict(self) -> dict:
        def _s(v):
            if isinstance(v, Decimal): return str(v)
            if isinstance(v, list):    return [_s(x) for x in v]
            if hasattr(v, "to_dict"): return v.to_dict()
            return v
        return {k: _s(v) for k, v in self.__dict__.items()}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _dec(raw: str | None) -> Optional[Decimal]:
    """'1.234,56' → Decimal('1234.56')"""
    if not raw:
        return None
    try:
        return Decimal(raw.strip().replace(".", "").replace(",", "."))
    except InvalidOperation:
        return None


def _find(pat: str, text: str, group: int = 1, flags: int = re.IGNORECASE) -> Optional[str]:
    m = re.search(pat, text, flags)
    return m.group(group).strip() if m else None


def _parse_ref(raw: str | None) -> tuple[Optional[int], Optional[int]]:
    """'MAI/26' ou '05/2026' → (5, 2026)"""
    if not raw:
        return None, None
    # MAI/26 ou MAI/2026
    m = re.match(r"([A-Za-z]{3})[/\-](\d{2,4})", raw.strip())
    if m:
        mes = _MESES.get(m.group(1).upper())
        ano = int(m.group(2))
        if ano < 100:
            ano += 2000
        return mes, ano
    # 05/2026
    m2 = re.match(r"(\d{2})[/\-](\d{4})", raw.strip())
    if m2:
        return int(m2.group(1)), int(m2.group(2))
    return None, None


def _join_words(words: list[dict], y_tol: int = 2) -> str:
    """
    Reconstrói texto a partir de words com kerning explodido (letras separadas).
    Agrupa por Y e concatena — ex: ['A','T','U','A','L','I','Z','A','Ç','ÃO'] → 'ATUALIZAÇÃO'
    """
    if not words:
        return ""
    sorted_w = sorted(words, key=lambda w: (round(w["top"] / y_tol), w["x0"]))
    parts: list[str] = []
    last_x1 = None
    for w in sorted_w:
        if last_x1 is not None and w["x0"] - last_x1 > 8:
            parts.append(" ")
        parts.append(w["text"])
        last_x1 = w["x1"]
    return "".join(parts)


# ── Extração de itens via coordenadas XY ──────────────────────────────────────

def _primeiro_valor(val_raw: str) -> Optional[Decimal]:
    """
    O PDF Enel coloca duas colunas coladas: valor + PIS/COFINS.
    Ex: '154,187,95' → pega apenas o primeiro valor '154,18'.
    """
    m = re.match(r"(\d[\d.]*,\d{2})", val_raw)
    return _dec(m.group(1)) if m else _dec(val_raw)


def _limpar_desc(desc: str) -> str:
    """Remove sufixos numéricos colados à descrição. Ex: 'TUSD KWH 274,000' → 'TUSD'."""
    # Remove 'KWH XXXX' no final
    desc = re.sub(r"\s+KWH\s+[\d.,]+\s*$", "", desc, flags=re.IGNORECASE)
    # Remove número flutuante no final (ex: 'BANDEIRA AMARELA 0,000')
    desc = re.sub(r"\s+\d+,\d+\s*$", "", desc)
    return desc.strip()


def _fix_kerning_desc(text: str) -> str:
    """Corrige descrições com kerning explodido."""
    text = re.sub(r"(ATUALIZA[CÇ][AÃ]O)(MONETÁRIA)", r"\1 \2", text, flags=re.IGNORECASE)
    text = re.sub(r"(MULTA)\(", r"\1 (", text)
    return text


def _extrair_itens_xy(page) -> list[ItemFatura]:
    """
    Usa coordenadas XY para extrair a tabela de itens da fatura corretamente,
    mesmo quando o PDF tem kerning ou texto de colunas adjacentes colado.

    Layout Enel SP (x0 aproximados):
        Col descrição: x0 < 200
        Col valor R$:  215 < x0 < 265   (primeira sub-coluna, as demais são PIS e outros)
    """
    from collections import defaultdict

    words = page.extract_words(x_tolerance=3, y_tolerance=3)

    # Localizar Y da tabela: de "TUSD" até "TOTAL"
    tusd_words  = [w for w in words if "TUSD" in w["text"].upper()]
    total_words = [w for w in words if w["text"].upper() == "TOTAL"]
    if not tusd_words or not total_words:
        return []

    y_start = min(w["top"] for w in tusd_words) - 3
    y_end   = max(w["top"] for w in total_words) + 3
    table_words = [w for w in words if y_start <= w["top"] <= y_end]

    rows: dict[int, list[dict]] = defaultdict(list)
    for w in table_words:
        rows[round(w["top"])].append(w)

    X_DESC_MAX = 200
    X_VAL_MIN  = 215
    X_VAL_MAX  = 265

    SKIP_RE = re.compile(
        r"^(subtotal|total|tributos|base\s+calc|alíq|icms|pis|cofins|"
        r"unid|quant|preço|valor|tarifa|mês/ano|consumo|dias|tipo|"
        r"faturamento|outros|kwh)$",
        re.IGNORECASE,
    )

    itens: list[ItemFatura] = []
    for y in sorted(rows):
        ws = sorted(rows[y], key=lambda w: w["x0"])
        desc_words = [w for w in ws if w["x0"] < X_DESC_MAX]
        val_words  = [w for w in ws if X_VAL_MIN <= w["x0"] <= X_VAL_MAX]

        if not desc_words or not val_words:
            continue

        # Detecta kerning: muitas letras isoladas
        single_chars = sum(1 for w in desc_words if len(w["text"]) == 1)
        has_kerning  = single_chars > len(desc_words) * 0.5 and len(desc_words) > 3

        if has_kerning:
            desc = _fix_kerning_desc(_join_words(desc_words))
        else:
            desc = _limpar_desc(" ".join(w["text"] for w in desc_words))

        if not desc or SKIP_RE.match(desc):
            continue

        val_raw = "".join(w["text"] for w in val_words)
        valor   = _primeiro_valor(val_raw)
        if valor is None:
            continue

        itens.append(ItemFatura(descricao=desc, valor=valor))

    return itens


# ── Parser principal ───────────────────────────────────────────────────────────

def extrair_dados_pdf(pdf_path: str | Path) -> DadosOcr:
    """
    Extrai todos os campos do PDF da conta Enel SP (Eletropaulo).

    Args:
        pdf_path: Caminho para o arquivo .pdf

    Returns:
        DadosOcr com todos os campos disponíveis preenchidos.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF não encontrado: {pdf_path}")

    textos: list[str] = []
    primeira_pagina = None

    with pdfplumber.open(str(pdf_path)) as pdf:
        for i, page in enumerate(pdf.pages):
            t = page.extract_text(x_tolerance=3, y_tolerance=3)
            if t:
                textos.append(t)
            if i == 0:
                primeira_pagina = page

    text = "\n".join(textos)
    log.debug("Texto extraído (%d chars)", len(text))

    # ── Distribuidora ────────────────────────────────────────────────────────
    # "Eletropaulo Metropolitana Eletricidade de São Paulo S.A"
    distribuidora = _find(
        r"(Eletropaulo\s+Metropolitana\s+Eletricidade\s+de\s+S[aã]o\s+Paulo\s+S\.?\s*A\.?)",
        text,
    )
    if not distribuidora:
        distribuidora = _find(r"(ENEL[\w\s]+(?:DISTRIBUI[CÇ][AÃ]O)[\w\s]*)", text)
        if distribuidora:
            distribuidora = distribuidora.split("\n")[0].strip()

    # ── Cliente ───────────────────────────────────────────────────────────────
    # Nome em maiúsculas, linha isolada (após "SEGUNDA VIA" ou antes do endereço)
    cliente = _find(r"^([A-ZÁÉÍÓÚÀÃÕÂÊÎÔÛÇ][A-ZÁÉÍÓÚÀÃÕÂÊÎÔÛÇ\s]{5,})$", text, flags=re.MULTILINE)

    # ── Endereço ──────────────────────────────────────────────────────────────
    # Está na mesma linha que a NOTA FISCAL — pegar apenas a parte esquerda
    end_match = re.search(r"^(R\s+.+?|[A-Z]{2,}\s+.+?)\s+NOTA FISCAL", text, re.MULTILINE | re.IGNORECASE)
    if end_match:
        endereco = end_match.group(1).strip()
    else:
        # Fallback: linha com CEP
        endereco = _find(r"^(R\s+.+?|[A-Z]{2,}\s+.+?)\s*\n.*?CEP", text, flags=re.MULTILINE | re.DOTALL)

    # ── Nota Fiscal ───────────────────────────────────────────────────────────
    nota_fiscal = _find(r"NOTA FISCAL N[Oº°]?\s*([\d]+)", text)

    # ── Aviso (Nº da fatura/conta) ────────────────────────────────────────────
    aviso = _find(r"N[Oº°]\s*([\d]+)\s+P[aá]gina", text)

    # ── Classificação tarifária ────────────────────────────────────────────────
    # "B - B1 - CONVENCIONAL - Residencial - Residencial"
    class_match = re.search(
        r"(B\s*-\s*(B\d)\s*-\s*([\w]+)\s*-\s*([\w]+)\s*-\s*([\w]+))",
        text, re.IGNORECASE,
    )
    grupo    = "B"
    subgrupo = None
    classe   = None
    subclasse = None
    if class_match:
        subgrupo  = class_match.group(2).strip()   # B1
        classe    = class_match.group(4).strip()    # Residencial
        subclasse = class_match.group(5).strip()    # Residencial

    # ── Mês de referência ─────────────────────────────────────────────────────
    # "05/2026" ou "MAI/26" — presente no cabeçalho
    ref_raw = _find(r"\b((?:JAN|FEV|MAR|ABR|MAI|JUN|JUL|AGO|SET|OUT|NOV|DEZ)/\d{2,4})\b", text)
    if not ref_raw:
        ref_raw = _find(r"\b(\d{2}/\d{4})\b", text)
    ref_mes, ref_ano = _parse_ref(ref_raw)

    # ── Datas ─────────────────────────────────────────────────────────────────
    # Emissão: "DATA DE EMISSÃO: 14/05/2026"
    emissao_data = _find(r"DATA DE EMISS[AÃ]O:\s*(\d{2}/\d{2}/\d{4})", text)

    # Apresentação: extraído do Protocolo de autorização (13/05/2026)
    # e confirmado pela linha final da página 2: "14/05/2026 05/2026 25/05/2026"
    data_apresentacao = _find(r"Data de apresenta[cç][aã]o:\s*(\d{2}/\d{2}/\d{4})", text)
    if not data_apresentacao:
        # Protocolo: "Protocolo de autorização: ... - 13/05/2026 às"
        data_apresentacao = _find(r"Protocolo de autoriza[cç][aã]o:\s*[\d\-]+\s*-\s*(\d{2}/\d{2}/\d{4})", text)

    # Vencimento: linha do boleto "063700580 25/05/2026 R$278,06"
    venc_match = re.search(
        r"[\d/-]+\s+(\d{6,})\s+(\d{2}/\d{2}/\d{4})\s+R\$([\d.,]+)",
        text,
    )
    vencimento = venc_match.group(2) if venc_match else None
    # Fallback: página 2 "14/05/2026 05/2026 25/05/2026"
    if not vencimento:
        venc2 = re.search(r"(\d{2}/\d{2}/\d{4})\s+\d{2}/\d{4}\s+(\d{2}/\d{2}/\d{4})\s*$", text, re.MULTILINE)
        if venc2:
            vencimento = venc2.group(2)

    # Leituras: "B - B1 - ... Bifásico 11/04/2026 13/05/2026 32 11/06/2026"
    leit = re.search(
        r"(\d{2}/\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})\s+(\d+)\s+(\d{2}/\d{2}/\d{4})",
        text,
    )
    leitura_anterior_data = leit.group(1) if leit else None
    leitura_data          = leit.group(2) if leit else None
    leitura_proxima_data  = leit.group(4) if leit else None

    # ── Energia consumida ─────────────────────────────────────────────────────
    # Linha medidor: "UQM9HDN24L00577314 ENRG ATV ÚNICO 4.592 4.866 1,00000 274,000"
    energia_raw = _find(r"ENRG\s+ATV\s+[ÚU]NICO\s+[\d.]+\s+[\d.]+\s+[\d.,]+\s+([\d.,]+)", text)
    if not energia_raw:
        # Primeira linha do histórico: "MAI/26 274,000 32 LID"
        energia_raw = _find(r"(?:MAI|JAN|FEV|MAR|ABR|JUN|JUL|AGO|SET|OUT|NOV|DEZ)/\d{2}\s+([\d.,]+)\s+\d+\s+LID", text)
    energia = _dec(energia_raw)

    # ── Valor total ───────────────────────────────────────────────────────────
    # Linha tabela: "TOTAL 278,06 13,50 261,27 47,02"
    valor_raw = _find(r"^TOTAL\s+([\d.,]+)", text, flags=re.MULTILINE)
    if not valor_raw:
        # Boleto: "R$278,06"
        valor_raw = _find(r"R\$([\d.,]+)", text)
    valor = _dec(valor_raw)

    # ── Preços TE / TUSD ──────────────────────────────────────────────────────
    # Linha: "ENERGIA (TE) KWH 274,000 0,38088 104,36 ..."  — coluna preço_unit = 0,38088
    preco_te_raw   = _find(r"ENERGIA\s*\(TE\)\s+KWH\s+[\d.,]+\s+([\d,]+)", text)
    preco_tusd_raw = _find(r"USO\s+SIST\.\s+DISTR\.\s*\(TUSD\)\s+KWH\s+[\d.,]+\s+([\d,]+)", text)
    preco_te   = _dec(preco_te_raw)
    preco_tusd = _dec(preco_tusd_raw)

    # Normalizados — 5 casas decimais (padrão ANEEL)
    normalizado_preco_te   = round(preco_te,   5) if preco_te   else None
    normalizado_preco_tusd = round(preco_tusd, 5) if preco_tusd else None
    normalizado_valor      = round(valor,      2) if valor      else None

    # ── Código de barras (linha digitável) ────────────────────────────────────
    # "23792.37205 90356.729807 53003.432704 5 14570000027806"
    cb_match = re.search(
        r"(\d{5}\.\d{5}\s+\d{5}\.\d{6}\s+\d{5}\.\d{6}\s+\d\s+\d{14})",
        text,
    )
    codigo_barras = cb_match.group(1).strip() if cb_match else None

    # Fallback: chave de acesso NF-e (47 dígitos agrupados)
    if not codigo_barras:
        chave = _find(r"Chave de acesso:\s*([\d\s]{47,})", text)
        if chave:
            codigo_barras = re.sub(r"\s+", " ", chave).strip()

    # ── Itens da fatura ───────────────────────────────────────────────────────
    itens_fatura: list[ItemFatura] = []
    if primeira_pagina is not None:
        try:
            itens_fatura = _extrair_itens_xy(primeira_pagina)
        except Exception as e:
            log.warning("Extração XY de itens falhou: %s", e)
            itens_fatura = _extrair_itens_fallback(text)

    return DadosOcr(
        cliente=cliente,
        distribuidora=distribuidora,
        nota_fiscal=nota_fiscal,
        aviso=aviso,
        endereco=endereco,
        codigo_barras=codigo_barras,
        classe=classe,
        subclasse=subclasse,
        grupo=grupo,
        subgrupo=subgrupo,
        ref_mes=ref_mes,
        ref_ano=ref_ano,
        emissao_data=emissao_data,
        data_apresentacao=data_apresentacao,
        leitura_anterior_data=leitura_anterior_data,
        leitura_data=leitura_data,
        leitura_proxima_data=leitura_proxima_data,
        energia=energia,
        valor=valor,
        vencimento=vencimento,
        preco_te=preco_te,
        preco_tusd=preco_tusd,
        normalizado_preco_te=normalizado_preco_te,
        normalizado_preco_tusd=normalizado_preco_tusd,
        normalizado_valor=normalizado_valor,
        itens_fatura=itens_fatura,
    )


def _extrair_itens_fallback(text: str) -> list[ItemFatura]:
    """Fallback simples para PDFs sem layout tabular claro."""
    itens = []
    known = {
        r"(USO SIST\.\s+DISTR\.\s*\(TUSD\)).*?KWH.*?\s+([\d,]+)\s+[\d,]+\s+[\d,]+\s+\d+%": "USO SIST. DISTR. (TUSD)",
        r"ENERGIA\s*\(TE\).*?KWH.*?\s+([\d,]+)\s+[\d,]+\s+[\d,]+\s+\d+%": "ENERGIA (TE)",
        r"ADICIONAL BANDEIRA (\w+).*?\s+([\d,]+)\s+[\d,]+": None,
        r"JUROS DE MORA\s+([\d,]+)": "JUROS DE MORA",
        r"COSIP[^\d]+([\d,]+)": "COSIP - SÃO PAULO - MUNICIPAL",
    }
    for pat, label in known.items():
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            val = _dec(m.group(2) if m.lastindex >= 2 else m.group(1))
            desc = label or f"ADICIONAL BANDEIRA {m.group(1)}"
            if val:
                itens.append(ItemFatura(descricao=desc, valor=val))
    return itens
