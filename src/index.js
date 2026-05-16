/**
 * Enel SP — Web Service
 * Consulta contas, dados da instalação e extração OCR
 *
 * Stack: Express + Playwright + pdf-parse + AES-256 secrets
 */

const express = require('express');
const { chromium } = require('playwright');
const pdfParse = require('pdf-parse');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const secretsManager = require('./secrets');

const app = express();
app.use(express.json());

const PORT = process.env.PORT || 3000;

// ──────────────────────────────────────────────
// Health check (usado pelo Render para marcar UP)
// ──────────────────────────────────────────────
app.get('/health', (_req, res) => res.json({ status: 'ok' }));

// ──────────────────────────────────────────────
// Rota principal
// ──────────────────────────────────────────────
app.post('/api/enel-sp/contas', async (req, res) => {
  const { instalacao, login_email, login_senha } = req.body;

  if (!instalacao || !login_email || !login_senha) {
    return res.status(422).json({
      error: {
        code: 'MISSING_PARAMS',
        message: 'Parâmetros obrigatórios: instalacao, login_email, login_senha.',
        status: 422,
      },
    });
  }

  // Salva/atualiza credencial criptografada (opcional — usa se enviado)
  if (req.body.salvar_credencial) {
    secretsManager.save(login_email, { login_email, login_senha, instalacao });
  }

  let browser;
  try {
    browser = await chromium.launch({ headless: true });
    const context = await browser.newContext({
      userAgent:
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
        '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    });
    const page = await context.newPage();

    // ── 1. Login ──────────────────────────────
    await page.goto('https://portalhome.eneldistribuicaosp.com.br/#/login', {
      waitUntil: 'networkidle',
      timeout: 30000,
    });

    await page.fill('input[type="email"], input[name="email"]', login_email);
    await page.fill('input[type="password"], input[name="senha"]', login_senha);
    await page.click('button[type="submit"], button.btn-login');

    // Aguarda navegação pós-login
    await page.waitForNavigation({ timeout: 20000 }).catch(() => {});

    // Detecta erro de credencial
    const loginError = await page.$('.error-login, .alert-danger, [class*="erro"]');
    if (loginError) {
      return res.status(401).json({
        error: { code: 'INVALID_CREDENTIALS', message: 'Login ou senha inválidos.', status: 401 },
      });
    }

    // ── 2. Dados do titular e instalações ────
    const nome = await page
      .$eval('.nome-usuario, [class*="nome-cliente"]', (el) => el.textContent.trim())
      .catch(() => '');

    const listaInstalacoes = await page
      .$$eval('option[value], .instalacao-item', (els) =>
        els.map((e) => e.value || e.dataset.instalacao).filter(Boolean)
      )
      .catch(() => [instalacao]);

    // Seleciona a instalação desejada (se houver seletor)
    const seletor = await page.$(`option[value="${instalacao}"]`);
    if (seletor) {
      await page.selectOption('select[name="instalacao"], select.instalacao-select', instalacao);
      await page.waitForTimeout(2000);
    }

    const endereco = await page
      .$eval('.endereco-instalacao, [class*="endereco"]', (el) => el.textContent.trim())
      .catch(() => '');

    // ── 3. Lista de contas ───────────────────
    await page.waitForSelector('.lista-contas, .faturas-container, table.faturas', {
      timeout: 15000,
    });

    const contas = await page.$$eval(
      '.item-fatura, tr.fatura, .conta-row',
      (rows) =>
        rows.map((row) => {
          const getText = (sel) => row.querySelector(sel)?.textContent.trim() ?? '';
          const getAttr = (sel, attr) => row.querySelector(sel)?.getAttribute(attr) ?? '';
          return {
            mes_referencia: getText('.mes-referencia, .competencia, td:nth-child(1)'),
            vencimento: getText('.vencimento, td:nth-child(2)'),
            valor: parseFloat(
              getText('.valor, td:nth-child(3)')
                .replace('R$', '')
                .replace('.', '')
                .replace(',', '.')
                .trim()
            ),
            status: getText('.status, .situacao, td:nth-child(4)').toLowerCase(),
            codigo_barras: getText('.codigo-barras, .linha-digitavel'),
            conta_pdf_url:
              getAttr('a.btn-pdf, a[href*=".pdf"]', 'href') ||
              getAttr('a[download], button[data-url]', 'data-url'),
          };
        })
    );

    // ── 4. Download e OCR da fatura mais recente ──
    let ocr = null;
    if (contas.length > 0 && contas[0].conta_pdf_url) {
      const pdfBuffer = await downloadPdf(page, context, contas[0].conta_pdf_url);
      if (pdfBuffer) {
        ocr = await extractOcr(pdfBuffer);
      }
    }

    await browser.close();

    return res.json({
      nome,
      instalacao,
      endereco,
      lista_instalacoes: listaInstalacoes,
      contas,
      ocr,
    });
  } catch (err) {
    if (browser) await browser.close().catch(() => {});
    console.error('[enel-sp] Erro:', err.message);

    if (err.message.includes('timeout') || err.message.includes('Timeout')) {
      return res.status(504).json({
        error: { code: 'TIMEOUT', message: 'Tempo limite excedido.', status: 504 },
      });
    }

    return res.status(503).json({
      error: { code: 'PORTAL_UNAVAILABLE', message: err.message, status: 503 },
    });
  }
});

// ──────────────────────────────────────────────
// Rota: consultar usando credencial salva
// ──────────────────────────────────────────────
app.post('/api/enel-sp/contas/salvo', async (req, res) => {
  const { login_email } = req.body;
  if (!login_email) {
    return res.status(422).json({ error: { code: 'MISSING_PARAMS', status: 422 } });
  }

  const cred = secretsManager.load(login_email);
  if (!cred) {
    return res.status(404).json({
      error: { code: 'CREDENTIAL_NOT_FOUND', message: 'Credencial não encontrada.', status: 404 },
    });
  }

  req.body = { ...req.body, ...cred };
  // Redireciona internamente para o handler principal
  return app._router.handle(
    { ...req, url: '/api/enel-sp/contas', method: 'POST' },
    res,
    () => {}
  );
});

// ──────────────────────────────────────────────
// Rota: gerenciar secrets
// ──────────────────────────────────────────────
app.get('/api/secrets', (req, res) => {
  const keys = secretsManager.list();
  res.json({ total: keys.length, emails: keys });
});

app.delete('/api/secrets/:email', (req, res) => {
  const ok = secretsManager.remove(req.params.email);
  res.json({ deleted: ok });
});

// ──────────────────────────────────────────────
// Helpers
// ──────────────────────────────────────────────

async function downloadPdf(page, context, url) {
  try {
    const fullUrl = url.startsWith('http')
      ? url
      : `https://portalhome.eneldistribuicaosp.com.br${url}`;

    const response = await context.request.get(fullUrl);
    if (!response.ok()) return null;
    return await response.body();
  } catch {
    return null;
  }
}

async function extractOcr(pdfBuffer) {
  try {
    const data = await pdfParse(pdfBuffer);
    const text = data.text;

    const get = (pattern) => {
      const m = text.match(pattern);
      return m ? m[1].trim() : null;
    };

    const getFloat = (pattern) => {
      const v = get(pattern);
      return v ? parseFloat(v.replace('.', '').replace(',', '.')) : null;
    };

    return {
      cliente: get(/Cliente[:\s]+(.+)/i),
      distribuidora: get(/Distribuidora[:\s]+(.+)/i) ?? 'Enel Distribuição São Paulo',
      classe: get(/Classe[:\s]+(.+)/i),
      subclasse: get(/Subclasse[:\s]+(.+)/i),
      grupo: get(/Grupo[:\s]+([A-Z]\d?)/i),
      subgrupo: get(/Subgrupo[:\s]+([A-Z]\d+)/i),
      mes: get(/(?:Mês|Mes) de Referência[:\s]+(\w+)/i),
      ref_mes: get(/Referência[:\s]+(\d{2})\/\d{4}/i),
      ref_ano: get(/Referência[:\s]+\d{2}\/(\d{4})/i),
      ano: get(/(\d{4})/),
      emissao_data: get(/(?:Data de Emissão|Emissão)[:\s]+([\d\/]+)/i),
      vencimento: get(/(?:Vencimento|Data de Vencimento)[:\s]+([\d\/]+)/i),
      leitura_anterior_data: get(/Leitura Anterior[:\s]+([\d\/]+)/i),
      leitura_data: get(/Leitura Atual[:\s]+([\d\/]+)/i),
      leitura_proxima_data: get(/Próxima Leitura[:\s]+([\d\/]+)/i),
      energia: getFloat(/Consumo[:\s]+([\d.,]+)\s*kWh/i),
      valor: getFloat(/(?:Total a Pagar|Valor Total)[:\s]+R?\$?\s*([\d.,]+)/i),
      nota_fiscal: get(/NF-?e[:\s]+([\d\/]+)/i),
      endereco: get(/Endereço[:\s]+(.+)/i),
      codigo_barras: get(/(?:Código de Barras|Linha Digitável)[:\s]+([\d\s]+)/i),
      data_apresentacao: get(/Data de Apresentação[:\s]+([\d\/]+)/i),
      aviso: get(/(?:Aviso|Atenção)[:\s]+(.+)/i),
      preco_te: getFloat(/Tarifa TE[:\s]+R?\$?\s*([\d.,]+)/i),
      preco_tusd: getFloat(/Tarifa TUSD[:\s]+R?\$?\s*([\d.,]+)/i),
      normalizado_preco_te: getFloat(/Tarifa TE[:\s]+R?\$?\s*([\d.,]+)/i),
      normalizado_preco_tusd: getFloat(/Tarifa TUSD[:\s]+R?\$?\s*([\d.,]+)/i),
      normalizado_valor: getFloat(/(?:Total a Pagar|Valor Total)[:\s]+R?\$?\s*([\d.,]+)/i),
      itens_fatura: extractItens(text),
    };
  } catch (err) {
    console.error('[ocr] Erro ao parsear PDF:', err.message);
    return null;
  }
}

function extractItens(text) {
  const itens = [];
  const lines = text.split('\n');
  // Detecta linhas no padrão "Descrição ... R$ valor"
  const linePattern = /^(.{5,60}?)\s+R?\$?\s*([\d.,]+)\s*$/;
  for (const line of lines) {
    const m = line.match(linePattern);
    if (m) {
      const valor = parseFloat(m[2].replace('.', '').replace(',', '.'));
      if (!isNaN(valor) && valor > 0) {
        itens.push({ descricao: m[1].trim(), valor });
      }
    }
  }
  return itens;
}

app.listen(PORT, () => {
  console.log(`✓ Enel SP Service rodando na porta ${PORT}`);
});
