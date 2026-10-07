import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, test, vi } from 'vitest'

// A runtime the probe judges through its published launcher. The launcher only
// exits 0 when the probe asked it to skip the update check, so acceptance also
// proves the probe never pays for git reads or a fetch it discards.
function installation(launcherBody: string): string {
  const root: string = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-source-probe-'))
  fs.mkdirSync(path.join(root, 'hermes_cli'))
  fs.writeFileSync(path.join(root, 'hermes_cli', 'main.py'), '')
  fs.mkdirSync(path.join(root, '.hermes', 'bin'), { recursive: true })
  fs.writeFileSync(path.join(root, '.hermes', 'bin', 'hermes'), `#!/bin/sh\n${launcherBody}\n`, { mode: 0o755 })

  return root
}

async function resolveWithShortProbe(root: string, log: string[]) {
  vi.stubEnv('HERMES_PROBE_TIMEOUT_MS', '400')
  vi.resetModules()
  const { resolveSourceInstallationBackend } = await import('./source-backend')

  return resolveSourceInstallationBackend(root, ['serve'], { hermesHome: root, log: message => log.push(message) })
}

const roots: string[] = []

afterEach(() => {
  vi.unstubAllEnvs()

  for (const root of roots.splice(0)) {
    fs.rmSync(root, { recursive: true, force: true })
  }
})

test.skipIf(process.platform === 'win32')(
  'a failed --version exit makes the runtime unusable; an unanswered probe does not',
  async (): Promise<void> => {
    const broken: string = installation('exit 3')
    const slow: string = installation('[ "$HERMES_VERSION_SKIP_UPDATE_CHECK" = 1 ] || exit 7\nexec sleep 30')
    const healthy: string = installation('[ "$HERMES_VERSION_SKIP_UPDATE_CHECK" = 1 ] || exit 7')
    roots.push(broken, slow, healthy)
    const log: string[] = []

    assert.equal(await resolveWithShortProbe(broken, log), null)
    assert.match(log.join('\n'), new RegExp(`${broken} is not usable: .*\\(3\\)`))

    // Timing out says nothing about usability: the spawn's ready wait decides,
    // never the first-run installer.
    assert.equal((await resolveWithShortProbe(slow, log))?.root, slow)
    assert.match(log.join('\n'), /did not answer within 400ms/)

    assert.equal((await resolveWithShortProbe(healthy, log))?.root, healthy)
  }
)
