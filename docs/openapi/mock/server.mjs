#!/usr/bin/env node
/**
 * One-command Prism mock for the ScholarLoop UI spec.
 *
 *   npm run mock                      # completion scenario on 0.0.0.0:4010
 *   npm run mock -- erosion-stop      # erosion stop
 *   npm run mock -- needs-input       # waiting for the user
 *   npm run mock -- completion --port 4011 --verboseLevel debug
 *
 * What it does, in order:
 *   1. reads `mock/prism.config.yaml` (Prism 5 has no --config flag, so this script is
 *      the translation layer and prints the exact CLI command it runs);
 *   2. inlines `$ref`s to shared responses/parameters (Prism's static mode serves the
 *      first example of a response, and a scenario can only reorder examples it can
 *      see - a `$ref` sibling would be ignored), then deep-merges `openapi.yaml` with
 *      the scenario overlay, putting the scenario's examples first;
 *   3. writes the composed document next to the spec (so every relative `$ref` -
 *      `../schemas/Brief.json`, `./examples/...` - still resolves) as
 *      `.mock.<scenario>.openapi.yaml`, and
 *   4. exec's Prism against it.
 *
 * Nothing here mutates the spec: the composed file is generated, git-ignored, and
 * regenerated on every start.
 */

import { spawn } from 'node:child_process'
import { existsSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import YAML from 'yaml'

const HERE = dirname(fileURLToPath(import.meta.url))
const SPEC_DIR = resolve(HERE, '..')
const CONFIG_PATH = resolve(HERE, 'prism.config.yaml')
const BASE_SPEC = resolve(SPEC_DIR, 'openapi.yaml')
const PRISM_BIN = resolve(SPEC_DIR, 'node_modules', '.bin', 'prism')

const isPlainObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value)

/**
 * Deep-merge a scenario overlay onto the spec.
 *
 * Objects merge key by key, arrays and scalars replace. `examples` maps are special:
 * the overlay's examples are appended *first* so Prism's static mode serves the
 * scenario's payload, while same-named base examples are kept for `Prefer: example=`.
 */
function mergeOverlay(base, overlay) {
  if (!isPlainObject(base) || !isPlainObject(overlay)) return overlay

  const merged = { ...base }
  for (const [key, value] of Object.entries(overlay)) {
    if (key === 'examples' && isPlainObject(value) && isPlainObject(merged.examples)) {
      const ordered = {}
      for (const [name, example] of Object.entries(value)) {
        ordered[name] = isPlainObject(merged.examples[name])
          ? mergeOverlay(merged.examples[name], example)
          : example
      }
      for (const [name, example] of Object.entries(merged.examples)) {
        if (!(name in ordered)) ordered[name] = example
      }
      merged.examples = ordered
    } else if (isPlainObject(value) && isPlainObject(merged[key])) {
      merged[key] = mergeOverlay(merged[key], value)
    } else {
      merged[key] = value
    }
  }
  return merged
}

const INLINE_PREFIXES = ['#/components/responses/', '#/components/parameters/', '#/components/requestBodies/']

/**
 * Inline `$ref`s to shared responses/parameters/request bodies.
 *
 * Schema `$ref`s are left alone: they can be recursive, and Prism resolves them.
 * Shared responses cannot stay behind a `$ref` because a scenario overlay adds
 * `examples` next to it and, per OpenAPI 3.1, siblings of `$ref` are ignored -
 * the overlay would silently do nothing.
 */
function inlineSharedRefs(document) {
  const resolvePointer = (pointer) =>
    pointer
      .replace(/^\//, '')
      .split('/')
      .map((token) => token.replace(/~1/g, '/').replace(/~0/g, '~'))
      .reduce((node, token) => (node == null ? undefined : node[token]), document)

  const walk = (node) => {
    if (Array.isArray(node)) return node.map(walk)
    if (!isPlainObject(node)) return node
    if (typeof node.$ref === 'string' && INLINE_PREFIXES.some((prefix) => node.$ref.startsWith(prefix))) {
      const target = resolvePointer(node.$ref.slice(1))
      if (target === undefined) throw new Error(`unresolvable $ref ${node.$ref}`)
      const { $ref: _ignored, ...siblings } = node
      return walk(mergeOverlay(structuredClone(target), siblings))
    }
    return Object.fromEntries(Object.entries(node).map(([key, value]) => [key, walk(value)]))
  }
  return walk(document)
}

function parseArgs(argv) {
  const args = { scenario: null, overrides: {} }
  const positional = []
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index]
    if (token.startsWith('--')) {
      const [flag, inlineValue] = token.slice(2).split('=')
      const value = inlineValue ?? argv[index + 1]
      if (inlineValue === undefined) index += 1
      args.overrides[flag] = value
    } else {
      positional.push(token)
    }
  }
  args.scenario = positional[0] ?? process.env.SCENARIO ?? 'completion'
  return args
}

