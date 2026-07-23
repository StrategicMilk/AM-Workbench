import { spawnSync } from 'node:child_process';

const args = process.argv.slice(2).filter((arg) => arg !== '--run');
const shell = process.platform === 'win32';

function run(command, commandArgs) {
  const result = spawnSync(command, commandArgs, {
    cwd: new URL('..', import.meta.url),
    shell,
    stdio: 'inherit',
  });
  process.exitCode = result.status ?? 1;
  return process.exitCode === 0;
}

if (!run('npm', ['run', 'build'])) {
  process.exit(process.exitCode);
}

if (!run('npm', ['run', 'test:unit'])) {
  process.exit(process.exitCode);
}

if (args.length > 0) {
  run('npx', ['playwright', 'test', ...args]);
}
