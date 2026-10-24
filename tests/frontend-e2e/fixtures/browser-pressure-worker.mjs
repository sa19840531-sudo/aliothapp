import { appendFileSync } from 'node:fs'
import { execFile } from 'node:child_process'
import os from 'node:os'
import { parentPort, workerData } from 'node:worker_threads'

// 独立线程保留测试线程阻塞期间的物理采样，不读取进程命令行或环境秘密。
let stopped = false
let timer
let sampling
function record(kind, data) {
  appendFileSync(workerData.path, JSON.stringify({
    mono_ms: Number(process.hrtime.bigint()) / 1e6, wall_ms: Date.now(), kind, ...data,
  }) + '\n')
}
function read(command, args) {
  return new Promise((resolve) => execFile(command, args, { timeout: 1500 }, (error, stdout) => {
    resolve(error ? { error: error.message } : { stdout })
  }))
}
async function sample() {
  const started = Number(process.hrtime.bigint()) / 1e6
  const result = await read('ps', ['-axo', 'pid=,ppid=,%cpu=,rss=,stat=,comm='])
  const all = (result.stdout || '').split('\n').flatMap((line) => {
    const match = line.trim().match(/^(\d+)\s+(\d+)\s+([\d.]+)\s+(\d+)\s+(\S+)\s+(.+)$/)
    return match ? [{ pid: +match[1], ppid: +match[2], cpu_percent: +match[3],
      rss_kib: +match[4], state: match[5], executable: match[6] }] : []
  })
  const owned = new Set([workerData.browserPid, workerData.nodePid])
  for (let depth = 0; depth < 8; depth++) {
    for (const row of all) if (owned.has(row.ppid)) owned.add(row.pid)
  }
  record('host_processes', { started_mono_ms: started, loadavg: os.loadavg(),
    free_bytes: os.freemem(), total_bytes: os.totalmem(),
    processes: all.filter((row) => owned.has(row.pid)), error: result.error || null })
  // macOS 压缩/换页计数与空闲内存分开保留，不能由 loadavg 单独认定资源压力。
  if (process.platform === 'darwin') record('vm_stat', await read('vm_stat', []))
}
async function tick() {
  sampling = sample()
  await sampling
  if (!stopped) timer = setTimeout(tick, 400)
}
parentPort.on('message', async (message) => {
  if (message !== 'stop') return
  stopped = true
  clearTimeout(timer)
  await sampling
  record('sampler_stopped', {})
  parentPort.close()
})
record('sampler_started', { browser_pid: workerData.browserPid, node_pid: workerData.nodePid })
parentPort.postMessage('ready')
void tick()
