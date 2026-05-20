"""
Enel SP - Web Service de Download de Conta
Automatiza login → home → contas → download do PDF da última conta
"""

import asyncio
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from ocr_parser import extrair_dados_pdf
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

# ── Playwright import (lazy, só carrega quando necessário) ──────────────────
try:
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

# ── App ─────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Enel SP – Download de Conta",
    description="Web service que automatiza o fluxo de login e download do PDF da última conta na Enel SP.",
    version="1.0.0",
)

# ── Autenticação por Bearer token (opcional — ativa se ENEL_API_KEY estiver definida) ──
_bearer = HTTPBearer(auto_error=False)
_API_KEY = os.getenv("ENEL_API_KEY")

def verificar_token(creds: HTTPAuthorizationCredentials | None = Security(_bearer)):
    if not _API_KEY:
        return  # sem variável configurada → sem proteção (dev local)
    if not creds or creds.credentials != _API_KEY:
        raise HTTPException(status_code=401, detail="Token inválido ou ausente.")

OUTPUT_DIR = Path(tempfile.gettempdir()) / "enel_boletos"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# ── Schemas ─────────────────────────────────────────────────────────────────
class LoginRequest(BaseModel):
    email: str
    senha: str
    headless: bool = True          # False = abre janela visível (útil p/ debug)
    timeout_segundos: int = 60     # timeout geral por ação


class DownloadResult(BaseModel):
    sucesso: bool
    mensagem: str
    arquivo: str | None = None
    tamanho_bytes: int | None = None
    timestamp: str
    ocr: dict | None = None