function fail(message) {
  console.error(`\n✖ ${message}\n`)
  if (!existsSync(PRISM_BIN)) {
    console.error('  Prism is not installed: run `npm install` in docs/openapi first.\n')
  }
  process.exit(1)
}

const { scenario, overrides } = parseArgs(process.argv.slice(2))

if (!existsSync(CONFIG_PATH)) fail(`missing mock config at ${CONFIG_PATH}`)
if (!existsSync(BASE_SPEC)) fail(`missing spec at ${BASE_SPEC}`)

const config = YAML.parse(readFileSync(CONFIG_PATH, 'utf8'))
const scenarios = config.scenarios ?? {}
const chosen = scenarios[scenario]
if (!chosen) {
  fail(`unknown scenario '${scenario}'. Known: ${Object.keys(scenarios).join(', ')}`)
}

const overlayPath = resolve(HERE, chosen.spec)
if (!existsSync(overlayPath)) fail(`missing scenario overlay at ${overlayPath}`)

const base = inlineSharedRefs(YAML.parse(readFileSync(BASE_SPEC, 'utf8')))
const overlay = YAML.parse(readFileSync(overlayPath, 'utf8'))
const composed = mergeOverlay(base, overlay)

const meta = overlay['x-scholarloop-scenario'] ?? { id: scenario, title: scenario, story: [] }
const outPath = resolve(SPEC_DIR, `.mock.${scenario}.openapi.yaml`)
writeFileSync(
  outPath,
  `# GENERATED by docs/openapi/mock/server.mjs for scenario "${scenario}". Do not edit.\n` +
    YAML.stringify(composed),
  'utf8',
)

const mock = config.mock ?? {}
const host = String(overrides.host ?? process.env.HOST ?? mock.host ?? '0.0.0.0')
const port = String(overrides.port ?? process.env.PORT ?? mock.port ?? 4010)
const verboseLevel = String(overrides.verboseLevel ?? mock.verboseLevel ?? 'info')

const prismArgs = [
  'mock',
  outPath,
  '--host',
  host,
  '--port',
  port,
  '--dynamic',
  String(mock.dynamic === true),
  '--errors',
  String(mock.errors === true),
  '--cors',
  String(mock.cors !== false),
  '--verboseLevel',
  verboseLevel,
]

console.log(`\nScholarLoop mock - scenario: ${meta.id ?? scenario}`)
console.log(`  ${meta.title ?? ''}`)
if (meta.summary) console.log(`  ${String(meta.summary).replace(/\s+/g, ' ').trim()}`)
console.log('\n  Story:')
for (const step of meta.story ?? []) console.log(`    - ${step}`)
if ((meta.try ?? []).length > 0) {
  console.log('\n  Try:')
  for (const step of meta.try) console.log(`    - ${step}`)
}
console.log(`\n  Spec:      ${outPath.replace(`${SPEC_DIR}/`, '')} (generated)`)
console.log(`  Endpoint:  http://${host}:${port}/api/health`)
console.log(`  Command:   prism ${prismArgs.map((a) => (a.includes(' ') ? JSON.stringify(a) : a)).join(' ')}\n`)

if (!existsSync(PRISM_BIN)) fail('Prism is not installed in docs/openapi/node_modules/.bin')

const child = spawn(PRISM_BIN, prismArgs, { stdio: 'inherit', cwd: SPEC_DIR })
child.on('exit', (code) => process.exit(code ?? 0))
for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => {
    child.kill(signal)
  })
}
