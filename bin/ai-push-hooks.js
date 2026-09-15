#!/usr/bin/env node

const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const packageRoot = path.resolve(__dirname, '..');
const bootstrap = path.join(packageRoot, 'bin', 'bootstrap.py');
const capabilityEnvironment = 'AI_PUSH_HOOKS_INTERNAL_CAPABILITY';
const args = ['-I', bootstrap, ...process.argv.slice(2)];
const pythonCommands = ['python3.14', 'python3.13', 'python3.12', 'python3.11', 'python3.10', 'python3', 'python'];

function buildEnv(capability = false) {
  const env = { ...process.env };
  env.AI_PUSH_HOOKS_NODE_EXECUTABLE = process.execPath;
  env.AI_PUSH_HOOKS_NODE_SCRIPT = fs.realpathSync(__filename);
  // -I intentionally ignores PYTHONPATH.  The bootstrap adds only shipped paths.
  delete env.PYTHONPATH;
  if (capability) {
    env[capabilityEnvironment] = '1';
  } else {
    delete env[capabilityEnvironment];
  }
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
    ['-I', bootstrap],
    { stdio: 'ignore', env: buildEnv(true) },
  );
  return check.status === 0;
}

const pythonCommand = pythonCommands.find(canRunPackage);
if (!pythonCommand) {
  console.error(
    '[ai-push-hooks] Python 3.10+ is required and must be available on PATH.',
  );
  process.exit(1);
}

const result = run(pythonCommand);
process.exit(typeof result.status === 'number' ? result.status : 1);