# ── Lógica de automação ──────────────────────────────────────────────────────
async def baixar_conta_enel(email: str, senha: str, headless: bool, timeout_ms: int) -> dict:
    if not PLAYWRIGHT_AVAILABLE:
        raise RuntimeError("Playwright não está instalado. Execute: pip install playwright && playwright install chromium")

    pdf_path: Path | None = None
    ocr_dict: dict | None = None
    log: list[str] = []

    def info(msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        log.append(f"[{ts}] {msg}")
        print(f"[{ts}] {msg}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )

        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            accept_downloads=True,
            locale="pt-BR",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )

        page = await context.new_page()

        try:
            # ── 1. Página de Login ───────────────────────────────────────────
            info("Abrindo página de login...")
            login_url = (
                "https://www.enel.com.br/pt-saopaulo/login.html"
                "?commonAuthCallerPath=%2Fsamlsso"
                "&forceAuth=false&passiveAuth=false"
                "&spEntityID=ENEL_SP_WEB_BRA"
                "&relyingParty=ENEL_SP_WEB_BRA"
                "&type=samlsso&sp=ENEL_SP_WEB_BRA"
                "&isSaaSApp=false"
                "&authenticators=EnelCustomBasicAuthenticator%3ALOCAL"
            )
            await page.goto(login_url, wait_until="domcontentloaded", timeout=timeout_ms)
            info("Página de login carregada.")

            # ── 2. Preencher email ──────────────────────────────────────────
            info("Preenchendo e-mail...")
            email_selectors = [
                'input[type="email"]',
                'input[name="username"]',
                'input[id="username"]',
                'input[placeholder*="mail" i]',
                'input[placeholder*="e-mail" i]',
                'input[placeholder*="usuário" i]',
            ]
            email_field = None
            for sel in email_selectors:
                try:
                    email_field = await page.wait_for_selector(sel, timeout=5000)
                    if email_field:
                        break
                except PWTimeout:
                    continue

            if not email_field:
                raise RuntimeError("Campo de e-mail não encontrado na página de login.")

            await email_field.clear()
            await email_field.type(email, delay=50)

            # ── 3. Preencher senha ──────────────────────────────────────────
            info("Preenchendo senha...")
            senha_selectors = [
                'input[type="password"]',
                'input[name="password"]',
                'input[id="password"]',
            ]
            senha_field = None
            for sel in senha_selectors:
                try:
                    senha_field = await page.wait_for_selector(sel, timeout=5000)
                    if senha_field:
                        break
                except PWTimeout:
                    continue

            if not senha_field:
                raise RuntimeError("Campo de senha não encontrado na página de login.")

            await senha_field.clear()
            await senha_field.type(senha, delay=50)

            # ── 4. Clicar em Acessar ────────────────────────────────────────
            info("Clicando em Acessar...")
            acessar_selectors = [
                'button[type="submit"]',
                'input[type="submit"]',
                'button:has-text("Acessar")',
                'button:has-text("Entrar")',
                'button:has-text("Login")',
                '[id*="login" i]',
                '[class*="login" i]',
            ]
            acessar_btn = None
            for sel in acessar_selectors:
                try:
                    acessar_btn = page.locator(sel).first
                    if await acessar_btn.is_visible(timeout=3000):
                        break
                except Exception:
                    continue

            if not acessar_btn:
                raise RuntimeError("Botão 'Acessar' não encontrado.")

            await acessar_btn.click()
            await page.wait_for_load_state("networkidle", timeout=timeout_ms)
            info(f"Login realizado. URL atual: {page.url}")

            # Verificar se houve erro de login
            current_url = page.url
            if "login" in current_url.lower() or "error" in current_url.lower():
                error_msg = await page.locator('[class*="error"], [class*="alert"], [id*="error"]').first.text_content(timeout=3000)
                raise RuntimeError(f"Falha no login: {error_msg or 'Credenciais inválidas ou sessão expirada'}")

            # ── 5. Home → Ver Contas ────────────────────────────────────────
            info("Navegando para home (área privada)...")
            home_url = "https://www.enel.com.br/pt-saopaulo/private-area/home.html"
            await page.goto(home_url, wait_until="domcontentloaded", timeout=timeout_ms)
            await page.wait_for_load_state("networkidle", timeout=timeout_ms)
            info("Home carregada.")

            info("Clicando em 'Ver contas'...")
            ver_contas_selectors = [
                'a:has-text("Ver contas")',
                'button:has-text("Ver contas")',
                'a:has-text("Contas")',
                '[href*="bills"]',
                '[href*="contas"]',
                '[href*="debt-control"]',
            ]
            ver_contas = None
            for sel in ver_contas_selectors:
                try:
                    ver_contas = page.locator(sel).first
                    if await ver_contas.is_visible(timeout=5000):
                        break
                except Exception:
                    continue

            if ver_contas and await ver_contas.is_visible():
                await ver_contas.click()
                await page.wait_for_load_state("networkidle", timeout=timeout_ms)
                info(f"URL após 'Ver contas': {page.url}")
            else:
                info("Link 'Ver contas' não encontrado, navegando diretamente...")
                await page.goto(
                    "https://www.enel.com.br/pt-saopaulo/private-area/home/debt-control/bills-analysis.html",
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                )

            # ── 6. Clicar em "Ver detalhes" da última conta ─────────────────
            info("Procurando 'Ver detalhes' da última conta...")
            ver_detalhes_selectors = [
                'a:has-text("Ver detalhes")',
                'button:has-text("Ver detalhes")',
                'a:has-text("Detalhes")',
                '[class*="details"]',
            ]
            ver_detalhes = None
            for sel in ver_detalhes_selectors:
                try:
                    # Pega o PRIMEIRO (última conta no topo)
                    ver_detalhes = page.locator(sel).first
                    if await ver_detalhes.is_visible(timeout=5000):
                        break
                except Exception:
                    continue

            if ver_detalhes and await ver_detalhes.is_visible():
                await ver_detalhes.click()
                await page.wait_for_load_state("networkidle", timeout=timeout_ms)
                info(f"URL após 'Ver detalhes': {page.url}")
            else:
                info("Botão 'Ver detalhes' não encontrado, continuando na página atual...")

            # ── 7. Navegar até página de análise de contas ──────────────────
            bills_url = "https://www.enel.com.br/pt-saopaulo/private-area/home/debt-control/bills-analysis.html"
            if "bills-analysis" not in page.url:
                info("Navegando para bills-analysis...")
                await page.goto(bills_url, wait_until="domcontentloaded", timeout=timeout_ms)
                await page.wait_for_load_state("networkidle", timeout=timeout_ms)

            info("Página de contas carregada.")

            # ── 8. Baixar PDF ────────────────────────────────────────────────
            info("Procurando botão de download do PDF...")
            pdf_selectors = [
                'a:has-text("Baixar PDF")',
                'button:has-text("Baixar PDF")',
                'a:has-text("Download PDF")',
                'a:has-text("Baixar")',
                'button:has-text("Baixar")',
                '[href*=".pdf"]',
                '[download]',
                'a[href*="pdf"]',
                '[class*="download"]',
                '[class*="pdf"]',
            ]

            pdf_btn = None
            for sel in pdf_selectors:
                try:
                    pdf_btn = page.locator(sel).first
                    if await pdf_btn.is_visible(timeout=5000):
                        info(f"Botão PDF encontrado: {sel}")
                        break
                except Exception:
                    continue

            if not pdf_btn or not await pdf_btn.is_visible():
                # Tira screenshot para debug
                ss_path = OUTPUT_DIR / "debug_screenshot.png"
                await page.screenshot(path=str(ss_path), full_page=True)
                info(f"Screenshot salvo: {ss_path}")
                raise RuntimeError(
                    "Botão de download PDF não encontrado. "
                    f"Screenshot salvo em {ss_path}. "
                    "Verifique se a conta possui boleto disponível."
                )

            # Inicia download
            info("Clicando para baixar PDF...")
            timestamp = int(time.time())
            pdf_filename = f"conta_enel_{timestamp}.pdf"
            pdf_path = OUTPUT_DIR / pdf_filename

            async with page.expect_download(timeout=timeout_ms) as dl_info:
                await pdf_btn.click()

            download = await dl_info.value
            await download.save_as(str(pdf_path))
            info(f"PDF salvo: {pdf_path}")

            # ── 9. Extração OCR ──────────────────────────────────────────────
            info("Extraindo dados do PDF (OCR)...")
            try:
                ocr_dados = extrair_dados_pdf(pdf_path)
                ocr_dict = ocr_dados.to_dict()
                info(f"OCR concluído: {len([v for v in ocr_dict.values() if v is not None])} campos extraídos.")
            except Exception as ocr_err:
                info(f"Aviso: OCR falhou — {ocr_err}")
                ocr_dict = None

        finally:
            await context.close()
            await browser.close()

    file_size = pdf_path.stat().st_size if pdf_path and pdf_path.exists() else None
    return {
        "sucesso": True,
        "pdf_path": str(pdf_path) if pdf_path else None,
        "pdf_filename": pdf_filename if pdf_path else None,
        "tamanho_bytes": file_size,
        "ocr": ocr_dict,
        "log": log,
    }


# ── Endpoints ────────────────────────────────────────────────────────────────
@app.get("/", tags=["Info"])
def root():
    return {
        "servico": "Enel SP – Download de Conta",
        "versao": "1.0.0",
        "endpoints": {
            "POST /baixar-conta": "Faz login e baixa o PDF da última conta",
            "GET  /health":       "Status do serviço",
        },
    }


@app.get("/health", tags=["Info"])
def health():
    return {
        "status": "ok",
        "playwright": PLAYWRIGHT_AVAILABLE,
        "timestamp": datetime.now().isoformat(),
    }


@app.post("/baixar-conta", response_model=DownloadResult, tags=["Automação"], dependencies=[Security(verificar_token)])
async def baixar_conta(req: LoginRequest):
    """
    Realiza o fluxo completo:
    1. Login com e-mail e senha
    2. Navega até a home da área privada
    3. Acessa a última conta
    4. Baixa o PDF e retorna o arquivo
    """
    if not PLAYWRIGHT_AVAILABLE:
        raise HTTPException(
            status_code=503,
            detail="Playwright não instalado. Execute: playwright install chromium",
        )

    timeout_ms = req.timeout_segundos * 1000

    try:
        resultado = await baixar_conta_enel(
            email=req.email,
            senha=req.senha,
            headless=req.headless,
            timeout_ms=timeout_ms,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not resultado["pdf_path"]:
        raise HTTPException(status_code=500, detail="PDF não foi gerado.")

    return FileResponse(
        path=resultado["pdf_path"],
        media_type="application/pdf",
        filename=resultado["pdf_filename"],
        headers={"X-File-Size": str(resultado["tamanho_bytes"] or 0)},
    )


@app.post("/baixar-conta/json", tags=["Automação"], dependencies=[Security(verificar_token)])
async def baixar_conta_json(req: LoginRequest):
    """
    Igual ao endpoint acima, mas retorna JSON com metadados em vez do arquivo diretamente.
    O PDF fica salvo no servidor e pode ser obtido via GET /arquivo/{nome}.
    """
    if not PLAYWRIGHT_AVAILABLE:
        raise HTTPException(status_code=503, detail="Playwright não instalado.")

    timeout_ms = req.timeout_segundos * 1000

    try:
        resultado = await baixar_conta_enel(
            email=req.email,
            senha=req.senha,
            headless=req.headless,
            timeout_ms=timeout_ms,
        )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content=DownloadResult(
                sucesso=False,
                mensagem=str(e),
                timestamp=datetime.now().isoformat(),
            ).model_dump(),
        )

    return DownloadResult(
        sucesso=True,
        mensagem="PDF baixado com sucesso.",
        arquivo=resultado["pdf_filename"],
        tamanho_bytes=resultado["tamanho_bytes"],
        timestamp=datetime.now().isoformat(),
        ocr=resultado.get("ocr"),
    )


@app.get("/arquivo/{nome_arquivo}", tags=["Arquivos"])
async def obter_arquivo(nome_arquivo: str):
    """Retorna um PDF previamente baixado pelo nome do arquivo."""
    # Segurança: impede path traversal
    safe_name = Path(nome_arquivo).name
    file_path = OUTPUT_DIR / safe_name

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Arquivo não encontrado.")

    return FileResponse(
        path=str(file_path),
        media_type="application/pdf",
        filename=safe_name,
    )
