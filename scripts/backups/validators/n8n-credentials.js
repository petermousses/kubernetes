'use strict';

// Read restored credentials using the Cipher class from the pinned n8n image.
// No CLI/server is started. Plaintext remains inside this process.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { DatabaseSync } = require('node:sqlite');

const EXPECTED_CIPHER_SHA256 = '223e50d75f7dea662a371bf30dbf8daa06258b3a4b0b8fdcff2d209e62d29257';

function digest(file) {
  const attributes = fs.lstatSync(file);
  if (!attributes.isFile()) throw new Error('invalid restored file');
  const descriptor = fs.openSync(file, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
  try {
    const checksum = crypto.createHash('sha256');
    const buffer = Buffer.alloc(1024 * 1024);
    let size;
    while ((size = fs.readSync(descriptor, buffer, 0, buffer.length, null)) > 0) checksum.update(buffer.subarray(0, size));
    return checksum.digest('hex');
  } finally {
    fs.closeSync(descriptor);
  }
}

function validate() {
  const data = '/restore-data/validated';
  const images = '/restore-images/validated';
  const dataProof = JSON.parse(fs.readFileSync(path.join(data, 'result.json'), 'utf8'));
  const imagesProof = JSON.parse(fs.readFileSync(path.join(images, 'result.json'), 'utf8'));
  for (const proof of [dataProof, imagesProof]) {
    if (proof.result !== 'passed' || proof.backup_id !== process.env.BACKUP_ID ||
        proof.archive_metadata_verified !== true || proof.file_checksums_verified !== true) {
      throw new Error('restored pair validation is incomplete');
    }
  }
  if (dataProof.claim !== 'n8n-data-live' || imagesProof.claim !== 'n8n-images' ||
      dataProof.sqlite_integrity !== true || dataProof.encryption_key_preserved !== true) {
    throw new Error('restored claim identity differs');
  }
  const tree = path.join(data, 'tree');
  const keyFile = path.join(tree, 'config');
  const databaseFile = path.join(tree, 'database.sqlite');
  if (digest(keyFile) !== dataProof.encryption_config_sha256 || digest(databaseFile) !== dataProof.database_sha256) {
    throw new Error('validated database or configuration changed');
  }
  const key = JSON.parse(fs.readFileSync(keyFile, 'utf8')).encryptionKey;
  if (typeof key !== 'string' || !key) throw new Error('restored key missing');
  const core = require.resolve('n8n-core', { paths: ['/usr/local/lib/node_modules/n8n'] });
  const cipherFile = path.join(path.dirname(core), 'encryption/cipher.js');
  if (digest(cipherFile) !== EXPECTED_CIPHER_SHA256) throw new Error('pinned cipher implementation differs');
  const { Cipher } = require(cipherFile);
  const { CipherAes256CBC } = require(path.join(path.dirname(cipherFile), 'aes-256-cbc.js'));
  const cipher = new Cipher({ encryptionKey: key }, undefined, new CipherAes256CBC(), { isConfigured: () => false });
  const probe = '{"restoreProbe":true}';
  const encryptedProbe = cipher.encrypt(probe, 'isolated-synthetic-probe');
  if (cipher.decrypt(encryptedProbe, 'isolated-synthetic-probe') !== probe) throw new Error('native cipher roundtrip failed');
  let wrongKeyAccepted = false;
  try { wrongKeyAccepted = cipher.decrypt(encryptedProbe, 'different-synthetic-key') === probe; } catch {}
  if (wrongKeyAccepted) throw new Error('native cipher wrong-key control failed');
  const database = new DatabaseSync(databaseFile, { readOnly: true });
  let verified = 0;
  try {
    const rows = database.prepare('SELECT data FROM credentials_entity').all();
    if (rows.length !== dataProof.relation_counts.credentials_entity) throw new Error('restored credential count differs');
    for (const row of rows) {
      if (typeof row.data !== 'string' || !row.data || row.data.includes(':')) throw new Error('unsupported credential encryption format');
      const plaintext = cipher.decrypt(row.data, key);
      const credential = JSON.parse(plaintext);
      if (!credential || typeof credential !== 'object' || Array.isArray(credential)) throw new Error('restored credential is invalid');
      verified += 1;
    }
  } finally {
    database.close();
  }
  if (digest(databaseFile) !== dataProof.database_sha256) throw new Error('read-only database changed');
  console.log('native n8n cipher decrypted every restored credential; no workflows were started');
  console.log('RESTORE_VALIDATED_JSON=' + JSON.stringify({
    result: 'passed', backup_id: process.env.BACKUP_ID,
    credentials_verified: verified, workflows: dataProof.relation_counts.workflow_entity,
    sqlite_tables: Object.keys(dataProof.relation_counts).length,
    image_files: imagesProof.files, archive_metadata_verified: true, file_checksums_verified: true,
    sqlite_integrity: true, encryption_key_preserved: true,
    cipher_sha256: EXPECTED_CIPHER_SHA256, workflows_started: false,
  }));
}

try { validate(); } catch {
  console.error('isolated credential validation failed; no credential contents were printed');
  process.exitCode = 1;
}
