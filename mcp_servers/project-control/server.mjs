#!/usr/bin/env node
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, '..', '..');
const PYTHON = process.env.PROJECT_CONTROL_PYTHON || 'python3';
const DB = process.env.PROJECT_CONTROL_DB || path.join(process.env.HOME || '/tmp', '.local/state/ai-bus/bus.sqlite3');
const ROUTES = (process.env.PROJECT_CONTROL_REPO_ROUTES || '')
  .split(',').map((value) => value.trim()).filter(Boolean);

const TOOLS = [
  {
    name: 'project_repos',
    description: 'List project repository aliases configured for the policy-limited control plane.',
    inputSchema: { type: 'object', properties: {}, additionalProperties: false },
  },
  { name: 'project_ssh_hosts', description: 'List registered break-glass host aliases. Arbitrary hostnames are never accepted at call time.', inputSchema: { type: 'object', properties: {}, additionalProperties: false } },
  { name: 'project_ssh_status', description: 'Read bounded health/status information from one registered host alias.', inputSchema: { type: 'object', properties: { host_id: { type: 'string' }, timeout: { type: 'integer' } }, required: ['host_id'], additionalProperties: false } },
  { name: 'project_ssh_exec', description: 'Run a bounded break-glass command on one registered host alias using local control or non-interactive SSH.', inputSchema: { type: 'object', properties: { host_id: { type: 'string' }, command: { type: 'string' }, timeout: { type: 'integer' } }, required: ['host_id','command'], additionalProperties: false } },
  { name: 'project_ssh_services', description: 'List allowlisted service-recovery actions and service units for one registered host.', inputSchema: { type: 'object', properties: { host_id: { type: 'string' } }, required: ['host_id'], additionalProperties: false } },
  { name: 'project_ssh_service', description: 'Run an allowlisted service status/start/restart/reset-failed action through the restricted Project Control root helper.', inputSchema: { type: 'object', properties: { host_id: { type: 'string' }, service: { type: 'string' }, action: { type: 'string', enum: ['status','start','restart','reset-failed'] }, timeout: { type: 'integer' } }, required: ['host_id','service','action'], additionalProperties: false } },
  {
    name: 'project_backlog',
    description: 'Return compact durable goal/task backlog metadata. Does not expand artifact bodies.',
    inputSchema: { type: 'object', properties: {}, additionalProperties: false },
  },
  {
    name: 'project_goal_status',
    description: 'Return one durable goal and its task metadata without prompt/result bodies.',
    inputSchema: {
      type: 'object', properties: { goal_id: { type: 'string' } },
      required: ['goal_id'], additionalProperties: false,
    },
  },
  {
    name: 'project_goal_requeue',
    description: 'Requeue one recovery-waiting original work task to ChatGPT after operator repair evidence exists.',
    inputSchema: {
      type: 'object', properties: { goal_id: { type: 'string' }, task_id: { type: 'string' } },
      required: ['goal_id', 'task_id'], additionalProperties: false,
    },
  },
  {
    name: 'project_goal_create',
    description: 'Create one durable goal owned by Gemini. This schedules no provider work by itself.',
    inputSchema: {
      type: 'object', properties: {
        goal_id: { type: 'string' }, description: { type: 'string' },
      }, required: ['goal_id', 'description'], additionalProperties: false,
    },
  },
  {
    name: 'project_task_add',
    description: 'Add one durable task with an explicit repository alias. Repository selection is never inferred from task text.',
    inputSchema: {
      type: 'object', properties: {
        goal_id: { type: 'string' }, task_id: { type: 'string' },
        assignee: { type: 'string', enum: ['codex', 'gemini', 'claude', 'chatgpt'] },
        repo_id: { type: 'string' }, description: { type: 'string' },
      }, required: ['goal_id', 'task_id', 'assignee', 'repo_id', 'description'],
      additionalProperties: false,
    },
  },
  {
    name: 'project_tick',
    description: 'Advance at most one durable task and then at most one Gemini management boundary through existing no-replay/provider-lock policy.',
    inputSchema: { type: 'object', properties: {}, additionalProperties: false },
  },
  { name: 'project_repo_read', description: 'Read bounded UTF-8 text from a registered repository and return its SHA-256.', inputSchema: { type: 'object', properties: { repo_id: { type: 'string' }, path: { type: 'string' }, start_line: { type: 'integer' }, max_lines: { type: 'integer' } }, required: ['repo_id','path'], additionalProperties: false } },
  { name: 'project_repo_search', description: 'Literal text search inside registered repository files.', inputSchema: { type: 'object', properties: { repo_id: { type: 'string' }, query: { type: 'string' }, path: { type: 'string' }, case_sensitive: { type: 'boolean' }, max_results: { type: 'integer' } }, required: ['repo_id','query'], additionalProperties: false } },
  { name: 'project_repo_patch', description: 'Apply exact hash-bound text replacements to one registered repository file.', inputSchema: { type: 'object', properties: { repo_id: { type: 'string' }, path: { type: 'string' }, expected_sha256: { type: 'string' }, create: { type: 'boolean' }, replacements: { type: 'array', items: { type: 'object', properties: { old_text: { type: 'string' }, new_text: { type: 'string' } }, required: ['old_text','new_text'], additionalProperties: false } } }, required: ['repo_id','path','expected_sha256','replacements'], additionalProperties: false } },
  { name: 'project_repo_test', description: 'Run only allowlisted test profiles.', inputSchema: { type: 'object', properties: { repo_id: { type: 'string' }, profile: { type: 'string' }, targets: { type: 'array', items: { type: 'string' } }, timeout: { type: 'integer' } }, required: ['repo_id','profile'], additionalProperties: false } },
  { name: 'project_repo_git_status', description: 'Return git status and HEAD.', inputSchema: { type: 'object', properties: { repo_id: { type: 'string' } }, required: ['repo_id'], additionalProperties: false } },
  { name: 'project_repo_diff', description: 'Return bounded git diff.', inputSchema: { type: 'object', properties: { repo_id: { type: 'string' }, path: { type: 'string' }, staged: { type: 'boolean' } }, required: ['repo_id'], additionalProperties: false } },
  { name: 'project_repo_commit', description: 'Stage only explicitly listed changed paths and create one local commit. Never pushes.', inputSchema: { type: 'object', properties: { repo_id: { type: 'string' }, message: { type: 'string' }, paths: { type: 'array', items: { type: 'string' } } }, required: ['repo_id','message','paths'], additionalProperties: false } },
];

