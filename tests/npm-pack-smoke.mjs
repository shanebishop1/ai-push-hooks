import { execFileSync, spawnSync } from 'node:child_process';
import { existsSync, mkdtempSync, rmSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const workDir = mkdtempSync(join(tmpdir(), 'ai-push-hooks-npm-smoke-'));
const remoteDir = join(workDir, 'remote path with spaces.git');
const clientDir = join(workDir, 'client repo');
const packageDir = clientDir;
const packDir = join(workDir, 'pack');
let tarballPath;

const originalPath = process.env.PATH || '';
const pathEntries = originalPath.split(':');
const findToolDir = (tool) => pathEntries.find((entry) => existsSync(join(entry, tool)));
const pythonDir = findToolDir('python3') || findToolDir('python');
const gitDir = findToolDir('git');
const minimalPath = [
  join(packageDir, 'node_modules', '.bin'),
  dirname(process.execPath),
  pythonDir,
  gitDir,
  '/usr/bin',
  '/bin',
].filter(Boolean).join(':');

const env = {
  ...process.env,
  PATH: minimalPath,
  GIT_CONFIG_NOSYSTEM: '1',
  GIT_CONFIG_GLOBAL: '/dev/null',
  GIT_CONFIG_SYSTEM: '/dev/null',
  GIT_AUTHOR_NAME: 'Installed Hook Test',
  GIT_AUTHOR_EMAIL: 'installed-hook@example.invalid',
  GIT_COMMITTER_NAME: 'Installed Hook Test',
  GIT_COMMITTER_EMAIL: 'installed-hook@example.invalid',
};
delete env.PYTHONPATH;

function run(command, args, options = {}) {
  return execFileSync(command, args, {
    cwd: options.cwd,
    env,
    stdio: 'inherit',
    timeout: options.timeout || 45000,
  });
}

function capture(command, args, cwd) {
  return execFileSync(command, args, {
    cwd,
    env,
    encoding: 'utf8',
    timeout: 45000,
  }).trim();
}

function tryRun(command, args, cwd, input) {
  return spawnSync(command, args, {
    cwd,
    env,
    encoding: 'utf8',
    input,
    timeout: 45000,
  });
}

function config(reject = false) {
  return reject
    ? `[general]\nallow_push_on_error = false\n\n[logging]\njsonl = false\ncapture_llm_transcript = false\n\n[workflow]\nmodules = ["gate"]\n\n[modules.gate]\nenabled = true\n\n[[modules.gate.steps]]\nid = "reject"\ntype = "exec"\nexecutor = "deliberately-unknown"\n`
    : `[general]\nallow_push_on_error = false\n\n[logging]\njsonl = false\ncapture_llm_transcript = false\n\n[workflow]\nmodules = ["docs"]\n\n[modules.docs]\nenabled = true\n\n[[modules.docs.steps]]\nid = "collect"\ntype = "collect"\ncollector = "docs_context"\n`;
}

try {
  run('mkdir', ['-p', packDir]);
  const packOutput = execFileSync('npm', ['pack', '--json', '--pack-destination', packDir], {
    cwd: repoRoot,
    env,
    encoding: 'utf8',
    timeout: 120000,
  });
  const [packed] = JSON.parse(packOutput);
  tarballPath = join(packDir, packed.filename);

  run('mkdir', ['-p', packageDir]);
  run('npm', ['init', '-y'], { cwd: packageDir });
  run('npm', ['install', '--offline', '--ignore-scripts', '--no-audit', '--no-fund', tarballPath], {
    cwd: packageDir,
  });
  const command = join(packageDir, 'node_modules', '.bin', 'ai-push-hooks');
  run(command, ['--help'], { cwd: packageDir });

  run('git', ['init', '--bare', remoteDir], { cwd: workDir });
  run('mkdir', ['-p', clientDir], { cwd: workDir });
  run('git', ['init', '-b', 'main', '.'], { cwd: clientDir });
  run('git', ['config', 'user.name', 'Installed Hook Test'], { cwd: clientDir });
  run('git', ['config', 'user.email', 'installed-hook@example.invalid'], { cwd: clientDir });
  run('git', ['remote', 'add', 'origin', remoteDir], { cwd: clientDir });
  run('mkdir', ['-p', join(clientDir, 'docs')], { cwd: clientDir });
  // Use Node's filesystem API rather than shell redirection for fixture setup.
  execFileSync('node', ['-e',
    `require('node:fs').writeFileSync(process.argv[1], '# Initial\\n');
     require('node:fs').writeFileSync(process.argv[2], '# Docs\\n');
     require('node:fs').writeFileSync(process.argv[3], process.argv[4]);
     require('node:fs').writeFileSync(process.argv[5], 'node_modules/\\n');`,
    join(clientDir, 'README.md'), join(clientDir, 'docs', 'INDEX.md'), join(clientDir, 'ai-push-hooks.toml'), config(), join(clientDir, '.gitignore')],
  { cwd: clientDir, env, stdio: 'inherit' });
  run('git', ['add', '.'], { cwd: clientDir });
  run('git', ['commit', '-m', 'initial'], { cwd: clientDir });
  run('git', ['push', 'origin', 'main'], { cwd: clientDir });

  run('npx', ['--no-install', 'ai-push-hooks', 'install'], { cwd: clientDir });
  env.PATH = [dirname(process.execPath), pythonDir, gitDir, '/usr/bin', '/bin']
    .filter(Boolean).join(':');
  let hookPath = capture('git', ['rev-parse', '--git-path', 'hooks'], clientDir);
  if (!hookPath.startsWith('/')) hookPath = resolve(clientDir, hookPath);
  hookPath = join(hookPath, 'pre-push');
  if (!existsSync(hookPath)) throw new Error(`Missing installed hook: ${hookPath}`);

  execFileSync('node', ['-e',
    `require('node:fs').writeFileSync(process.argv[1], 'outgoing docs\\n');`,
    join(clientDir, 'change.md')], { cwd: clientDir, env, stdio: 'inherit' });
  run('git', ['add', 'change.md'], { cwd: clientDir });
  run('git', ['commit', '-m', 'outgoing'], { cwd: clientDir });
  const localOid = capture('git', ['rev-parse', 'HEAD'], clientDir);
  const remoteOid = capture('git', ['rev-parse', 'refs/remotes/origin/main'], clientDir);
  if (localOid.length !== 40 || remoteOid.length !== 40) throw new Error('Expected full SHA-1 object IDs');
  const direct = tryRun(
    hookPath,
    ['origin', `${remoteDir} with spaces`],
    clientDir,
    `refs/heads/main ${localOid} refs/heads/main ${remoteOid}\n`,
  );
  if (direct.status !== 0) throw new Error(`Installed hook failed directly: ${direct.stderr}`);
  run('git', ['push', 'origin', 'main'], { cwd: clientDir });
  const pushed = capture('git', ['--git-dir', remoteDir, 'rev-parse', 'refs/heads/main'], clientDir);
  if (pushed !== localOid) throw new Error('Successful local push did not update the bare remote');

  execFileSync('node', ['-e',
    `require('node:fs').writeFileSync(process.argv[1], process.argv[2]);
     require('node:fs').writeFileSync(process.argv[3], 'must not arrive\\n');`,
    join(clientDir, 'ai-push-hooks.toml'), config(true), join(clientDir, 'rejected.txt')],
  { cwd: clientDir, env, stdio: 'inherit' });
  run('git', ['add', 'ai-push-hooks.toml', 'rejected.txt'], { cwd: clientDir });
  run('git', ['commit', '-m', 'rejected'], { cwd: clientDir });
  const beforeReject = capture('git', ['--git-dir', remoteDir, 'rev-parse', 'refs/heads/main'], clientDir);
  const rejected = tryRun('git', ['push', 'origin', 'main'], clientDir);
  if (rejected.status === 0) throw new Error('Rejecting installed hook allowed a local push');
  const afterReject = capture('git', ['--git-dir', remoteDir, 'rev-parse', 'refs/heads/main'], clientDir);
  if (afterReject !== beforeReject) throw new Error('Rejected push changed the bare remote');
} finally {
  rmSync(workDir, { recursive: true, force: true });
}
