/**
 * Secrets Manager — Enel SP Service
 *
 * Criptografia: AES-256-GCM
 * Chave derivada de SECRETS_MASTER_KEY via PBKDF2 (100.000 iterações, SHA-512)
 * Cada registro tem: salt (16B) + iv (12B) + authTag (16B) + ciphertext
 * Armazenado em: secrets/vault.json (nunca commitar no git)
 */

const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const VAULT_PATH = path.join(__dirname, '../secrets/vault.json');
const MASTER_KEY_ENV = 'SECRETS_MASTER_KEY';
const PBKDF2_ITER = 100_000;
const PBKDF2_DIGEST = 'sha512';
const KEY_LEN = 32; // 256 bits

// ──────────────────────────────────────────────
// Garante que o diretório de secrets existe
// ──────────────────────────────────────────────
function ensureDir() {
  const dir = path.dirname(VAULT_PATH);
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
}

// ──────────────────────────────────────────────
// Carrega o vault (arquivo JSON com registros cifrados)
// ──────────────────────────────────────────────
function loadVault() {
  ensureDir();
  if (!fs.existsSync(VAULT_PATH)) return {};
  try {
    return JSON.parse(fs.readFileSync(VAULT_PATH, 'utf8'));
  } catch {
    return {};
  }
}

function saveVault(vault) {
  ensureDir();
  fs.writeFileSync(VAULT_PATH, JSON.stringify(vault, null, 2), { mode: 0o600 });
}

// ──────────────────────────────────────────────
// Derivação de chave por PBKDF2
// ──────────────────────────────────────────────
function deriveKey(masterKey, salt) {
  return crypto.pbkdf2Sync(masterKey, salt, PBKDF2_ITER, KEY_LEN, PBKDF2_DIGEST);
}

// ──────────────────────────────────────────────
// Criptografia AES-256-GCM
// ──────────────────────────────────────────────
function encrypt(plaintext, masterKey) {
  const salt = crypto.randomBytes(16);
  const iv = crypto.randomBytes(12);
  const key = deriveKey(masterKey, salt);

  const cipher = crypto.createCipheriv('aes-256-gcm', key, iv);
  const encrypted = Buffer.concat([cipher.update(plaintext, 'utf8'), cipher.final()]);
  const authTag = cipher.getAuthTag();

  return {
    salt: salt.toString('hex'),
    iv: iv.toString('hex'),
    authTag: authTag.toString('hex'),
    ciphertext: encrypted.toString('hex'),
  };
}

function decrypt(record, masterKey) {
  const salt = Buffer.from(record.salt, 'hex');
  const iv = Buffer.from(record.iv, 'hex');
  const authTag = Buffer.from(record.authTag, 'hex');
  const ciphertext = Buffer.from(record.ciphertext, 'hex');

  const key = deriveKey(masterKey, salt);
  const decipher = crypto.createDecipheriv('aes-256-gcm', key, iv);
  decipher.setAuthTag(authTag);

  const decrypted = Buffer.concat([decipher.update(ciphertext), decipher.final()]);
  return decrypted.toString('utf8');
}

// ──────────────────────────────────────────────
// API pública
// ──────────────────────────────────────────────

function getMasterKey() {
  const key = process.env[MASTER_KEY_ENV];
  if (!key || key.length < 32) {
    throw new Error(
      `Variável de ambiente ${MASTER_KEY_ENV} não definida ou muito curta (mínimo 32 chars).`
    );
  }
  return key;
}

/**
 * Salva credencial criptografada no vault.
 * @param {string} id  - identificador (ex: email do cliente)
 * @param {object} data - objeto com { login_email, login_senha, instalacao }
 */
function save(id, data) {
  const masterKey = getMasterKey();
  const vault = loadVault();
  const plaintext = JSON.stringify(data);
  vault[id] = encrypt(plaintext, masterKey);
  saveVault(vault);
  console.log(`[secrets] Credencial salva para: ${id}`);
}

/**
 * Carrega e decifra credencial do vault.
 * @param {string} id
 * @returns {object|null}
 */
function load(id) {
  const masterKey = getMasterKey();
  const vault = loadVault();
  if (!vault[id]) return null;
  try {
    return JSON.parse(decrypt(vault[id], masterKey));
  } catch (err) {
    console.error(`[secrets] Falha ao decifrar credencial para ${id}:`, err.message);
    return null;
  }
}

/**
 * Remove credencial do vault.
 * @param {string} id
 * @returns {boolean}
 */
function remove(id) {
  const vault = loadVault();
  if (!vault[id]) return false;
  delete vault[id];
  saveVault(vault);
  console.log(`[secrets] Credencial removida: ${id}`);
  return true;
}

/**
 * Lista os IDs armazenados (sem decifrar).
 * @returns {string[]}
 */
function list() {
  const vault = loadVault();
  return Object.keys(vault);
}

module.exports = { save, load, remove, list };
