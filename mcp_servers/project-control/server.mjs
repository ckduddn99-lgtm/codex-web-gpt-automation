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
];

function commandFor(name, args = {}) {
  const base = [path.join(REPO_ROOT, 'bin', 'project_control.py'), '--db', DB];
  for (const route of ROUTES) base.push('--repo-route', route);
  if (name === 'project_repos') return [...base, 'repos'];
  if (name === 'project_backlog') return [...base, 'backlog'];
  if (name === 'project_goal_status') return [...base, 'goal-status', '--goal-id', String(args.goal_id || '')];
  if (name === 'project_goal_create') return [...base, 'create-goal', '--goal-id', String(args.goal_id || ''), '--description', String(args.description || '')];
  if (name === 'project_task_add') return [
    ...base, 'add-task', '--goal-id', String(args.goal_id || ''), '--task-id', String(args.task_id || ''),
    '--assignee', String(args.assignee || ''), '--repo-id', String(args.repo_id || ''),
    '--description', String(args.description || ''),
  ];
  if (name === 'project_tick') return [...base, 'tick'];
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
    send({ jsonrpc: '2.0', id, result: { protocolVersion: params?.protocolVersion || '2024-11-05', capabilities: { tools: {} }, serverInfo: { name: 'project-control', version: '0.1.0' } } });
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
    handle(message).catch((error) => {
      if (message.id !== undefined) send({ jsonrpc: '2.0', id: message.id, error: { code: -32000, message: String(error.message || error) } });
    });
  }
});
process.stdin.on('end', () => process.exit(0));