function commandFor(name, args = {}) {
  const base = [path.join(REPO_ROOT, 'bin', 'project_control.py'), '--db', DB];
  for (const route of ROUTES) base.push('--repo-route', route);
  if (name === 'project_repos') return [...base, 'repos'];
  if (name === 'project_ssh_hosts') return [...base, 'ssh-hosts'];
  if (name === 'project_ssh_status') return [...base, 'ssh-status', '--host-id', String(args.host_id || ''), '--timeout', String(args.timeout || 30)];
  if (name === 'project_ssh_exec') return [...base, 'ssh-exec', '--host-id', String(args.host_id || ''), `--command=${String(args.command || '')}`, '--timeout', String(args.timeout || 60)];
  if (name === 'project_ssh_services') return [...base, 'ssh-services', '--host-id', String(args.host_id || '')];
  if (name === 'project_ssh_service') return [...base, 'ssh-service', '--host-id', String(args.host_id || ''), '--service', String(args.service || ''), '--action', String(args.action || ''), '--timeout', String(args.timeout || 60)];
  if (name === 'project_backlog') return [...base, 'backlog'];
  if (name === 'project_goal_status') return [...base, 'goal-status', '--goal-id', String(args.goal_id || '')];
  if (name === 'project_goal_requeue') return [...base, 'requeue-goal-task', '--goal-id', String(args.goal_id || ''), '--task-id', String(args.task_id || '')];
  if (name === 'project_goal_create') return [...base, 'create-goal', '--goal-id', String(args.goal_id || ''), `--description=${String(args.description || '')}`];
  if (name === 'project_task_add') return [
    ...base, 'add-task', '--goal-id', String(args.goal_id || ''), '--task-id', String(args.task_id || ''),
    '--assignee', String(args.assignee || ''), '--repo-id', String(args.repo_id || ''),
    `--description=${String(args.description || '')}`,
  ];
  if (name === 'project_tick') return [...base, 'tick'];
  if (name === 'project_repo_read') return [...base, 'repo-read', '--repo-id', String(args.repo_id || ''), '--path', String(args.path || ''), '--start-line', String(args.start_line || 1), '--max-lines', String(args.max_lines || 200)];
  if (name === 'project_repo_search') {
    const out = [...base, 'repo-search', '--repo-id', String(args.repo_id || ''), `--query=${String(args.query || '')}`, '--path', String(args.path || '.'), '--max-results', String(args.max_results || 50)];
    if (args.case_sensitive === false) out.push('--ignore-case');
    return out;
  }
  if (name === 'project_repo_patch') {
    const out = [...base, 'repo-patch', '--repo-id', String(args.repo_id || ''), '--path', String(args.path || ''), '--expected-sha256', String(args.expected_sha256 || ''), '--replacements-json', JSON.stringify(args.replacements || [])];
    if (args.create) out.push('--create');
    return out;
  }
  if (name === 'project_repo_test') {
    const out = [...base, 'repo-test', '--repo-id', String(args.repo_id || ''), '--profile', String(args.profile || ''), '--timeout', String(args.timeout || 300)];
    for (const target of (args.targets || [])) out.push('--target', String(target));
    return out;
  }
  if (name === 'project_repo_git_status') return [...base, 'repo-git-status', '--repo-id', String(args.repo_id || '')];
  if (name === 'project_repo_diff') {
    const out = [...base, 'repo-diff', '--repo-id', String(args.repo_id || '')];
    if (args.path) out.push('--path', String(args.path));
    if (args.staged) out.push('--staged');
    return out;
  }
  if (name === 'project_repo_commit') {
    const out = [...base, 'repo-commit', '--repo-id', String(args.repo_id || ''), `--message=${String(args.message || '')}`];
    for (const item of (args.paths || [])) out.push('--path', String(item));
    return out;
  }
  throw new Error(`Unknown tool: ${name}`);
}

