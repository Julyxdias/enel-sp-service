const express = require('express');
const { chromium } = require('playwright');
const pdfParse = require('pdf-parse');
const crypto = require('crypto');
const secretsManager = require('./secrets');

const app = express();
app.use(express.json());
const PORT = process.env.PORT || 3000;
const jobs = {};

function criarJob() {
  const id = crypto.randomUUID();
  jobs[id] = { status: 'pendente', result: null, error: null, criado_em: new Date().toISOString() };
  return id;
}

setInterval(() => {
  const limite = Date.now() - 60 * 60 * 1000;
  for (const [id, job] of Object.entries(jobs)) {
    if (new Date(job.criado_em).getTime() < limite) delete jobs[id];
  }
}, 15 * 60 * 1000);

app.get('/health', (_req, res) => res.json({ status: 'ok', version: '4' }));
app.get('/debug', (_req, res) => res.json({ routes: app._router.stack.filter(r => r.route).map(r => ({ path: r.route.path, method: Object.keys(r.route.methods)[0] })) }));

app.post('/api/enel-sp/contas', async (req, res) => {
  const { instalacao, login_email, login_senha, salvar_credencial } = req.body;
  if (!instalacao || !login_email || !login_senha) {
    return res.status(422).json({ error: { code: 'MISSING_PARAMS', status: 422 } });
  }
  if (salvar_credencial) {
    try { secretsManager.save(login_email, { login_email, login_senha, instalacao }); }
    catch (err) { return res.status(500).json({ error: { code: 'SECRET_ERROR', message: err.message, status: 500 } }); }
  }
  const jobId = criarJob();
  res.status(202).json({ job_id: jobId, status: 'pendente', poll_url: `/api/enel-sp/job/${jobId}` });
  processarConsulta(jobId, { instalacao, login_email, login_senha });
});

app.post('/api/enel-sp/contas/salvo', (req, res) => {
  const { login_email } = req.body;
  if (!login_email) return res.status(422).json({ error: { code: 'MISSING_PARAMS', status: 422 } });
  const cred = secretsManager.load(login_email);
  if (!cred) return res.status(404).json({ error: { code: 'CREDENTIAL_NOT_FOUND', status: 404 } });
  const jobId = criarJob();
  res.status(202).json({ job_id: jobId, status: 'pendente', poll_url: `/api/enel-sp/job/${jobId}` });
  processarConsulta(jobId, cred);
});

app.get('/api/enel-sp/job/:id', (req, res) => {
  const job = jobs[req.params.id];
  if (!job) return res.status(404).json({ error: { code: 'JOB_NOT_FOUND', status: 404 } });
  res.json(job);
});

app.get('/api/secrets', (req, res) => res.json({ emails: secretsManager.list() }));
app.delete('/api/secrets/:email', (req, res) => res.json({ deleted: secretsManager.remove(req.params.email) }));

