#!/usr/bin/env node

const { spawnSync } = require('node:child_process');
const path = require('node:path');

const packageRoot = path.resolve(__dirname, '..');
const srcDir = path.join(packageRoot, 'src');
const args = ['-m', 'ai_push_hooks', ...process.argv.slice(2)];

function buildEnv() {
  const env = { ...process.env };
  env.PYTHONPATH = env.PYTHONPATH
    ? `${srcDir}${path.delimiter}${env.PYTHONPATH}`
    : srcDir;
  return env;
}

function run(command) {
  return spawnSync(command, args, {
    stdio: 'inherit',
    env: buildEnv(),
  });
}

let result = run('python3');
if (result.error && result.error.code === 'ENOENT') {
  result = run('python');
}

if (result.error && result.error.code === 'ENOENT') {
  console.error('[ai-push-hooks] python3/python is required but not installed.');
  process.exit(1);
}

process.exit(typeof result.status === 'number' ? result.status : 1);