function invoke(name, args) {
  return new Promise((resolve, reject) => {
    const child = spawn(PYTHON, commandFor(name, args), {
      cwd: REPO_ROOT, env: process.env, stdio: ['ignore', 'pipe', 'pipe'],
    });
    let stdout = ''; let stderr = '';
    child.stdout.setEncoding('utf8'); child.stderr.setEncoding('utf8');
    child.stdout.on('data', (chunk) => { stdout += chunk; });
    child.stderr.on('data', (chunk) => { stderr += chunk; });
    child.on('error', reject);
    child.on('close', (code) => {
      const text = stdout.trim();
      if (code !== 0) return reject(new Error(text || stderr.trim() || `project control exited ${code}`));
      try { resolve(JSON.parse(text)); } catch { reject(new Error('project control returned invalid JSON')); }
    });
  });
}

function textResult(value) {
  return { content: [{ type: 'text', text: JSON.stringify(value) }] };
}
function errorResult(error) {
  return { isError: true, content: [{ type: 'text', text: String(error?.message || error) }] };
}
function send(value) { process.stdout.write(JSON.stringify(value) + '\n'); }

async function handle(message) {
  const { id, method, params } = message;
  if (method === 'initialize') {
    send({ jsonrpc: '2.0', id, result: { protocolVersion: params?.protocolVersion || '2024-11-05', capabilities: { tools: {} }, serverInfo: { name: 'project-control', version: '0.2.0' } } });
    return;
  }
  if (method === 'notifications/initialized') return;
  if (method === 'tools/list') {
    send({ jsonrpc: '2.0', id, result: { tools: TOOLS } });
    return;
  }
  if (method === 'tools/call') {
    try { send({ jsonrpc: '2.0', id, result: textResult(await invoke(params?.name, params?.arguments || {})) }); }
    catch (error) { send({ jsonrpc: '2.0', id, result: errorResult(error) }); }
    return;
  }
  send({ jsonrpc: '2.0', id, error: { code: -32601, message: `Method not found: ${method}` } });
}

let buffer = '';
let pending = 0;
let stdinEnded = false;
function maybeExit() {
  if (stdinEnded && pending === 0) process.exit(0);
}
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => {
  buffer += chunk;
  let index;
  while ((index = buffer.indexOf('\n')) >= 0) {
    const line = buffer.slice(0, index).trim(); buffer = buffer.slice(index + 1);
    if (!line) continue;
    let message;
    try { message = JSON.parse(line); }
    catch (error) { send({ jsonrpc: '2.0', id: null, error: { code: -32700, message: String(error.message || error) } }); continue; }
    pending += 1;
    handle(message).catch((error) => {
      if (message.id !== undefined) send({ jsonrpc: '2.0', id: message.id, error: { code: -32000, message: String(error.message || error) } });
    }).finally(() => {
      pending -= 1;
      maybeExit();
    });
  }
});
process.stdin.on('end', () => {
  stdinEnded = true;
  maybeExit();
});
