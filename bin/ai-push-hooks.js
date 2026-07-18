#!/usr/bin/env node

const { spawnSync } = require('node:child_process');
const path = require('node:path');

const packageRoot = path.resolve(__dirname, '..');
const srcDir = path.join(packageRoot, 'src');
const args = ['-m', 'ai_push_hooks', ...process.argv.slice(2)];
const pythonCommands = ['python3.14', 'python3.13', 'python3.12', 'python3.11', 'python3', 'python'];

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

function canRunPackage(command) {
  const check = spawnSync(
    command,
    [
      '-c',
      'import sys; assert sys.version_info >= (3, 10); __import__("tomllib" if sys.version_info >= (3, 11) else "tomli")',
    ],
    { stdio: 'ignore' },
  );
  return check.status === 0;
}

const pythonCommand = pythonCommands.find(canRunPackage);
if (!pythonCommand) {
  console.error(
    '[ai-push-hooks] Python 3.11+ is required for npm installs. Python 3.10 can be used if tomli is installed.',
  );
  process.exit(1);
}

const result = run(pythonCommand);
process.exit(typeof result.status === 'number' ? result.status : 1);
