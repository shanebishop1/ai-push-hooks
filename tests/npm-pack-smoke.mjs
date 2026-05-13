import { execFileSync } from 'node:child_process';
import { existsSync, mkdtempSync, rmSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const projectDir = mkdtempSync(join(tmpdir(), 'ai-push-hooks-npm-smoke-'));
let tarballPath;

function run(command, args, options = {}) {
  execFileSync(command, args, {
    stdio: 'inherit',
    ...options,
    env: {
      ...process.env,
      npm_config_audit: 'false',
      npm_config_fund: 'false',
      ...options.env,
    },
  });
}

try {
  const packOutput = execFileSync('npm', ['pack', '--json'], {
    cwd: repoRoot,
    encoding: 'utf8',
  });
  const [packed] = JSON.parse(packOutput);
  tarballPath = join(repoRoot, packed.filename);

  run('npm', ['init', '-y'], { cwd: projectDir });
  run('npm', ['install', tarballPath], { cwd: projectDir });
  run('npx', ['ai-push-hooks', '--help'], { cwd: projectDir });
  run('npx', ['ai-push-hooks', 'init', '--template', 'minimal-docs'], { cwd: projectDir });

  const configPath = join(projectDir, 'ai-push-hooks.toml');
  if (!existsSync(configPath)) {
    throw new Error(`Expected init to create ${configPath}`);
  }
} finally {
  rmSync(projectDir, { recursive: true, force: true });
  if (tarballPath) {
    rmSync(tarballPath, { force: true });
  }
}