async function processarConsulta(jobId, { instalacao, login_email, login_senha }) {
  jobs[jobId].status = 'processando';
  let browser;
  try {
    browser = await chromium.launch({
      headless: true,
      executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || '/usr/bin/chromium',
      args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'],
    });
    const context = await browser.newContext({ userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36' });
    const page = await context.newPage();
    await page.goto('https://portalhome.eneldistribuicaosp.com.br/#/login', { waitUntil: 'networkidle', timeout: 30000 });
    await page.fill('input[type="email"], input[name="email"]', login_email);
    await page.fill('input[type="password"], input[name="senha"]', login_senha);
    await page.click('button[type="submit"], button.btn-login');
    await page.waitForNavigation({ timeout: 20000 }).catch(() => {});
    const loginError = await page.$('.error-login, .alert-danger, [class*="erro"]');
    if (loginError) {
      jobs[jobId].status = 'erro';
      jobs[jobId].error = { code: 'INVALID_CREDENTIALS', message: 'Login ou senha invalidos.', status: 401 };
      await browser.close();
      return;
    }
    const nome = await page.$eval('.nome-usuario, [class*="nome-cliente"]', el => el.textContent.trim()).catch(() => '');
    const listaInstalacoes = await page.$$eval('option[value]', els => els.map(e => e.value).filter(Boolean)).catch(() => [instalacao]);
    const endereco = await page.$eval('.endereco-instalacao, [class*="endereco"]', el => el.textContent.trim()).catch(() => '');
    await page.waitForSelector('.lista-contas, .faturas-container, table.faturas', { timeout: 15000 }).catch(() => {});
    const contas = await page.$$eval('.item-fatura, tr.fatura, .conta-row', rows => rows.map(row => {
      const t = sel => row.querySelector(sel)?.textContent.trim() ?? '';
      const a = (sel, attr) => row.querySelector(sel)?.getAttribute(attr) ?? '';
      return {
        mes_referencia: t('.mes-referencia, td:nth-child(1)'),
        vencimento: t('.vencimento, td:nth-child(2)'),
        valor: parseFloat(t('.valor, td:nth-child(3)').replace('R$','').replace('.','').replace(',','.').trim()),
        status: t('.status, td:nth-child(4)').toLowerCase(),
        codigo_barras: t('.codigo-barras, .linha-digitavel'),
        conta_pdf_url: a('a.btn-pdf, a[href*=".pdf"]','href') || a('button[data-url]','data-url'),
      };
    })).catch(() => []);
    let ocr = null;
    if (contas.length > 0 && contas[0].conta_pdf_url) {
      try {
        const url = contas[0].conta_pdf_url.startsWith('http') ? contas[0].conta_pdf_url : `https://portalhome.eneldistribuicaosp.com.br${contas[0].conta_pdf_url}`;
        const resp = await context.request.get(url);
        if (resp.ok()) { const buf = await resp.body(); ocr = await extractOcr(buf); }
      } catch {}
    }
    await browser.close();
    jobs[jobId].status = 'concluido';
    jobs[jobId].result = { nome, instalacao, endereco, lista_instalacoes: listaInstalacoes, contas, ocr };
  } catch (err) {
    if (browser) await browser.close().catch(() => {});
    console.error(`[job ${jobId}] Erro:`, err.message);
    jobs[jobId].status = 'erro';
    jobs[jobId].error = { code: err.message.includes('timeout') ? 'TIMEOUT' : 'PORTAL_UNAVAILABLE', message: err.message, status: 503 };
  }
}

async function extractOcr(pdfBuffer) {
  try {
    const data = await pdfParse(pdfBuffer);
    const text = data.text;
    const get = p => { const m = text.match(p); return m ? m[1].trim() : null; };
    const getF = p => { const v = get(p); return v ? parseFloat(v.replace('.','').replace(',','.')) : null; };
    return {
      cliente: get(/Cliente[:\s]+(.+)/i), distribuidora: get(/Distribuidora[:\s]+(.+)/i) ?? 'Enel',
      classe: get(/Classe[:\s]+(.+)/i), subclasse: get(/Subclasse[:\s]+(.+)/i),
      grupo: get(/Grupo[:\s]+([A-Z]\d?)/i), subgrupo: get(/Subgrupo[:\s]+([A-Z]\d+)/i),
      ref_mes: get(/Referencia[:\s]+(\d{2})\/\d{4}/i), ref_ano: get(/Referencia[:\s]+\d{2}\/(\d{4})/i),
      emissao_data: get(/Emissao[:\s]+([\d\/]+)/i), vencimento: get(/Vencimento[:\s]+([\d\/]+)/i),
      leitura_anterior_data: get(/Leitura Anterior[:\s]+([\d\/]+)/i), leitura_data: get(/Leitura Atual[:\s]+([\d\/]+)/i),
      leitura_proxima_data: get(/Proxima Leitura[:\s]+([\d\/]+)/i),
      energia: getF(/Consumo[:\s]+([\d.,]+)\s*kWh/i), valor: getF(/Total a Pagar[:\s]+R?\$?\s*([\d.,]+)/i),
      preco_te: getF(/Tarifa TE[:\s]+R?\$?\s*([\d.,]+)/i), preco_tusd: getF(/Tarifa TUSD[:\s]+R?\$?\s*([\d.,]+)/i),
      normalizado_valor: getF(/Total a Pagar[:\s]+R?\$?\s*([\d.,]+)/i),
      itens_fatura: text.split('\n').reduce((acc, line) => { const m = line.match(/^(.{5,60}?)\s+([\d.,]+)\s*$/); if (m) { const v = parseFloat(m[2].replace(',','.')); if (!isNaN(v) && v > 0) acc.push({ descricao: m[1].trim(), valor: v }); } return acc; }, []),
    };
  } catch { return null; }
}

app.listen(PORT, () => console.log(`Enel SP Service rodando na porta ${PORT}`));
